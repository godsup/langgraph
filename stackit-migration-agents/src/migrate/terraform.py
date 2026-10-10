"""Deterministic Terraform generation from the MigrationPlan.

Jinja2 templates per resource type (`templates/terraform/*.tf.j2`) turn the
plan into HCL for the official `stackitcloud/stackit` provider. No LLM output
is ever written into Terraform: names, sizes and references all come from the
plan. Resources with a ❌ gap are not rendered; they are listed in the
generated README instead.

Every resource type and argument used here was checked against the provider
docs and validated with `terraform validate` against provider v0.119.0.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from migrate.reports import environment
from migrate.state import (
    MappingStatus,
    MigrationPlan,
    ResourceKind,
    SourceResource,
    TargetMapping,
)

PROVIDER_VERSION = "~> 0.119"  # latest release when this was written: v0.119.0
TERRAFORM_VERSION = ">= 1.5.0"

FILES = (
    "versions.tf",
    "variables.tf",
    "network.tf",
    "security.tf",
    "compute.tf",
    "databases.tf",
    "storage.tf",
    "loadbalancer.tf",
    "kubernetes.tf",
    "outputs.tf",
)


# --------------------------------------------------------------------------
# Naming and HCL helpers
# --------------------------------------------------------------------------


def tf_ident(name: str) -> str:
    ident = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not ident or not ident[0].isalpha():
        ident = f"r_{ident}"
    return ident


def dns_name(name: str, max_len: int = 63) -> str:
    """Lowercase DNS-label-style name: ^[a-z]([-a-z0-9]*[a-z0-9])?$."""
    value = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", name.lower())).strip("-")
    if not value or not value[0].isalpha():
        value = f"r-{value}"
    return value[:max_len].rstrip("-")


def ske_name(name: str, limit: int = 11) -> str:
    """SKE cluster names are short (TODO verify the exact limit; the provider's
    acceptance tests use <= 10 characters). Drop leading words until it fits."""
    words = dns_name(name).split("-")
    while len(words) > 1 and len("-".join(words)) > limit:
        words = words[1:]
    return dns_name("-".join(words), limit)


def unique_ske_name(name: str, used: set[str], limit: int = 11) -> str:
    """`ske_name`, made unique: two clusters must never share a name, or the
    second apply would act on the first cluster."""
    candidate, n = ske_name(name, limit), 2
    while candidate in used:
        suffix = f"-{n}"
        candidate, n = ske_name(name, limit - len(suffix)) + suffix, n + 1
    used.add(candidate)
    return candidate


def hcl_string(value: Any) -> str:
    """A safely quoted HCL string literal (no interpolation possible)."""
    return json.dumps(str(value)).replace("${", "$${").replace("%{", "%%{")


def hcl_list(values: list[Any]) -> str:
    return "[" + ", ".join(hcl_string(v) for v in values) + "]"


_ATTR = re.compile(r"^(\s*)([A-Za-z0-9_\-\"().\[\]]+)\s*=\s(.*)$")


def align_hcl(text: str) -> str:
    """Align `=` like `terraform fmt`.

    Consecutive single-line attributes at the same indentation form a group and
    are padded to the longest key. Blank lines, comments, block headers and
    attributes whose value opens a multi-line construct end the group.
    """
    out: list[str] = []
    group: list[tuple[str, str, str]] = []

    def flush() -> None:
        if group:
            width = max(len(key) for _, key, _ in group)
            out.extend(
                f"{indent}{key.ljust(width)} = {value}" for indent, key, value in group
            )
            group.clear()

    for line in text.splitlines():
        match = _ATTR.match(line)
        if match and not _opens_block(match.group(3)):
            indent, key, value = match.groups()
            if group and group[0][0] != indent:
                flush()
            group.append((indent, key, value))
            continue
        flush()
        if match:  # multi-line value: keep a single space before "="
            indent, key, value = match.groups()
            out.append(f"{indent}{key} = {value}")
        else:
            out.append(line.rstrip())
    flush()
    return "\n".join(out) + "\n"


def _opens_block(value: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
    return depth > 0


def _squash_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\{\n\n+", "{\n", text)
    text = re.sub(r"\n\n+(\s*\})", r"\n\1", text)
    return text.strip("\n") + "\n"


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class Skipped:
    name: str
    source_type: str
    reason: str


@dataclass
class TerraformModel:
    run_id: str
    source_cloud: str
    region: str
    networks: list[dict[str, Any]] = field(default_factory=list)
    security_groups: list[dict[str, Any]] = field(default_factory=list)
    servers: list[dict[str, Any]] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)
    postgres: list[dict[str, Any]] = field(default_factory=list)
    mariadb: list[dict[str, Any]] = field(default_factory=list)
    buckets: list[dict[str, Any]] = field(default_factory=list)
    load_balancers: list[dict[str, Any]] = field(default_factory=list)
    ske_clusters: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)

    @property
    def needs_ssh_key(self) -> bool:
        return bool(self.servers)


@dataclass
class RenderedTerraform:
    files: dict[str, str]
    model: TerraformModel

    @property
    def addresses(self) -> list[str]:
        found: list[str] = []
        for content in self.files.values():
            for kind, rtype, name in re.findall(
                r'^(resource|data) "([^"]+)" "([^"]+)"', content, re.M
            ):
                found.append(f"{'data.' if kind == 'data' else ''}{rtype}.{name}")
        return found


class _Names:
    def __init__(self) -> None:
        self._used: dict[str, set[str]] = {}

    def take(self, scope: str, name: str) -> str:
        used = self._used.setdefault(scope, set())
        base = tf_ident(name)
        candidate, n = base, 2
        while candidate in used:
            candidate, n = f"{base}_{n}", n + 1
        used.add(candidate)
        return candidate


def build_model(plan: MigrationPlan) -> TerraformModel:
    model = TerraformModel(
        run_id=plan.run_id,
        source_cloud=plan.source_cloud.value,
        region=plan.target_region,
    )
    names = _Names()
    by_id: dict[str, SourceResource] = {r.id: r for r in plan.resources}
    mappings: dict[str, TargetMapping] = {m.resource_id: m for m in plan.mappings}

    def renderable(res: SourceResource) -> TargetMapping | None:
        mapping = mappings.get(res.id)
        if mapping is None or mapping.terraform_type is None:
            return None
        if mapping.status == MappingStatus.GAP:
            model.skipped.append(
                Skipped(
                    res.name,
                    res.source_type,
                    "no clean STACKIT equivalent (see discovery report)",
                )
            )
            return None
        return mapping

    # Networks: one stackit_network per subnet.
    network_tf: dict[str, str] = {}
    for res in plan.resources:
        if res.kind != ResourceKind.SUBNET or not (m := renderable(res)):
            continue
        tf = names.take("network", res.name)
        network_tf[res.id] = tf
        model.networks.append(
            {
                "tf": tf,
                "name": dns_name(res.name),
                "ipv4_prefix": m.target_spec.get("ipv4_prefix"),
                "source_id": res.id,
                "review": m.status == MappingStatus.PARTIAL,
            }
        )

    # Servers (+ NIC, optional public IP).
    server_tf: dict[str, str] = {}
    used_sgs: set[str] = set()
    for res in plan.resources:
        if res.kind != ResourceKind.COMPUTE_INSTANCE or not (m := renderable(res)):
            continue
        spec = m.target_spec
        net = network_tf.get(spec.get("network") or "")
        if net is None:
            model.skipped.append(
                Skipped(res.name, res.source_type, "its subnet is not migrated")
            )
            continue
        tf = names.take("server", res.name)
        server_tf[res.id] = tf
        sgs = [sg for sg in spec.get("security_groups", []) if sg in by_id]
        used_sgs.update(sgs)
        model.servers.append(
            {
                "tf": tf,
                "name": dns_name(res.name),
                "nic_name": dns_name(res.name, 59) + "-nic",
                "machine_type": spec["machine_type"],
                "availability_zone": spec.get("availability_zone", "eu01-1"),
                "boot_volume_gb": max(int(spec.get("boot_volume_gb") or 20), 20),
                "network_tf": net,
                "security_group_ids": sgs,
                "public_ip": spec.get("public_ip", False),
                "source_id": res.id,
                "source_type": res.properties.get("instance_type"),
                "review": m.status == MappingStatus.PARTIAL,
                "decided_by": m.decided_by.value,
            }
        )

    # Security groups: only those attached to migrated servers or referenced by them.
    sg_tf: dict[str, str] = {}
    pending = [
        sg
        for sg in plan.resources
        if sg.kind == ResourceKind.SECURITY_GROUP and sg.id in used_sgs
    ]
    referenced = {
        rule.get("source_security_group")
        for sg in pending
        for rule in sg.properties.get("rules", [])
        if rule.get("source_security_group")
    }
    for res in plan.resources:
        if res.kind != ResourceKind.SECURITY_GROUP:
            continue
        if res.id not in used_sgs and res.id not in referenced:
            model.skipped.append(
                Skipped(
                    res.name,
                    res.source_type,
                    "not attached to a migrated server "
                    "(managed databases use ACLs, SKE and load balancers manage their own groups)",
                )
            )
            continue
        if renderable(res):
            sg_tf[res.id] = names.take("security_group", res.name)
    for res in plan.resources:
        if res.id in sg_tf:
            model.security_groups.append(_security_group(res, sg_tf, by_id, model))
    for server in model.servers:
        server["security_group_tfs"] = [
            sg_tf[s] for s in server.pop("security_group_ids") if s in sg_tf
        ]

    # Data volumes (root volumes are the servers' boot volumes).
    for res in plan.resources:
        if res.kind != ResourceKind.BLOCK_VOLUME or not (m := renderable(res)):
            continue
        attach = m.target_spec.get("attach_to")
        if attach and attach not in server_tf:
            model.skipped.append(
                Skipped(
                    res.name,
                    res.source_type,
                    "attached to a server that is not migrated",
                )
            )
            continue
        tf = names.take("volume", res.name)
        model.volumes.append(
            {
                "tf": tf,
                "name": dns_name(res.name),
                "size": res.properties.get("size_gib"),
                "availability_zone": m.target_spec.get("availability_zone", "eu01-1"),
                "performance_class": m.target_spec.get("performance_class"),
                "server_tf": server_tf.get(attach) if attach else None,
                "source_id": res.id,
                "review": m.status == MappingStatus.PARTIAL,
            }
        )

    # SKE clusters before databases (databases may allow SKE egress).
    ske_names: set[str] = set()
    for res in plan.resources:
        if res.kind != ResourceKind.KUBERNETES_CLUSTER or not (m := renderable(res)):
            continue
        spec = m.target_spec
        pools = _node_pools(spec.get("node_pools", []))
        if not pools:
            model.skipped.append(
                Skipped(
                    res.name,
                    res.source_type,
                    "no node group with a STACKIT machine type (Fargate-only clusters "
                    "have no node pools to create)",
                )
            )
            continue
        name = unique_ske_name(res.name, ske_names)
        if name != ske_name(res.name):
            model.reviews.append(
                f"{res.name}: SKE name {ske_name(res.name)!r} is already used by "
                f"another cluster; renamed to {name!r}"
            )
        version = spec.get("kubernetes_version_min") or ""
        model.ske_clusters.append(
            {
                "tf": names.take("ske", res.name),
                "name": name,
                "source_name": res.name,
                "kubernetes_version_min": ".".join(version.split(".")[:2]) or None,
                "node_pools": pools,
                "source_id": res.id,
                "review": m.status == MappingStatus.PARTIAL,
            }
        )

    # Databases.
    for res in plan.resources:
        if res.kind not in (
            ResourceKind.POSTGRES,
            ResourceKind.MYSQL,
            ResourceKind.MARIADB,
        ):
            continue
        if not (m := renderable(res)):
            continue
        spec = m.target_spec
        clients = [
            network_tf[s] for s in spec.get("client_networks", []) if s in network_tf
        ]
        k8s = set(spec.get("kubernetes_clients", []))
        ske_tfs = [c["tf"] for c in model.ske_clusters if c["source_name"] in k8s]
        if not clients and not ske_tfs:
            model.reviews.append(
                f"{res.name}: no migrated client network or SKE cluster was found, so its "
                "ACL is empty; set var.extra_db_allowed_cidrs or terraform plan stops"
            )
        common = {
            "source_id": res.id,
            "review": m.status == MappingStatus.PARTIAL,
            "client_network_tfs": clients,
            "ske_tfs": ske_tfs,
        }
        if m.terraform_type == "stackit_postgresflex_instance":
            flavor = spec.get("flavor")
            if not flavor:
                model.skipped.append(
                    Skipped(
                        res.name, res.source_type, "no PostgreSQL Flex flavor selected"
                    )
                )
                continue
            db_name = re.sub(
                r"[^a-z0-9_]", "_", (spec.get("db_name") or res.name).lower()
            )
            model.postgres.append(
                {
                    **common,
                    "tf": names.take("postgres", res.name),
                    "name": dns_name(res.name),
                    "flavor": flavor,
                    "version": spec.get("version"),
                    "storage_gb": spec.get("storage_gb") or 20,
                    "storage_class": spec.get("storage_class"),
                    "backup_schedule": spec.get("backup_schedule") or "0 2 * * *",
                    "db_name": db_name,
                    "owner": f"{db_name}_owner",
                }
            )
        elif m.terraform_type == "stackit_mariadb_instance":
            model.mariadb.append(
                {
                    **common,
                    "tf": names.take("mariadb", res.name),
                    "name": dns_name(res.name),
                    "plan_name": spec.get("plan_name"),
                    "version": spec.get("version"),
                    "source_engine": spec.get("source_engine"),
                }
            )

    # Object storage.
    for res in plan.resources:
        if res.kind != ResourceKind.OBJECT_BUCKET or not (m := renderable(res)):
            continue
        model.buckets.append(
            {
                "tf": names.take("bucket", res.name),
                "name": dns_name(m.target_spec.get("bucket_name") or res.name),
                "source_name": res.name,
                "source_id": res.id,
                "review": m.status == MappingStatus.PARTIAL,
            }
        )

    # Load balancers.
    for res in plan.resources:
        if res.kind != ResourceKind.LOAD_BALANCER or not (m := renderable(res)):
            continue
        lb = _load_balancer(res, m, server_tf, model, names)
        if lb:
            model.load_balancers.append(lb)
    return model


def _security_group(
    res: SourceResource,
    sg_tf: dict[str, str],
    by_id: dict[str, SourceResource],
    model: TerraformModel,
) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    notes: list[str] = []
    for rule in res.properties.get("rules", []):
        protocol = rule.get("protocol")
        entry: dict[str, Any] = {
            "direction": rule["direction"],
            "description": rule.get("description") or "",
        }
        cidr = rule.get("cidr")
        if cidr and ":" in cidr:
            notes.append(
                f"IPv6 rule {rule['direction']} {protocol} {cidr} not migrated automatically"
            )
            continue
        if rule.get("prefix_list"):
            notes.append(
                f"prefix-list rule {rule['prefix_list']} not migrated (no STACKIT equivalent)"
            )
            continue
        if cidr:
            entry["ip_range"] = cidr
        elif rule.get("source_security_group"):
            remote = rule["source_security_group"]
            if remote == res.id:
                entry["remote_tf"] = sg_tf[res.id]
            elif remote in sg_tf:
                entry["remote_tf"] = sg_tf[remote]
            else:
                name = by_id[remote].name if remote in by_id else remote
                notes.append(
                    f"{rule['direction']} {protocol} {rule.get('port_min')} from security group {name} "
                    "not migrated (that group is not migrated)"
                )
                continue
        if protocol not in (None, "all", "-1"):
            entry["protocol"] = protocol
            if protocol in ("tcp", "udp") and rule.get("port_min") is not None:
                entry["port_min"], entry["port_max"] = (
                    rule["port_min"],
                    rule["port_max"],
                )
            elif protocol == "icmp" and rule.get("port_min") not in (None, -1):
                entry["icmp_type"] = rule["port_min"]
                entry["icmp_code"] = max(int(rule.get("port_max") or 0), 0)
        rules.append(entry)
    for i, rule in enumerate(rules, 1):
        rule["tf"] = f"{sg_tf[res.id]}_{rule['direction']}_{i}"
    if notes:
        model.reviews.extend(f"{res.name}: {n}" for n in notes)
    return {
        "tf": sg_tf[res.id],
        "name": dns_name(res.name),
        "description": res.properties.get("description", ""),
        "rules": rules,
        "notes": notes,
        "source_id": res.id,
    }


def _node_pools(pools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Node pools with a machine type, with names unique within the cluster."""
    used: set[str] = set()
    out = []
    for pool in pools:
        if not pool.get("machine_type"):
            continue
        base = pool.get("name") or "pool"
        name, n = base, 2
        while name in used:
            name, n = f"{base[: 15 - len(str(n))]}{n}", n + 1
        used.add(name)
        out.append(
            {
                **pool,
                "name": name,
                "max_surge": len(pool.get("availability_zones") or ["eu01-1"]),
            }
        )
    return out


# Provider limits (checked in the v0.119.0 schema): the network load balancer
# takes exactly one network, the application load balancer one or two.
MAX_LB_NETWORKS = {"stackit_loadbalancer": 1, "stackit_application_load_balancer": 2}

# AWS NLB listener protocol -> STACKIT network load balancer protocols.
NLB_PROTOCOLS = {
    "TCP": ["PROTOCOL_TCP"],
    "UDP": ["PROTOCOL_UDP"],
    "TCP_UDP": ["PROTOCOL_TCP", "PROTOCOL_UDP"],
    "TLS": ["PROTOCOL_TLS_PASSTHROUGH"],
    "HTTP": ["PROTOCOL_TCP"],
    "HTTPS": ["PROTOCOL_TLS_PASSTHROUGH"],
}


def _load_balancer(
    res: SourceResource,
    m: TargetMapping,
    server_tf: dict[str, str],
    model: TerraformModel,
    names: _Names,
) -> dict[str, Any] | None:
    props = res.properties
    groups = props.get("target_groups", [])
    targets = [t for g in groups for t in g.get("targets", [])]
    migrated = [t for t in targets if t["id"] in server_tf]
    if not migrated:
        model.skipped.append(
            Skipped(res.name, res.source_type, "none of its targets are migrated")
        )
        return None
    servers = {s["tf"]: s for s in model.servers}
    notes = []

    # Keep the networks with the most targets, up to the provider limit.
    by_network: dict[str, list[dict[str, Any]]] = {}
    for t in migrated:
        by_network.setdefault(servers[server_tf[t["id"]]]["network_tf"], []).append(t)
    ranked = sorted(by_network, key=lambda net: (-len(by_network[net]), net))
    limit = MAX_LB_NETWORKS.get(m.terraform_type or "", 1)
    kept, dropped = ranked[:limit], ranked[limit:]
    if dropped:
        lost = sorted(
            servers[server_tf[t["id"]]]["name"]
            for net in dropped
            for t in by_network[net]
        )
        notes.append(
            f"targets {', '.join(lost)} are not added: this STACKIT load balancer type "
            f"supports {limit} network(s) and they sit in another one. Route that network "
            "or move the servers, then add them to the target pool."
        )
    migrated = [
        t for t in migrated if servers[server_tf[t["id"]]]["network_tf"] in kept
    ]

    target_port = next((t.get("port") for t in migrated if t.get("port")), None) or (
        groups[0].get("port") if groups else 80
    )
    health = (groups[0].get("health_check") or {}) if groups else {}
    listeners = props.get("listeners", [])
    tls = [li for li in listeners if li.get("tls_certificates")]
    redirects = [
        li for li in listeners if "redirect" in (li.get("default_actions") or [])
    ]
    if redirects:
        notes.append(
            f"listener {redirects[0]['protocol']}:{redirects[0]['port']} redirects on AWS; the STACKIT ALB has no "
            "redirect action. Handle HTTP->HTTPS in the application or leave the port closed."
        )
    lb = {
        "tf": names.take("load_balancer", res.name),
        "name": dns_name(res.name),
        "type": m.terraform_type,
        "public": props.get("scheme") != "internal",
        "target_port": target_port,
        "health_path": health.get("path") or "/",
        "targets": [
            {
                "server_tf": server_tf[t["id"]],
                "name": servers[server_tf[t["id"]]]["name"],
            }
            for t in migrated
        ],
        "network_tfs": kept,
        "tls": False,
        "backend_tls": False,
        "source_id": res.id,
        "notes": notes,
        "review": m.status == MappingStatus.PARTIAL,
    }
    if m.terraform_type == "stackit_application_load_balancer":
        lb["listeners"] = [
            {
                "name": f"{li['protocol'].lower()}-{li['port']}",
                "port": li["port"],
                "protocol": "PROTOCOL_HTTPS"
                if li.get("tls_certificates")
                else "PROTOCOL_HTTP",
            }
            for li in listeners
            if "redirect" not in (li.get("default_actions") or [])
        ]
        lb["tls"] = bool(tls)
        if any((g.get("protocol") or "").upper() == "HTTPS" for g in groups):
            lb["backend_tls"] = True
            notes.append(
                "targets are reached over HTTPS (TLS bridging, public CAs trusted). For private or "
                "self-signed backend certificates add tls_config.custom_ca."
            )
    else:
        lb["listeners"] = []
        seen: set[str] = set()
        for li in listeners:
            source = (li.get("protocol") or "TCP").upper()
            protocols = NLB_PROTOCOLS.get(source)
            if protocols is None:
                notes.append(
                    f"listener {source}:{li['port']} has no STACKIT network load balancer "
                    "equivalent and is not migrated"
                )
                continue
            for protocol in protocols:
                short = protocol.removeprefix("PROTOCOL_").split("_")[0].lower()
                name = f"{short}-{li['port']}"
                if name in seen:
                    continue
                seen.add(name)
                lb["listeners"].append(
                    {"name": name, "port": li["port"], "protocol": protocol}
                )
        if tls or any(
            (li.get("protocol") or "").upper() in ("TLS", "HTTPS") for li in listeners
        ):
            notes.append(
                "TLS terminated on the AWS load balancer is passed through to the targets on STACKIT (L4)."
            )
    model.reviews.extend(f"{res.name}: {n}" for n in notes)
    return lb


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(plan: MigrationPlan) -> RenderedTerraform:
    model = build_model(plan)
    env = environment()
    env.filters.update(hcl=hcl_string, hcl_list=hcl_list)
    files: dict[str, str] = {}
    context = {
        "m": model,
        "provider_version": PROVIDER_VERSION,
        "terraform_version": TERRAFORM_VERSION,
    }
    for name in FILES:
        text = env.get_template(f"terraform/{name}.j2").render(**context)
        text = _squash_blank_lines(text)
        if text.strip():
            files[name] = align_hcl(text)
    files["README.md"] = env.get_template("terraform/README.md.j2").render(**context)
    return RenderedTerraform(files=files, model=model)
