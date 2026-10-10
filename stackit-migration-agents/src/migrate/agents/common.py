"""Helpers shared by the agent subgraphs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel

from migrate.approval import sha256_artifact
from migrate.context import AppContext
from migrate.runs import save_plan
from migrate.state import (
    ApprovalDecision,
    ApprovalRecord,
    Gate,
    MigrationPlan,
    MigrationState,
    Stage,
    load_options,
    load_plan,
    utcnow,
)

Node = Callable[[MigrationState, Runtime[AppContext]], Awaitable[dict[str, Any]]]


def commit(ctx: AppContext, plan: MigrationPlan, **extra: Any) -> dict[str, Any]:
    """Persist the plan to disk and return it as a graph state update."""
    plan.touch()
    save_plan(ctx.paths, plan)
    return {"plan": plan.model_dump(mode="json"), **extra}


def mutations_allowed(ctx: AppContext, state: MigrationState) -> bool:
    """Real mutations need `--execute`; mock mode only ever simulates them."""
    return ctx.mock or load_options(state).execute


def enter_node(stage: Stage, title: str) -> Node:
    async def enter(
        state: MigrationState, runtime: Runtime[AppContext]
    ) -> dict[str, Any]:
        ctx = runtime.context
        ctx.reporter.stage(stage.value, title)
        ctx.audit.record(
            agent=stage.value, action="stage.enter", inputs={"goal": state.get("goal")}
        )
        return {"visited": [stage.value]}

    return enter


def halt(
    ctx: AppContext, plan: MigrationPlan, agent: str, reason: str
) -> dict[str, Any]:
    """Stop this invocation cleanly, with a reason the CLI shows to the user."""
    plan.halt_reason = reason
    ctx.reporter.warn(agent, reason)
    ctx.audit.record(agent=agent, action="stage.halt", result={"reason": reason})
    return commit(ctx, plan)


# --------------------------------------------------------------------------
# Human approval gate (LangGraph interrupt)
# --------------------------------------------------------------------------


class ApprovalResponse(BaseModel):
    approved: bool
    approver: str
    comment: str = ""


def request_approval(
    ctx: AppContext, plan: MigrationPlan, *, gate: Gate, artifact: Path, agent: str
) -> None:
    """Record a pending approval for the exact artefact on disk."""
    record = ApprovalRecord(gate=gate, artifact_sha256=sha256_artifact(artifact))
    plan.approvals.append(record)
    ctx.audit.record(
        agent=agent,
        action=f"approval.{gate.value}.requested",
        inputs={"artifact": str(artifact), "artifact_sha256": record.artifact_sha256},
    )


def await_approval(
    ctx: AppContext,
    plan: MigrationPlan,
    *,
    gate: Gate,
    payload: dict[str, Any],
    agent: str,
) -> ApprovalRecord:
    """Pause the graph until a human decides. Re-runs from here on resume."""
    record = plan.latest_approval(gate)
    if record is None or record.decision != ApprovalDecision.PENDING:
        raise RuntimeError(f"no pending {gate.value} approval to wait for")
    raw = interrupt(
        {**payload, "gate": gate.value, "artifact_sha256": record.artifact_sha256}
    )
    response = ApprovalResponse.model_validate(raw)
    record.decision = (
        ApprovalDecision.APPROVED if response.approved else ApprovalDecision.REJECTED
    )
    record.approver = response.approver
    record.comment = response.comment
    record.decided_at = utcnow()
    ctx.audit.record(
        agent="orchestrator",
        action=f"approval.{gate.value}.decision",
        inputs={"approver": response.approver, "comment": response.comment},
        result={
            "decision": record.decision.value,
            "artifact_sha256": record.artifact_sha256,
        },
    )
    return record


def approval_token(
    ctx: AppContext, plan: MigrationPlan, *, gate: Gate, action: str, artifact: Path
) -> str:
    """Issue a token for an approved gate, re-checking the artefact hash first."""
    record = plan.latest_approval(gate)
    if record is None or record.decision != ApprovalDecision.APPROVED:
        raise PermissionError(f"{gate.value} gate is not approved")
    current = sha256_artifact(artifact)
    if current != record.artifact_sha256:
        raise PermissionError(
            f"{artifact.name} changed after it was approved; re-plan and approve again"
        )
    token = ctx.approvals.issue(
        run_id=ctx.run_id,
        action=action,
        artifact_sha256=current,
        ttl_s=ctx.settings.approval_ttl_s,
    )
    ctx.audit.record(
        agent="orchestrator",
        action="approval.token.issued",
        inputs={"gate": gate.value, "action": action, "artifact_sha256": current},
    )
    return token


def stage_state(state: MigrationState) -> tuple[MigrationPlan, str]:
    return load_plan(state), state.get("goal", "all")
