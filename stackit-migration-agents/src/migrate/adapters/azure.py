"""Azure adapter: stub backed by fixtures.

Implements the same `SourceCloudAdapter` interface as AWS so the rest of the
system is cloud-agnostic. A real implementation (azure-mgmt-* SDKs with the
built-in Reader role) replaces `_load` later; nothing else needs to change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from migrate.state import SourceResource


class AzureAdapter:
    cloud = "azure"

    def __init__(self, region: str, fixture: Path) -> None:
        self.region = region
        self._data: dict[str, Any] = json.loads(fixture.read_text(encoding="utf-8"))

    def account_id(self) -> str:
        return self._data["account_id"]

    def _section(self, name: str) -> list[SourceResource]:
        return [
            SourceResource.model_validate(r)
            for r in self._data["sections"].get(name, [])
        ]

    def compute_instances(self) -> list[SourceResource]:
        return self._section("compute_instances")

    def block_volumes(self) -> list[SourceResource]:
        return self._section("block_volumes")

    def networks(self) -> list[SourceResource]:
        return self._section("networks")

    def security_groups(self) -> list[SourceResource]:
        return self._section("security_groups")

    def databases(self) -> list[SourceResource]:
        return self._section("databases")

    def object_buckets(self) -> list[SourceResource]:
        return self._section("object_buckets")

    def load_balancers(self) -> list[SourceResource]:
        return self._section("load_balancers")

    def kubernetes_clusters(self) -> list[SourceResource]:
        return self._section("kubernetes_clusters")
