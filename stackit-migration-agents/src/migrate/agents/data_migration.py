"""Data migration agent.

    enter -> transfer -> verify -> gate -> await_approval (interrupt) -> cutover -> END

Milestone 1 skeleton: the shape, the cutover gate and the token-checked
cutover are real; rclone/pg transfers and verification arrive in milestone 4.
Source data is never deleted.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from migrate.agents.common import (
    approval_token,
    await_approval,
    commit,
    enter_node,
    halt,
    mutations_allowed,
    request_approval,
)
from migrate.context import AppContext
from migrate.runs import write_atomic
from migrate.state import (
    ApprovalDecision,
    CutoverStatus,
    Gate,
    MigrationState,
    Stage,
    load_plan,
    utcnow,
)

AGENT = Stage.DATA_MIGRATION.value


async def transfer(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    ctx.reporter.step(AGENT, "Copying data (milestone 4)")
    return commit(ctx, plan)


async def verify(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    report = ctx.paths.verification_report
    write_atomic(report, "# Data verification\n\nMilestone 1 placeholder.\n")
    plan.data_migration.report_path = str(report)
    plan.data_migration.status = CutoverStatus.VERIFIED
    return commit(ctx, plan)


async def gate(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    if not mutations_allowed(ctx, state):
        return halt(
            ctx,
            plan,
            AGENT,
            "Dry run: verification done, no cutover. Re-run with --execute.",
        )
    request_approval(
        ctx,
        plan,
        gate=Gate.CUTOVER,
        artifact=ctx.paths.verification_report,
        agent=AGENT,
    )
    plan.data_migration.status = CutoverStatus.AWAITING_APPROVAL
    return commit(ctx, plan)


def after_gate(state: MigrationState) -> str:
    return END if load_plan(state).halt_reason else "await_approval"


async def await_approval_node(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    record = await_approval(
        ctx,
        plan,
        gate=Gate.CUTOVER,
        agent=AGENT,
        payload={
            "title": "Approve cutover",
            "summary": plan.data_migration.summary or "Verification report attached.",
            "artifact": plan.data_migration.report_path,
        },
    )
    if record.decision == ApprovalDecision.REJECTED:
        plan.data_migration.status = CutoverStatus.REJECTED
        return halt(ctx, plan, AGENT, f"Cutover rejected by {record.approver}.")
    return commit(ctx, plan)


def after_approval(state: MigrationState) -> str:
    return END if load_plan(state).halt_reason else "cutover"


async def cutover(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    token = approval_token(
        ctx,
        plan,
        gate=Gate.CUTOVER,
        action="data.cutover",
        artifact=ctx.paths.verification_report,
    )
    async with ctx.mcp.toolbox("datamove", agent=AGENT) as tools:
        await tools.call(
            "cutover",
            mutating=True,
            report_path=str(ctx.paths.verification_report),
            approval_token=token,
        )
    plan.data_migration.status = CutoverStatus.CUT_OVER
    plan.data_migration.finished_at = utcnow()
    ctx.reporter.ok(AGENT, "Cutover complete (source untouched)")
    return commit(ctx, plan)


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node("enter", enter_node(Stage.DATA_MIGRATION, "Data migration agent"))
    builder.add_node("transfer", transfer)
    builder.add_node("verify", verify)
    builder.add_node("gate", gate)
    builder.add_node("await_approval", await_approval_node)
    builder.add_node("cutover", cutover)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "transfer")
    builder.add_edge("transfer", "verify")
    builder.add_edge("verify", "gate")
    builder.add_conditional_edges("gate", after_gate, ["await_approval", END])
    builder.add_conditional_edges("await_approval", after_approval, ["cutover", END])
    builder.add_edge("cutover", END)
    return builder.compile()
