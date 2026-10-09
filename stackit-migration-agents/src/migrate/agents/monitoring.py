"""Monitoring agent (read-only).

    enter -> poll -> END

Milestone 1 skeleton: one PromQL query through the observability MCP server.
Rules, triage and webhooks arrive in milestone 5. Never auto-remediates.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from migrate.agents.common import commit, enter_node
from migrate.context import AppContext
from migrate.state import MigrationState, Stage, load_plan, utcnow

AGENT = Stage.MONITORING.value


async def poll(state: MigrationState, runtime: Runtime[AppContext]) -> dict[str, Any]:
    ctx = runtime.context
    plan = load_plan(state)
    async with ctx.mcp.toolbox("observability", agent=AGENT) as tools:
        await tools.call("query_promql", query="up")
    plan.monitoring.polls += 1
    plan.monitoring.last_poll_at = utcnow()
    ctx.reporter.ok(AGENT, "Metrics polled")
    return commit(ctx, plan)


def build_graph() -> CompiledStateGraph:
    builder = StateGraph(MigrationState, context_schema=AppContext)
    builder.add_node(
        "enter", enter_node(Stage.MONITORING, "Monitoring agent · read-only")
    )
    builder.add_node("poll", poll)
    builder.add_edge(START, "enter")
    builder.add_edge("enter", "poll")
    builder.add_edge("poll", END)
    return builder.compile()
