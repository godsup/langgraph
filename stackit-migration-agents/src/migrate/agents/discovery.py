"""Discovery agent (read-only).

    enter -> inventory -> summarize -> END

Milestone 1 skeleton: pulls the inventory through the read-only MCP server
and records an LLM summary. Mapping, gaps and costs follow in milestone 2.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from migrate.agents.common import commit, enter_node
from migrate.context import AppContext
from migrate.state import (
    MigrationState,
    SourceCloud,
    SourceResource,
    Stage,
    load_plan,
    utcnow,
)

AGENT = Stage.DISCOVERY.value


async def inventory(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    server = "aws" if plan.source_cloud == SourceCloud.AWS else "azure"
    ctx.reporter.step(
        AGENT,
        f"Reading {plan.source_cloud.value.upper()} inventory in {plan.source_region} (read-only)",
    )
    async with ctx.mcp.toolbox(server, agent=AGENT) as tools:
        data = await tools.call("get_inventory", region=plan.source_region)
    plan.discovery.account_id = data.get("account_id")
    plan.resources = [
        SourceResource.model_validate(r) for r in data.get("resources", [])
    ]
    ctx.reporter.ok(AGENT, f"Discovered {len(plan.resources)} resources")
    return commit(ctx, plan)


async def summarize(
    state: MigrationState, runtime: Runtime[AppContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    plan.discovery.summary = await ctx.llm.write(
        agent=AGENT,
        task="discovery_summary",
        key=plan.source_cloud.value,
        system="You are a cloud migration architect. Be concise and factual.",
        prompt=f"Summarise this inventory for a migration to STACKIT: {len(plan.resources)} resources.",
        fallback="Inventory collected.",
    )
    plan.discovery.completed_at = utcnow()
    return commit(ctx, plan)


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node(
        "enter", enter_node(Stage.DISCOVERY, "Discovery agent · read-only")
    )
    builder.add_node("inventory", inventory)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "inventory")
    builder.add_edge("inventory", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile()
