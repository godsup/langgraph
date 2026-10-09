"""STACKIT MCP server (milestone 1 stub)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_servers.common import (
    is_mock,
    make_server,
    path_inside,
    require_approval,
    run_dir,
    server_audit,
)
from migrate.approval import sha256_file

mcp = make_server(
    "stackit", "Terraform and STACKIT CLI. Mutations need an approval token."
)


@mcp.tool()
def terraform_plan(workdir: str) -> dict[str, Any]:
    """Run terraform plan in a run's terraform directory."""
    tf_dir = path_inside(run_dir(), workdir)
    plan_file = tf_dir / "tfplan"
    if not is_mock():
        raise NotImplementedError("real terraform plan arrives in milestone 3")
    plan_file.write_text("mock plan\n", encoding="utf-8")
    return {
        "plan_file": str(plan_file),
        "plan_text": "Plan: 0 to add, 0 to change, 0 to destroy.",
    }


@mcp.tool()
def terraform_apply(
    workdir: str, plan_file: str, approval_token: str
) -> dict[str, Any]:
    """Apply a saved plan. Refuses without a valid approval token."""
    tf_dir = path_inside(run_dir(), workdir)
    plan_path = path_inside(tf_dir, plan_file)
    require_approval(
        approval_token,
        action="terraform.apply",
        artifact_sha256=sha256_file(Path(plan_path)),
    )
    audit = server_audit("stackit")
    if audit:
        audit.record(
            agent="mcp:stackit",
            action="terraform_apply",
            inputs={"plan_file": str(plan_path)},
            phase="before",
            status="started",
        )
    result = {"applied": True, "simulated": is_mock()}
    if audit:
        audit.record(
            agent="mcp:stackit",
            action="terraform_apply",
            inputs={"plan_file": str(plan_path)},
            phase="after",
            result=result,
        )
    return result


def main() -> None:
    mcp.run("stdio")
