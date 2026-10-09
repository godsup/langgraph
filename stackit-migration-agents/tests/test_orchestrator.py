from __future__ import annotations

import asyncio
from typing import Any

import pytest

from migrate.agents import deployment
from migrate.runner import GatePendingError, NoPendingGateError, Runner
from migrate.state import (
    CutoverStatus,
    DeploymentStatus,
    DeployMode,
    RunOptions,
    SourceCloud,
)


def _new_run(runner: Runner) -> str:
    return runner.create_run(source=SourceCloud.AWS, region=None, mock=True).run_id


def test_full_mock_flow_passes_both_gates(runner: Runner) -> None:
    run_id = _new_run(runner)
    outcome = asyncio.run(runner.advance(run_id, "all", RunOptions()))
    assert outcome.pending_gate and outcome.pending_gate["gate"] == "deployment"
    assert outcome.plan.deployment.status == DeploymentStatus.AWAITING_APPROVAL

    outcome = asyncio.run(runner.decide(run_id, approved=True, approver="alice"))
    assert outcome.pending_gate and outcome.pending_gate["gate"] == "cutover"
    assert outcome.plan.deployment.status == DeploymentStatus.APPLIED

    outcome = asyncio.run(runner.decide(run_id, approved=True, approver="bob"))
    assert outcome.pending_gate is None
    plan = outcome.plan
    assert plan.data_migration.status == CutoverStatus.CUT_OVER
    assert plan.monitoring.polls == 1
    assert [a.approver for a in plan.approvals] == ["alice", "bob"]


def test_goal_plan_stops_before_the_gate(runner: Runner) -> None:
    run_id = _new_run(runner)
    outcome = asyncio.run(runner.advance(run_id, "plan", RunOptions()))
    assert outcome.pending_gate is None
    assert outcome.plan.deployment.status == DeploymentStatus.PLANNED
    assert outcome.plan.discovery.completed_at is not None


def test_rejection_halts_and_replanning_requests_a_new_approval(runner: Runner) -> None:
    run_id = _new_run(runner)
    asyncio.run(
        runner.advance(run_id, "deploy", RunOptions(deploy_mode=DeployMode.APPLY))
    )
    outcome = asyncio.run(
        runner.decide(run_id, approved=False, approver="carol", comment="too big")
    )
    assert outcome.plan.deployment.status == DeploymentStatus.REJECTED
    assert outcome.halted and "rejected" in outcome.halted

    outcome = asyncio.run(runner.advance(run_id, "deploy", RunOptions()))
    assert outcome.pending_gate and outcome.pending_gate["gate"] == "deployment"
    assert len(outcome.plan.approvals) == 2


def test_commands_are_blocked_while_a_gate_is_pending(runner: Runner) -> None:
    run_id = _new_run(runner)
    asyncio.run(runner.advance(run_id, "deploy", RunOptions()))
    with pytest.raises(GatePendingError):
        asyncio.run(runner.advance(run_id, "data", RunOptions()))


def test_approve_without_pending_gate_is_an_error(runner: Runner) -> None:
    run_id = _new_run(runner)
    asyncio.run(runner.advance(run_id, "discover", RunOptions()))
    with pytest.raises(NoPendingGateError):
        asyncio.run(runner.decide(run_id, approved=True, approver="x"))


def test_run_resumes_after_a_crash(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _new_run(runner)
    asyncio.run(runner.advance(run_id, "deploy", RunOptions()))

    original = deployment.execute
    calls: list[int] = []

    async def crash_once(state: Any, runtime: Any) -> Any:
        calls.append(1)
        raise RuntimeError("simulated crash during apply")

    monkeypatch.setattr(deployment, "execute", crash_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(runner.decide(run_id, approved=True, approver="alice"))
    assert calls == [1]

    # "Restart": a fresh runner with the real node picks up where it stopped.
    monkeypatch.setattr(deployment, "execute", original)
    fresh = Runner(runner.settings)
    status = asyncio.run(fresh.status(run_id))
    assert status.next_nodes == ("deployment",)
    outcome = asyncio.run(fresh.resume(run_id, RunOptions()))
    assert outcome.plan.deployment.status == DeploymentStatus.APPLIED
    assert outcome.plan.approvals[-1].approver == "alice"

    from migrate.audit import AuditLog
    from migrate.runs import run_paths

    entries = AuditLog(run_paths(runner.settings.runs_dir, run_id).audit_file).entries()
    discovery_runs = [
        e for e in entries if e["agent"] == "discovery" and e["action"] == "stage.enter"
    ]
    assert len(discovery_runs) == 1  # completed work is not repeated


def test_checkpoint_db_loss_reseeds_from_plan_file(runner: Runner) -> None:
    run_id = _new_run(runner)
    asyncio.run(runner.advance(run_id, "discover", RunOptions()))
    runner.settings.sqlite_path.unlink()
    outcome = asyncio.run(runner.advance(run_id, "plan", RunOptions()))
    assert outcome.plan.discovery.completed_at is not None
    assert outcome.plan.deployment.status == DeploymentStatus.PLANNED
