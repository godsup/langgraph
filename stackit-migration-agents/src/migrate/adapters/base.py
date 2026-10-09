"""The interface every source cloud adapter implements."""

from __future__ import annotations

from typing import Protocol

from migrate.state import SourceResource


class SourceCloudAdapter(Protocol):
    """Read-only inventory of one source cloud account/subscription in one region.

    Implementations must never call a mutating API.
    """

    cloud: str
    region: str

    def account_id(self) -> str: ...

    def compute_instances(self) -> list[SourceResource]: ...

    def block_volumes(self) -> list[SourceResource]: ...

    def networks(self) -> list[SourceResource]:
        """Networks (VPC/VNet) and their subnets."""
        ...

    def security_groups(self) -> list[SourceResource]: ...

    def databases(self) -> list[SourceResource]: ...

    def object_buckets(self) -> list[SourceResource]: ...

    def load_balancers(self) -> list[SourceResource]: ...

    def kubernetes_clusters(self) -> list[SourceResource]: ...


INVENTORY_SECTIONS: tuple[str, ...] = (
    "compute_instances",
    "block_volumes",
    "networks",
    "security_groups",
    "databases",
    "object_buckets",
    "load_balancers",
    "kubernetes_clusters",
)
