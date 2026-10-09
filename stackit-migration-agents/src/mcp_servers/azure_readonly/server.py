"""Azure read-only MCP server.

Stub: serves fixtures through the same interface as the AWS server. A real
implementation (azure-mgmt-* SDKs, Reader role) will replace it later.
"""

from __future__ import annotations

from typing import Any

from mcp_servers.common import load_fixture, make_server

mcp = make_server(
    "azure_readonly", "Read-only Azure inventory (stub backed by fixtures)."
)


@mcp.tool()
def get_inventory(region: str) -> dict[str, Any]:
    """Return the normalised inventory of the Azure subscription in `region`."""
    return load_fixture("azure", "inventory.json")


def main() -> None:
    mcp.run("stdio")
