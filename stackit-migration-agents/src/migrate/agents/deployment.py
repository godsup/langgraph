"""Deployment agent.

    enter -> plan -> (goal "plan" | dry run) -> END
                  -> request_approval -> await_approval (interrupt) -> execute -> END

Milestone 1 skeleton: the shape, the approval gate and the token-checked
apply are real; Terraform rendering arrives in milestone 3.
"""

from __future__ import annotations

from pathlib import Path
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
from migrate.state import (
    ApprovalDecision,
    DeploymentStatus,
    DeployMode,
    Gate,
    MigrationState,
    Stage,
    load_options,
    load_plan,
    utcnow,
)

AGENT = Stage.DEPLOYMENT.value


async def plan_node(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    options = load_options(state)
    if options.deploy_mode:
        plan.deployment.mode = options.deploy_mode
    tf_dir = ctx.paths.terraform_dir
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "main.tf").write_text("# rendered in milestone 3\n", encoding="utf-8")
    ctx.reporter.step(AGENT, "Running terraform plan")
    async with ctx.mcp.toolbox("stackit", agent=AGENT) as tools:
        result = await tools.call("terraform_plan", workdir=str(tf_dir))
    plan.deployment.terraform_dir = str(tf_dir)
    plan.deployment.plan_file = result["plan_file"]
    plan.deployment.status = DeploymentStatus.PLANNED
    plan.deployment.summary = await ctx.llm.write(
        agent=AGENT,
        task="deployment_plan_summary",
        key="*",
        system="Summarise a Terraform plan for a non-technical approver.",
        prompt=result.get("plan_text", ""),
        fallback="Terraform plan created.",
    )
    ctx.reporter.ok(AGENT, "Plan ready")
    return commit(ctx, plan)


def after_plan(state: MigrationState) -> str:
    if state.get("goal") == "plan":
        return END
    return "gate"


async def gate(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    if not mutations_allowed(ctx, state):
        return halt(
            ctx,
            plan,
            AGENT,
            "Dry run: plan created, nothing applied. Re-run with --execute to request approval.",
        )
    request_approval(
        ctx,
        plan,
        gate=Gate.DEPLOYMENT,
        artifact=Path(plan.deployment.plan_file),
        agent=AGENT,
    )
    plan.deployment.status = DeploymentStatus.AWAITING_APPROVAL
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
        gate=Gate.DEPLOYMENT,
        agent=AGENT,
        payload={
            "title": f"Approve deployment ({plan.deployment.mode.value} mode)",
            "summary": plan.deployment.summary,
            "artifact": plan.deployment.plan_file,
        },
    )
    if record.decision == ApprovalDecision.REJECTED:
        plan.deployment.status = DeploymentStatus.REJECTED
        return halt(ctx, plan, AGENT, f"Deployment rejected by {record.approver}.")
    return commit(ctx, plan)


def after_approval(state: MigrationState) -> str:
    return END if load_plan(state).halt_reason else "execute"


async def execute(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    plan_file = Path(plan.deployment.plan_file)
    token = approval_token(
        ctx, plan, gate=Gate.DEPLOYMENT, action="terraform.apply", artifact=plan_file
    )
    async with ctx.mcp.toolbox("stackit", agent=AGENT) as tools:
        await tools.call(
            "terraform_apply",
            mutating=True,
            workdir=plan.deployment.terraform_dir,
            plan_file=str(plan_file),
            approval_token=token,
        )
    plan.deployment.status = (
        DeploymentStatus.APPLIED
        if plan.deployment.mode == DeployMode.APPLY
        else DeploymentStatus.PR_OPENED
    )
    plan.deployment.finished_at = utcnow()
    ctx.reporter.ok(AGENT, "Terraform applied")
    return commit(ctx, plan)


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node(
        "enter", enter_node(Stage.DEPLOYMENT, "Deployment agent · Terraform")
    )
    builder.add_node("plan", plan_node)
    builder.add_node("gate", gate)
    builder.add_node("await_approval", await_approval_node)
    builder.add_node("execute", execute)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "plan")
    builder.add_conditional_edges("plan", after_plan, ["gate", END])
    builder.add_conditional_edges("gate", after_gate, ["await_approval", END])
    builder.add_conditional_edges("await_approval", after_approval, ["execute", END])
    builder.add_edge("execute", END)
    return builder.compile()
