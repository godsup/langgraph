"""Orchestrator: the parent LangGraph graph.

    START -> begin -> route -> {discovery | deployment | data_migration | monitoring} -> route ...

Each agent is a compiled subgraph sharing `MigrationState`. Agents never call
each other; they only read and extend the `MigrationPlan`. The router decides
which stage runs next from the plan's progress and the invocation's goal, and
stops when the goal is reached or a stage halted (dry run, rejection, error).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from migrate.agents import data_migration, deployment, discovery, monitoring
from migrate.agents.common import commit
from migrate.context import AppContext
from migrate.state import (
    VISITED_RESET,
    CutoverStatus,
    DeploymentStatus,
    MigrationPlan,
    MigrationState,
    Stage,
    load_plan,
)

GOAL_STAGES: dict[str, list[Stage]] = {
    "discover": [Stage.DISCOVERY],
    "plan": [Stage.DISCOVERY, Stage.DEPLOYMENT],
    "deploy": [Stage.DISCOVERY, Stage.DEPLOYMENT],
    "data": [Stage.DISCOVERY, Stage.DEPLOYMENT, Stage.DATA_MIGRATION],
    "monitor": [Stage.MONITORING],
    "all": [Stage.DISCOVERY, Stage.DEPLOYMENT, Stage.DATA_MIGRATION, Stage.MONITORING],
}

_PLANNED_OR_LATER = {
    DeploymentStatus.PLANNED,
    DeploymentStatus.AWAITING_APPROVAL,
    DeploymentStatus.APPLIED,
    DeploymentStatus.PR_OPENED,
}
_DEPLOYED = {DeploymentStatus.APPLIED, DeploymentStatus.PR_OPENED}


def stage_done(
    plan: MigrationPlan, stage: Stage, goal: str, visited: list[str]
) -> bool:
    if stage == Stage.DISCOVERY:
        return plan.discovery.completed_at is not None
    if stage == Stage.DEPLOYMENT:
        wanted = _PLANNED_OR_LATER if goal == "plan" else _DEPLOYED
        return plan.deployment.status in wanted
    if stage == Stage.DATA_MIGRATION:
        return plan.data_migration.status == CutoverStatus.CUT_OVER
    return stage.value in visited  # monitoring: one poll per invocation


def next_stage(state: MigrationState) -> str:
    plan = load_plan(state)
    if plan.halt_reason:
        return END
    goal = state.get("goal", "all")
    visited = state.get("visited", [])
    for stage in GOAL_STAGES[goal]:
        if stage_done(plan, stage, goal, visited):
            continue
        # A stage that already ran in this invocation without finishing must
        # have halted; never loop on it.
        return END if stage.value in visited else stage.value
    return END


async def begin(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    plan.halt_reason = None
    ctx.audit.record(
        agent="orchestrator",
        action="invocation.start",
        inputs={"goal": state.get("goal"), "options": state.get("options")},
    )
    goal = state.get("goal", "all")
    if all(stage_done(plan, stage, goal, []) for stage in GOAL_STAGES[goal]):
        ctx.reporter.ok(
            "orchestrator",
            f"Nothing to do: everything up to `{goal}` is already complete for this run "
            "(see `migrate status`).",
        )
    return commit(ctx, plan, visited=[VISITED_RESET])


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node("begin", begin)
    builder.add_node(Stage.DISCOVERY.value, discovery.build_graph())
    builder.add_node(Stage.DEPLOYMENT.value, deployment.build_graph())
    builder.add_node(Stage.DATA_MIGRATION.value, data_migration.build_graph())
    builder.add_node(Stage.MONITORING.value, monitoring.build_graph())

    builder.add_edge(START, "begin")
    targets = [s.value for s in Stage] + [END]
    builder.add_conditional_edges("begin", next_stage, targets)
    for stage in Stage:
        builder.add_conditional_edges(stage.value, next_stage, targets)
    return builder.compile(checkpointer=checkpointer)
