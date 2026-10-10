"""Object storage transfers with rclone (S3 on both sides).

Remotes are defined through environment variables only (no config file, no
secrets on the command line):

- `src`: AWS S3, credentials from the standard AWS_* variables / profile.
- `dst`: STACKIT Object Storage (S3-compatible), endpoint and keys from
  STACKIT_S3_* variables.

Only `rclone copy` is used for transfers. `sync`, `move`, `delete`, `purge`
and friends can remove data and are never called (a test enforces this).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from migrate.proc import run

ALLOWED_SUBCOMMANDS = frozenset({"copy", "size", "lsjson", "md5sum", "version"})


def remote_env() -> dict[str, str]:
    env = dict(os.environ)
    src_root, dst_root = (
        env.get("MIGRATE_RCLONE_SRC_ROOT"),
        env.get("MIGRATE_RCLONE_DST_ROOT"),
    )
    if src_root and dst_root:
        # Local directories on both sides (integration tests, offline demos).
        env.update(
            RCLONE_CONFIG_SRC_TYPE="alias",
            RCLONE_CONFIG_SRC_REMOTE=src_root,
            RCLONE_CONFIG_DST_TYPE="alias",
            RCLONE_CONFIG_DST_REMOTE=dst_root,
        )
        return env
    env.update(
        {
            "RCLONE_CONFIG_SRC_TYPE": "s3",
            "RCLONE_CONFIG_SRC_PROVIDER": "AWS",
            "RCLONE_CONFIG_SRC_ENV_AUTH": "true",
            "RCLONE_CONFIG_SRC_REGION": env.get("AWS_REGION", "eu-central-1"),
            "RCLONE_CONFIG_DST_TYPE": "s3",
            "RCLONE_CONFIG_DST_PROVIDER": "Other",
            "RCLONE_CONFIG_DST_ENDPOINT": env.get(
                "STACKIT_S3_ENDPOINT", "https://object.storage.eu01.onstackit.cloud"
            ),
            "RCLONE_CONFIG_DST_REGION": "eu01",
            "RCLONE_CONFIG_DST_ACCESS_KEY_ID": env.get("STACKIT_S3_ACCESS_KEY_ID", ""),
            "RCLONE_CONFIG_DST_SECRET_ACCESS_KEY": env.get(
                "STACKIT_S3_SECRET_ACCESS_KEY", ""
            ),
        }
    )
    return env


def rclone(subcommand: str, *args: str, timeout: float = 24 * 3600) -> str:
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise PermissionError(
            f"rclone {subcommand} is not allowed (data must never be deleted)"
        )
    return run(["rclone", subcommand, *args], env=remote_env(), timeout=timeout).stdout


def size(remote: str, bucket: str) -> dict[str, Any]:
    data = json.loads(rclone("size", f"{remote}:{bucket}", "--json"))
    return {"objects": int(data.get("count", 0)), "bytes": int(data.get("bytes", 0))}


def listing(remote: str, bucket: str) -> list[dict[str, Any]]:
    out = rclone(
        "lsjson", f"{remote}:{bucket}", "--recursive", "--files-only", "--no-mimetype"
    )
    return json.loads(out or "[]")


def copy(source_bucket: str, target_bucket: str, *, dry_run: bool) -> dict[str, Any]:
    args = [
        f"src:{source_bucket}",
        f"dst:{target_bucket}",
        "--transfers",
        "16",
        "--checkers",
        "32",
        "--stats",
        "0",
        "--log-level",
        "NOTICE",
    ]
    if dry_run:
        args.append("--dry-run")
    rclone("copy", *args)
    return {"dry_run": dry_run}


def sample_keys(keys: list[str], sample_size: int) -> list[str]:
    """Deterministic, evenly spread sample (stable across runs for audits)."""
    ranked = sorted(keys, key=lambda k: hashlib.sha256(k.encode()).hexdigest())
    return sorted(ranked[:sample_size])


def md5_of(remote: str, bucket: str, keys: list[str]) -> dict[str, str]:
    """MD5 per key, downloading the objects (works for multipart uploads too)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("\n".join(keys) + "\n")
        files_from = fh.name
    try:
        out = rclone(
            "md5sum", f"{remote}:{bucket}", "--download", "--files-from", files_from
        )
    finally:
        Path(files_from).unlink(missing_ok=True)
    sums: dict[str, str] = {}
    for line in out.splitlines():
        if "  " in line:
            digest, key = line.split("  ", 1)
            sums[key] = digest
    return sums
