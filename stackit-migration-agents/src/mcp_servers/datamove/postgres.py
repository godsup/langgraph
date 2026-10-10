"""PostgreSQL transfer and verification.

Connection strings are referenced by name (e.g. `shop_db`) and resolved from
the server's environment: `PG_DSN_SOURCE_SHOP_DB` / `PG_DSN_TARGET_SHOP_DB`,
falling back to `SOURCE_PG_DSN` / `TARGET_PG_DSN`. They are turned into libpq
environment variables (PGHOST, PGPASSWORD, ...) so that passwords never show
up in process arguments, tool arguments, prompts or logs.

v1 strategy: `pg_dump --format=custom` + `pg_restore --clean --if-exists` (the
clean-up only ever runs against the target). `LogicalReplication`
is the extension point for near-zero-downtime migrations.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from migrate.proc import run

ROW_COUNTS_SQL = """
select table_schema || '.' || table_name,
       (xpath('/row/cnt/text()',
              query_to_xml(format('select count(*) as cnt from %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text::bigint
from information_schema.tables
where table_type = 'BASE TABLE'
  and table_schema not in ('pg_catalog', 'information_schema')
order by 1
"""


def dsn_for(side: str, ref: str) -> str:
    if side not in ("source", "target"):
        raise ValueError("side must be 'source' or 'target'")
    key = re.sub(r"[^A-Z0-9]", "_", ref.upper())
    dsn = os.environ.get(f"PG_DSN_{side.upper()}_{key}") or os.environ.get(
        f"{side.upper()}_PG_DSN"
    )
    if not dsn:
        raise RuntimeError(
            f"no {side} connection configured for {ref!r} (PG_DSN_{side.upper()}_{key})"
        )
    return dsn


def libpq_env(dsn: str) -> dict[str, str]:
    """postgresql://user:pass@host:port/db?sslmode=require -> PG* variables.

    Parse errors never echo the connection string: parts of it (even of the
    password) would otherwise end up in the audit log, reports or prompts."""
    try:
        url = urlparse(dsn)
        values = {
            "PGHOST": url.hostname,
            "PGPORT": str(url.port) if url.port else None,
            "PGUSER": unquote(url.username) if url.username else None,
            "PGPASSWORD": unquote(url.password) if url.password else None,
            "PGDATABASE": url.path.lstrip("/") or None,
        }
    except ValueError:
        raise ValueError(
            "invalid PostgreSQL connection string (percent-encode special characters "
            "in the password)"
        ) from None
    if url.scheme not in ("postgres", "postgresql"):
        raise ValueError("expected a postgresql:// connection string")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    query = parse_qs(url.query)
    if "sslmode" in query:
        values["PGSSLMODE"] = query["sslmode"][0]
    env.update({k: v for k, v in values.items() if v})
    return env


class PostgresStrategy(Protocol):
    def transfer(self, ref: str, workdir: Path) -> dict[str, Any]: ...


class DumpRestore:
    """v1: full dump of the source, restore into the (empty) target."""

    def dump(self, ref: str, workdir: Path) -> Path:
        workdir.mkdir(parents=True, exist_ok=True)
        dump_file = workdir / f"{ref}.dump"
        run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(dump_file),
            ],
            env=libpq_env(dsn_for("source", ref)),
            timeout=24 * 3600,
        )
        return dump_file

    def restore(self, ref: str, dump_file: Path) -> dict[str, Any]:
        # --clean --if-exists drops and recreates the dumped objects on the
        # TARGET only, so re-running the data stage (after a failed check, a
        # rejected cutover or a crash) does not fail on existing tables.
        result = run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--dbname",
                libpq_env(dsn_for("target", ref))["PGDATABASE"],
                str(dump_file),
            ],
            env=libpq_env(dsn_for("target", ref)),
            timeout=24 * 3600,
        )
        return {"stderr": result.stderr[-2000:]}

    def transfer(self, ref: str, workdir: Path) -> dict[str, Any]:
        dump_file = self.dump(ref, workdir)
        restore = self.restore(ref, dump_file)
        return {
            "strategy": "pg_dump/pg_restore",
            "dump_bytes": dump_file.stat().st_size,
            **restore,
        }


class LogicalReplication:
    """Extension point (not implemented in v1).

    Plan: CREATE PUBLICATION on the source (needs rds.logical_replication=1),
    restore the schema on the target (pg_dump --schema-only), CREATE
    SUBSCRIPTION on the target, wait until all tables are in state 'r'
    (pg_subscription_rel), then at cutover stop writes, wait for zero lag,
    sync sequences and drop the subscription. Source data is never deleted.
    """

    def transfer(self, ref: str, workdir: Path) -> dict[str, Any]:
        raise NotImplementedError("logical replication is planned for a later version")


STRATEGIES: dict[str, type] = {
    "dump_restore": DumpRestore,
    "logical_replication": LogicalReplication,
}


def row_counts(side: str, ref: str) -> dict[str, int]:
    env = libpq_env(dsn_for(side, ref))
    out = run(
        ["psql", "--no-psqlrc", "-At", "-F", "\t", "-c", ROW_COUNTS_SQL],
        env=env,
        timeout=3600,
        max_output=None,  # data: never cut off
    ).stdout
    counts = {}
    for line in out.splitlines():
        if "\t" in line:
            table, count = line.split("\t", 1)
            counts[table] = int(count)
    return counts


def schema(side: str, ref: str) -> str:
    """Normalised schema-only dump (comments, SET lines and blank lines removed)."""
    env = libpq_env(dsn_for(side, ref))
    out = run(
        ["pg_dump", "--schema-only", "--no-owner", "--no-privileges", "--no-comments"],
        env=env,
        timeout=3600,
        max_output=None,  # a cut-off schema would hide differences
    ).stdout
    lines = [
        line
        for line in out.splitlines()
        if line.strip()
        and not line.startswith(
            ("--", "SET ", "SELECT pg_catalog.set_config", "\\restrict", "\\unrestrict")
        )
    ]
    return "\n".join(lines) + "\n"


def schema_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
