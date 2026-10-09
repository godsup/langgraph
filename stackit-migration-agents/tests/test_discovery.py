from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from migrate.config import PROJECT_ROOT, Settings
from migrate.runner import Runner
from migrate.state import DecidedBy, MappingStatus, RunOptions, SourceCloud


def _discover(runner: Runner, source: SourceCloud = SourceCloud.AWS):
    run_id = runner.create_run(source=source, region=None, mock=True).run_id
    return asyncio.run(runner.advance(run_id, "discover", RunOptions())).plan


def test_mock_discovery_end_to_end(runner: Runner) -> None:
    plan = _discover(runner)
    assert len(plan.resources) == 36
    statuses = [m.status for m in plan.mappings]
    assert statuses.count(MappingStatus.MAPPED) == 29
    assert statuses.count(MappingStatus.PARTIAL) == 6
    assert statuses.count(MappingStatus.GAP) == 1
    by_id = {m.resource_id: m for m in plan.mappings}
    assert by_id["i-0a1b2c3d4e5f60003"].decided_by == DecidedBy.LLM
    assert by_id["i-0a1b2c3d4e5f60003"].target_spec["machine_type"] == "c2i.4"
    assert by_id["elb:shop-alb"].terraform_type == "stackit_application_load_balancer"
    assert by_id["rds:shop-db"].role.startswith("primary transactional database")
    assert any(g.source == "llm" and "RDP" in g.reason for g in plan.gaps)
    assert plan.cost.missing_prices  # placeholders are reported, not invented

    report = Path(plan.discovery.report_path).read_text()
    for heading in (
        "## Executive summary",
        "## Gaps and risks",
        "## Cost estimate",
        "## Decisions made by the LLM",
    ):
        assert heading in report
    assert "price not set" in report
    assert "MOCK" in report


def test_azure_stub_uses_the_same_interface(runner: Runner) -> None:
    plan = _discover(runner, SourceCloud.AZURE)
    assert plan.source_region == "germanywestcentral"
    kinds = {r.kind.value for r in plan.resources}
    assert {"compute_instance", "postgres", "object_bucket"} <= kinds
    assert all(m.status != MappingStatus.GAP for m in plan.mappings)


def test_llm_review_cannot_invent_resources(tmp_path: Path, reporter) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(PROJECT_ROOT / "fixtures", fixtures)
    review = json.loads((fixtures / "llm" / "inventory_review.json").read_text())
    review["responses"]["aws"]["concerns"].append(
        {
            "resource_id": "i-does-not-exist",
            "severity": "high",
            "concern": "x",
            "suggestion": "y",
        }
    )
    (fixtures / "llm" / "inventory_review.json").write_text(json.dumps(review))
    settings = Settings(
        _env_file=None,
        runs_dir=tmp_path / "runs",
        fixtures_dir=fixtures,
        config_dir=PROJECT_ROOT / "config",
    )
    plan = _discover(Runner(settings, reporter))
    assert all(g.resource_id != "i-does-not-exist" for g in plan.gaps)
