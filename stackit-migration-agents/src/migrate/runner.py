"""Drives the orchestrator graph for the CLI.

Every CLI command maps to one `Runner` call. The LangGraph thread id is the
run id, so any command can pick up a run where it stopped, including after a
crash or restart: unfinished work is resumed from the last checkpoint before
new work starts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from migrate.agents.common import ApprovalResponse
from migrate.audit import AuditLog, Redactor
from migrate.config import Settings
from migrate.console import NullReporter, Reporter
from migrate.context import AppContext, build_context
from migrate.graph.checkpoint import open_checkpointer
from migrate.graph.orchestrator import build_graph
from migrate.runs import RunPaths, new_run_id, read_plan, run_paths, save_plan
from migrate.state import Goal, MigrationPlan, RunOptions, SourceCloud


class GatePendingError(RuntimeError):
    """A human decision is required before the run can continue."""


class NoPendingGateError(RuntimeError):
    pass


@dataclass
class Outcome:
    plan: MigrationPlan
    pending_gate: dict[str, Any] | None = None
    next_nodes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def halted(self) -> str | None:
        return self.plan.halt_reason


class Runner:
    def __init__(self, settings: Settings, reporter: Reporter | None = None) -> None:
        self.settings = settings
        self.reporter = reporter or NullReporter()

    # ------------------------------------------------------------------ setup

    def create_run(
        self, *, source: SourceCloud, region: str | None, mock: bool
    ) -> RunPaths:
        run_id = new_run_id()
        paths = run_paths(self.settings.runs_dir, run_id, must_exist=False)
        paths.root.mkdir(parents=True, exist_ok=False)
        if region is None:
            region = (
                self.settings.aws_region
                if source == SourceCloud.AWS
                else "germanywestcentral"
            )
        plan = MigrationPlan(
            run_id=run_id,
            source_cloud=source,
            source_region=region,
            target_region=self.settings.stackit_region,
            mock=mock,
        )
        save_plan(paths, plan)
        AuditLog(paths.audit_file, Redactor(self.settings.secret_values())).record(
            agent="orchestrator",
            action="run.created",
            inputs={"source": source.value, "region": region, "mock": mock},
            result={"run_id": run_id},
        )
        return paths

    @asynccontextmanager
    async def _graph(
        self, run_id: str, options: RunOptions
    ) -> AsyncIterator[tuple[CompiledStateGraph, dict[str, Any], AppContext]]:
        paths = run_paths(self.settings.runs_dir, run_id)
        plan = read_plan(paths)
        ctx = build_context(
            self.settings,
            paths,
            mock=plan.mock,
            live_llm=options.live_llm,
            reporter=self.reporter,
        )
        async with open_checkpointer(self.settings) as saver:
            graph = build_graph(saver)
            yield graph, {"configurable": {"thread_id": run_id}}, ctx

    # --------------------------------------------------------------- commands

    async def advance(self, run_id: str, goal: Goal, options: RunOptions) -> Outcome:
        """Run the graph towards `goal`, resuming unfinished work first."""
        async with self._graph(run_id, options) as (graph, config, ctx):
            snapshot = await graph.aget_state(config, subgraphs=True)
            if _interrupt_payload(snapshot):
                raise GatePendingError(
                    "A human decision is pending. Run `migrate approve "
                    f"{run_id}` (or `--reject`) first."
                )
            if snapshot.next:
                self.reporter.step(
                    "orchestrator", "Resuming unfinished work from the last checkpoint"
                )
                await graph.ainvoke(None, config, context=ctx)
                snapshot = await graph.aget_state(config, subgraphs=True)
                if _interrupt_payload(snapshot):
                    return await self._outcome(graph, config)
            graph_input: dict[str, Any] = {
                "goal": goal,
                "options": options.model_dump(mode="json"),
            }
            if not snapshot.values:
                # New thread (or lost checkpoint DB): seed from the plan file.
                graph_input["plan"] = read_plan(ctx.paths).model_dump(mode="json")
            await graph.ainvoke(graph_input, config, context=ctx)
            return await self._outcome(graph, config)

    async def resume(self, run_id: str, options: RunOptions) -> Outcome:
        """Continue a run that stopped mid-stage (crash, kill, restart)."""
        async with self._graph(run_id, options) as (graph, config, ctx):
            snapshot = await graph.aget_state(config, subgraphs=True)
            if snapshot.next and not _interrupt_payload(snapshot):
                await graph.ainvoke(None, config, context=ctx)
            return await self._outcome(graph, config)

    async def decide(
        self,
        run_id: str,
        *,
        approved: bool,
        approver: str,
        comment: str = "",
        options: RunOptions | None = None,
    ) -> Outcome:
        """Answer the pending approval gate and continue the run."""
        options = options or RunOptions()
        async with self._graph(run_id, options) as (graph, config, ctx):
            snapshot = await graph.aget_state(config, subgraphs=True)
            if not _interrupt_payload(snapshot):
                raise NoPendingGateError(f"run {run_id} is not waiting for an approval")
            response = ApprovalResponse(
                approved=approved, approver=approver, comment=comment
            )
            await graph.ainvoke(
                Command(resume=response.model_dump()), config, context=ctx
            )
            return await self._outcome(graph, config)

    async def status(self, run_id: str) -> Outcome:
        async with self._graph(run_id, RunOptions()) as (graph, config, _ctx):
            return await self._outcome(graph, config)

    async def _outcome(
        self, graph: CompiledStateGraph, config: dict[str, Any]
    ) -> Outcome:
        snapshot = await graph.aget_state(config, subgraphs=True)
        run_id = config["configurable"]["thread_id"]
        plan_json = _latest_plan_json(snapshot)
        plan = (
            MigrationPlan.model_validate(plan_json)
            if plan_json
            else read_plan(run_paths(self.settings.runs_dir, run_id))
        )
        return Outcome(
            plan=plan,
            pending_gate=_interrupt_payload(snapshot),
            next_nodes=tuple(snapshot.next),
        )


def _latest_plan_json(snapshot: Any) -> dict[str, Any] | None:
    """The newest plan: a paused agent subgraph is ahead of its parent graph."""
    for task in getattr(snapshot, "tasks", ()) or ():
        sub = getattr(task, "state", None)
        if sub is not None and hasattr(sub, "values"):
            nested = _latest_plan_json(sub)
            if nested:
                return nested
    values = getattr(snapshot, "values", None) or {}
    return values.get("plan")


def _interrupt_payload(snapshot: Any) -> dict[str, Any] | None:
    """Payload of the gate a human must answer, if any.

    Needs a snapshot taken with `subgraphs=True`. The deepest subgraph state is
    authoritative: after a crash past an answered gate, the parent graph still
    lists the old interrupt although the subgraph has already moved on.
    """
    for task in getattr(snapshot, "tasks", ()) or ():
        sub = getattr(task, "state", None)
        if sub is not None and hasattr(sub, "tasks"):
            payload = _interrupt_payload(sub)
            if payload:
                return payload
            continue
        for item in getattr(task, "interrupts", ()) or ():
            value = item.value
            return value if isinstance(value, dict) else {"value": value}
    return None
