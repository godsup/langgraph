"""Datamove MCP server (milestone 1 stub)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_servers.common import (
    is_mock,
    make_server,
    path_inside,
    require_approval,
    run_dir,
)
from migrate.approval import sha256_file

mcp = make_server("datamove", "rclone and PostgreSQL tools. Never deletes source data.")


@mcp.tool()
def cutover(report_path: str, approval_token: str) -> dict[str, Any]:
    """Final cutover step. Refuses without a valid approval token."""
    report = path_inside(run_dir(), report_path)
    require_approval(
        approval_token, action="data.cutover", artifact_sha256=sha256_file(Path(report))
    )
    return {"cut_over": True, "simulated": is_mock()}


def main() -> None:
    mcp.run("stdio")
