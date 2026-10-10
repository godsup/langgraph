"""Observability MCP server: PromQL queries against STACKIT Observability.

Read-only by construction: the only tools are instant and range queries.
There is no tool that changes anything, so the monitoring agent (and the LLM
it lets investigate) cannot remediate.

Live mode calls the Prometheus-compatible HTTP API of the Observability
instance with basic auth (credential from `stackit_observability_credential`).
TODO(verify): the query base URL. We use `<OBSERVABILITY_URL>/api/v1/query`;
the instance's `metrics_url` is the expected value of OBSERVABILITY_URL.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from mcp_servers.common import is_mock, load_fixture, make_server

mcp = make_server(
    "observability", "Read-only PromQL queries against STACKIT Observability."
)
MAX_SERIES = 200


def _client() -> httpx.Client:
    base = os.environ.get("OBSERVABILITY_URL")
    if not base:
        raise RuntimeError("OBSERVABILITY_URL is not configured")
    auth = None
    if os.environ.get("OBSERVABILITY_USERNAME"):
        auth = (
            os.environ["OBSERVABILITY_USERNAME"],
            os.environ.get("OBSERVABILITY_PASSWORD", ""),
        )
    return httpx.Client(base_url=base.rstrip("/"), auth=auth, timeout=30.0)


def _trim(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", {})
    result = data.get("result", [])
    return {
        "status": payload.get("status"),
        "resultType": data.get("resultType"),
        "result": result[:MAX_SERIES],
        "truncated": len(result) > MAX_SERIES,
    }


def _mock_query(query: str) -> dict[str, Any]:
    metrics = load_fixture("metrics", "metrics.json")
    result = metrics["instant"].get(" ".join(query.split()), [])
    return {
        "status": "success",
        "resultType": "vector",
        "result": result,
        "truncated": False,
    }


@mcp.tool()
def query_promql(query: str) -> dict[str, Any]:
    """Read-only: run an instant PromQL query and return the result vector."""
    if is_mock():
        return _mock_query(query)
    with _client() as client:
        response = client.get("/api/v1/query", params={"query": query})
        response.raise_for_status()
        return _trim(response.json())


@mcp.tool()
def query_range(
    query: str, minutes: int = 60, step_seconds: int = 60
) -> dict[str, Any]:
    """Read-only: run a PromQL range query over the last `minutes` minutes."""
    minutes = max(1, min(int(minutes), 24 * 60))
    step_seconds = max(15, int(step_seconds))
    if is_mock():
        metrics = load_fixture("metrics", "metrics.json")
        result = metrics["range"].get(" ".join(query.split()), [])
        return {
            "status": "success",
            "resultType": "matrix",
            "result": result,
            "truncated": False,
        }
    end = time.time()
    with _client() as client:
        response = client.get(
            "/api/v1/query_range",
            params={
                "query": query,
                "start": end - minutes * 60,
                "end": end,
                "step": step_seconds,
            },
        )
        response.raise_for_status()
        return _trim(response.json())


def main() -> None:
    mcp.run("stdio")
