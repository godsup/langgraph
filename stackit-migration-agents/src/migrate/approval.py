"""Approval tokens.

The orchestrator issues a token only after a human approved a gate. Mutating
MCP tools (`terraform_apply`, `open_pull_request`, `cutover`) refuse to run
without a valid token. A token is bound to:

- the run id,
- the action (e.g. `terraform.apply`),
- the SHA-256 of the exact artefact that was approved (plan file or
  verification report), so a changed plan invalidates the approval,
- an expiry time.

Tokens are HMAC-SHA256 signed with a secret that is shared only with the MCP
server processes (via their environment, never via prompts or tool args).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

TOKEN_VERSION = "v1"  # noqa: S105 - a version tag, not a secret
SECRET_ENV = "MIGRATE_APPROVAL_SECRET"  # noqa: S105 - env var name


class ApprovalError(PermissionError):
    """Raised when an approval token is missing, invalid, expired or mismatched."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class ApprovalTokens:
    def __init__(self, secret: str) -> None:
        if len(secret) < 16:
            raise ValueError("approval secret must be at least 16 characters")
        self._key = secret.encode()

    def issue(
        self, *, run_id: str, action: str, artifact_sha256: str, ttl_s: int = 900
    ) -> str:
        claims = {
            "run": run_id,
            "act": action,
            "sha": artifact_sha256,
            "exp": int(time.time()) + ttl_s,
            "nonce": secrets.token_hex(8),
        }
        body = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        sig = _b64(
            hmac.new(self._key, f"{TOKEN_VERSION}.{body}".encode(), "sha256").digest()
        )
        return f"{TOKEN_VERSION}.{body}.{sig}"

    def verify(
        self, token: str | None, *, run_id: str, action: str, artifact_sha256: str
    ) -> None:
        if not token:
            raise ApprovalError(f"refusing {action}: no approval token supplied")
        try:
            version, body, sig = token.split(".")
        except ValueError as exc:
            raise ApprovalError("malformed approval token") from exc
        if version != TOKEN_VERSION:
            raise ApprovalError("unsupported approval token version")
        expected = _b64(
            hmac.new(self._key, f"{version}.{body}".encode(), "sha256").digest()
        )
        if not hmac.compare_digest(expected, sig):
            raise ApprovalError("approval token signature is invalid")
        claims = json.loads(_unb64(body))
        if claims.get("exp", 0) < time.time():
            raise ApprovalError("approval token has expired; approve again")
        if claims.get("run") != run_id:
            raise ApprovalError("approval token belongs to a different run")
        if claims.get("act") != action:
            raise ApprovalError(
                f"approval token is for {claims.get('act')!r}, not {action!r}"
            )
        if not hmac.compare_digest(str(claims.get("sha")), artifact_sha256):
            raise ApprovalError(
                "the approved artefact changed after approval (hash mismatch); re-plan and approve again"
            )
