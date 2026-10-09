from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from migrate import costing
from migrate.audit import AuditLog
from migrate.config import PROJECT_ROOT, Settings
from migrate.llm import LLMGateway
from migrate.mapping import Catalog, MappingEngine
from migrate.state import DecidedBy, MappingStatus, ResourceKind, SourceResource

CONFIG = yaml.safe_load((PROJECT_ROOT / "config" / "mapping.yaml").read_text())
CATALOG = Catalog.from_dict(
    {
        "source": "test",
        **json.loads(
            (PROJECT_ROOT / "fixtures" / "stackit" / "catalog.json").read_text()
        ),
    }
)


def _vm(
    id: str = "i-1",
    instance_type: str = "t3.large",
    vcpus: int = 2,
    mem: int = 8192,
    **props,
) -> SourceResource:
    return SourceResource(
        id=id,
        name=id,
        kind=ResourceKind.COMPUTE_INSTANCE,
        source_type="aws:ec2:instance",
        region="eu-central-1",
        properties={
            "instance_type": instance_type,
            "vcpus": vcpus,
            "memory_mib": mem,
            "platform": "linux",
            "architecture": "x86_64",
            "availability_zone": "eu-central-1b",
            **props,
        },
    )


def _gateway(tmp_path: Path, responses: dict) -> LLMGateway:
    (tmp_path / "llm").mkdir(exist_ok=True)
    (tmp_path / "llm" / "mapping_choice.json").write_text(
        json.dumps({"responses": responses})
    )
    return LLMGateway(
        Settings(_env_file=None),
        AuditLog(tmp_path / "a.jsonl"),
        recorded_dir=tmp_path / "llm",
    )


def test_unique_smallest_machine_type_is_a_rule_decision() -> None:
    mapping, gaps, pending = MappingEngine(CONFIG, CATALOG).map_one(_vm(), [])
    assert mapping.status == MappingStatus.MAPPED
    assert mapping.target_spec["machine_type"] == "g2i.2"
    assert mapping.target_spec["availability_zone"] == "eu01-2"
    assert not gaps and not pending


def test_ambiguous_machine_type_goes_to_the_llm(tmp_path: Path) -> None:
    worker = _vm("i-w", "c5.xlarge", 4, 8192)
    llm = _gateway(tmp_path, {"i-w": {"choice": "c2i.4", "rationale": "cpu bound"}})
    mappings, _ = asyncio.run(
        MappingEngine(CONFIG, CATALOG).map_all([worker], llm, agent="t")
    )
    assert mappings[0].target_spec["machine_type"] == "c2i.4"
    assert mappings[0].decided_by == DecidedBy.LLM


def test_llm_choice_outside_the_candidates_is_rejected(tmp_path: Path) -> None:
    worker = _vm("i-w", "c5.xlarge", 4, 8192)
    llm = _gateway(tmp_path, {"i-w": {"choice": "x9.huge", "rationale": "made up"}})
    mappings, _ = asyncio.run(
        MappingEngine(CONFIG, CATALOG).map_all([worker], llm, agent="t")
    )
    assert mappings[0].target_spec["machine_type"] in {"c2i.4", "g2i.4"}
    assert mappings[0].decided_by == DecidedBy.FALLBACK
    assert mappings[0].needs_review


def test_windows_and_arm_are_gaps() -> None:
    engine = MappingEngine(CONFIG, CATALOG)
    windows, gaps, _ = engine.map_one(_vm(platform="windows"), [])
    assert windows.status == MappingStatus.GAP and gaps[0].severity.value == "high"
    arm, _, _ = engine.map_one(_vm(architecture="arm64"), [])
    assert arm.status == MappingStatus.GAP


def test_too_large_instance_is_a_gap() -> None:
    mapping, gaps, _ = MappingEngine(CONFIG, CATALOG).map_one(
        _vm(vcpus=96, mem=786432), []
    )
    assert mapping.status == MappingStatus.GAP
    assert "No STACKIT machine type" in gaps[0].reason


def _db(
    engine: str = "postgres", version: str = "15.7", multi_az: bool = True
) -> SourceResource:
    kind = {"postgres": ResourceKind.POSTGRES, "mysql": ResourceKind.MYSQL}[engine]
    return SourceResource(
        id="rds:db",
        name="db",
        kind=kind,
        source_type=f"aws:rds:{engine}",
        region="eu-central-1",
        properties={
            "engine": engine,
            "engine_version": version,
            "instance_class": "db.m5.large",
            "vcpus": 2,
            "memory_mib": 8192,
            "multi_az": multi_az,
            "allocated_storage_gb": 100,
        },
    )


def test_postgres_flavor_respects_replica_and_size() -> None:
    mapping, gaps, _ = MappingEngine(CONFIG, CATALOG).map_one(_db(), [])
    assert mapping.target_spec["flavor"]["id"] == "4.8-replica"
    assert mapping.target_spec["version"] == "15"
    assert mapping.status == MappingStatus.MAPPED and not gaps
    single, _, _ = MappingEngine(CONFIG, CATALOG).map_one(_db(multi_az=False), [])
    assert single.target_spec["flavor"]["id"] == "4.8"


def test_unsupported_postgres_version_is_upgraded_and_flagged() -> None:
    mapping, gaps, _ = MappingEngine(CONFIG, CATALOG).map_one(_db(version="13.12"), [])
    assert mapping.target_spec["version"] == "14"
    assert mapping.status == MappingStatus.PARTIAL
    assert "major upgrade" in gaps[0].reason


def test_mysql_maps_to_mariadb_with_review() -> None:
    mapping, gaps, _ = MappingEngine(CONFIG, CATALOG).map_one(
        _db("mysql", "8.0.35", multi_az=False), []
    )
    assert mapping.terraform_type == "stackit_mariadb_instance"
    assert mapping.status == MappingStatus.PARTIAL and mapping.needs_review
    assert "no managed MySQL" in gaps[0].reason


def test_eks_nodes_are_covered_by_the_node_pool() -> None:
    mapping, gaps, _ = MappingEngine(CONFIG, CATALOG).map_one(
        _vm(eks_cluster="analytics"), []
    )
    assert (
        mapping.terraform_type is None
        and mapping.status == MappingStatus.MAPPED
        and not gaps
    )


def test_unknown_kind_is_a_gap() -> None:
    other = SourceResource(
        id="x",
        name="x",
        kind=ResourceKind.OTHER,
        source_type="aws:rds:oracle-ee",
        region="r",
    )
    mapping, gaps, _ = MappingEngine(CONFIG, CATALOG).map_one(other, [])
    assert mapping.status == MappingStatus.GAP and gaps


def test_cost_estimate_never_invents_prices() -> None:
    pricing = yaml.safe_load((PROJECT_ROOT / "config" / "pricing.yaml").read_text())
    mapping, _, _ = MappingEngine(CONFIG, CATALOG).map_one(_vm(), [])
    estimate = costing.estimate([_vm()], [mapping], pricing)
    assert estimate.source_total == 0 and estimate.target_total == 0
    assert "aws.ec2_instance_hourly[t3.large]" in estimate.missing_prices
    assert estimate.lines[0].note == "price not set"


def test_cost_estimate_with_prices() -> None:
    pricing = {
        "hours_per_month": 730,
        "aws": {"ec2_instance_hourly": {"t3.large": 0.1}},
        "stackit": {"machine_type_hourly": {"g2i.2": 0.05}},
    }
    mapping, _, _ = MappingEngine(CONFIG, CATALOG).map_one(_vm(), [])
    estimate = costing.estimate([_vm()], [mapping], pricing)
    assert estimate.source_total == pytest.approx(73.0)
    assert estimate.target_total == pytest.approx(36.5)
    assert not estimate.missing_prices


def test_shipped_prices_are_all_placeholders() -> None:
    pricing = yaml.safe_load((PROJECT_ROOT / "config" / "pricing.yaml").read_text())

    def values(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from values(v)
        else:
            yield node

    prices = [v for side in ("aws", "stackit") for v in values(pricing[side])]
    assert prices and all(v is None for v in prices)
