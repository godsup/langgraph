"""Connects agents to the local MCP tool servers via langchain-mcp-adapters.

Each server runs as a stdio subprocess with a minimal, server-specific
environment (least privilege: the AWS server never sees STACKIT credentials and
vice versa). Every tool call goes through `ToolBox.call`, which writes the
audit log: once for read-only calls, before *and* after for mutating calls.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from migrate.approval import SECRET_ENV
from migrate.audit import AuditLog
from migrate.config import Settings

SERVER_MODULES: dict[str, str] = {
    "aws": "mcp_servers.aws_readonly",
    "azure": "mcp_servers.azure_readonly",
    "stackit": "mcp_servers.stackit",
    "datamove": "mcp_servers.datamove",
    "observability": "mcp_servers.observability",
}

# Environment every server needs to run at all (no credentials here).
_BASE_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PYTHONPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)
# Credentials passed through from the process environment, per server.
_PASSTHROUGH_PREFIXES: dict[str, tuple[str, ...]] = {
    "aws": ("AWS_",),
    "azure": ("AZURE_",),
    "stackit": ("STACKIT_", "TF_", "GH_", "GITHUB_"),
    "datamove": ("AWS_", "RCLONE_"),
    "observability": (),
}
# Servers that perform (approval-gated) mutations.
MUTATING_SERVERS = frozenset({"stackit", "datamove"})


class ToolCallError(RuntimeError):
    pass


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    return (
        value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    )


def server_env(
    server: str,
    settings: Settings,
    *,
    mock: bool,
    run_id: str,
    run_dir: Path,
    approval_secret: str,
) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _BASE_ENV}
    for key, value in os.environ.items():
        if key.startswith(_PASSTHROUGH_PREFIXES[server]):
            env[key] = value
    env.update(
        {
            "MIGRATE_MOCK": "1" if mock else "0",
            "MIGRATE_FIXTURES_DIR": str(settings.fixtures_dir),
            "MIGRATE_RUN_DIR": str(run_dir),
            "MIGRATE_RUN_ID": run_id,
            "MIGRATE_CONFIG_DIR": str(settings.config_dir),
        }
    )
    extra: dict[str, str | None] = {}
    if server == "aws":
        extra = {
            "AWS_REGION": settings.aws_region,
            "AWS_DEFAULT_REGION": settings.aws_region,
            "AWS_PROFILE": settings.aws_profile,
        }
    elif server == "stackit":
        extra = {
            "STACKIT_PROJECT_ID": settings.stackit_project_id,
            "STACKIT_REGION": settings.stackit_region,
            "STACKIT_SERVICE_ACCOUNT_KEY_PATH": _str(
                settings.stackit_service_account_key_path
            ),
            "STACKIT_PRIVATE_KEY_PATH": _str(settings.stackit_private_key_path),
            "PR_REPO": settings.pr_repo,
            "PR_BASE_BRANCH": settings.pr_base_branch,
            "PR_REPO_PATH": _str(settings.pr_repo_path),
        }
    elif server == "datamove":
        extra = {
            "AWS_REGION": settings.aws_region,
            "SOURCE_PG_DSN": _secret(settings.source_pg_dsn),
            "TARGET_PG_DSN": _secret(settings.target_pg_dsn),
            "STACKIT_S3_ENDPOINT": settings.stackit_s3_endpoint,
            "STACKIT_S3_ACCESS_KEY_ID": _secret(settings.stackit_s3_access_key_id),
            "STACKIT_S3_SECRET_ACCESS_KEY": _secret(
                settings.stackit_s3_secret_access_key
            ),
            "VERIFY_SAMPLE_SIZE": str(settings.verify_sample_size),
        }
    elif server == "observability":
        extra = {
            "OBSERVABILITY_URL": settings.observability_url,
            "OBSERVABILITY_USERNAME": _secret(settings.observability_username),
            "OBSERVABILITY_PASSWORD": _secret(settings.observability_password),
        }
    env.update({k: v for k, v in extra.items() if v})
    if server in MUTATING_SERVERS:
        env[SECRET_ENV] = approval_secret
    return env


def _str(value: Path | None) -> str | None:
    return str(value) if value else None


class ToolBox:
    """The tools of one MCP server, bound to one agent, with audit logging."""

    def __init__(
        self, server: str, agent: str, tools: list[BaseTool], audit: AuditLog
    ) -> None:
        self.server = server
        self.agent = agent
        self.audit = audit
        self._tools = {tool.name: tool for tool in tools}

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self, *names: str) -> list[BaseTool]:
        """LangChain tools for LLM binding. Only ever pass read-only tools."""
        return [self._tools[name] for name in names]

    async def call(self, name: str, *, mutating: bool = False, **args: Any) -> Any:
        if name not in self._tools:
            raise ToolCallError(
                f"{self.server} has no tool {name!r}; available: {self.names}"
            )
        action = f"{self.server}.{name}"
        if mutating:
            with self.audit.mutation(
                agent=self.agent, action=action, inputs=args
            ) as out:
                result = await self._invoke(name, args)
                out["result"] = result
            return result
        try:
            result = await self._invoke(name, args)
        except Exception as exc:
            self.audit.record(
                agent=self.agent,
                action=action,
                inputs=args,
                status="error",
                result={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self.audit.record(agent=self.agent, action=action, inputs=args, result=result)
        return result

    async def _invoke(self, name: str, args: dict[str, Any]) -> Any:
        call = {
            "name": name,
            "args": args,
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "tool_call",
        }
        try:
            message = await self._tools[name].ainvoke(call)
        except Exception as exc:
            raise ToolCallError(f"{self.server}.{name} failed: {exc}") from exc
        return _result_of(message)


def _result_of(message: Any) -> Any:
    if isinstance(message, ToolMessage):
        artifact = message.artifact or {}
        if (
            isinstance(artifact, dict)
            and artifact.get("structured_content") is not None
        ):
            content = artifact["structured_content"]
            # FastMCP wraps non-object return values as {"result": ...}.
            if isinstance(content, dict) and set(content) == {"result"}:
                return content["result"]
            return content
        message = message.content
    if isinstance(message, list):
        message = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in message
        )
    if isinstance(message, str):
        try:
            return json.loads(message)
        except json.JSONDecodeError:
            return message
    return message


class McpHub:
    """Starts MCP servers on demand and hands out audited tool boxes."""

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        *,
        mock: bool,
        run_id: str,
        run_dir: Path,
        approval_secret: str,
    ) -> None:
        self.audit = audit
        connections: dict[str, Any] = {}
        for server, module in SERVER_MODULES.items():
            connections[server] = {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", module],
                "env": server_env(
                    server,
                    settings,
                    mock=mock,
                    run_id=run_id,
                    run_dir=run_dir,
                    approval_secret=approval_secret,
                ),
            }
        self._client = MultiServerMCPClient(connections)

    @asynccontextmanager
    async def toolbox(self, server: str, *, agent: str) -> AsyncIterator[ToolBox]:
        """One server session for the duration of an agent step."""
        async with self._client.session(server) as session:
            tools = await load_mcp_tools(session, handle_tool_errors=False)
            yield ToolBox(server, agent, tools, self.audit)
