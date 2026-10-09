"""Append-only audit log with secret redaction.

Every tool call, LLM decision and approval is written to
`runs/<run-id>/audit.jsonl` as one JSON object per line:
`{ts, agent, action, phase, status, inputs, result}`.
Mutating actions are recorded twice: once before (`phase="before"`) and once
after (`phase="after"`) execution.
"""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr

REDACTED = "***REDACTED***"
MAX_FIELD_CHARS = 4000

# Keys whose values are always secret, whatever they contain.
_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|authorization|dsn|session[_-]?key|webhook)",
    re.IGNORECASE,
)
# Values that look like secrets wherever they appear.
_SECRET_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),  # AWS access key ids
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(postgres(ql)?|mysql)://[^:\s/]+:[^@\s]+@"),  # DSN passwords
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    re.compile(r"(?i)\b(password|passwd|secret|token)=[^\s&;]+"),
    re.compile(r"https://hooks\.slack\.com/services/\S+"),
    re.compile(r"https://[a-z0-9.-]+\.webhook\.office\.com/\S+"),
]


class Redactor:
    """Removes secrets from anything before it is logged or sent to an LLM."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        # Longest first, so overlapping secrets are fully masked.
        self._values = sorted(
            {v for v in secret_values if len(v) >= 4}, key=len, reverse=True
        )

    def add(self, *values: str) -> None:
        self._values = sorted(
            {*self._values, *(v for v in values if len(v) >= 4)}, key=len, reverse=True
        )

    def text(self, value: str) -> str:
        for secret in self._values:
            value = value.replace(secret, REDACTED)
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def obj(self, value: Any, *, _key: str | None = None) -> Any:
        if (
            _key is not None
            and _SECRET_KEY.search(_key)
            and value not in (None, "", False)
        ):
            return REDACTED
        if isinstance(value, SecretStr):
            return REDACTED
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(k): self.obj(v, _key=str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple | set):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, int | float | bool) or value is None:
            return value
        return self.text(str(value))


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return (
            value[:MAX_FIELD_CHARS]
            + f"... [truncated {len(value) - MAX_FIELD_CHARS} chars]"
        )
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value


class AuditLog:
    """Thread- and process-safe JSONL writer (uses an advisory file lock)."""

    def __init__(self, path: Path, redactor: Redactor | None = None) -> None:
        self.path = path
        self.redactor = redactor or Redactor()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        agent: str,
        action: str,
        inputs: Any = None,
        result: Any = None,
        status: str = "ok",
        phase: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "agent": agent,
            "action": action,
            "status": status,
        }
        if phase:
            entry["phase"] = phase
        entry["inputs"] = _truncate(self.redactor.obj(inputs))
        entry["result"] = _truncate(self.redactor.obj(result))
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line + "\n")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return entry

    @contextmanager
    def mutation(
        self, *, agent: str, action: str, inputs: Any
    ) -> Iterator[dict[str, Any]]:
        """Record a mutating action before and after it runs.

        Usage::

            with audit.mutation(agent="deployment", action="terraform.apply", inputs=...) as out:
                out["result"] = run_apply()
        """
        self.record(
            agent=agent, action=action, inputs=inputs, phase="before", status="started"
        )
        outcome: dict[str, Any] = {}
        try:
            yield outcome
        except BaseException as exc:
            self.record(
                agent=agent,
                action=action,
                inputs=inputs,
                phase="after",
                status="error",
                result={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self.record(
            agent=agent,
            action=action,
            inputs=inputs,
            phase="after",
            status=outcome.get("status", "ok"),
            result=outcome.get("result"),
        )

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
