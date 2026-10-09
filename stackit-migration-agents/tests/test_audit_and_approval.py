from __future__ import annotations

import time
from pathlib import Path

import pytest

from migrate.approval import ApprovalError, ApprovalTokens, sha256_file
from migrate.audit import REDACTED, AuditLog, Redactor


def test_redacts_secret_keys_patterns_and_known_values(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl", Redactor(["hunter2-very-secret"]))
    audit.record(
        agent="x",
        action="y",
        inputs={
            "password": "p@ss",
            "nested": {"api_key": "abc", "note": "uses hunter2-very-secret inside"},
            "dsn": "postgresql://u:pw@host/db",
            "text": "key AKIAABCDEFGHIJKLMNOP and Bearer abc.def.ghi",
            "count": 3,
        },
    )
    entry = audit.entries()[0]
    flat = str(entry)
    assert entry["inputs"]["password"] == REDACTED
    assert entry["inputs"]["nested"]["api_key"] == REDACTED
    assert entry["inputs"]["dsn"] == REDACTED
    assert entry["inputs"]["count"] == 3
    for secret in (
        "p@ss",
        "hunter2-very-secret",
        "AKIAABCDEFGHIJKLMNOP",
        "abc.def.ghi",
    ):
        assert secret not in flat


def test_mutation_logs_before_and_after(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    with audit.mutation(agent="d", action="apply", inputs={"x": 1}) as out:
        out["result"] = {"ok": True}
    with (
        pytest.raises(RuntimeError),
        audit.mutation(agent="d", action="apply", inputs={}),
    ):
        raise RuntimeError("boom")
    phases = [(e["phase"], e["status"]) for e in audit.entries()]
    assert phases == [
        ("before", "started"),
        ("after", "ok"),
        ("before", "started"),
        ("after", "error"),
    ]


def test_approval_token_roundtrip_and_binding(tmp_path: Path) -> None:
    artefact = tmp_path / "tfplan"
    artefact.write_text("plan v1")
    sha = sha256_file(artefact)
    tokens = ApprovalTokens("x" * 32)
    token = tokens.issue(run_id="r1", action="terraform.apply", artifact_sha256=sha)
    tokens.verify(token, run_id="r1", action="terraform.apply", artifact_sha256=sha)

    with pytest.raises(ApprovalError, match="no approval token"):
        tokens.verify(None, run_id="r1", action="terraform.apply", artifact_sha256=sha)
    with pytest.raises(ApprovalError, match="different run"):
        tokens.verify(token, run_id="r2", action="terraform.apply", artifact_sha256=sha)
    with pytest.raises(ApprovalError, match="not 'data.cutover'"):
        tokens.verify(token, run_id="r1", action="data.cutover", artifact_sha256=sha)
    artefact.write_text("plan v2 (changed after approval)")
    with pytest.raises(ApprovalError, match="hash mismatch"):
        tokens.verify(
            token,
            run_id="r1",
            action="terraform.apply",
            artifact_sha256=sha256_file(artefact),
        )
    with pytest.raises(ApprovalError, match="signature"):
        ApprovalTokens("y" * 32).verify(
            token, run_id="r1", action="terraform.apply", artifact_sha256=sha
        )
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(ApprovalError):
        tokens.verify(
            tampered, run_id="r1", action="terraform.apply", artifact_sha256=sha
        )


def test_approval_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = ApprovalTokens("x" * 32)
    token = tokens.issue(run_id="r", action="a", artifact_sha256="s", ttl_s=10)
    monkeypatch.setattr(time, "time", lambda: 10**12)
    with pytest.raises(ApprovalError, match="expired"):
        tokens.verify(token, run_id="r", action="a", artifact_sha256="s")
