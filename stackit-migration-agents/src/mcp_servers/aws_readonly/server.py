"""Read-only AWS MCP server.

Exposes describe/list tools only. The underlying `AwsAdapter` rejects any
non-allowlisted API operation before a request is sent.
"""

from __future__ import annotations

import os

from mcp_servers.common import make_server
from mcp_servers.inventory import register_inventory_tools
from migrate.adapters.aws import AwsAdapter

mcp = make_server("aws_readonly", "Read-only AWS inventory. Describe/list calls only.")
register_inventory_tools(
    mcp, "aws", lambda: AwsAdapter(region=os.environ.get("AWS_REGION", "eu-central-1"))
)


def main() -> None:
    mcp.run("stdio")
