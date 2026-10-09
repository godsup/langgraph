"""Map source resources to STACKIT targets.

Deterministic first: `config/mapping.yaml` decides the target service, and
sizing picks the smallest STACKIT offering that fits. The LLM is only asked
when a choice is genuinely ambiguous (several equally small offerings, or an
application load balancer), and it can only pick one of the offered
candidates; anything else falls back to the rule's default and is flagged for
human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from migrate.llm import LLMGateway
from migrate.state import (
    DecidedBy,
    Gap,
    GapSeverity,
    MappingStatus,
    ResourceKind,
    SourceResource,
    TargetMapping,
)

_STATUS_ORDER = {
    MappingStatus.MAPPED: 0,
    MappingStatus.PARTIAL: 1,
    MappingStatus.GAP: 2,
}
# AWS AZ suffix -> STACKIT eu01 AZ. TODO(verify): eu01-2/eu01-3 for servers and
# volumes (documented for SKE; eu01-1 is documented for servers and volumes).
_AZ_SUFFIX = {"a": "eu01-1", "b": "eu01-2", "c": "eu01-3"}


@dataclass
class Catalog:
    source: str
    machine_types: list[dict[str, Any]] = field(default_factory=list)
    postgres_flavors: list[dict[str, Any]] = field(default_factory=list)
    postgres_versions: list[str] = field(default_factory=list)
    mariadb_plans: list[dict[str, Any]] = field(default_factory=list)
    ske_kubernetes_versions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        return cls(
            source=data.get("source", "unknown"),
            machine_types=data.get("machine_types", []),
            postgres_flavors=data.get("postgres_flavors", []),
            postgres_versions=[str(v) for v in data.get("postgres_versions", [])],
            mariadb_plans=data.get("mariadb_plans", []),
            ske_kubernetes_versions=[
                str(v) for v in data.get("ske_kubernetes_versions", [])
            ],
        )


class MappingChoice(BaseModel):
    """LLM output for an ambiguous mapping. `choice` must be a candidate."""

    choice: str = Field(description="Exactly one of the candidate identifiers.")
    rationale: str = Field(description="One or two sentences for the migration report.")


@dataclass
class _Pending:
    mapping: TargetMapping
    candidates: list[dict[str, Any]]
    spec_key: str  # key the chosen candidate goes into ("terraform_type" = the mapping itself)
    question: str
    llm_key: str
    target: dict[str, Any] | None = None  # dict to update; default mapping.target_spec


def stackit_az(aws_az: str | None) -> str:
    if not aws_az:
        return "eu01-1"
    return _AZ_SUFFIX.get(aws_az[-1], "eu01-1")


def _major(version: str | None) -> str | None:
    if not version:
        return None
    match = re.match(r"(\d+)", version)
    return match.group(1) if match else None


def _minor_version(version: str | None) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version or "")[:2])


def _matches(resource: SourceResource, when: dict[str, Any]) -> bool:
    for key, expected in when.items():
        actual = resource.kind.value if key == "kind" else resource.properties.get(key)
        options = expected if isinstance(expected, list) else [expected]
        if actual not in options:
            return False
    return True


class MappingEngine:
    def __init__(self, config: dict[str, Any], catalog: Catalog) -> None:
        self.kinds: dict[str, Any] = config.get("kinds", {})
        self.rules: list[dict[str, Any]] = config.get("rules", [])
        self.catalog = catalog

    # ------------------------------------------------------------ public

    async def map_all(
        self, resources: list[SourceResource], llm: LLMGateway, *, agent: str
    ) -> tuple[list[TargetMapping], list[Gap]]:
        mappings: list[TargetMapping] = []
        gaps: list[Gap] = []
        pending: list[_Pending] = []
        for resource in resources:
            mapping, resource_gaps, choices = self.map_one(resource, resources)
            mappings.append(mapping)
            gaps.extend(resource_gaps)
            pending.extend(choices)
        for item in pending:
            await self._decide(item, llm, agent=agent, resources=resources)
        return mappings, gaps

    def map_one(
        self, resource: SourceResource, inventory: list[SourceResource]
    ) -> tuple[TargetMapping, list[Gap], list[_Pending]]:
        cfg = self._kind_config(resource)
        mapping = TargetMapping(
            resource_id=resource.id,
            status=MappingStatus(cfg.get("status", "gap")),
            target_service=cfg.get("target_service"),
            terraform_type=cfg.get("terraform_type"),
            rationale=cfg.get("note", "").strip(),
        )
        gaps: list[Gap] = []
        pending: list[_Pending] = []
        if cfg.get("reason"):
            mapping.needs_review = mapping.status != MappingStatus.MAPPED
            gaps.append(
                Gap(
                    resource_id=resource.id,
                    reason=" ".join(str(cfg["reason"]).split()),
                    severity=GapSeverity(cfg.get("severity", "medium")),
                    suggestion=cfg.get("suggestion", ""),
                )
            )
        if mapping.status != MappingStatus.GAP:
            pending += self._size(resource, cfg, mapping, gaps, inventory)
        for rule in self.rules:
            if _matches(resource, rule.get("when", {})):
                self._escalate(mapping, MappingStatus(rule.get("status", "partial")))
                gaps.append(
                    Gap(
                        resource_id=resource.id,
                        reason=rule["reason"],
                        severity=GapSeverity(rule.get("severity", "medium")),
                        suggestion=rule.get("suggestion", ""),
                    )
                )
        if mapping.status != MappingStatus.MAPPED:
            mapping.needs_review = True
        return mapping, gaps, pending

    # ----------------------------------------------------------- helpers

    def _kind_config(self, resource: SourceResource) -> dict[str, Any]:
        cfg = dict(
            self.kinds.get(resource.kind.value)
            or self.kinds.get("other")
            or {"status": "gap"}
        )
        props = resource.properties
        if resource.kind == ResourceKind.COMPUTE_INSTANCE and props.get("eks_cluster"):
            return {**cfg.get("managed_by_kubernetes", {}), "sizing": None}
        if resource.kind == ResourceKind.BLOCK_VOLUME and props.get("is_root"):
            return dict(cfg.get("root_volume", {}))
        if resource.kind == ResourceKind.LOAD_BALANCER:
            lb_cfg = cfg.get(props.get("lb_type") or "")
            if lb_cfg is None:
                return {
                    "status": "gap",
                    "reason": f"Load balancer type {props.get('lb_type')!r} has no mapping.",
                    "severity": "high",
                }
            return dict(lb_cfg)
        return cfg

    @staticmethod
    def _escalate(mapping: TargetMapping, status: MappingStatus) -> None:
        if _STATUS_ORDER[status] > _STATUS_ORDER[mapping.status]:
            mapping.status = status

    def _size(
        self,
        resource: SourceResource,
        cfg: dict[str, Any],
        mapping: TargetMapping,
        gaps: list[Gap],
        inventory: list[SourceResource],
    ) -> list[_Pending]:
        props = resource.properties
        kind = resource.kind
        if (
            kind == ResourceKind.COMPUTE_INSTANCE
            and cfg.get("sizing") == "machine_type"
        ):
            mapping.target_spec.update(
                {
                    "availability_zone": stackit_az(props.get("availability_zone")),
                    "boot_volume_gb": _root_volume_size(resource, inventory),
                    "os_family": props.get("platform", "linux"),
                    "network": props.get("subnet_id"),
                    "security_groups": props.get("security_group_ids", []),
                    "public_ip": bool(props.get("public_ip")),
                }
            )
            return self._machine_type(
                resource.id, props, mapping, gaps, key="machine_type"
            )
        if kind == ResourceKind.BLOCK_VOLUME and mapping.terraform_type:
            attachments = props.get("attachments") or []
            mapping.target_spec.update(
                {
                    "size_gb": props.get("size_gib"),
                    "performance_class": (cfg.get("performance_class") or {}).get(
                        props.get("volume_type")
                    ),
                    "availability_zone": stackit_az(props.get("availability_zone")),
                    "attach_to": attachments[0]["instance_id"] if attachments else None,
                }
            )
        elif kind == ResourceKind.SUBNET:
            mapping.target_spec.update(
                {"ipv4_prefix": props.get("cidr_block"), "vpc": props.get("vpc_id")}
            )
        elif kind == ResourceKind.SECURITY_GROUP:
            mapping.target_spec.update({"rule_count": len(props.get("rules", []))})
        elif kind == ResourceKind.POSTGRES:
            self._postgres(resource, cfg, mapping, gaps, inventory)
        elif kind in (ResourceKind.MYSQL, ResourceKind.MARIADB):
            self._mariadb(resource, mapping, gaps)
        elif kind == ResourceKind.OBJECT_BUCKET:
            mapping.target_spec.update(
                {
                    "bucket_name": resource.name,
                    "size_bytes": props.get("size_bytes"),
                    "object_count": props.get("object_count"),
                }
            )
        elif kind == ResourceKind.LOAD_BALANCER:
            mapping.target_spec.update(
                {
                    "listeners": props.get("listeners", []),
                    "targets": resource.depends_on,
                    "scheme": props.get("scheme"),
                }
            )
            if cfg.get("candidates"):
                return [
                    _Pending(
                        mapping=mapping,
                        candidates=cfg["candidates"],
                        spec_key="terraform_type",
                        question="Which STACKIT load balancer should replace this AWS load balancer?",
                        llm_key=resource.id,
                    )
                ]
        elif kind == ResourceKind.KUBERNETES_CLUSTER:
            return self._ske(resource, mapping, gaps)
        return []

    def _fitting_machine_types(
        self, vcpus: int, memory_mib: int
    ) -> list[dict[str, Any]]:
        fits = [
            mt
            for mt in self.catalog.machine_types
            if mt["vcpus"] >= vcpus and mt["ram_mb"] >= memory_mib
        ]
        return sorted(fits, key=lambda mt: (mt["vcpus"], mt["ram_mb"], mt["name"]))

    def _machine_type(
        self,
        resource_id: str,
        props: dict[str, Any],
        mapping: TargetMapping,
        gaps: list[Gap],
        *,
        key: str,
        llm_key: str | None = None,
        target: dict[str, Any] | None = None,
    ) -> list[_Pending]:
        target = mapping.target_spec if target is None else target
        vcpus, memory = props.get("vcpus"), props.get("memory_mib")
        if not vcpus or not memory:
            self._escalate(mapping, MappingStatus.PARTIAL)
            gaps.append(
                Gap(
                    resource_id=resource_id,
                    reason=f"Size of {props.get('instance_type') or 'instance'} is unknown; machine type must be chosen manually.",
                    severity=GapSeverity.MEDIUM,
                )
            )
            return []
        fits = self._fitting_machine_types(int(vcpus), int(memory))
        if not fits:
            self._escalate(mapping, MappingStatus.GAP)
            gaps.append(
                Gap(
                    resource_id=resource_id,
                    reason=f"No STACKIT machine type with >= {vcpus} vCPU and >= {memory} MiB RAM in the catalogue.",
                    severity=GapSeverity.HIGH,
                )
            )
            return []
        smallest = [mt for mt in fits if mt["vcpus"] == fits[0]["vcpus"]][:3]
        target[key] = smallest[0]["name"]
        target[f"{key}_spec"] = {
            "vcpus": smallest[0]["vcpus"],
            "ram_mb": smallest[0]["ram_mb"],
        }
        source = props.get("instance_type") or ", ".join(
            props.get("instance_types", [])
        )
        mapping.rationale = (
            f"{source} ({vcpus} vCPU, {memory} MiB) -> {smallest[0]['name']} "
            f"({smallest[0]['vcpus']} vCPU, {smallest[0]['ram_mb']} MB): smallest fitting machine type."
        )
        if len(smallest) == 1:
            return []
        mapping.rationale = (
            f"{source} ({vcpus} vCPU, {memory} MiB): {len(smallest)} STACKIT machine types fit with "
            f"{smallest[0]['vcpus']} vCPU ({', '.join(mt['name'] for mt in smallest)})."
        )
        return [
            _Pending(
                mapping=mapping,
                candidates=[
                    {
                        "terraform_type": mt["name"],
                        "target_service": f"{mt['vcpus']} vCPU / {mt['ram_mb']} MB",
                        **mt,
                    }
                    for mt in smallest
                ],
                spec_key=key,
                question=(
                    f"Several STACKIT machine types fit {source} ({vcpus} vCPU, {memory} MiB). "
                    "Which one suits this workload best?"
                ),
                llm_key=llm_key or resource_id,
                target=target,
            )
        ]

    def _postgres(
        self,
        resource: SourceResource,
        cfg: dict[str, Any],
        mapping: TargetMapping,
        gaps: list[Gap],
        inventory: list[SourceResource],
    ) -> None:
        props = resource.properties
        node_type = "Replica" if props.get("multi_az") else "Single"
        vcpus, memory_gb = (
            props.get("vcpus") or 0,
            (props.get("memory_mib") or 0) / 1024,
        )
        fits = sorted(
            (
                f
                for f in self.catalog.postgres_flavors
                if f["node_type"].lower() == node_type.lower()
                and f["cpu"] >= vcpus
                and f["memory_gb"] >= memory_gb
            ),
            key=lambda f: (f["cpu"], f["memory_gb"]),
        )
        if fits:
            mapping.target_spec["flavor"] = fits[0]
            mapping.rationale = (
                f"{props.get('instance_class')} ({vcpus} vCPU, {memory_gb:g} GiB, "
                f"{'Multi-AZ' if props.get('multi_az') else 'single-AZ'}) -> flavor {fits[0]['id']} "
                f"({fits[0]['cpu']} CPU, {fits[0]['memory_gb']} GiB, {node_type})."
            )
        else:
            self._escalate(mapping, MappingStatus.PARTIAL)
            gaps.append(
                Gap(
                    resource_id=resource.id,
                    reason=f"No PostgreSQL Flex {node_type} flavor with >= {vcpus} CPU / {memory_gb:g} GiB in the catalogue.",
                    severity=GapSeverity.MEDIUM,
                )
            )
        major = _major(props.get("engine_version"))
        versions = sorted(
            self.catalog.postgres_versions, key=lambda v: int(_major(v) or 0)
        )
        if major and major in versions:
            mapping.target_spec["version"] = major
        else:
            newer = [v for v in versions if int(_major(v) or 0) > int(major or 0)]
            if newer:
                mapping.target_spec["version"] = newer[0]
                self._escalate(mapping, MappingStatus.PARTIAL)
                gaps.append(
                    Gap(
                        resource_id=resource.id,
                        reason=f"PostgreSQL {major} is not offered; target is {newer[0]} (major upgrade).",
                        severity=GapSeverity.MEDIUM,
                        suggestion="Test the application against the new major version before cutover.",
                    )
                )
            else:
                self._escalate(mapping, MappingStatus.GAP)
                gaps.append(
                    Gap(
                        resource_id=resource.id,
                        reason=f"No supported PostgreSQL Flex version for source version {props.get('engine_version')}.",
                        severity=GapSeverity.HIGH,
                    )
                )
        mapping.target_spec.update(
            {
                "storage_gb": props.get("allocated_storage_gb"),
                "storage_class": cfg.get("storage_class"),
                "backup_schedule": cfg.get("backup_schedule"),
                "db_name": props.get("db_name"),
                "acl": _app_cidrs(resource, inventory),
            }
        )

    def _mariadb(
        self, resource: SourceResource, mapping: TargetMapping, gaps: list[Gap]
    ) -> None:
        props = resource.properties
        want = "replica" if props.get("multi_az") else "single"
        plans = self.catalog.mariadb_plans
        chosen = next(
            (p for p in plans if want in p["name"].lower()), plans[0] if plans else None
        )
        if chosen is None:
            self._escalate(mapping, MappingStatus.GAP)
            gaps.append(
                Gap(
                    resource_id=resource.id,
                    reason="No MariaDB plans in the catalogue.",
                    severity=GapSeverity.HIGH,
                )
            )
            return
        mapping.target_spec.update(
            {
                "plan_name": chosen["name"],
                "version": chosen.get("version"),
                "source_engine": f"{props.get('engine')} {props.get('engine_version')}",
                "db_name": props.get("db_name"),
            }
        )
        mapping.needs_review = (
            True  # plan sizes are not directly comparable to instance classes
        )
        mapping.rationale = (
            f"{props.get('engine')} {props.get('engine_version')} ({props.get('instance_class')}) -> "
            f"MariaDB plan {chosen['name']}; plan size must be confirmed by a human."
        )

    def _ske(
        self, resource: SourceResource, mapping: TargetMapping, gaps: list[Gap]
    ) -> list[_Pending]:
        props = resource.properties
        version = props.get("kubernetes_version")
        offered = sorted(self.catalog.ske_kubernetes_versions, key=_minor_version)
        target_version = next(
            (v for v in offered if _minor_version(v) >= _minor_version(version)), None
        )
        if target_version is None:
            self._escalate(mapping, MappingStatus.GAP)
            gaps.append(
                Gap(
                    resource_id=resource.id,
                    reason=f"No SKE Kubernetes version >= {version} in the catalogue.",
                    severity=GapSeverity.HIGH,
                )
            )
        elif _minor_version(target_version) != _minor_version(version):
            self._escalate(mapping, MappingStatus.PARTIAL)
            gaps.append(
                Gap(
                    resource_id=resource.id,
                    reason=f"Kubernetes {version} is not offered on SKE; target is {target_version}.",
                    severity=GapSeverity.MEDIUM,
                    suggestion="Check deprecated APIs in manifests/Helm charts before moving workloads.",
                )
            )
        pools: list[dict[str, Any]] = []
        pending: list[_Pending] = []
        for ng in props.get("node_groups", []):
            pool: dict[str, Any] = {
                "name": re.sub(r"[^a-z0-9]", "", ng["name"].lower())[:15] or "pool",
                "minimum": max(int(ng.get("min_size") or 1), 1),
                "maximum": max(
                    int(ng.get("max_size") or 1), int(ng.get("min_size") or 1), 1
                ),
                "desired": ng.get("desired_size") or ng.get("min_size") or 1,
                "volume_size": ng.get("disk_size_gib") or 50,
                "source_instance_types": ng.get("instance_types", []),
            }
            pending += self._machine_type(
                resource.id,
                ng,
                mapping,
                gaps,
                key="machine_type",
                llm_key=f"{resource.id}:{ng['name']}",
                target=pool,
            )
            pools.append(pool)
        mapping.target_spec.update(
            {"kubernetes_version_min": target_version, "node_pools": pools}
        )
        mapping.rationale = f"EKS {version} -> SKE {target_version}; {len(pools)} node pool(s) sized from EKS node groups."
        return pending

    async def _decide(
        self,
        item: _Pending,
        llm: LLMGateway,
        *,
        agent: str,
        resources: list[SourceResource],
    ) -> None:
        mapping = item.mapping
        resource = next(r for r in resources if r.id == mapping.resource_id)
        names = [c["terraform_type"] for c in item.candidates]
        default = item.candidates[0]
        options = "\n".join(
            f"- {c['terraform_type']}: {c.get('target_service', '')} {c.get('note', '')}".rstrip()
            for c in item.candidates
        )
        decision = await llm.decide(
            agent=agent,
            task="mapping_choice",
            key=item.llm_key,
            system=(
                "You are a cloud migration architect mapping AWS resources to STACKIT. "
                "Choose exactly one of the candidates. Answer with its identifier and a short rationale."
            ),
            prompt=(
                f"{item.question}\n\nResource: {resource.name} ({resource.source_type})\n"
                f"Tags: {resource.tags}\nProperties: {_summary(resource.properties)}\n\n"
                f"Candidates:\n{options}"
            ),
            schema=MappingChoice,
            fallback=MappingChoice(
                choice=default["terraform_type"],
                rationale="Default rule: first candidate.",
            ),
        )
        choice = decision.value
        chosen = next(
            (c for c in item.candidates if c["terraform_type"] == choice.choice), None
        )
        if chosen is None:
            chosen, fallback = default, True
            rationale = f"LLM answer {choice.choice!r} is not a candidate ({', '.join(names)}); default used."
        else:
            fallback = decision.fallback_used
            rationale = choice.rationale
        if item.spec_key == "terraform_type":
            mapping.terraform_type = chosen["terraform_type"]
            mapping.target_service = chosen.get(
                "target_service", mapping.target_service
            )
        else:
            holder = item.target if item.target is not None else mapping.target_spec
            holder[item.spec_key] = chosen["terraform_type"]
            if "vcpus" in chosen:
                holder[f"{item.spec_key}_spec"] = {
                    "vcpus": chosen["vcpus"],
                    "ram_mb": chosen["ram_mb"],
                }
        mapping.decided_by = DecidedBy.FALLBACK if fallback else DecidedBy.LLM
        mapping.rationale = f"{mapping.rationale} Choice: {chosen['terraform_type']}. {rationale}".strip()
        if fallback:
            mapping.needs_review = True


def _summary(props: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "instance_type",
        "vcpus",
        "memory_mib",
        "platform",
        "architecture",
        "lb_type",
        "scheme",
        "listeners",
        "target_groups",
        "engine",
        "engine_version",
        "multi_az",
        "kubernetes_version",
        "instance_types",
        "min_size",
        "max_size",
    )
    return {k: v for k, v in props.items() if k in keep}


def _root_volume_size(
    resource: SourceResource, inventory: list[SourceResource]
) -> int | None:
    root = resource.properties.get("root_device")
    for vol in resource.properties.get("volumes", []):
        if vol.get("device") == root:
            match = next((r for r in inventory if r.id == vol["volume_id"]), None)
            if match:
                return match.properties.get("size_gib")
    return None


def _app_cidrs(db: SourceResource, inventory: list[SourceResource]) -> list[str]:
    """CIDRs of subnets that host compute in the database's VPC (for the ACL)."""
    subnets = {r.id: r for r in inventory if r.kind == ResourceKind.SUBNET}
    db_subnets = set(db.properties.get("subnet_ids", []))
    vpc = next(
        (subnets[s].properties.get("vpc_id") for s in db_subnets if s in subnets), None
    )
    cidrs = {
        subnets[r.properties["subnet_id"]].properties["cidr_block"]
        for r in inventory
        if r.kind == ResourceKind.COMPUTE_INSTANCE
        and r.properties.get("subnet_id") in subnets
        and (
            vpc is None
            or subnets[r.properties["subnet_id"]].properties.get("vpc_id") == vpc
        )
    }
    return sorted(cidrs)
