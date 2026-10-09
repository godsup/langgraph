from __future__ import annotations

from pathlib import Path

import pytest

from migrate.approval import SECRET_ENV, ApprovalError, ApprovalTokens, sha256_file


@pytest.fixture
def run_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    run_dir = tmp_path / "20261009-120000-abcd"
    (run_dir / "terraform").mkdir(parents=True)
    (run_dir / "terraform" / "tfplan").write_text("plan")
    monkeypatch.setenv("MIGRATE_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MIGRATE_RUN_ID", run_dir.name)
    monkeypatch.setenv("MIGRATE_MOCK", "1")
    monkeypatch.setenv(SECRET_ENV, "s" * 32)
    return run_dir


def test_terraform_apply_refuses_without_valid_token(run_env: Path) -> None:
    from mcp_servers.stackit import server

    tf = run_env / "terraform"
    with pytest.raises(ApprovalError):
        server.terraform_apply(workdir=str(tf), plan_file="tfplan", approval_token="")
    with pytest.raises(ApprovalError):
        forged = ApprovalTokens("f" * 32).issue(
            run_id=run_env.name,
            action="terraform.apply",
            artifact_sha256=sha256_file(tf / "tfplan"),
        )
        server.terraform_apply(
            workdir=str(tf), plan_file="tfplan", approval_token=forged
        )

    good = ApprovalTokens("s" * 32).issue(
        run_id=run_env.name,
        action="terraform.apply",
        artifact_sha256=sha256_file(tf / "tfplan"),
    )
    assert server.terraform_apply(
        workdir=str(tf), plan_file="tfplan", approval_token=good
    )["applied"]


def test_terraform_apply_rejects_plan_changed_after_approval(run_env: Path) -> None:
    from mcp_servers.stackit import server

    tf = run_env / "terraform"
    token = ApprovalTokens("s" * 32).issue(
        run_id=run_env.name,
        action="terraform.apply",
        artifact_sha256=sha256_file(tf / "tfplan"),
    )
    (tf / "tfplan").write_text("a different plan")
    with pytest.raises(ApprovalError, match="hash mismatch"):
        server.terraform_apply(
            workdir=str(tf), plan_file="tfplan", approval_token=token
        )


def test_servers_refuse_paths_outside_the_run(run_env: Path) -> None:
    from mcp_servers.stackit import server

    with pytest.raises(PermissionError):
        server.terraform_plan(workdir="/etc")
    with pytest.raises(PermissionError):
        server.terraform_plan(workdir="../../")


def test_cutover_refuses_without_token(run_env: Path) -> None:
    from mcp_servers.datamove import server

    report = run_env / "data-verification.md"
    report.write_text("ok")
    with pytest.raises(ApprovalError):
        server.cutover(report_path=str(report), approval_token="v1.bad.token")
