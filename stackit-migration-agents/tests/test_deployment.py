from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from migrate.approval import sha256_bundle
from migrate.runner import Runner
from migrate.state import DeploymentStatus, DeployMode, RunOptions, SourceCloud


def _run(runner: Runner) -> str:
    return runner.create_run(source=SourceCloud.AWS, region=None, mock=True).run_id


def test_plan_writes_terraform_and_summary(runner: Runner) -> None:
    run_id = _run(runner)
    plan = asyncio.run(runner.advance(run_id, "plan", RunOptions())).plan
    dep = plan.deployment
    assert dep.status == DeploymentStatus.PLANNED
    tf_dir = Path(dep.terraform_dir)
    assert (tf_dir / "compute.tf").exists() and (
        tf_dir / "terraform.tfvars.json"
    ).exists()
    assert (
        Path(dep.plan_text_file)
        .read_text()
        .strip()
        .endswith("Plan: 39 to add, 0 to change, 0 to destroy.")
    )
    assert len(dep.changes) == 39 and all(c.action == "create" for c in dep.changes)
    assert dep.summary.startswith("Terraform will create 39 resources")
    assert len(dep.risks) == 6
    assert any("legacy-reporting" in s for s in dep.skipped)


def test_gate_payload_has_summary_and_raw_plan(runner: Runner) -> None:
    run_id = _run(runner)
    outcome = asyncio.run(
        runner.advance(run_id, "deploy", RunOptions(deploy_mode=DeployMode.APPLY))
    )
    gate = outcome.pending_gate
    assert gate["gate"] == "deployment"
    assert "Plan: 39 to add" in gate["raw_plan"]
    assert "Risks / review points" in gate["summary"]
    # The approval covers the whole Terraform bundle: saved plan, code, inputs.
    tf_dir = Path(outcome.plan.deployment.terraform_dir)
    assert gate["artifact_sha256"] == sha256_bundle(tf_dir)


def test_apply_after_approval(runner: Runner) -> None:
    run_id = _run(runner)
    asyncio.run(runner.advance(run_id, "deploy", RunOptions()))
    plan = asyncio.run(runner.decide(run_id, approved=True, approver="alice")).plan
    assert plan.deployment.status == DeploymentStatus.APPLIED
    assert plan.deployment.outputs["note"].startswith("simulated")


def test_pr_mode_opens_a_pull_request(runner: Runner) -> None:
    run_id = _run(runner)
    asyncio.run(runner.advance(run_id, "deploy", RunOptions(deploy_mode=DeployMode.PR)))
    plan = asyncio.run(runner.decide(run_id, approved=True, approver="alice")).plan
    assert plan.deployment.status == DeploymentStatus.PR_OPENED
    assert plan.deployment.pr_url.startswith("https://github.com/")


def _approve_after_tampering(runner: Runner, tamper) -> object:
    run_id = _run(runner)
    outcome = asyncio.run(runner.advance(run_id, "deploy", RunOptions()))
    tamper(Path(outcome.plan.deployment.terraform_dir))
    return asyncio.run(runner.decide(run_id, approved=True, approver="alice")).plan


def test_plan_changed_after_approval_is_refused(runner: Runner) -> None:
    plan = _approve_after_tampering(
        runner,
        lambda tf: (tf / "tfplan").write_text('{"changes": [], "tampered": true}'),
    )
    assert plan.deployment.status == DeploymentStatus.FAILED
    assert "changed after it was approved" in plan.halt_reason
    assert "migrate deploy" in plan.halt_reason


@pytest.mark.parametrize("mode", [DeployMode.APPLY, DeployMode.PR])
def test_terraform_code_changed_after_approval_is_refused(
    runner: Runner, mode: DeployMode
) -> None:
    run_id = _run(runner)
    outcome = asyncio.run(
        runner.advance(run_id, "deploy", RunOptions(deploy_mode=mode))
    )
    compute = Path(outcome.plan.deployment.terraform_dir) / "compute.tf"
    compute.write_text(compute.read_text() + "\n# sneaky edit\n")
    plan = asyncio.run(runner.decide(run_id, approved=True, approver="alice")).plan
    assert plan.deployment.status == DeploymentStatus.FAILED
    assert "changed after it was approved" in plan.halt_reason
    assert plan.deployment.pr_url is None
