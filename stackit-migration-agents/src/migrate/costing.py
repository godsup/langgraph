"""Rough before/after monthly cost estimate.

Prices come only from `config/pricing.yaml`. Missing prices are never
guessed: the line shows "price not set" and the exact key to fill in is listed
in `missing_prices`, so the estimate's gaps are visible.
"""

from __future__ import annotations

from typing import Any

from migrate.state import (
    CostEstimate,
    CostLine,
    MappingStatus,
    ResourceKind,
    SourceResource,
    TargetMapping,
)

GIB = 1024**3


class _Prices:
    def __init__(self, pricing: dict[str, Any]) -> None:
        self.data = pricing
        self.missing: set[str] = set()
        self.hours = float(pricing.get("hours_per_month", 730))

    def get(self, side: str, table: str, key: str | None = None) -> float | None:
        node = self.data.get(side, {}).get(table)
        label = f"{side}.{table}" + (f"[{key}]" if key is not None else "")
        value = node.get(key) if isinstance(node, dict) and key is not None else node
        if isinstance(value, dict):
            value = None
        if value is None:
            self.missing.add(label)
            return None
        return float(value)


def _mul(*values: float | None) -> float | None:
    result = 1.0
    for value in values:
        if value is None:
            return None
        result *= value
    return result


def _add(*values: float | None) -> float | None:
    if any(v is None for v in values):
        return None
    return float(sum(v for v in values if v is not None))


def estimate(
    resources: list[SourceResource],
    mappings: list[TargetMapping],
    pricing: dict[str, Any],
) -> CostEstimate:
    prices = _Prices(pricing)
    by_id = {m.resource_id: m for m in mappings}
    lines: list[CostLine] = []
    for res in resources:
        mapping = by_id.get(res.id)
        line = _line(res, mapping, prices)
        if line is not None:
            lines.append(line)
    source_total = sum(line.source_monthly or 0.0 for line in lines)
    target_total = sum(line.target_monthly or 0.0 for line in lines)
    missing = sorted(prices.missing)
    disclaimer = (
        "Rough estimate from config/pricing.yaml. "
        + (
            f"{len(missing)} price(s) are not set, so totals are incomplete. "
            if missing
            else "All required prices are set. "
        )
        + "Excludes data transfer, support plans, licences and discounts."
    )
    return CostEstimate(
        currency=str(pricing.get("currency", "EUR")),
        lines=lines,
        source_total=round(source_total, 2),
        target_total=round(target_total, 2),
        missing_prices=missing,
        disclaimer=disclaimer,
    )


def _line(
    res: SourceResource, mapping: TargetMapping | None, p: _Prices
) -> CostLine | None:
    props = res.properties
    spec = mapping.target_spec if mapping else {}
    has_target = bool(
        mapping and mapping.terraform_type and mapping.status != MappingStatus.GAP
    )
    kind = res.kind
    if kind == ResourceKind.COMPUTE_INSTANCE:
        source = _mul(
            p.get("aws", "ec2_instance_hourly", props.get("instance_type")), p.hours
        )
        if props.get("eks_cluster"):
            # Counted in the SKE node pool line on the target side.
            target: float | None = 0.0
            desc = f"{props.get('instance_type')} -> SKE node pool ({props['eks_cluster']})"
        else:
            target = (
                _mul(
                    p.get("stackit", "machine_type_hourly", spec.get("machine_type")),
                    p.hours,
                )
                if has_target
                else None
            )
            desc = (
                f"{props.get('instance_type')} -> {spec.get('machine_type') or 'n/a'}"
            )
    elif kind == ResourceKind.BLOCK_VOLUME:
        size = float(props.get("size_gib") or 0)
        source = _mul(p.get("aws", "ebs_gb_month", props.get("volume_type")), size)
        target = _mul(
            p.get(
                "stackit", "volume_gb_month", spec.get("performance_class") or "default"
            ),
            size,
        )
        desc = f"{props.get('volume_type')} {size:g} GiB"
    elif kind == ResourceKind.POSTGRES:
        flavor = spec.get("flavor") or {}
        size = float(props.get("allocated_storage_gb") or 0)
        source = _add(
            _mul(
                p.get("aws", "rds_instance_hourly", props.get("instance_class")),
                p.hours,
                2.0 if props.get("multi_az") else 1.0,
            ),
            _mul(p.get("aws", "rds_storage_gb_month", props.get("storage_type")), size),
        )
        target = (
            _add(
                _mul(
                    p.get("stackit", "postgresflex_flavor_hourly", flavor.get("id")),
                    p.hours,
                ),
                _mul(
                    p.get(
                        "stackit",
                        "postgresflex_storage_gb_month",
                        spec.get("storage_class"),
                    ),
                    size,
                ),
            )
            if has_target
            else None
        )
        desc = f"{props.get('instance_class')} -> PostgreSQL Flex {flavor.get('id', 'n/a')}"
    elif kind in (ResourceKind.MYSQL, ResourceKind.MARIADB):
        size = float(props.get("allocated_storage_gb") or 0)
        source = _add(
            _mul(
                p.get("aws", "rds_instance_hourly", props.get("instance_class")),
                p.hours,
                2.0 if props.get("multi_az") else 1.0,
            ),
            _mul(p.get("aws", "rds_storage_gb_month", props.get("storage_type")), size),
        )
        target = (
            p.get("stackit", "mariadb_plan_month", spec.get("plan_name"))
            if has_target
            else None
        )
        desc = (
            f"{props.get('instance_class')} -> MariaDB {spec.get('plan_name', 'n/a')}"
        )
    elif kind == ResourceKind.OBJECT_BUCKET:
        gb = float(props.get("size_bytes") or 0) / GIB
        classes = props.get("storage_classes") or {
            "StandardStorage": props.get("size_bytes") or 0
        }
        source = _add(
            *[
                _mul(p.get("aws", "s3_gb_month", cls), float(b) / GIB)
                for cls, b in classes.items()
            ]
        )
        target = _mul(p.get("stackit", "object_storage_gb_month"), gb)
        desc = f"{gb:,.0f} GiB"
    elif kind == ResourceKind.LOAD_BALANCER:
        source = _mul(p.get("aws", "lb_hourly", props.get("lb_type")), p.hours)
        target = p.get(
            "stackit", "loadbalancer_month", mapping.terraform_type if mapping else None
        )
        desc = (
            f"{props.get('lb_type')} -> {mapping.terraform_type if mapping else 'n/a'}"
        )
    elif kind == ResourceKind.KUBERNETES_CLUSTER:
        # Source: control plane only (EKS nodes are EC2 lines). Target: cluster
        # fee plus node pools at their current (desired) size.
        source = _mul(p.get("aws", "eks_cluster_hourly"), p.hours)
        pools = spec.get("node_pools") or []
        target = _add(
            p.get("stackit", "ske_cluster_month"),
            *[
                _mul(
                    p.get("stackit", "machine_type_hourly", pool.get("machine_type")),
                    p.hours,
                    float(pool.get("desired") or pool.get("minimum") or 1),
                )
                for pool in pools
            ],
        )
        desc = f"EKS control plane -> SKE cluster + {len(pools)} node pool(s)"
    else:
        return None
    note = "" if source is not None and target is not None else "price not set"
    return CostLine(
        resource_id=res.id,
        description=desc,
        source_monthly=round(source, 2) if source is not None else None,
        target_monthly=round(target, 2) if target is not None else None,
        note=note,
    )
