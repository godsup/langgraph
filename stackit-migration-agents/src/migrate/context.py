"""Per-invocation runtime context handed to every graph node.

Passed via LangGraph's `context=` (not checkpointed), so a resumed run gets a
fresh context with fresh credentials and a fresh approval secret.
"""

from __future__ import annotations

from dataclasses import dataclass

from migrate.approval import ApprovalTokens, new_secret
from migrate.audit import AuditLog, Redactor
from migrate.config import Settings, YamlConfig
from migrate.console import NullReporter, Reporter
from migrate.llm import LLMGateway
from migrate.mcp_client import McpHub
from migrate.runs import RunPaths


@dataclass
class AppContext:
    settings: Settings
    config: YamlConfig
    paths: RunPaths
    audit: AuditLog
    llm: LLMGateway
    mcp: McpHub
    approvals: ApprovalTokens
    reporter: Reporter
    mock: bool

    @property
    def run_id(self) -> str:
        return self.paths.run_id


def build_context(
    settings: Settings,
    paths: RunPaths,
    *,
    mock: bool,
    live_llm: bool = False,
    reporter: Reporter | None = None,
) -> AppContext:
    redactor = Redactor(settings.secret_values())
    approval_secret = (
        settings.approval_secret.get_secret_value()
        if settings.approval_secret
        else new_secret()
    )
    redactor.add(approval_secret)
    audit = AuditLog(paths.audit_file, redactor)
    recorded_dir = settings.fixtures_dir / "llm" if mock and not live_llm else None
    return AppContext(
        settings=settings,
        config=YamlConfig(settings.config_dir),
        paths=paths,
        audit=audit,
        llm=LLMGateway(settings, audit, recorded_dir=recorded_dir, redactor=redactor),
        mcp=McpHub(
            settings,
            audit,
            mock=mock,
            run_id=paths.run_id,
            run_dir=paths.root,
            approval_secret=approval_secret,
        ),
        approvals=ApprovalTokens(approval_secret),
        reporter=reporter or NullReporter(),
        mock=mock,
    )
