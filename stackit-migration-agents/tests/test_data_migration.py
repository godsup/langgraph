from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from migrate.config import PROJECT_ROOT, Settings
from migrate.runner import Runner
from migrate.state import CutoverStatus, RunOptions, SourceCloud, TransferStatus


def _deployed(runner: Runner) -> str:
    run_id = runner.create_run(source=SourceCloud.AWS, region=None, mock=True).run_id
    asyncio.run(runner.advance(run_id, "deploy", RunOptions()))
    asyncio.run(runner.decide(run_id, approved=True, approver="alice"))
    return run_id


def test_data_flow_verifies_and_waits_for_cutover_approval(runner: Runner) -> None:
    run_id = _deployed(runner)
    outcome = asyncio.run(runner.advance(run_id, "data", RunOptions()))
    dm = outcome.plan.data_migration
    assert outcome.pending_gate and outcome.pending_gate["gate"] == "cutover"
    statuses = {t.id: t.status for t in dm.tasks}
    assert statuses["s3:shop-assets-prod"] == TransferStatus.COPIED
    assert statuses["pg:shop_db"] == TransferStatus.COPIED
    assert statuses["s3:shop-logs-archive"] == TransferStatus.SKIPPED  # Glacier objects
    assert (
        statuses["mysql:legacy_crm_mysql"] == TransferStatus.SKIPPED
    )  # not automated in v1
    assert len(dm.checks) == 8 and all(c.passed for c in dm.checks)
    assert {c.name for c in dm.checks} == {
        "object_count",
        "total_size_bytes",
        "sample_checksums",
        "row_counts",
        "schema_diff",
    }
    assert Path(dm.report_path).exists() and Path(dm.runbook_path).exists()
    assert outcome.pending_gate["artifact_sha256"] == dm.report_sha256

    plan = asyncio.run(runner.decide(run_id, approved=True, approver="bob")).plan
    assert plan.data_migration.status == CutoverStatus.CUT_OVER


def test_report_changed_after_approval_request_is_refused(runner: Runner) -> None:
    run_id = _deployed(runner)
    outcome = asyncio.run(runner.advance(run_id, "data", RunOptions()))
    report = Path(outcome.plan.data_migration.report_path)
    report.write_text(report.read_text().replace("✅", "✔"))
    plan = asyncio.run(runner.decide(run_id, approved=True, approver="bob")).plan
    assert plan.data_migration.status == CutoverStatus.FAILED
    assert "changed after it was approved" in plan.halt_reason
    assert "migrate data" in plan.halt_reason


def test_failed_verification_blocks_the_cutover(tmp_path: Path, reporter) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(PROJECT_ROOT / "fixtures", fixtures)
    results_file = fixtures / "datamove" / "mock-results.json"
    results = json.loads(results_file.read_text())
    results["buckets"]["shop-assets-prod"]["target"]["objects"] -= 17
    results["databases"]["shop_db"]["row_counts"]["target"]["public.orders"] -= 3
    results_file.write_text(json.dumps(results))
    settings = Settings(
        _env_file=None,
        runs_dir=tmp_path / "runs",
        fixtures_dir=fixtures,
        config_dir=PROJECT_ROOT / "config",
    )
    runner = Runner(settings, reporter)
    run_id = _deployed(runner)
    outcome = asyncio.run(runner.advance(run_id, "data", RunOptions()))
    dm = outcome.plan.data_migration
    assert outcome.pending_gate is None
    assert dm.status == CutoverStatus.FAILED
    assert "Verification failed" in outcome.halted
    failed = {(c.task_id, c.name) for c in dm.checks if not c.passed}
    assert failed == {
        ("s3:shop-assets-prod", "object_count"),
        ("pg:shop_db", "row_counts"),
    }
    assert "public.orders" in next(
        c.details for c in dm.checks if c.name == "row_counts"
    )
    assert "❌" in Path(dm.report_path).read_text()


def test_rclone_wrapper_never_deletes() -> None:
    from mcp_servers.datamove import rclone

    for forbidden in (
        "sync",
        "move",
        "moveto",
        "delete",
        "deletefile",
        "purge",
        "rmdir",
        "rmdirs",
        "cleanup",
    ):
        with pytest.raises(PermissionError):
            rclone.rclone(forbidden, "src:x")
    for path in (PROJECT_ROOT / "src" / "mcp_servers" / "datamove").glob("*.py"):
        text = path.read_text()
        for forbidden in (
            '"sync"',
            '"move"',
            '"delete"',
            '"purge"',
            "DROP ",
            "TRUNCATE",
        ):
            assert forbidden not in text, f"{forbidden} found in {path.name}"


def test_only_the_target_restore_cleans() -> None:
    import inspect

    from mcp_servers.datamove import postgres

    restore = inspect.getsource(postgres.DumpRestore.restore)
    assert "--clean" in restore and 'dsn_for("target", ref)' in restore
    assert 'dsn_for("source"' not in restore
    rest = inspect.getsource(postgres).replace(restore, "")
    assert '"--clean"' not in rest


def test_dsn_becomes_libpq_env_not_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_servers.datamove import postgres

    monkeypatch.setenv(
        "PG_DSN_SOURCE_SHOP_DB",
        "postgresql://app%40x:s3cr%2Ft@db.example:6432/shop?sslmode=require",
    )
    env = postgres.libpq_env(postgres.dsn_for("source", "shop_db"))
    assert env["PGUSER"] == "app@x" and env["PGPASSWORD"] == "s3cr/t"
    assert env["PGHOST"] == "db.example" and env["PGPORT"] == "6432"
    assert env["PGDATABASE"] == "shop" and env["PGSSLMODE"] == "require"
    with pytest.raises(RuntimeError, match="PG_DSN_TARGET_SHOP_DB"):
        postgres.dsn_for("target", "shop_db")


def test_bad_dsn_errors_do_not_echo_the_password() -> None:
    from mcp_servers.datamove import postgres

    with pytest.raises(ValueError) as exc:
        postgres.libpq_env("postgresql://app:p4ss:w0rd-x@db.example:notaport/shop")
    assert "p4ss" not in str(exc.value) and "w0rd" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_redactor_masks_decoded_dsn_passwords() -> None:
    from migrate.audit import Redactor, secret_variants

    dsn = "postgresql://app:s3cr%2Ft-pw@db.example:5432/shop"
    redactor = Redactor(secret_variants(dsn))
    for leak in ("s3cr/t-pw", "s3cr%2Ft-pw", dsn):
        assert "s3cr" not in redactor.text(f"boom: {leak}")


def test_dotenv_dsns_reach_the_datamove_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrate.mcp_client import server_env

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "# per-database DSNs\n"
        "PG_DSN_SOURCE_SHOP_DB='postgresql://u:from-dotenv@src/shop'\n"
        "export AWS_PROFILE=readonly # comment\n"
        "GH_TOKEN=not-for-datamove\n"
    )
    monkeypatch.delenv("PG_DSN_SOURCE_SHOP_DB", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    settings = Settings()
    env = server_env(
        "datamove",
        settings,
        mock=False,
        run_id="r",
        run_dir=tmp_path,
        approval_secret="x" * 32,
    )
    assert env["PG_DSN_SOURCE_SHOP_DB"] == "postgresql://u:from-dotenv@src/shop"
    assert env["AWS_PROFILE"] == "readonly"
    assert "GH_TOKEN" not in env
    assert "from-dotenv" in settings.secret_values()
    monkeypatch.setenv("PG_DSN_SOURCE_SHOP_DB", "postgresql://u:from-env@src/shop")
    env = server_env(
        "datamove",
        settings,
        mock=False,
        run_id="r",
        run_dir=tmp_path,
        approval_secret="x" * 32,
    )
    assert env["PG_DSN_SOURCE_SHOP_DB"].endswith("from-env@src/shop")  # env wins


def test_rclone_source_needs_explicit_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_servers.datamove import rclone

    for key in (
        *rclone.EXPLICIT_AWS_CREDENTIALS,
        "AWS_USE_DEFAULT_CREDENTIALS",
        "MIGRATE_RCLONE_SRC_ROOT",
        "MIGRATE_RCLONE_DST_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(PermissionError, match="no AWS credentials"):
        rclone.remote_env()
    monkeypatch.setenv("AWS_PROFILE", "migration-readonly")
    assert rclone.remote_env()["RCLONE_CONFIG_SRC_TYPE"] == "s3"
    monkeypatch.delenv("AWS_PROFILE")
    monkeypatch.setenv("AWS_USE_DEFAULT_CREDENTIALS", "1")
    assert rclone.remote_env()["RCLONE_CONFIG_SRC_ENV_AUTH"] == "true"


def test_data_commands_are_never_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_servers.datamove import rclone

    seen = {}

    def fake_run(args, **kw):
        seen[args[1]] = kw["max_output"]
        return type("R", (), {"stdout": "{}"})()

    monkeypatch.setattr(rclone, "run", fake_run)
    monkeypatch.setattr(rclone, "remote_env", lambda: {})
    for sub in ("lsjson", "md5sum", "size", "copy"):
        rclone.rclone(sub, "src:x")
    assert seen == {"lsjson": None, "md5sum": None, "size": None, "copy": 200_000}


def test_rerun_after_cutover_does_nothing(runner: Runner, reporter) -> None:
    run_id = _deployed(runner)
    asyncio.run(runner.advance(run_id, "data", RunOptions()))
    asyncio.run(runner.decide(run_id, approved=True, approver="bob"))
    before = (runner.settings.runs_dir / run_id / "audit.jsonl").read_text()
    outcome = asyncio.run(runner.advance(run_id, "data", RunOptions()))
    assert outcome.plan.data_migration.status == CutoverStatus.CUT_OVER
    after = (runner.settings.runs_dir / run_id / "audit.jsonl").read_text()
    assert after[len(before) :].count("datamove.") == 0  # no transfer, no cutover
