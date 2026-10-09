"""Azure read-only MCP server (stub backed by fixtures, same tools as AWS)."""

from __future__ import annotations

import os

from mcp_servers.common import fixtures_dir, make_server
from mcp_servers.inventory import register_inventory_tools
from migrate.adapters.azure import AzureAdapter

mcp = make_server(
    "azure_readonly", "Read-only Azure inventory (stub backed by fixtures)."
)
register_inventory_tools(
    mcp,
    "azure",
    lambda: AzureAdapter(
        region=os.environ.get("AZURE_REGION", "germanywestcentral"),
        fixture=fixtures_dir() / "azure" / "inventory.json",
    ),
)


def main() -> None:
    mcp.run("stdio")
