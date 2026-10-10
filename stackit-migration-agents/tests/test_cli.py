from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from migrate import config
from migrate.cli import app


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)  # no stray .env
    config.get_settings.cache_clear()
    yield tmp_path / "runs"
    config.get_settings.cache_clear()


def test_run_mock_end_to_end(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--source", "aws", "--mock", "--yes"])
    assert result.exit_code == 0, result.output
    assert "MOCK MODE" in result.output
    run_id = re.search(r"Run (\d{8}-\d{6}-[0-9a-f]{4}) created", result.output).group(1)
    assert (cli_env / run_id / "migration-plan.json").exists()
    assert (cli_env / run_id / "audit.jsonl").exists()

    status = CliRunner().invoke(app, ["status", run_id])
    assert status.exit_code == 0, status.output
    assert "cut_over" in status.output


def test_step_by_step_commands(cli_env: Path) -> None:
    cli = CliRunner()
    out = cli.invoke(app, ["discover", "--source", "aws", "--mock"])
    assert out.exit_code == 0, out.output
    run_id = re.search(r"Run (\S+) created", out.output).group(1)
    assert cli.invoke(app, ["plan", run_id]).exit_code == 0
    deploy = cli.invoke(app, ["deploy", run_id, "--mode", "apply"])
    assert deploy.exit_code == 0 and "migrate approve" in deploy.output
    blocked = cli.invoke(app, ["data", run_id])
    assert blocked.exit_code == 1 and "pending" in blocked.output
    assert cli.invoke(app, ["approve", run_id, "--approver", "alice"]).exit_code == 0


def test_yes_requires_mock(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--yes"])
    assert result.exit_code == 1
    assert "only allowed together with --mock" in result.output


def test_unknown_run_is_a_clean_error(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["status", "20990101-000000-ffff"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_failed_deployment_exits_with_code_3(cli_env: Path) -> None:
    cli = CliRunner()
    out = cli.invoke(app, ["discover", "--source", "aws", "--mock"])
    run_id = re.search(r"Run (\S+) created", out.output).group(1)
    assert cli.invoke(app, ["deploy", run_id]).exit_code == 0
    compute = cli_env / run_id / "terraform" / "compute.tf"
    compute.write_text(compute.read_text() + "# edited after the approval request\n")
    result = cli.invoke(app, ["approve", run_id, "--approver", "alice"])
    assert result.exit_code == 3, result.output
    assert "changed after it was approved" in result.output


def test_run_mock_end_to_end_azure(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--source", "azure", "--mock", "--yes"])
    assert result.exit_code == 0, result.output
    run_id = re.search(r"Run (\d{8}-\d{6}-[0-9a-f]{4}) created", result.output).group(1)
    status = CliRunner().invoke(app, ["status", run_id])
    assert "cut_over" in status.output, status.output
