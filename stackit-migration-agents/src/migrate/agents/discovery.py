"""Discovery agent (read-only).

    enter -> inventory -> map_resources -> review -> estimate -> report -> END

- inventory: describe/list calls through the read-only MCP server.
- map_resources: deterministic mapping from config/mapping.yaml plus sizing
  against the STACKIT catalogue; the LLM only settles ambiguous choices.
- review: the LLM classifies workloads and flags extra migration risks.
- estimate: cost from config/pricing.yaml (never invented).
- report: discovery-report.md with an LLM-written executive summary.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from migrate import costing
from migrate.adapters.base import INVENTORY_SECTIONS
from migrate.agents.common import commit, enter_node
from migrate.context import AppContext
from migrate.mapping import Catalog, MappingEngine
from migrate.reports import render
from migrate.runs import write_atomic
from migrate.state import (
    Gap,
    GapSeverity,
    MappingStatus,
    MigrationPlan,
    MigrationState,
    ResourceKind,
    SourceCloud,
    SourceResource,
    Stage,
    load_plan,
    utcnow,
)

AGENT = Stage.DISCOVERY.value
WORKLOAD_KINDS = {
    ResourceKind.COMPUTE_INSTANCE,
    ResourceKind.POSTGRES,
    ResourceKind.MYSQL,
    ResourceKind.MARIADB,
    ResourceKind.OBJECT_BUCKET,
    ResourceKind.LOAD_BALANCER,
    ResourceKind.KUBERNETES_CLUSTER,
    ResourceKind.OTHER,
}


# --------------------------------------------------------------------------
# LLM output schemas
# --------------------------------------------------------------------------


class Classification(BaseModel):
    resource_id: str
    role: str = Field(
        description="Short workload role, e.g. 'web frontend', 'primary database'."
    )
    criticality: Literal["high", "medium", "low"]


class Concern(BaseModel):
    resource_id: str
    concern: str = Field(description="A migration risk not already listed.")
    severity: Literal["high", "medium", "low"]
    suggestion: str


class InventoryReview(BaseModel):
    classifications: list[Classification] = Field(default_factory=list)
    concerns: list[Concern] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


async def inventory(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    server = "aws" if plan.source_cloud == SourceCloud.AWS else "azure"
    ctx.reporter.step(
        AGENT,
        f"Inventory of {plan.source_cloud.value.upper()} in {plan.source_region} (read-only describe/list calls)",
    )
    resources: list[SourceResource] = []
    async with ctx.mcp.toolbox(server, agent=AGENT) as tools:
        account = await tools.call("get_account")
        for section in INVENTORY_SECTIONS:
            data = await tools.call(f"list_{section}")
            found = [
                SourceResource.model_validate(r) for r in data.get("resources", [])
            ]
            resources.extend(found)
            ctx.reporter.step(
                AGENT, f"  {section.replace('_', ' '):<22} {len(found):>3}"
            )
    plan.discovery.account_id = account.get("account_id")
    plan.resources = resources
    ctx.reporter.ok(
        AGENT,
        f"Discovered {len(resources)} resources in account {plan.discovery.account_id}",
    )
    return commit(ctx, plan)


async def map_resources(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    async with ctx.mcp.toolbox("stackit", agent=AGENT) as tools:
        raw_catalog = await tools.call("get_catalog")
    catalog = Catalog.from_dict(raw_catalog)
    write_atomic(
        ctx.paths.root / "stackit-catalog.json",
        json.dumps(raw_catalog, indent=2) + "\n",
    )
    plan.discovery.catalog_source = catalog.source
    plan.discovery.notes = []
    if catalog.source == "fixture":
        plan.discovery.notes.append(
            "STACKIT catalogue (machine types, flavors, plans, versions) comes from a mock fixture "
            "with illustrative sizes. Live runs query the STACKIT CLI."
        )
    ctx.reporter.step(AGENT, f"Mapping to STACKIT (catalogue: {catalog.source})")
    engine = MappingEngine(ctx.config.load("mapping.yaml"), catalog)
    plan.mappings, plan.gaps = await engine.map_all(
        plan.resources, ctx.llm, agent=AGENT
    )
    counts = Counter(m.status for m in plan.mappings)
    llm_decided = sum(1 for m in plan.mappings if m.decided_by.value == "llm")
    ctx.reporter.ok(
        AGENT,
        f"{counts[MappingStatus.MAPPED]} mapped, {counts[MappingStatus.PARTIAL]} need review, "
        f"{counts[MappingStatus.GAP]} gaps ({llm_decided} ambiguous choices settled by the LLM)",
    )
    return commit(ctx, plan)


async def review(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    workloads = [r for r in plan.resources if r.kind in WORKLOAD_KINDS]
    ids = {r.id for r in plan.resources}
    known_gaps = [f"{g.resource_id}: {g.reason}" for g in plan.gaps]
    lines = [
        f"- {r.id} | {r.name} | {r.source_type} | tags={r.tags} | "
        f"{ {k: v for k, v in r.properties.items() if k in _REVIEW_KEYS} }"
        for r in workloads
    ]
    decision = await ctx.llm.decide(
        agent=AGENT,
        task="inventory_review",
        key=plan.source_cloud.value,
        system=(
            "You are a senior cloud migration architect reviewing a source inventory before a "
            "migration to STACKIT (sovereign German cloud). Be precise and conservative. "
            "Only use resource ids from the list."
        ),
        prompt=(
            "1) Classify each workload resource: a short role and a criticality (high/medium/low).\n"
            "2) List migration risks that are NOT already in the known gaps (security exposure, "
            "hidden dependencies, scheduled jobs, data residency). Max 5, most important first.\n\n"
            "Workload resources:\n"
            + "\n".join(lines)
            + "\n\nKnown gaps:\n"
            + "\n".join(known_gaps)
        ),
        schema=InventoryReview,
        fallback=InventoryReview(),
    )
    result = decision.value
    by_id = {m.resource_id: m for m in plan.mappings}
    for item in result.classifications:
        if item.resource_id in by_id:
            by_id[item.resource_id].role = item.role
            by_id[item.resource_id].criticality = item.criticality
    added = 0
    for concern in result.concerns:
        if concern.resource_id not in ids:
            continue  # never accept references to resources that do not exist
        plan.gaps.append(
            Gap(
                resource_id=concern.resource_id,
                reason=concern.concern,
                severity=GapSeverity(concern.severity),
                suggestion=concern.suggestion,
                source="llm",
            )
        )
        if concern.resource_id in by_id:
            by_id[concern.resource_id].needs_review = True
        added += 1
    ctx.reporter.ok(
        AGENT,
        f"LLM review: {len(result.classifications)} workloads classified, {added} extra risks flagged",
    )
    return commit(ctx, plan)


_REVIEW_KEYS = {
    "instance_type",
    "platform",
    "public_ip",
    "security_group_ids",
    "engine",
    "engine_version",
    "multi_az",
    "publicly_accessible",
    "size_bytes",
    "object_count",
    "versioning",
    "website",
    "lb_type",
    "scheme",
    "listeners",
    "kubernetes_version",
    "node_groups",
}


async def estimate(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    plan.cost = costing.estimate(
        plan.resources, plan.mappings, ctx.config.load("pricing.yaml")
    )
    if plan.cost.missing_prices:
        ctx.reporter.warn(
            AGENT,
            f"Cost estimate incomplete: {len(plan.cost.missing_prices)} prices not set in config/pricing.yaml",
        )
    else:
        ctx.reporter.ok(
            AGENT,
            f"Estimated monthly cost: {plan.cost.source_total:,.2f} -> {plan.cost.target_total:,.2f} {plan.cost.currency}",
        )
    return commit(ctx, plan)


async def report(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    facts = _facts(plan)
    plan.discovery.summary = await ctx.llm.write(
        agent=AGENT,
        task="discovery_summary",
        key=plan.source_cloud.value,
        system=(
            "You write executive summaries for migration readiness reports. Audience: IT leadership. "
            "Plain language, 5-8 sentences, no marketing, no invented numbers: only use the facts given."
        ),
        prompt=json.dumps(facts, indent=2),
        fallback=(
            f"{facts['resources']} resources discovered; {facts['mapped']} map cleanly to STACKIT, "
            f"{facts['partial']} need review and {facts['gaps']} have no clean equivalent."
        ),
    )
    plan.discovery.completed_at = utcnow()
    path = ctx.paths.discovery_report
    write_atomic(path, render_report(plan))
    plan.discovery.report_path = str(path)
    _console_summary(ctx, plan)
    ctx.reporter.ok(AGENT, f"Report written: {path.name}")
    return commit(ctx, plan)


def _facts(plan: MigrationPlan) -> dict[str, Any]:
    counts = Counter(m.status for m in plan.mappings)
    return {
        "source": f"{plan.source_cloud.value} {plan.source_region}",
        "target": f"STACKIT {plan.target_region}",
        "resources": len(plan.resources),
        "by_kind": dict(Counter(r.kind.value for r in plan.resources)),
        "mapped": counts[MappingStatus.MAPPED],
        "partial": counts[MappingStatus.PARTIAL],
        "gaps": counts[MappingStatus.GAP],
        "top_issues": [
            {
                "resource": plan.resource(g.resource_id).name,
                "severity": g.severity.value,
                "reason": g.reason,
            }
            for g in sorted(plan.gaps, key=lambda g: _SEVERITY_RANK[g.severity])[:8]
        ],
        "cost_complete": not plan.cost.missing_prices,
        "missing_prices": len(plan.cost.missing_prices),
    }


_SEVERITY_RANK = {GapSeverity.HIGH: 0, GapSeverity.MEDIUM: 1, GapSeverity.LOW: 2}


def render_report(plan: MigrationPlan) -> str:
    by_id = {m.resource_id: m for m in plan.mappings}
    rows = [(r, by_id.get(r.id)) for r in plan.resources]
    gaps = sorted(plan.gaps, key=lambda g: (_SEVERITY_RANK[g.severity], g.resource_id))
    return render(
        "reports/discovery-report.md.j2",
        plan=plan,
        rows=rows,
        gaps=[(g, plan.resource(g.resource_id)) for g in gaps],
        counts=Counter(m.status.value for m in plan.mappings),
        llm_decisions=[
            (m, plan.resource(m.resource_id))
            for m in plan.mappings
            if m.decided_by.value != "rule"
        ],
        names={r.id: r.name for r in plan.resources},
        issues=Counter(g.resource_id for g in plan.gaps),
    )


def _console_summary(ctx: AppContext, plan: MigrationPlan) -> None:
    icon = {"mapped": "✅", "partial": "⚠️ ", "gap": "❌"}
    rows = []
    by_id = {m.resource_id: m for m in plan.mappings}
    for res in plan.resources:
        mapping = by_id.get(res.id)
        if mapping is None or res.kind not in WORKLOAD_KINDS:
            continue
        rows.append(
            [
                res.name,
                res.source_type.split(":", 1)[1],
                mapping.target_service or "-",
                f"{icon[mapping.status.value]} {mapping.status.value}",
            ]
        )
    ctx.reporter.table(
        "Workloads → STACKIT", ["Resource", "Type", "STACKIT target", "Status"], rows
    )


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node(
        "enter", enter_node(Stage.DISCOVERY, "Discovery agent · read-only")
    )
    builder.add_node("inventory", inventory)
    builder.add_node("map_resources", map_resources)
    builder.add_node("review", review)
    builder.add_node("estimate", estimate)
    builder.add_node("report", report)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "inventory")
    builder.add_edge("inventory", "map_resources")
    builder.add_edge("map_resources", "review")
    builder.add_edge("review", "estimate")
    builder.add_edge("estimate", "report")
    builder.add_edge("report", END)
    return builder.compile()
