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


# --------------------------------------------------------------------------
# Edge cases the AWS fixture does not cover: NLBs, multi-network targets,
# colliding SKE names, Fargate-only EKS, databases without clients.
# --------------------------------------------------------------------------


def _res(id: str, name: str, kind: ResourceKind, **props: object) -> SourceResource:
    return SourceResource(
        id=id,
        name=name,
        kind=kind,
        source_type=f"aws:{kind.value}",
        region="r",
        properties=props,
    )


def _map(id: str, tf_type: str, **spec: object) -> TargetMapping:
    return TargetMapping(
        resource_id=id,
        status=MappingStatus.MAPPED,
        terraform_type=tf_type,
        target_spec=spec,
    )


def _edge_plan() -> MigrationPlan:
    resources, mappings = [], []
    for i in range(3):
        resources.append(_res(f"subnet-{i}", f"app-{i}", ResourceKind.SUBNET))
        mappings.append(
            _map(f"subnet-{i}", "stackit_network", ipv4_prefix=f"10.0.{i}.0/24")
        )
        name = f"web--{i}" if i else "web--" + "x" * 70  # hyphen run, too long
        resources.append(
            _res(
                f"i-{i}", name, ResourceKind.COMPUTE_INSTANCE, instance_type="t3.large"
            )
        )
        mappings.append(
            _map(
                f"i-{i}",
                "stackit_server",
                machine_type="g2i.2",
                network=f"subnet-{i}",
                availability_zone="eu01-1",
            )
        )
    targets = [{"id": f"i-{i}", "port": 8443} for i in range(3)]
    resources.append(
        _res(
            "nlb-1",
            "edge-nlb",
            ResourceKind.LOAD_BALANCER,
            scheme="internet-facing",
            listeners=[
                {"protocol": "TCP_UDP", "port": 53},
                {"protocol": "TLS", "port": 443, "tls_certificates": ["arn:cert"]},
                {"protocol": "UDP", "port": 514},
            ],
            target_groups=[{"protocol": "TCP", "port": 8443, "targets": targets}],
        )
    )
    mappings.append(_map("nlb-1", "stackit_loadbalancer"))
    resources.append(
        _res(
            "alb-1",
            "edge-alb",
            ResourceKind.LOAD_BALANCER,
            scheme="internet-facing",
            listeners=[{"protocol": "HTTP", "port": 80}],
            target_groups=[
                {
                    "protocol": "HTTPS",
                    "port": 8443,
                    "health_check": {"path": "/up"},
                    "targets": targets,
                }
            ],
        )
    )
    mappings.append(_map("alb-1", "stackit_application_load_balancer"))
    pool = {
        "machine_type": "g2i.2",
        "minimum": 1,
        "maximum": 2,
        "volume_size": 50,
        "availability_zones": ["eu01-1"],
    }
    for id, name, pools in (
        (
            "eks-1",
            "prod-analytics",
            [{**pool, "name": "workers"}, {**pool, "name": "workers"}],
        ),
        ("eks-2", "dev-analytics", [{**pool, "name": "workers"}]),
        ("eks-3", "fargate-only", []),
    ):
        resources.append(_res(id, name, ResourceKind.KUBERNETES_CLUSTER))
        mappings.append(
            _map(
                id,
                "stackit_ske_cluster",
                kubernetes_version_min="1.31",
                node_pools=pools,
            )
        )
    resources.append(_res("db-1", "lonely-db", ResourceKind.POSTGRES))
    mappings.append(
        _map(
            "db-1",
            "stackit_postgresflex_instance",
            version="16",
            storage_gb=20,
            storage_class="premium-perf2-stackit",
            flavor={"id": "2.4", "cpu": 2, "memory_gb": 4, "node_type": "Single"},
        )
    )
    resources.append(_res("db-2", "lonely-maria", ResourceKind.MARIADB))
    mappings.append(
        _map(
            "db-2",
            "stackit_mariadb_instance",
            plan_name="stackit-mariadb-1.4.10-single",
            version="10.11",
            source_engine="mysql",
        )
    )
    return MigrationPlan(
        run_id="20261009-000000-edge",
        source_cloud=SourceCloud.AWS,
        source_region="r",
        resources=resources,
        mappings=mappings,
    )


@pytest.fixture(scope="module")
def edge() -> tuple[MigrationPlan, object]:
    plan = _edge_plan()
    return plan, render(plan)


def test_load_balancers_respect_provider_network_limits(edge) -> None:
    _, rendered = edge
    lbs = {lb["name"]: lb for lb in rendered.model.load_balancers}
    assert len(lbs["edge-nlb"]["network_tfs"]) == 1
    assert len(lbs["edge-nlb"]["targets"]) == 1
    assert len(lbs["edge-alb"]["network_tfs"]) == 2
    assert len(lbs["edge-alb"]["targets"]) == 2
    reviews = "\n".join(rendered.model.reviews)
    assert "edge-nlb: targets" in reviews and "edge-alb: targets" in reviews


def test_nlb_listener_protocols(edge) -> None:
    _, rendered = edge
    nlb = next(lb for lb in rendered.model.load_balancers if lb["name"] == "edge-nlb")
    assert [(li["name"], li["protocol"]) for li in nlb["listeners"]] == [
        ("tcp-53", "PROTOCOL_TCP"),
        ("udp-53", "PROTOCOL_UDP"),
        ("tls-443", "PROTOCOL_TLS_PASSTHROUGH"),
        ("udp-514", "PROTOCOL_UDP"),
    ]
    assert nlb["tls"] is False


def test_alb_https_backends_use_tls_bridging(edge) -> None:
    _, rendered = edge
    lb_tf = rendered.files["loadbalancer.tf"]
    assert "tls_config = {" in lb_tf and "enabled = true" in lb_tf
    assert "custom_ca" in "\n".join(rendered.model.reviews)


def test_ske_names_are_unique_and_fargate_only_is_skipped(edge) -> None:
    _, rendered = edge
    clusters = rendered.model.ske_clusters
    names = [c["name"] for c in clusters]
    assert len(names) == 2 and len(set(names)) == 2
    assert all(len(n) <= 11 for n in names)
    pools = [p["name"] for p in clusters[0]["node_pools"]]
    assert pools == ["workers", "workers2"]
    assert any(s.name == "fargate-only" for s in rendered.model.skipped)


def test_generated_names_fit_provider_validators(edge) -> None:
    _, rendered = edge
    for server in rendered.model.servers:
        assert "--" not in server["name"] and len(server["name"]) <= 63
        assert len(server["nic_name"]) <= 63 and server["nic_name"].endswith("-nic")
    assert dns_name("a--b---c") == "a-b-c"


def test_databases_without_clients_fail_plan_with_a_clear_message(edge) -> None:
    _, rendered = edge
    dbs = rendered.files["databases.tf"]
    assert dbs.count("length(local.lonely_db_acl) > 0") == 1
    assert dbs.count("length(local.lonely_maria_acl) > 0") == 1
    reviews = "\n".join(rendered.model.reviews)
    assert "lonely-db: no migrated client network" in reviews


@pytest.mark.skipif(TERRAFORM is None, reason="terraform not installed")
def test_edge_terraform_is_fmt_clean(edge, tmp_path: Path) -> None:
    _, rendered = edge
    for name, text in rendered.files.items():
        (tmp_path / name).write_text(text)
    result = subprocess.run(
        [TERRAFORM, "fmt", "-check", "-diff"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.terraform
@pytest.mark.skipif(
    TERRAFORM is None or os.environ.get("MIGRATE_TF_VALIDATE") != "1",
    reason="needs terraform + provider download (set MIGRATE_TF_VALIDATE=1)",
)
def test_edge_terraform_validates(edge, tmp_path: Path) -> None:
    _, rendered = edge
    for name, text in rendered.files.items():
        (tmp_path / name).write_text(text)
    for args in (["init", "-backend=false", "-input=false"], ["validate"]):
        result = subprocess.run(
            [TERRAFORM, *args, "-no-color"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
