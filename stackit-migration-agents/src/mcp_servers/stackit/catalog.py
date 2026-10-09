"""STACKIT offerings catalogue (read-only), used by discovery for sizing.

Live mode queries the STACKIT CLI. Commands are taken from the official CLI
docs (stackitcloud/stackit-cli, docs/*.md):

- `stackit server machine-type list -o json`
- `stackit postgresflex flavor list -o json`
- `stackit postgresflex version list -o json`
- `stackit mariadb plans -o json`
- `stackit ske options kubernetes-versions --supported -o json`

TODO(verify): the JSON key names of these outputs are not documented; the
normalisers below accept the likely shapes and drop anything unrecognised.
"""

from __future__ import annotations

import json
import os
from typing import Any

from migrate.proc import run


def _items(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _stackit_json(*args: str, env: dict[str, str]) -> Any:
    argv = ["stackit", *args, "--output-format", "json"]
    if os.environ.get("STACKIT_PROJECT_ID"):
        argv += ["--project-id", os.environ["STACKIT_PROJECT_ID"]]
    result = run(argv, env=env, timeout=120)
    return json.loads(result.stdout or "null")


def normalise_machine_types(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in _items(raw, "items", "machineTypes"):
        if (
            isinstance(item, dict)
            and item.get("name")
            and item.get("vcpus")
            and item.get("ram")
        ):
            out.append(
                {
                    "name": item["name"],
                    "vcpus": int(item["vcpus"]),
                    "ram_mb": int(item["ram"]),
                }
            )
    return out


def normalise_postgres_flavors(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in _items(raw, "flavors", "items"):
        if not isinstance(item, dict) or "id" not in item:
            continue
        node_type = item.get("nodeType") or item.get("node_type")
        if not node_type:
            text = f"{item.get('id', '')} {item.get('description', '')}".lower()
            node_type = "Replica" if "replica" in text else "Single"
        out.append(
            {
                "id": item["id"],
                "cpu": int(item.get("cpu", 0)),
                "memory_gb": int(item.get("memory", 0)),
                "node_type": str(node_type).capitalize(),
            }
        )
    return out


def normalise_versions(raw: Any) -> list[str]:
    versions = []
    for item in _items(raw, "versions", "kubernetesVersions", "items"):
        if isinstance(item, str):
            versions.append(item)
        elif isinstance(item, dict) and item.get("version"):
            versions.append(str(item["version"]))
    return versions


def normalise_mariadb_plans(raw: Any) -> list[dict[str, Any]]:
    plans = []
    for offering in _items(raw, "offerings", "items"):
        if not isinstance(offering, dict):
            continue
        for plan in offering.get("plans", []):
            if isinstance(plan, dict) and plan.get("name"):
                plans.append(
                    {
                        "name": plan["name"],
                        "version": str(offering.get("version", "")),
                        "description": plan.get("description", ""),
                    }
                )
    return plans


def live_catalog(env: dict[str, str]) -> dict[str, Any]:
    return {
        "source": "stackit-cli",
        "machine_types": normalise_machine_types(
            _stackit_json("server", "machine-type", "list", env=env)
        ),
        "postgres_flavors": normalise_postgres_flavors(
            _stackit_json("postgresflex", "flavor", "list", env=env)
        ),
        "postgres_versions": normalise_versions(
            _stackit_json("postgresflex", "version", "list", env=env)
        ),
        "mariadb_plans": normalise_mariadb_plans(
            _stackit_json("mariadb", "plans", env=env)
        ),
        "ske_kubernetes_versions": normalise_versions(
            _stackit_json(
                "ske", "options", "kubernetes-versions", "--supported", env=env
            )
        ),
    }
