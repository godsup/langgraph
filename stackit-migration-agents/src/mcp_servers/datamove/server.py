"""Datamove MCP server: rclone (object storage) and PostgreSQL tools.

Rules:
- Source data is never modified or deleted. Object copies use `rclone copy`
  only; databases are read with `pg_dump`.
- Credentials never pass through tool arguments: buckets and databases are
  referenced by name, connection details come from this server's env.
- `cutover` refuses without an approval token for the exact verification
  report that a human approved, and is audit-logged before and after.

Mock mode serves recorded transfer and verification results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_servers.common import (
    is_mock,
    load_fixture,
    make_server,
    path_inside,
    require_approval,
    run_dir,
    safe_name,
    server_audit,
)
from mcp_servers.datamove import postgres, rclone
from migrate.approval import sha256_file
from migrate.state import utcnow

mcp = make_server("datamove", "rclone and PostgreSQL tools. Never deletes source data.")


def _mock(section: str, key: str) -> dict[str, Any]:
    data = load_fixture("datamove", "mock-results.json")
    try:
        return data[section][key]
    except KeyError as exc:
        raise KeyError(f"no mock data for {section}/{key}") from exc


def _audit(action: str, inputs: dict[str, Any], phase: str, **kw: Any) -> None:
    audit = server_audit("datamove")
    if audit:
        audit.record(
            agent="mcp:datamove", action=action, inputs=inputs, phase=phase, **kw
        )


# --------------------------------------------------------------------------
# Object storage
# --------------------------------------------------------------------------


@mcp.tool()
def bucket_stats(side: str, bucket: str) -> dict[str, Any]:
    """Read-only: object count and total bytes of a bucket ('source' or 'target')."""
    safe_name(bucket, "bucket")
    if side not in ("source", "target"):
        raise ValueError("side must be 'source' or 'target'")
    if is_mock():
        return _mock("buckets", bucket)[side]
    return rclone.size("src" if side == "source" else "dst", bucket)


@mcp.tool()
def copy_bucket(
    source_bucket: str, target_bucket: str, dry_run: bool = True
) -> dict[str, Any]:
    """Copy new/changed objects from the source to the target bucket.

    Uses `rclone copy` (never sync/move/delete). With dry_run nothing is written.
    """
    safe_name(source_bucket, "bucket")
    safe_name(target_bucket, "bucket")
    inputs = {
        "source_bucket": source_bucket,
        "target_bucket": target_bucket,
        "dry_run": dry_run,
    }
    _audit("copy_bucket", inputs, "before", status="started")
    try:
        if is_mock():
            mock = _mock("buckets", source_bucket)
            result = {"dry_run": dry_run, "simulated": True, **mock["copy"]}
        else:
            result = {
                **rclone.copy(source_bucket, target_bucket, dry_run=dry_run),
                "simulated": False,
            }
    except Exception as exc:
        _audit(
            "copy_bucket",
            inputs,
            "after",
            status="error",
            result={"error": str(exc)[:1000]},
        )
        raise
    _audit("copy_bucket", inputs, "after", result=result)
    return result


@mcp.tool()
def sample_checksums(
    source_bucket: str, target_bucket: str, sample_size: int = 20
) -> dict[str, Any]:
    """Read-only: compare MD5 of a deterministic sample of objects on both sides."""
    safe_name(source_bucket, "bucket")
    safe_name(target_bucket, "bucket")
    sample_size = max(1, min(int(sample_size), 1000))
    if is_mock():
        return _mock("buckets", source_bucket)["checksums"]
    keys = [o["Path"] for o in rclone.listing("src", source_bucket)]
    sample = rclone.sample_keys(keys, sample_size)
    if not sample:
        return {"sampled": 0, "matched": 0, "mismatches": [], "missing": []}
    source = rclone.md5_of("src", source_bucket, sample)
    target = rclone.md5_of("dst", target_bucket, sample)
    missing = [k for k in sample if k not in target]
    mismatches = [k for k in sample if k in target and source.get(k) != target[k]]
    return {
        "sampled": len(sample),
        "matched": len(sample) - len(missing) - len(mismatches),
        "mismatches": mismatches[:20],
        "missing": missing[:20],
    }


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------


@mcp.tool()
def pg_transfer(
    ref: str, strategy: str = "dump_restore", dry_run: bool = True
) -> dict[str, Any]:
    """Copy a database from source to target (v1: pg_dump/pg_restore).

    `ref` names a connection pair configured in the server env; no credentials
    are accepted as arguments. With dry_run only the source is checked.
    """
    safe_name(ref, "database reference")
    if strategy not in postgres.STRATEGIES:
        raise ValueError(
            f"unknown strategy {strategy!r}; choose from {sorted(postgres.STRATEGIES)}"
        )
    inputs = {"ref": ref, "strategy": strategy, "dry_run": dry_run}
    _audit("pg_transfer", inputs, "before", status="started")
    try:
        if is_mock():
            result = {
                "simulated": True,
                "dry_run": dry_run,
                **_mock("databases", ref)["transfer"],
            }
        elif dry_run:
            result = {
                "simulated": False,
                "dry_run": True,
                "tables": len(postgres.row_counts("source", ref)),
            }
        else:
            strategy_impl = postgres.STRATEGIES[strategy]()
            result = {
                "simulated": False,
                "dry_run": False,
                **strategy_impl.transfer(ref, run_dir() / "data"),
            }
    except Exception as exc:
        _audit(
            "pg_transfer",
            inputs,
            "after",
            status="error",
            result={"error": str(exc)[:1000]},
        )
        raise
    _audit("pg_transfer", inputs, "after", result=result)
    return result


@mcp.tool()
def pg_row_counts(side: str, ref: str) -> dict[str, Any]:
    """Read-only: exact row count per table."""
    safe_name(ref, "database reference")
    if is_mock():
        return {"tables": _mock("databases", ref)["row_counts"][side]}
    return {"tables": postgres.row_counts(side, ref)}


@mcp.tool()
def pg_schema(side: str, ref: str) -> dict[str, Any]:
    """Read-only: normalised schema dump (saved in the run dir) and its digest."""
    safe_name(ref, "database reference")
    if side not in ("source", "target"):
        raise ValueError("side must be 'source' or 'target'")
    if is_mock():
        text = _mock("databases", ref)["schema"][side]
    else:
        text = postgres.schema(side, ref)
    out = run_dir() / "data" / f"{ref}.{side}.schema.sql"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {
        "file": str(out),
        "sha256": postgres.schema_digest(text),
        "lines": text.count("\n"),
    }


# --------------------------------------------------------------------------
# Cutover (approval token required)
# --------------------------------------------------------------------------


@mcp.tool()
def cutover(
    report_path: str, approval_token: str, buckets: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Final step after the human approved the verification report.

    Runs a last delta `rclone copy` per bucket (catches objects written since
    the first copy; never deletes) and records the cutover. Switching DNS and
    application connection strings follows the generated runbook.
    """
    report = path_inside(run_dir(), report_path)
    require_approval(
        approval_token, action="data.cutover", artifact_sha256=sha256_file(Path(report))
    )
    pairs = [
        (safe_name(b["source"], "bucket"), safe_name(b["target"], "bucket"))
        for b in buckets or []
    ]
    inputs = {"report": str(report), "buckets": [f"{s}->{t}" for s, t in pairs]}
    _audit("cutover", inputs, "before", status="started")
    try:
        synced = []
        for source, target in pairs:
            if not is_mock():
                rclone.copy(source, target, dry_run=False)
            synced.append(f"{source}->{target}")
        marker = run_dir() / "data" / "cutover.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        result = {
            "cut_over": True,
            "simulated": is_mock(),
            "delta_synced": synced,
            "at": utcnow().isoformat(),
        }
        marker.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as exc:
        _audit(
            "cutover",
            inputs,
            "after",
            status="error",
            result={"error": str(exc)[:1000]},
        )
        raise
    _audit("cutover", inputs, "after", result=result)
    return result


def main() -> None:
    mcp.run("stdio")
