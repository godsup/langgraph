"""STACKIT MCP server (milestone 1 stub)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp_servers.common import (
    is_mock,
    load_fixture,
    make_server,
    path_inside,
    require_approval,
    run_dir,
    server_audit,
)
from mcp_servers.stackit.catalog import live_catalog
from migrate.approval import sha256_file
from migrate.proc import run

mcp = make_server(
    "stackit", "Terraform and STACKIT CLI. Mutations need an approval token."
)
_cli_env: dict[str, str] = {}


def stackit_cli_env() -> dict[str, str]:
    """Environment for the stackit CLI with a short-lived access token.

    Per the CLI docs, `auth activate-service-account --only-print-access-token`
    prints a token without storing credentials, and STACKIT_ACCESS_TOKEN is
    then used by subsequent commands.
    """
    if not _cli_env:
        env = dict(os.environ)
        key_path = env.get("STACKIT_SERVICE_ACCOUNT_KEY_PATH")
        if key_path and not env.get("STACKIT_ACCESS_TOKEN"):
            argv = [
                "stackit",
                "auth",
                "activate-service-account",
                "--service-account-key-path",
                key_path,
                "--only-print-access-token",
            ]
            if env.get("STACKIT_PRIVATE_KEY_PATH"):
                argv += ["--private-key-path", env["STACKIT_PRIVATE_KEY_PATH"]]
            env["STACKIT_ACCESS_TOKEN"] = run(argv, env=env, timeout=60).stdout.strip()
        _cli_env.update(env)
    return dict(_cli_env)


@mcp.tool()
def get_catalog() -> dict[str, Any]:
    """Read-only: STACKIT offerings (machine types, PostgreSQL Flex flavors and
    versions, MariaDB plans, SKE Kubernetes versions) for sizing decisions."""
    if is_mock():
        return {"source": "fixture", **load_fixture("stackit", "catalog.json")}
    return live_catalog(stackit_cli_env())


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
