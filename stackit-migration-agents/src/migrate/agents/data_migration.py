"""Data migration agent.

    enter -> prepare -> transfer -> (dry run -> END) -> verify -> report
          -> (failed -> END) -> gate -> await_approval (interrupt) -> cutover -> END

- prepare: transfer tasks from the plan (object storage buckets, PostgreSQL).
- transfer: `rclone copy` / `pg_dump`+`pg_restore` via the datamove MCP server.
  Without --execute (and outside mock mode) everything is a dry run.
- verify: object count, total size, sampled checksums, row counts per table,
  schema diff. All checks must pass before a cutover can be requested.
- report: data-verification.md (LLM summary + deterministic tables) and a
  cutover runbook.
- cutover: needs a human approval of the exact verification report.

Source data is never modified or deleted.
"""

from __future__ import annotations

import difflib
import json
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
from migrate.approval import sha256_file
from migrate.context import AppContext
from migrate.reports import render
from migrate.runs import write_atomic
from migrate.state import (
    ApprovalDecision,
    CutoverStatus,
    Gate,
    MappingStatus,
    MigrationPlan,
    MigrationState,
    ResourceKind,
    Stage,
    TransferKind,
    TransferStatus,
    TransferTask,
    VerificationCheck,
    load_plan,
    utcnow,
)
from migrate.terraform import dns_name, tf_ident

AGENT = Stage.DATA_MIGRATION.value


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


async def prepare(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    plan.data_migration.tasks = build_tasks(plan)
    plan.data_migration.checks = []
    plan.data_migration.status = CutoverStatus.NOT_STARTED
    active = [
        t for t in plan.data_migration.tasks if t.status == TransferStatus.PENDING
    ]
    skipped = [
        t for t in plan.data_migration.tasks if t.status == TransferStatus.SKIPPED
    ]
    ctx.reporter.ok(
        AGENT, f"{len(active)} transfers planned, {len(skipped)} skipped (see report)"
    )
    for task in skipped:
        ctx.reporter.warn(AGENT, f"{task.source}: {task.error}")
    return commit(ctx, plan)


async def transfer(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    dry_run = not mutations_allowed(ctx, state)
    async with ctx.mcp.toolbox("datamove", agent=AGENT) as tools:
        for task in plan.data_migration.tasks:
            if task.status != TransferStatus.PENDING:
                continue
            label = f"{task.source} -> {task.target}"
            ctx.reporter.step(
                AGENT, f"{task.method}: {label}" + (" [dry run]" if dry_run else "")
            )
            try:
                if task.kind == TransferKind.OBJECT_STORAGE:
                    result = await tools.call(
                        "copy_bucket",
                        mutating=not dry_run,
                        source_bucket=task.source,
                        target_bucket=task.target,
                        dry_run=dry_run,
                    )
                else:
                    result = await tools.call(
                        "pg_transfer",
                        mutating=not dry_run,
                        ref=task.id.removeprefix("pg:"),
                        dry_run=dry_run,
                    )
            except Exception as exc:  # one failed transfer must not hide the others
                task.status = TransferStatus.FAILED
                task.error = str(exc)[:1000]
                ctx.reporter.warn(AGENT, f"{label} failed: {task.error[:200]}")
                continue
            task.stats = {k: v for k, v in result.items() if k not in ("dry_run",)}
            task.status = TransferStatus.DRY_RUN if dry_run else TransferStatus.COPIED
            if not dry_run:
                ctx.reporter.ok(AGENT, f"{label} copied{_stats_text(task)}")
    if dry_run:
        plan.data_migration.report_path = None
        return halt(
            ctx,
            plan,
            AGENT,
            "Dry run: transfers checked, nothing written to STACKIT. Re-run `migrate data` with --execute.",
        )
    return commit(ctx, plan)


async def verify(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    checks: list[VerificationCheck] = []
    sample_size = ctx.settings.verify_sample_size
    async with ctx.mcp.toolbox("datamove", agent=AGENT) as tools:
        for task in plan.data_migration.tasks:
            if task.status == TransferStatus.FAILED:
                checks.append(
                    VerificationCheck(
                        task_id=task.id,
                        name="transfer",
                        passed=False,
                        details=task.error or "",
                    )
                )
                continue
            if task.status != TransferStatus.COPIED:
                continue
            try:
                if task.kind == TransferKind.OBJECT_STORAGE:
                    checks += await _verify_bucket(tools, task, sample_size)
                else:
                    checks += await _verify_postgres(ctx, tools, task)
            except Exception as exc:  # a check that cannot run is a failed check
                checks.append(
                    VerificationCheck(
                        task_id=task.id,
                        name="verification_error",
                        passed=False,
                        details=f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                )
    plan.data_migration.checks = checks
    failed = [c for c in checks if not c.passed]
    plan.data_migration.status = (
        CutoverStatus.FAILED if failed else CutoverStatus.VERIFIED
    )
    if failed:
        ctx.reporter.warn(
            AGENT, f"Verification: {len(failed)} of {len(checks)} checks FAILED"
        )
    else:
        ctx.reporter.ok(AGENT, f"Verification: all {len(checks)} checks passed")
    ctx.reporter.table(
        "Data verification",
        ["Transfer", "Check", "Source", "Target", "Result"],
        [
            [
                c.task_id,
                c.name,
                _short(c.source_value),
                _short(c.target_value),
                "✅" if c.passed else "❌",
            ]
            for c in checks
        ],
    )
    return commit(ctx, plan)


async def report(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    dm = plan.data_migration
    facts = {
        "transfers": [
            {
                "task": t.id,
                "status": t.status.value,
                "method": t.method,
                "skip_or_error": t.error,
            }
            for t in dm.tasks
        ],
        "checks_total": len(dm.checks),
        "checks_failed": [
            f"{c.task_id}/{c.name}: {c.details}" for c in dm.checks if not c.passed
        ],
        "source_untouched": True,
    }
    dm.summary = await ctx.llm.write(
        agent=AGENT,
        task="data_verification_summary",
        key=f"{plan.source_cloud.value}:"
        + ("passed" if dm.status == CutoverStatus.VERIFIED else "failed"),
        system=(
            "You summarise data migration verification results for the person approving the cutover. "
            "3-5 sentences, factual, only use the facts given. Mention anything not migrated."
        ),
        prompt=json.dumps(facts, indent=2),
        fallback=(
            f"{len(dm.checks) - len(facts['checks_failed'])} of {len(dm.checks)} verification checks passed."
        ),
    )
    write_atomic(
        ctx.paths.cutover_runbook,
        render("reports/cutover-runbook.md.j2", **_runbook_context(plan)),
    )
    dm.runbook_path = str(ctx.paths.cutover_runbook)
    write_atomic(ctx.paths.verification_report, render_verification_report(plan))
    dm.report_path = str(ctx.paths.verification_report)
    dm.report_sha256 = sha256_file(ctx.paths.verification_report)
    ctx.reporter.ok(
        AGENT,
        f"Report written: {ctx.paths.verification_report.name}, runbook: {ctx.paths.cutover_runbook.name}",
    )
    if dm.status == CutoverStatus.FAILED:
        return halt(
            ctx,
            plan,
            AGENT,
            "Verification failed: no cutover. Fix the issues in data-verification.md and re-run `migrate data`.",
        )
    return commit(ctx, plan)


def after_report(state: MigrationState) -> str:
    return END if load_plan(state).halt_reason else "gate"


async def gate(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    request_approval(
        ctx,
        plan,
        gate=Gate.CUTOVER,
        artifact=ctx.paths.verification_report,
        agent=AGENT,
    )
    plan.data_migration.status = CutoverStatus.AWAITING_APPROVAL
    return commit(ctx, plan)


async def await_approval_node(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    dm = plan.data_migration
    passed = sum(1 for c in dm.checks if c.passed)
    record = await_approval(
        ctx,
        plan,
        gate=Gate.CUTOVER,
        agent=AGENT,
        payload={
            "title": "Approve cutover to STACKIT",
            "summary": (
                f"{dm.summary}\n\n**Verification:** {passed}/{len(dm.checks)} checks passed. "
                f"Runbook: `{Path(dm.runbook_path or '').name}`. Source data stays untouched."
            ),
            "artifact": dm.report_path,
            "simulated": ctx.mock,
        },
    )
    if record.decision == ApprovalDecision.REJECTED:
        dm.status = CutoverStatus.REJECTED
        return halt(
            ctx,
            plan,
            AGENT,
            f"Cutover rejected by {record.approver}: {record.comment or 'no reason given'}.",
        )
    ctx.reporter.ok(AGENT, f"Cutover approved by {record.approver}")
    return commit(ctx, plan)


def after_approval(state: MigrationState) -> str:
    return END if load_plan(state).halt_reason else "cutover"


async def cutover(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    report_file = ctx.paths.verification_report
    buckets = [
        {"source": t.source, "target": t.target}
        for t in plan.data_migration.tasks
        if t.kind == TransferKind.OBJECT_STORAGE and t.status == TransferStatus.COPIED
    ]
    try:
        token = approval_token(
            ctx, plan, gate=Gate.CUTOVER, action="data.cutover", artifact=report_file
        )
        async with ctx.mcp.toolbox("datamove", agent=AGENT) as tools:
            result = await tools.call(
                "cutover",
                mutating=True,
                report_path=str(report_file),
                approval_token=token,
                buckets=buckets,
            )
    except Exception as exc:  # record and stop; the next `data` re-verifies
        plan.data_migration.status = CutoverStatus.FAILED
        return halt(
            ctx,
            plan,
            AGENT,
            f"Cutover failed: {str(exc)[:300]}. Run `migrate data` again to re-verify "
            "and request a new approval.",
        )
    plan.data_migration.status = CutoverStatus.CUT_OVER
    plan.data_migration.finished_at = utcnow()
    ctx.reporter.ok(
        AGENT,
        f"Cutover complete: final delta sync of {len(result.get('delta_synced', []))} bucket(s)"
        + (" [simulated]" if result.get("simulated") else "")
        + f". Source untouched. Next: follow {ctx.paths.cutover_runbook.name}",
    )
    return commit(ctx, plan)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def build_tasks(plan: MigrationPlan) -> list[TransferTask]:
    tasks: list[TransferTask] = []
    by_id = {m.resource_id: m for m in plan.mappings}
    for res in plan.resources:
        mapping = by_id.get(res.id)
        if (
            mapping is None
            or mapping.status == MappingStatus.GAP
            or not mapping.terraform_type
        ):
            continue
        if res.kind == ResourceKind.OBJECT_BUCKET:
            task = TransferTask(
                id=f"s3:{res.name}",
                kind=TransferKind.OBJECT_STORAGE,
                source_resource_id=res.id,
                source=res.name,
                target=dns_name(mapping.target_spec.get("bucket_name") or res.name),
                method="rclone copy",
            )
            if res.properties.get("has_archive_storage"):
                task.status = TransferStatus.SKIPPED
                task.error = (
                    "holds Glacier/Deep Archive objects: restore them first (cost, time) or keep "
                    "the archive on AWS; then re-run `migrate data`"
                )
            tasks.append(task)
        elif res.kind == ResourceKind.POSTGRES:
            tasks.append(
                TransferTask(
                    id=f"pg:{tf_ident(res.name)}",
                    kind=TransferKind.POSTGRES,
                    source_resource_id=res.id,
                    source=f"{res.name}/{res.properties.get('db_name') or 'postgres'}",
                    target=f"{dns_name(res.name)}/{mapping.target_spec.get('db_name') or 'postgres'}",
                    method="pg_dump/pg_restore",
                )
            )
        elif res.kind in (ResourceKind.MYSQL, ResourceKind.MARIADB):
            tasks.append(
                TransferTask(
                    id=f"mysql:{tf_ident(res.name)}",
                    kind=TransferKind.MYSQL,
                    source_resource_id=res.id,
                    source=res.name,
                    target=dns_name(res.name),
                    method="mysqldump (manual)",
                    status=TransferStatus.SKIPPED,
                    error=(
                        "MySQL -> MariaDB data copy is not automated in v1; use mysqldump and the "
                        "MariaDB credential from Terraform (extension point)"
                    ),
                )
            )
    return tasks


async def _verify_bucket(
    tools: Any, task: TransferTask, sample_size: int
) -> list[VerificationCheck]:
    source = await tools.call("bucket_stats", side="source", bucket=task.source)
    target = await tools.call("bucket_stats", side="target", bucket=task.target)
    sums = await tools.call(
        "sample_checksums",
        source_bucket=task.source,
        target_bucket=task.target,
        sample_size=sample_size,
    )
    return [
        VerificationCheck(
            task_id=task.id,
            name="object_count",
            passed=source["objects"] == target["objects"],
            source_value=source["objects"],
            target_value=target["objects"],
        ),
        VerificationCheck(
            task_id=task.id,
            name="total_size_bytes",
            passed=source["bytes"] == target["bytes"],
            source_value=source["bytes"],
            target_value=target["bytes"],
        ),
        VerificationCheck(
            task_id=task.id,
            name="sample_checksums",
            passed=sums["sampled"] > 0 and sums["matched"] == sums["sampled"],
            source_value=f"{sums['sampled']} sampled",
            target_value=f"{sums['matched']} match",
            details="; ".join(
                [f"mismatch: {k}" for k in sums.get("mismatches", [])]
                + [f"missing: {k}" for k in sums.get("missing", [])]
            ),
        ),
    ]


async def _verify_postgres(
    ctx: AppContext, tools: Any, task: TransferTask
) -> list[VerificationCheck]:
    ref = task.id.removeprefix("pg:")
    source = (await tools.call("pg_row_counts", side="source", ref=ref))["tables"]
    target = (await tools.call("pg_row_counts", side="target", ref=ref))["tables"]
    differing = sorted(
        t for t in set(source) | set(target) if source.get(t) != target.get(t)
    )
    task.stats["row_counts"] = {
        t: {"source": source.get(t), "target": target.get(t)}
        for t in sorted(set(source) | set(target))
    }
    schema_src = await tools.call("pg_schema", side="source", ref=ref)
    schema_dst = await tools.call("pg_schema", side="target", ref=ref)
    schema_details = ""
    if schema_src["sha256"] != schema_dst["sha256"]:
        diff = list(
            difflib.unified_diff(
                Path(schema_src["file"]).read_text().splitlines(),
                Path(schema_dst["file"]).read_text().splitlines(),
                "source",
                "target",
                lineterm="",
                n=1,
            )
        )
        diff_file = ctx.paths.data_dir / f"{ref}.schema.diff"
        write_atomic(diff_file, "\n".join(diff) + "\n")
        schema_details = f"{len(diff)} diff lines, see data/{diff_file.name}"
    return [
        VerificationCheck(
            task_id=task.id,
            name="row_counts",
            passed=not differing,
            source_value=f"{len(source)} tables, {sum(source.values()):,} rows",
            target_value=f"{len(target)} tables, {sum(target.values()):,} rows",
            details=", ".join(
                f"{t}: {source.get(t)} vs {target.get(t)}" for t in differing[:20]
            ),
        ),
        VerificationCheck(
            task_id=task.id,
            name="schema_diff",
            passed=schema_src["sha256"] == schema_dst["sha256"],
            source_value=schema_src["sha256"][:12],
            target_value=schema_dst["sha256"][:12],
            details=schema_details or "identical (normalised pg_dump --schema-only)",
        ),
    ]


def _stats_text(task: TransferTask) -> str:
    stats = task.stats
    parts = []
    if "objects_copied" in stats:
        parts.append(f"{stats['objects_copied']:,} objects")
    if "bytes_copied" in stats:
        parts.append(f"{stats['bytes_copied'] / 1024**3:,.1f} GiB")
    if "dump_bytes" in stats:
        parts.append(f"dump {stats['dump_bytes'] / 1024**3:,.1f} GiB")
    return f" ({', '.join(parts)})" if parts else ""


def _short(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return str(value) if value is not None else "-"


def render_verification_report(plan: MigrationPlan) -> str:
    dm = plan.data_migration
    return render(
        "reports/data-verification.md.j2",
        plan=plan,
        dm=dm,
        passed=sum(1 for c in dm.checks if c.passed),
        failed=[c for c in dm.checks if not c.passed],
        generated=utcnow(),
    )


def _runbook_context(plan: MigrationPlan) -> dict[str, Any]:
    by_id = {m.resource_id: m for m in plan.mappings}
    databases = [
        {
            "name": r.name,
            "tf": tf_ident(r.name),
            "kind": r.kind.value,
            "terraform_type": by_id[r.id].terraform_type,
        }
        for r in plan.resources
        if r.kind in (ResourceKind.POSTGRES, ResourceKind.MYSQL, ResourceKind.MARIADB)
        and r.id in by_id
        and by_id[r.id].status != MappingStatus.GAP
    ]
    lbs = [
        {"name": r.name, "tf": tf_ident(r.name)}
        for r in plan.resources
        if r.kind == ResourceKind.LOAD_BALANCER
        and r.id in by_id
        and by_id[r.id].status != MappingStatus.GAP
    ]
    consumers = sorted({g.reason for g in plan.gaps if g.source == "llm"})
    return {
        "plan": plan,
        "databases": databases,
        "load_balancers": lbs,
        "llm_notes": consumers,
        "buckets": [
            t
            for t in plan.data_migration.tasks
            if t.kind == TransferKind.OBJECT_STORAGE
        ],
    }


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node("enter", enter_node(Stage.DATA_MIGRATION, "Data migration agent"))
    builder.add_node("prepare", prepare)
    builder.add_node("transfer", transfer)
    builder.add_node("verify", verify)
    builder.add_node("report", report)
    builder.add_node("gate", gate)
    builder.add_node("await_approval", await_approval_node)
    builder.add_node("cutover", cutover)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "prepare")
    builder.add_edge("prepare", "transfer")
    builder.add_conditional_edges(
        "transfer",
        lambda s: END if load_plan(s).halt_reason else "verify",
        ["verify", END],
    )
    builder.add_edge("verify", "report")
    builder.add_conditional_edges("report", after_report, ["gate", END])
    builder.add_edge("gate", "await_approval")
    builder.add_conditional_edges("await_approval", after_approval, ["cutover", END])
    builder.add_edge("cutover", END)
    return builder.compile()
