"""Read-only AWS MCP server (stub for milestone 1)."""

from __future__ import annotations

from typing import Any

from mcp_servers.common import is_mock, load_fixture, make_server

mcp = make_server("aws_readonly", "Read-only AWS inventory. Describe/list calls only.")


@mcp.tool()
def get_inventory(region: str) -> dict[str, Any]:
    """Return the normalised inventory of the AWS account in `region`."""
    if is_mock():
        return load_fixture("aws", "inventory.json")
    raise NotImplementedError("live AWS discovery arrives in milestone 2")


def main() -> None:
    mcp.run("stdio")
