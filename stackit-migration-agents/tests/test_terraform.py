from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from migrate.state import (
    MappingStatus,
    MigrationPlan,
    ResourceKind,
    SourceCloud,
    SourceResource,
    TargetMapping,
)
from migrate.terraform import align_hcl, dns_name, hcl_string, render, ske_name
from migrate.tools.render_fixture_terraform import render_fixture

TERRAFORM = shutil.which("terraform")


@pytest.fixture(scope="module")
def fixture_tf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("tf") / "terraform"
    render_fixture(out)
    return out


def test_fixture_renders_expected_resources(fixture_tf: Path) -> None:
    text = "\n".join(p.read_text() for p in fixture_tf.glob("*.tf"))
    assert 'resource "stackit_server" "shop_web_1"' in text
    assert 'resource "stackit_postgresflex_instance" "shop_db"' in text
    assert 'resource "stackit_application_load_balancer" "shop_alb"' in text
    assert 'resource "stackit_ske_cluster" "shop_analytics"' in text
    # The Windows gap and its volume are not rendered...
    assert "legacy_reporting" not in text.replace("legacy_crm", "")
    # ...but listed in the generated README.
    assert "legacy-reporting" in (fixture_tf / "README.md").read_text()
    # Secrets never land in generated files.
    assert "sensitive   = true" in (fixture_tf / "variables.tf").read_text()
    assert "BEGIN" not in text and "password =" not in text


def test_database_acl_follows_security_group_rules(fixture_tf: Path) -> None:
    dbs = (fixture_tf / "databases.tf").read_text()
    shop_acl = next(
        line for line in dbs.splitlines() if line.strip().startswith("shop_db_acl")
    )
    crm_acl = next(
        line
        for line in dbs.splitlines()
        if line.strip().startswith("legacy_crm_mysql_acl")
    )
    assert (
        "shop_public_a" not in shop_acl
    )  # the Windows server's subnet is not a client
    assert "stackit_ske_cluster.shop_analytics.egress_address_ranges" in shop_acl
    assert "stackit_ske_cluster" not in crm_acl  # only the web tier talks to MySQL


def test_no_deprecated_or_beta_arguments(fixture_tf: Path) -> None:
    text = "\n".join(p.read_text() for p in fixture_tf.glob("*.tf"))
    for banned in (
        "enable_beta_resources",
        "experiments",
        "os_version ",
        "replicas",
        "access_scope",
        'resource "stackit_vpc"',
    ):
        assert banned not in text, banned


@pytest.mark.skipif(TERRAFORM is None, reason="terraform not installed")
def test_generated_terraform_is_fmt_clean(fixture_tf: Path) -> None:
    result = subprocess.run(
        [TERRAFORM, "fmt", "-check", "-diff"],
        cwd=fixture_tf,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.terraform
@pytest.mark.skipif(
    TERRAFORM is None or os.environ.get("MIGRATE_TF_VALIDATE") != "1",
    reason="needs terraform + provider download (set MIGRATE_TF_VALIDATE=1)",
)
def test_generated_terraform_validates(fixture_tf: Path) -> None:
    init = subprocess.run(
        [TERRAFORM, "init", "-backend=false", "-input=false", "-no-color"],
        cwd=fixture_tf,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stdout + init.stderr
    result = subprocess.run(
        [TERRAFORM, "validate", "-no-color"],
        cwd=fixture_tf,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_align_hcl_mimics_terraform_fmt() -> None:
    raw = (
        'resource "x" "y" {\n'
        "  project_id = var.project_id\n"
        '  name = "n"\n'
        "  boot_volume = {\n"
        "    size = 1\n"
        '    source_type = "image"\n'
        "  }\n"
        '  availability_zone = "eu01-1"\n'
        '  machine_type = "g2i.1"\n'
        "}\n"
    )
    assert align_hcl(raw) == (
        'resource "x" "y" {\n'
        "  project_id = var.project_id\n"
        '  name       = "n"\n'
        "  boot_volume = {\n"
        "    size        = 1\n"
        '    source_type = "image"\n'
        "  }\n"
        '  availability_zone = "eu01-1"\n'
        '  machine_type      = "g2i.1"\n'
        "}\n"
    )


def test_hcl_strings_cannot_interpolate() -> None:
    assert (
        hcl_string('evil ${file("/etc/passwd")} %{if}')
        == '"evil $${file(\\"/etc/passwd\\")} %%{if}"'
    )


def test_names() -> None:
    assert dns_name("Shop_DB (prod)") == "shop-db-prod"
    assert dns_name("1st") == "r-1st"
    assert ske_name("shop-analytics") == "analytics"
    assert len(ske_name("averyveryverylongclustername")) <= 11


def _plan_with_rule(rule: dict) -> MigrationPlan:
    sg = SourceResource(
        id="sg-1",
        name="web",
        kind=ResourceKind.SECURITY_GROUP,
        source_type="aws:ec2:security-group",
        region="r",
        properties={"rules": [rule], "description": "d"},
    )
    subnet = SourceResource(
        id="subnet-1",
        name="app",
        kind=ResourceKind.SUBNET,
        source_type="aws:ec2:subnet",
        region="r",
        properties={"cidr_block": "10.0.0.0/24"},
    )
    vm = SourceResource(
        id="i-1",
        name="vm",
        kind=ResourceKind.COMPUTE_INSTANCE,
        source_type="aws:ec2:instance",
        region="r",
        properties={"instance_type": "t3.large"},
    )
    mappings = [
        TargetMapping(
            resource_id="sg-1",
            status=MappingStatus.MAPPED,
            terraform_type="stackit_security_group",
        ),
        TargetMapping(
            resource_id="subnet-1",
            status=MappingStatus.MAPPED,
            terraform_type="stackit_network",
            target_spec={"ipv4_prefix": "10.0.0.0/24"},
        ),
        TargetMapping(
            resource_id="i-1",
            status=MappingStatus.MAPPED,
            terraform_type="stackit_server",
            target_spec={
                "machine_type": "g2i.2",
                "network": "subnet-1",
                "security_groups": ["sg-1"],
                "availability_zone": "eu01-1",
                "boot_volume_gb": 20,
            },
        ),
    ]
    return MigrationPlan(
        run_id="20261009-000000-abcd",
        source_cloud=SourceCloud.AWS,
        source_region="r",
        resources=[sg, subnet, vm],
        mappings=mappings,
    )


def test_ipv6_and_unknown_group_rules_become_review_notes() -> None:
    plan = _plan_with_rule(
        {
            "direction": "ingress",
            "protocol": "tcp",
            "port_min": 22,
            "port_max": 22,
            "cidr": "::/0",
        }
    )
    rendered = render(plan)
    assert "IPv6 rule" in rendered.files["security.tf"]
    assert "stackit_security_group_rule" not in rendered.files["security.tf"]
    plan = _plan_with_rule(
        {
            "direction": "ingress",
            "protocol": "tcp",
            "port_min": 5432,
            "port_max": 5432,
            "source_security_group": "sg-unknown",
        }
    )
    assert "not migrated" in render(plan).files["security.tf"]


def test_icmp_and_all_protocol_rules() -> None:
    icmp = render(
        _plan_with_rule(
            {
                "direction": "ingress",
                "protocol": "icmp",
                "port_min": 8,
                "port_max": 0,
                "cidr": "10.0.0.0/8",
            }
        )
    ).files["security.tf"]
    assert "icmp_parameters" in icmp and "type = 8" in icmp
    everything = render(
        _plan_with_rule(
            {
                "direction": "egress",
                "protocol": "all",
                "port_min": None,
                "port_max": None,
                "cidr": "0.0.0.0/0",
            }
        )
    ).files["security.tf"]
    assert "protocol =" not in everything and "port_range" not in everything
