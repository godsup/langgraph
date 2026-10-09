"""Shared tool set for the read-only inventory servers (AWS, Azure).

Every tool is a describe/list call. There are no mutating tools.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_servers.common import is_mock, load_fixture
from migrate.adapters.base import INVENTORY_SECTIONS, SourceCloudAdapter


class FixtureAdapter:
    """Serves a recorded, already-normalised inventory (mock mode)."""

    def __init__(self, cloud: str, fixture: str) -> None:
        self.cloud = cloud
        self._data = load_fixture(cloud, fixture)
        self.region = self._data.get("region", "")

    def account_id(self) -> str:
        return self._data["account_id"]

    def section(self, name: str) -> list[dict[str, Any]]:
        return list(self._data["sections"].get(name, []))


def register_inventory_tools(
    mcp: FastMCP, cloud: str, live_factory: Callable[[], SourceCloudAdapter]
) -> None:
    state: dict[str, Any] = {}

    def adapter() -> Any:
        if "adapter" not in state:
            state["adapter"] = (
                FixtureAdapter(cloud, "inventory.json") if is_mock() else live_factory()
            )
        return state["adapter"]

    def section(name: str) -> dict[str, Any]:
        impl = adapter()
        if isinstance(impl, FixtureAdapter):
            resources = impl.section(name)
        else:
            resources = [r.model_dump(mode="json") for r in getattr(impl, name)()]
        return {"section": name, "count": len(resources), "resources": resources}

    @mcp.tool()
    def get_account() -> dict[str, Any]:
        """Account/subscription id and region being inventoried."""
        impl = adapter()
        return {"cloud": cloud, "account_id": impl.account_id(), "region": impl.region}

    def make_tool(name: str) -> None:
        def tool() -> dict[str, Any]:
            return section(name)

        tool.__name__ = f"list_{name}"
        tool.__doc__ = (
            f"Read-only: list {name.replace('_', ' ')} in the configured region."
        )
        mcp.tool()(tool)

    for name in INVENTORY_SECTIONS:
        make_tool(name)
