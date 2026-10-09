"""Shared helpers for the local MCP tool servers.

Each server is a FastMCP stdio process started by the orchestrator. It only
receives the environment variables it needs. Mock mode (`MIGRATE_MOCK=1`)
serves recorded fixtures instead of touching any cloud.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from migrate.approval import SECRET_ENV, ApprovalError, ApprovalTokens
from migrate.audit import AuditLog, Redactor

MOCK_ENV = "MIGRATE_MOCK"
FIXTURES_ENV = "MIGRATE_FIXTURES_DIR"
RUN_DIR_ENV = "MIGRATE_RUN_DIR"
RUN_ID_ENV = "MIGRATE_RUN_ID"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def make_server(name: str, instructions: str) -> FastMCP:
    # WARNING keeps request chatter off the demo console (stderr is shared).
    return FastMCP(name=name, instructions=instructions, log_level="WARNING")


def is_mock() -> bool:
    return os.environ.get(MOCK_ENV) == "1"


def fixtures_dir() -> Path:
    return Path(os.environ.get(FIXTURES_ENV, "fixtures")).resolve()


def load_fixture(*parts: str) -> Any:
    path = fixtures_dir().joinpath(*parts)
    with path.open(encoding="utf-8") as fh:
        if path.suffix == ".json":
            return json.load(fh)
        return fh.read()


def run_dir() -> Path:
    value = os.environ.get(RUN_DIR_ENV)
    if not value:
        raise RuntimeError(f"{RUN_DIR_ENV} is not set")
    return Path(value).resolve()


def run_id() -> str:
    return os.environ.get(RUN_ID_ENV, "")


def server_audit(server: str) -> AuditLog | None:
    """Audit log of the current run, written from inside the server process."""
    if not os.environ.get(RUN_DIR_ENV):
        return None
    secrets = [v for k, v in os.environ.items() if _looks_secret(k) and v]
    return AuditLog(run_dir() / "audit.jsonl", Redactor(secrets))


def _looks_secret(env_name: str) -> bool:
    return bool(
        re.search(r"(SECRET|PASSWORD|TOKEN|KEY|DSN)", env_name)
    ) and not env_name.endswith("_PATH")


def require_approval(token: str | None, *, action: str, artifact_sha256: str) -> None:
    """Refuse a mutating action unless the orchestrator issued a valid token."""
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise ApprovalError(
            f"refusing {action}: server has no approval secret configured"
        )
    ApprovalTokens(secret).verify(
        token, run_id=run_id(), action=action, artifact_sha256=artifact_sha256
    )


def safe_name(value: str, what: str = "name") -> str:
    """Validate identifiers that end up as CLI arguments."""
    if not _SAFE_NAME.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def path_inside(base: Path, candidate: str | Path) -> Path:
    """Resolve `candidate` and make sure it stays inside `base`."""
    resolved = (
        (base / candidate).resolve()
        if not Path(candidate).is_absolute()
        else Path(candidate).resolve()
    )
    base = base.resolve()
    if resolved != base and base not in resolved.parents:
        raise PermissionError(f"path {candidate!s} escapes {base}")
    return resolved
