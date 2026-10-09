"""Deployment agent.

    enter -> render -> terraform -> summarize -> (goal "plan" -> END)
          -> gate -> await_approval (interrupt) -> execute -> END

- render: deterministic Terraform from Jinja2 templates (migrate.terraform).
- terraform: init, validate, plan through the stackit MCP server.
- summarize: the LLM explains the plan in plain language and lists risks.
- gate: dry run unless --execute (mock mode only simulates); otherwise asks a
  human, with the summary and the raw plan, via LangGraph interrupt().
- execute: `terraform apply` of the approved plan file, or (mode `pr`) a
  pull request to the client's infrastructure repository. Both need an
  approval token bound to the plan file's SHA-256.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from migrate import terraform
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
    DeploymentStatus,
    DeployMode,
    Gate,
    MigrationPlan,
    MigrationState,
    PlannedChange,
    Stage,
    load_options,
    load_plan,
    utcnow,
)

AGENT = Stage.DEPLOYMENT.value
MOCK_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
RAW_PLAN_LINES = 60


class PlanSummary(BaseModel):
    summary: str = Field(
        description="Plain-language summary for a non-technical approver, 4-6 sentences."
    )
    risks: list[str] = Field(
        description="Concrete risks or review points, most important first, max 6."
    )


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


async def render(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    options = load_options(state)
    if options.deploy_mode:
        plan.deployment.mode = options.deploy_mode
    rendered = terraform.render(plan)
    tf_dir = ctx.paths.terraform_dir
    _clean(tf_dir)
    for name, content in rendered.files.items():
        write_atomic(tf_dir / name, content)
    write_atomic(
        tf_dir / "terraform.tfvars.json",
        json.dumps(_tfvars(ctx, plan, rendered.model), indent=2) + "\n",
    )
    plan.deployment.terraform_dir = str(tf_dir)
    plan.deployment.rendered_files = sorted(rendered.files) + ["terraform.tfvars.json"]
    plan.deployment.skipped = [f"{s.name}: {s.reason}" for s in rendered.model.skipped]
    plan.deployment.review_items = list(rendered.model.reviews)
    plan.deployment.status = DeploymentStatus.RENDERED
    plan.deployment.error = None
    by_type = Counter(
        a.split(".")[0] for a in rendered.addresses if not a.startswith("data.")
    )
    ctx.audit.record(
        agent=AGENT,
        action="terraform.render",
        inputs={"templates": list(terraform.FILES)},
        result={
            "files": plan.deployment.rendered_files,
            "resources": dict(by_type),
            "skipped": plan.deployment.skipped,
        },
    )
    ctx.reporter.ok(
        AGENT,
        f"Rendered {sum(by_type.values())} resources into {len(rendered.files)} files "
        f"({len(rendered.model.skipped)} source resources not migrated)",
    )
    return commit(ctx, plan)


async def terraform_plan(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    workdir = plan.deployment.terraform_dir
    async with ctx.mcp.toolbox("stackit", agent=AGENT) as tools:
        ctx.reporter.step(AGENT, "terraform init")
        await tools.call("terraform_init", workdir=workdir)
        ctx.reporter.step(AGENT, "terraform validate")
        validation = await tools.call("terraform_validate", workdir=workdir)
        if not validation.get("valid"):
            plan.deployment.status = DeploymentStatus.FAILED
            plan.deployment.error = "; ".join(
                d["summary"] for d in validation.get("diagnostics", [])
            )[:2000]
            return halt(
                ctx, plan, AGENT, f"terraform validate failed: {plan.deployment.error}"
            )
        ctx.reporter.step(AGENT, "terraform plan")
        result = await tools.call("terraform_plan", workdir=workdir)
    plan.deployment.plan_file = result["plan_file"]
    plan.deployment.plan_sha256 = result["plan_sha256"]
    plan.deployment.plan_text_file = result["plan_text_file"]
    plan.deployment.changes = [
        PlannedChange.model_validate(c) for c in result["changes"]
    ]
    plan.deployment.status = DeploymentStatus.PLANNED
    ctx.reporter.ok(
        AGENT,
        result.get("summary_line") or f"{len(plan.deployment.changes)} changes planned",
    )
    return commit(ctx, plan)


async def summarize(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    facts = _plan_facts(plan)
    decision = await ctx.llm.decide(
        agent=AGENT,
        task="deployment_plan_summary",
        key=plan.source_cloud.value,
        system=(
            "You explain Terraform plans to the person who must approve them. Plain language, no "
            "jargon where avoidable, no invented numbers: only use the facts given. Name concrete risks."
        ),
        prompt=json.dumps(facts, indent=2),
        schema=PlanSummary,
        fallback=PlanSummary(
            summary=(
                f"Terraform will create {facts['to_create']} resources in STACKIT project "
                f"{facts['target']}. Nothing is changed or deleted on the source cloud."
            ),
            risks=facts["review_items"][:6],
        ),
    )
    plan.deployment.summary = decision.value.summary
    plan.deployment.risks = decision.value.risks
    if state.get("goal") == "plan":
        ctx.reporter.panel(
            "Deployment plan (LLM summary)", _summary_markdown(plan), style="magenta"
        )
    else:  # the approval gate shows the full summary next
        ctx.reporter.ok(
            AGENT,
            f"Plan summarised by the LLM ({len(plan.deployment.risks)} risks listed)",
        )
    return commit(ctx, plan)


def after_summarize(state: MigrationState) -> str:
    plan = load_plan(state)
    if plan.halt_reason or state.get("goal") == "plan":
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
            "Dry run: Terraform planned, nothing applied. Re-run `migrate deploy` with --execute to request approval.",
        )
    request_approval(
        ctx,
        plan,
        gate=Gate.DEPLOYMENT,
        artifact=Path(plan.deployment.plan_file or ""),
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
    action = (
        "terraform apply"
        if plan.deployment.mode == DeployMode.APPLY
        else "open a pull request"
    )
    record = await_approval(
        ctx,
        plan,
        gate=Gate.DEPLOYMENT,
        agent=AGENT,
        payload={
            "title": f"Approve deployment: {action} ({plan.deployment.mode.value} mode)",
            "summary": _summary_markdown(plan),
            "artifact": plan.deployment.plan_file,
            "raw_plan": _raw_plan(plan),
            "raw_plan_file": plan.deployment.plan_text_file,
            "mode": plan.deployment.mode.value,
            "simulated": ctx.mock,
        },
    )
    if record.decision == ApprovalDecision.REJECTED:
        plan.deployment.status = DeploymentStatus.REJECTED
        return halt(
            ctx,
            plan,
            AGENT,
            f"Deployment rejected by {record.approver}: {record.comment or 'no reason given'}.",
        )
    ctx.reporter.ok(AGENT, f"Approved by {record.approver}")
    return commit(ctx, plan)


def after_approval(state: MigrationState) -> str:
    return END if load_plan(state).halt_reason else "execute"


async def execute(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    plan_file = Path(plan.deployment.plan_file or "")
    workdir = plan.deployment.terraform_dir
    apply_mode = plan.deployment.mode == DeployMode.APPLY
    # The token is issued only for the exact plan file that was approved.
    token = approval_token(
        ctx,
        plan,
        gate=Gate.DEPLOYMENT,
        action="terraform.apply" if apply_mode else "terraform.pr",
        artifact=plan_file,
    )
    async with ctx.mcp.toolbox("stackit", agent=AGENT) as tools:
        if apply_mode:
            ctx.reporter.step(
                AGENT,
                "terraform apply (approved plan file)"
                + (" [simulated]" if ctx.mock else ""),
            )
            result = await tools.call(
                "terraform_apply",
                mutating=True,
                workdir=workdir,
                plan_file=plan_file.name,
                approval_token=token,
            )
        else:
            result = await tools.call(
                "open_pull_request",
                mutating=True,
                workdir=workdir,
                plan_file=plan_file.name,
                approval_token=token,
                title=f"STACKIT migration {plan.run_id}: target infrastructure",
                body=ctx.audit.redactor.text(_pr_body(plan)),
            )
    if apply_mode:
        plan.deployment.outputs = result.get("outputs", {})
        plan.deployment.status = DeploymentStatus.APPLIED
        output = result.get("output", "").strip()
        ctx.reporter.ok(AGENT, output.splitlines()[-1] if output else "Applied")
    else:
        plan.deployment.pr_url = result.get("pr_url")
        plan.deployment.status = DeploymentStatus.PR_OPENED
        ctx.reporter.ok(
            AGENT,
            f"Pull request opened: {plan.deployment.pr_url}"
            + (" [simulated]" if result.get("simulated") else "")
            + ". The client pipeline applies it after review.",
        )
    plan.deployment.finished_at = utcnow()
    return commit(ctx, plan)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _clean(tf_dir: Path) -> None:
    """Remove previously generated files, keep the provider cache and state."""
    tf_dir.mkdir(parents=True, exist_ok=True)
    for path in tf_dir.iterdir():
        if path.is_file() and (
            path.suffix in {".tf", ".md"}
            or path.name in {"terraform.tfvars.json", "tfplan", "plan.txt"}
        ):
            path.unlink()


def _tfvars(
    ctx: AppContext, plan: MigrationPlan, model: terraform.TerraformModel
) -> dict[str, Any]:
    """Non-secret inputs. Secrets (TLS keys) only ever come from TF_VAR_* env."""
    settings = ctx.settings
    if ctx.mock:
        values: dict[str, Any] = {
            "project_id": MOCK_PROJECT_ID,
            "region": plan.target_region,
        }
        if model.servers:
            values["image_id"] = "11111111-2222-3333-4444-555555555555"
            values["ssh_public_key"] = (
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKeyForDemoOnly demo@mock"
            )
        return values
    missing = []
    if not settings.stackit_project_id:
        missing.append("STACKIT_PROJECT_ID")
    if model.servers and not settings.stackit_image_id:
        missing.append("STACKIT_IMAGE_ID")
    if model.servers and not settings.ssh_public_key_path:
        missing.append("SSH_PUBLIC_KEY_PATH")
    if missing:
        raise ValueError(
            f"deployment needs {', '.join(missing)} in the environment or .env"
        )
    values = {"project_id": settings.stackit_project_id, "region": plan.target_region}
    if model.servers:
        values["image_id"] = settings.stackit_image_id
        values["ssh_public_key"] = (
            Path(settings.ssh_public_key_path).read_text(encoding="utf-8").strip()
        )
    return values


def _plan_facts(plan: MigrationPlan) -> dict[str, Any]:
    changes = plan.deployment.changes
    return {
        "target": f"STACKIT {plan.target_region}",
        "mode": plan.deployment.mode.value,
        "to_create": sum(1 for c in changes if c.action == "create"),
        "to_change": sum(
            1 for c in changes if c.action not in ("create", "delete", "replace")
        ),
        "to_destroy": sum(1 for c in changes if c.action in ("delete", "replace")),
        "by_type": dict(Counter(c.resource_type for c in changes)),
        "not_migrated": plan.deployment.skipped,
        "review_items": plan.deployment.review_items,
        "open_gaps": [
            f"{plan.resource(g.resource_id).name}: {g.reason}" for g in plan.gaps
        ][:12],
        "monthly_cost": (
            "unknown: prices not set in config/pricing.yaml"
            if plan.cost.missing_prices
            else f"{plan.cost.target_total:.2f} {plan.cost.currency} (source {plan.cost.source_total:.2f})"
        ),
    }


def _summary_markdown(plan: MigrationPlan) -> str:
    counts = Counter(c.action for c in plan.deployment.changes)
    by_type = Counter(
        c.resource_type.removeprefix("stackit_") for c in plan.deployment.changes
    )
    lines = [plan.deployment.summary, ""]
    lines.append(
        f"**Changes:** {counts.get('create', 0)} to create, "
        f"{sum(v for k, v in counts.items() if k not in ('create', 'delete', 'replace'))} to change, "
        f"{counts.get('delete', 0) + counts.get('replace', 0)} to destroy  "
    )
    lines.append(
        "**Resources:** " + ", ".join(f"{n}× {t}" for t, n in by_type.most_common())
    )
    if plan.deployment.risks:
        lines.append("")
        lines.append("**Risks / review points**")
        lines.extend(f"- {r}" for r in plan.deployment.risks)
    if plan.deployment.skipped:
        lines.append("")
        lines.append("**Not migrated by this plan**")
        lines.extend(f"- {s}" for s in plan.deployment.skipped)
    return "\n".join(lines)


def _raw_plan(plan: MigrationPlan) -> str:
    if not plan.deployment.plan_text_file:
        return ""
    lines = (
        Path(plan.deployment.plan_text_file).read_text(encoding="utf-8").splitlines()
    )
    head = lines[:RAW_PLAN_LINES]
    if len(lines) > RAW_PLAN_LINES:
        head += [
            f"... ({len(lines) - RAW_PLAN_LINES} more lines in {Path(plan.deployment.plan_text_file).name})",
            next((ln for ln in reversed(lines) if ln.startswith("Plan:")), ""),
        ]
    return "\n".join(head)


def _pr_body(plan: MigrationPlan) -> str:
    return (
        f"Generated by stackit-migration-agents for run `{plan.run_id}` "
        f"({plan.source_cloud.value} {plan.source_region} → STACKIT {plan.target_region}).\n\n"
        f"{_summary_markdown(plan)}\n\n"
        "The plan was approved by a human before this PR was opened. Apply it through the "
        "normal pipeline after review. Search for `REVIEW:` comments in the .tf files.\n"
    )


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node(
        "enter", enter_node(Stage.DEPLOYMENT, "Deployment agent · Terraform")
    )
    builder.add_node("render", render)
    builder.add_node("terraform", terraform_plan)
    builder.add_node("summarize", summarize)
    builder.add_node("gate", gate)
    builder.add_node("await_approval", await_approval_node)
    builder.add_node("execute", execute)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "render")
    builder.add_edge("render", "terraform")
    builder.add_conditional_edges(
        "terraform",
        lambda s: END if load_plan(s).halt_reason else "summarize",
        ["summarize", END],
    )
    builder.add_conditional_edges("summarize", after_summarize, ["gate", END])
    builder.add_conditional_edges("gate", after_gate, ["await_approval", END])
    builder.add_conditional_edges("await_approval", after_approval, ["execute", END])
    builder.add_edge("execute", END)
    return builder.compile()
