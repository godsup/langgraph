"""Real rclone and PostgreSQL against local backends (no cloud needed).

Skipped automatically when the tools are not installed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

PG_BIN = Path("/usr/lib/postgresql/16/bin")


@pytest.fixture
def datamove_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    run_dir = tmp_path / "20261009-120000-abcd"
    run_dir.mkdir()
    monkeypatch.setenv("MIGRATE_MOCK", "0")
    monkeypatch.setenv("MIGRATE_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MIGRATE_RUN_ID", run_dir.name)
    return run_dir


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")
def test_rclone_copy_and_verify_local(
    tmp_path: Path, datamove_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_servers.datamove import server

    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "assets" / "img").mkdir(parents=True)
    (dst / "assets").mkdir(parents=True)
    for i in range(40):
        (src / "assets" / "img" / f"{i:03d}.bin").write_bytes(os.urandom(512 + i))
    monkeypatch.setenv("MIGRATE_RCLONE_SRC_ROOT", str(src))
    monkeypatch.setenv("MIGRATE_RCLONE_DST_ROOT", str(dst))

    dry = server.copy_bucket(
        source_bucket="assets", target_bucket="assets", dry_run=True
    )
    assert (
        dry["dry_run"]
        and server.bucket_stats(side="target", bucket="assets")["objects"] == 0
    )

    server.copy_bucket(source_bucket="assets", target_bucket="assets", dry_run=False)
    assert server.bucket_stats(side="source", bucket="assets") == server.bucket_stats(
        side="target", bucket="assets"
    )
    sums = server.sample_checksums(
        source_bucket="assets", target_bucket="assets", sample_size=10
    )
    assert sums["sampled"] == 10 and sums["matched"] == 10

    victim = sorted((dst / "assets" / "img").iterdir())[0]
    victim.write_bytes(b"corrupted")
    sums = server.sample_checksums(
        source_bucket="assets", target_bucket="assets", sample_size=40
    )
    assert sums["matched"] == 39 and sums["mismatches"] == [f"img/{victim.name}"]
    assert len(list((src / "assets" / "img").iterdir())) == 40  # source untouched


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _as_postgres(*args: str) -> None:
    subprocess.run(
        ["runuser", "-u", "postgres", "--", *args], check=True, capture_output=True
    )


@pytest.fixture
def two_clusters() -> Iterator[tuple[int, int]]:
    if (
        not (PG_BIN / "initdb").exists()
        or shutil.which("runuser") is None
        or os.geteuid() != 0
    ):
        pytest.skip(
            "needs PostgreSQL 16 server binaries and root (to run as the postgres user)"
        )
    # pytest's tmp dirs are root-only; the postgres user needs its own base dir.
    base = Path(tempfile.mkdtemp(prefix="migrate-pg-"))
    shutil.chown(base, "postgres")
    ports = []
    try:
        for name in ("source", "target"):
            data = base / name
            _as_postgres(
                str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"
            )
            port = _free_port()
            _as_postgres(
                str(PG_BIN / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(base / f"{name}.log"),
                "-w",
                "-o",
                f"-p {port} -k {base} -c listen_addresses=127.0.0.1",
                "start",
            )
            ports.append(port)
        yield ports[0], ports[1]
    finally:
        for name in ("source", "target"):
            subprocess.run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    str(PG_BIN / "pg_ctl"),
                    "-D",
                    str(base / name),
                    "-m",
                    "fast",
                    "stop",
                ],
                capture_output=True,
            )
        shutil.rmtree(base, ignore_errors=True)


def _psql(port: int, db: str, sql: str) -> str:
    return subprocess.run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "postgres",
            "-d",
            db,
            "-At",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_pg_dump_restore_and_verify(
    two_clusters: tuple[int, int], datamove_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_servers.datamove import server

    src_port, dst_port = two_clusters
    _psql(src_port, "postgres", "create database shop")
    _psql(dst_port, "postgres", "create database shop")
    _psql(
        src_port,
        "shop",
        """
        create table customers (id bigint primary key, email text not null);
        create table orders (id bigint primary key, customer_id bigint references customers(id), total int);
        insert into customers select g, 'c' || g || '@example.com' from generate_series(1, 500) g;
        insert into orders select g, 1 + g % 500, g * 3 from generate_series(1, 2500) g;
    """,
    )
    monkeypatch.setenv(
        "PG_DSN_SOURCE_SHOP_DB", f"postgresql://postgres@127.0.0.1:{src_port}/shop"
    )
    monkeypatch.setenv(
        "PG_DSN_TARGET_SHOP_DB", f"postgresql://postgres@127.0.0.1:{dst_port}/shop"
    )

    dry = server.pg_transfer(ref="shop_db", dry_run=True)
    assert (
        dry["tables"] == 2
        and server.pg_row_counts(side="target", ref="shop_db")["tables"] == {}
    )

    result = server.pg_transfer(ref="shop_db", dry_run=False)
    assert result["strategy"] == "pg_dump/pg_restore" and result["dump_bytes"] > 0
    src_counts = server.pg_row_counts(side="source", ref="shop_db")["tables"]
    assert src_counts == {"public.customers": 500, "public.orders": 2500}
    assert server.pg_row_counts(side="target", ref="shop_db")["tables"] == src_counts
    assert (
        server.pg_schema(side="source", ref="shop_db")["sha256"]
        == server.pg_schema(side="target", ref="shop_db")["sha256"]
    )

    _psql(dst_port, "shop", "alter table orders add column note text")
    assert (
        server.pg_schema(side="source", ref="shop_db")["sha256"]
        != server.pg_schema(side="target", ref="shop_db")["sha256"]
    )
    assert (
        _psql(src_port, "shop", "select count(*) from orders").strip() == "2500"
    )  # source untouched
