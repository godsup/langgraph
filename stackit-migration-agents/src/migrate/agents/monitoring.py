"""Monitoring agent (read-only, never remediates).

    enter -> poll -> triage -> report -> END

- poll: every rule in config/monitoring-rules.yaml is an instant PromQL query
  against STACKIT Observability; thresholds are evaluated deterministically.
- triage: for each anomaly the LLM may run further read-only PromQL queries
  and writes a short triage note with a proposed fix. Nothing is applied: the
  observability MCP server has no mutating tools at all.
- report: console, monitoring-report.md and an optional Slack/Teams webhook.

`migrate monitor <run-id> --interval 60` runs one poll per interval.
"""

from __future__ import annotations

import operator
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from migrate import notify
from migrate.agents.common import commit, enter_node
from migrate.context import AppContext
from migrate.reports import render
from migrate.runs import write_atomic
from migrate.state import (
    Anomaly,
    DeploymentStatus,
    MigrationPlan,
    MigrationState,
    RuleEvaluation,
    Stage,
    load_plan,
    utcnow,
)

AGENT = Stage.MONITORING.value
MAX_HISTORY = 50
OPERATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}
TRIAGE_TOOLS = ("query_promql", "query_range")


async def poll(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    if plan.deployment.status not in (
        DeploymentStatus.APPLIED,
        DeploymentStatus.PR_OPENED,
    ):
        ctx.reporter.warn(
            AGENT,
            "Target not deployed by this run yet; querying the configured Observability instance anyway",
        )
    rules = ctx.config.load("monitoring-rules.yaml").get("rules", [])
    evaluations: list[RuleEvaluation] = []
    anomalies: list[Anomaly] = []
    async with ctx.mcp.toolbox("observability", agent=AGENT) as tools:
        for rule in rules:
            evaluation, found = await _evaluate(tools, rule)
            evaluations.append(evaluation)
            anomalies.extend(found)
    now = utcnow()
    for anomaly in anomalies:
        anomaly.detected_at = now
    plan.monitoring.polls += 1
    plan.monitoring.last_poll_at = now
    plan.monitoring.last_evaluations = evaluations
    plan.monitoring.anomalies = (anomalies + plan.monitoring.anomalies)[:MAX_HISTORY]
    ctx.reporter.table(
        f"Poll #{plan.monitoring.polls}: {len(rules)} rules",
        ["Rule", "Series", "Worst", "Threshold", "Status"],
        [
            [
                e.rule,
                str(e.series),
                "-" if e.worst is None else f"{e.worst:g}{e.unit}",
                f"{e.operator} {e.threshold:g}{e.unit}",
                "⚠ error" if e.error else ("🔴 breach" if e.breaches else "🟢 ok"),
            ]
            for e in evaluations
        ],
    )
    if anomalies:
        ctx.reporter.warn(
            AGENT,
            f"{len(anomalies)} anomaly(ies) detected; starting LLM triage (read-only)",
        )
    else:
        ctx.reporter.ok(AGENT, "No anomalies")
    return commit(ctx, plan)


async def _evaluate(
    tools: Any, rule: dict[str, Any]
) -> tuple[RuleEvaluation, list[Anomaly]]:
    op_symbol = str(rule.get("operator", ">"))
    compare = OPERATORS[op_symbol]
    threshold = float(rule["threshold"])
    evaluation = RuleEvaluation(
        rule=rule["name"],
        description=rule.get("description", ""),
        severity=rule.get("severity", "warning"),
        threshold=threshold,
        operator=op_symbol,
        unit=str(rule.get("unit", "")),
    )
    try:
        result = await tools.call("query_promql", query=rule["query"])
    except Exception as exc:  # one broken rule must not stop the others
        evaluation.error = str(exc)[:300]
        return evaluation, []
    anomalies: list[Anomaly] = []
    values: list[float] = []
    for series in result.get("result", []):
        try:
            value = float(series["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        values.append(value)
        if compare(value, threshold):
            anomalies.append(
                Anomaly(
                    rule=rule["name"],
                    severity=evaluation.severity,
                    query=rule["query"],
                    value=value,
                    threshold=threshold,
                    labels={k: str(v) for k, v in series.get("metric", {}).items()},
                )
            )
    evaluation.series = len(values)
    evaluation.breaches = len(anomalies)
    if values:
        evaluation.worst = max(values) if op_symbol in (">", ">=") else min(values)
    return evaluation, anomalies


async def triage(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    current = [
        a
        for a in plan.monitoring.anomalies
        if a.detected_at == plan.monitoring.last_poll_at
    ]
    if not current:
        return commit(ctx, plan)
    async with ctx.mcp.toolbox("observability", agent=AGENT) as tools:
        for anomaly in current:
            target = ", ".join(f"{k}={v}" for k, v in anomaly.labels.items())
            anomaly.triage = await ctx.llm.investigate(
                agent=AGENT,
                task="monitoring_triage",
                key=anomaly.rule,
                system=(
                    "You are an SRE triaging an alert on a workload that was just migrated to STACKIT. "
                    "You may run read-only PromQL queries to investigate. You cannot change anything. "
                    "Answer with 4 short Markdown bullets labelled What, Likely cause, Proposed fix (not applied), Urgency. "
                    "Only state facts you saw in the data."
                ),
                prompt=(
                    f"Alert: {anomaly.rule} ({anomaly.severity}) on {target}: value {anomaly.value:g}, "
                    f"threshold {anomaly.threshold:g}.\nQuery: {anomaly.query}\n"
                    f"Migration context: {_context(plan, anomaly)}"
                ),
                tools=tools.tools(*TRIAGE_TOOLS),
                execute=lambda name, args: tools.call(name, **args),
                fallback=(
                    f"{anomaly.rule} breached on {target} ({anomaly.value:g} vs {anomaly.threshold:g}). "
                    "No automated triage available; investigate manually. Nothing was changed."
                ),
            )
            ctx.reporter.panel(
                f"🔴 {anomaly.rule} · {target}", anomaly.triage, style="red"
            )
    return commit(ctx, plan)


def _context(plan: MigrationPlan, anomaly: Anomaly) -> str:
    """Relevant facts from the plan for the alerting target (names only)."""
    names = set(anomaly.labels.values())
    notes = []
    for res in plan.resources:
        if res.name in names:
            mapping = plan.mapping_for(res.id)
            notes.append(
                f"{res.name}: {res.source_type} -> {mapping.target_service if mapping else '?'} "
                f"{mapping.target_spec.get('machine_type') or mapping.target_spec.get('flavor', {}).get('id', '') if mapping else ''}; "
                f"tags={res.tags}"
            )
    for gap in plan.gaps:
        if plan.resource(gap.resource_id).name in names:
            notes.append(f"known issue: {gap.reason}")
    return " | ".join(notes) or "no matching resource in the plan"


async def report(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    mon = plan.monitoring
    current = [a for a in mon.anomalies if a.detected_at == mon.last_poll_at]
    write_atomic(
        ctx.paths.monitoring_report,
        render("reports/monitoring-report.md.j2", plan=plan, mon=mon, current=current),
    )
    mon.report_path = str(ctx.paths.monitoring_report)
    webhook = ctx.settings.webhook_url
    if current and webhook:
        url = webhook.get_secret_value()
        inputs = {
            "kind": ctx.settings.webhook_kind,
            "url": url,
            "anomalies": len(current),
        }
        try:
            status = notify.send(url, ctx.settings.webhook_kind, plan.run_id, current)
            mon.notifications_sent += 1
            ctx.audit.record(
                agent=AGENT,
                action="notify.webhook",
                inputs=inputs,
                result={"status": status},
            )
            ctx.reporter.ok(
                AGENT, f"Webhook notification sent ({ctx.settings.webhook_kind})"
            )
        except Exception as exc:  # notifications are best effort
            ctx.audit.record(
                agent=AGENT,
                action="notify.webhook",
                inputs=inputs,
                status="error",
                result={"error": str(exc)[:300]},
            )
            ctx.reporter.warn(AGENT, f"Webhook failed: {type(exc).__name__}")
    ctx.reporter.ok(
        AGENT,
        f"Report updated: {ctx.paths.monitoring_report.name} (nothing was changed automatically)",
    )
    return commit(ctx, plan)


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node(
        "enter", enter_node(Stage.MONITORING, "Monitoring agent · read-only")
    )
    builder.add_node("poll", poll)
    builder.add_node("triage", triage)
    builder.add_node("report", report)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "poll")
    builder.add_edge("poll", "triage")
    builder.add_edge("triage", "report")
    builder.add_edge("report", END)
    return builder.compile()
