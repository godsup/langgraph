from __future__ import annotations

import json

import boto3
import pytest

from migrate.adapters.aws import (
    READ_ONLY_OPERATIONS,
    AwsAdapter,
    ReadOnlyViolationError,
    iam_actions,
    security_group_to_resource,
)
from migrate.config import PROJECT_ROOT
from migrate.tools.build_aws_fixture import build


def _adapter() -> AwsAdapter:
    session = boto3.Session(
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="x",
        region_name="eu-central-1",
    )
    return AwsAdapter(region="eu-central-1", session=session)


@pytest.mark.parametrize(
    ("service", "method", "kwargs"),
    [
        ("ec2", "terminate_instances", {"InstanceIds": ["i-1"]}),
        ("ec2", "create_security_group", {"GroupName": "x", "Description": "x"}),
        ("rds", "delete_db_instance", {"DBInstanceIdentifier": "db"}),
        ("s3", "delete_bucket", {"Bucket": "b"}),
        ("s3", "get_object", {"Bucket": "b", "Key": "k"}),  # contents are never read
    ],
)
def test_guard_blocks_non_allowlisted_operations_before_sending(
    service: str, method: str, kwargs: dict
) -> None:
    with pytest.raises(ReadOnlyViolationError):
        getattr(_adapter().client(service), method)(**kwargs)


def test_guard_blocks_unknown_services() -> None:
    with pytest.raises(ReadOnlyViolationError):
        _adapter().client("iam")


def test_allowlist_contains_only_read_operations() -> None:
    for operations in READ_ONLY_OPERATIONS.values():
        for op in operations:
            assert op.startswith(("Describe", "List", "Get")), op


def test_iam_policy_document_matches_the_allowlist() -> None:
    policy = json.loads(
        (PROJECT_ROOT / "docs" / "aws-readonly-policy.json").read_text()
    )
    documented = {a for st in policy["Statement"] for a in st["Action"]}
    assert all(st["Effect"] == "Allow" for st in policy["Statement"])
    assert documented == iam_actions()
    assert not any(a.endswith("*") for a in documented)


def test_committed_mock_inventory_is_real_adapter_output() -> None:
    committed = json.loads(
        (PROJECT_ROOT / "fixtures" / "aws" / "inventory.json").read_text()
    )
    assert build() == committed


def test_normalisation_details() -> None:
    sections = build()["sections"]
    instances = {r["name"]: r for r in sections["compute_instances"]}
    assert instances["shop-web-1"]["properties"]["vcpus"] == 2
    assert instances["legacy-reporting"]["properties"]["platform"] == "windows"
    assert (
        instances["shop-analytics-node-1"]["properties"]["eks_cluster"]
        == "shop-analytics"
    )

    volumes = {r["id"]: r for r in sections["block_volumes"]}
    assert volumes["vol-0a1b2c3d4e5f60101"]["properties"]["is_root"] is True
    assert volumes["vol-0a1b2c3d4e5f60203"]["properties"]["is_root"] is False

    buckets = {r["name"]: r for r in sections["object_buckets"]}
    assert "shop-terraform-state-us" not in buckets  # other region is filtered out
    logs = buckets["shop-logs-archive"]["properties"]
    assert logs["has_archive_storage"] is True
    assert logs["size_bytes"] == 128_849_018_880 + 2_308_974_329_856
    assert buckets["shop-user-uploads-prod"]["properties"]["versioning"] == "Enabled"
    assert buckets["shop-assets-prod"]["properties"]["website"] is False

    lb = sections["load_balancers"][0]
    assert lb["depends_on"] == ["i-0a1b2c3d4e5f60001", "i-0a1b2c3d4e5f60002"]
    cluster = sections["kubernetes_clusters"][0]["properties"]
    assert cluster["node_groups"][0]["vcpus"] == 4


def test_security_group_rules_are_expanded() -> None:
    sg = {
        "GroupId": "sg-1",
        "GroupName": "db",
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 5432,
                "ToPort": 5432,
                "IpRanges": [{"CidrIp": "10.0.0.0/24"}],
                "UserIdGroupPairs": [{"GroupId": "sg-2"}, {"GroupId": "sg-1"}],
            }
        ],
        "IpPermissionsEgress": [
            {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        ],
    }
    res = security_group_to_resource(sg, "eu-central-1")
    rules = res.properties["rules"]
    assert len(rules) == 4
    assert {
        "direction": "egress",
        "protocol": "all",
        "port_min": None,
        "port_max": None,
        "cidr": "0.0.0.0/0",
        "description": "",
    } in rules
    assert res.depends_on == ["sg-2"]  # self-references are not dependencies
