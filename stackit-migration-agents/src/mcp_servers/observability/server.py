"""Observability MCP server (milestone 1 stub)."""

from __future__ import annotations

from typing import Any

from mcp_servers.common import is_mock, make_server

mcp = make_server(
    "observability", "Read-only PromQL queries against STACKIT Observability."
)


@mcp.tool()
def query_promql(query: str) -> dict[str, Any]:
    """Run an instant PromQL query."""
    if not is_mock():
        raise NotImplementedError("live PromQL arrives in milestone 5")
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def main() -> None:
    mcp.run("stdio")
