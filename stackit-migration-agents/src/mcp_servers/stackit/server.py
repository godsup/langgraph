"""STACKIT MCP server: wraps the `terraform` and `stackit` CLIs.

Read-only tools: `get_catalog`, `terraform_init`, `terraform_validate`,
`terraform_plan` (plan only writes a plan file inside the run directory).
Mutating tools: `terraform_apply`, `open_pull_request`. Both refuse to run
without a valid approval token issued by the orchestrator for the exact plan
file, and both write the audit log before and after execution.

All paths must stay inside the current run directory. Commands are argument
lists (never a shell). In mock mode nothing is executed: realistic output is
simulated from the generated Terraform files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from mcp_servers.common import (
    is_mock,
    load_fixture,
    make_server,
    path_inside,
    require_approval,
    run_dir,
    run_id,
    safe_name,
    server_audit,
)
from mcp_servers.stackit.catalog import live_catalog
from migrate.approval import sha256_file
from migrate.proc import run

mcp = make_server(
    "stackit", "Terraform and STACKIT CLI. Mutations need an approval token."
)
_cli_env: dict[str, str] = {}
PLAN_FILE = "tfplan"
PLAN_TEXT = "plan.txt"
TFVARS = "terraform.tfvars.json"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def stackit_cli_env() -> dict[str, str]:
    """Environment for the stackit CLI with a short-lived access token.

    Per the CLI docs, `auth activate-service-account --only-print-access-token`
    prints a token without storing credentials, and STACKIT_ACCESS_TOKEN is
    then used by subsequent commands.
    """
    if not _cli_env:
        env = dict(os.environ)
        key_path = env.get("STACKIT_SERVICE_ACCOUNT_KEY_PATH")
        if key_path and not env.get("STACKIT_ACCESS_TOKEN"):
            argv = [
                "stackit",
                "auth",
                "activate-service-account",
                "--service-account-key-path",
                key_path,
                "--only-print-access-token",
            ]
            if env.get("STACKIT_PRIVATE_KEY_PATH"):
                argv += ["--private-key-path", env["STACKIT_PRIVATE_KEY_PATH"]]
            env["STACKIT_ACCESS_TOKEN"] = run(argv, env=env, timeout=60).stdout.strip()
        _cli_env.update(env)
    return dict(_cli_env)


def _tf_dir(workdir: str) -> Path:
    tf_dir = path_inside(run_dir(), workdir)
    if not tf_dir.is_dir():
        raise FileNotFoundError(f"{tf_dir} does not exist")
    return tf_dir


def _terraform(tf_dir: Path, *args: str, timeout: float = 3600) -> str:
    env = dict(os.environ)
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    return run(["terraform", *args], cwd=tf_dir, env=env, timeout=timeout).stdout


def _audit(action: str, inputs: dict[str, Any], phase: str, **kw: Any) -> None:
    audit = server_audit("stackit")
    if audit:
        audit.record(
            agent="mcp:stackit", action=action, inputs=inputs, phase=phase, **kw
        )


_RESOURCE = re.compile(r'^resource "([^"]+)" "([^"]+)" \{$')
_LITERAL = re.compile(r'^  ([a-z_]+)\s*=\s*("[^"]*"|-?\d+|true|false)$')


def _declared_resources(tf_dir: Path) -> list[dict[str, Any]]:
    """Resources and their literal top-level attributes, from our own HCL."""
    resources: list[dict[str, Any]] = []
    for path in sorted(tf_dir.glob("*.tf")):
        current: dict[str, Any] | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := _RESOURCE.match(line):
                current = {"type": match.group(1), "name": match.group(2), "attrs": {}}
                resources.append(current)
            elif line == "}":
                current = None
            elif current is not None and (lit := _LITERAL.match(line)):
                current["attrs"][lit.group(1)] = lit.group(2)
    return resources


def _mock_plan_text(resources: list[dict[str, Any]]) -> str:
    lines = [
        "Terraform used the selected providers to generate the following execution",
        "plan. Resource actions are indicated with the following symbols:",
        "  + create",
        "",
        "Terraform will perform the following actions:",
        "",
    ]
    for res in resources:
        address = f"{res['type']}.{res['name']}"
        lines.append(f"  # {address} will be created")
        lines.append(f'  + resource "{res["type"]}" "{res["name"]}" {{')
        attrs = dict(sorted(res["attrs"].items()))
        attrs.setdefault("id", "(known after apply)")
        width = max(len(k) for k in attrs)
        for key, value in attrs.items():
            lines.append(f"      + {key.ljust(width)} = {value}")
        lines.append("    }")
        lines.append("")
    lines.append(f"Plan: {len(resources)} to add, 0 to change, 0 to destroy.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------


@mcp.tool()
def get_catalog() -> dict[str, Any]:
    """Read-only: STACKIT offerings (machine types, PostgreSQL Flex flavors and
    versions, MariaDB plans, SKE Kubernetes versions) for sizing decisions."""
    if is_mock():
        return {"source": "fixture", **load_fixture("stackit", "catalog.json")}
    return live_catalog(stackit_cli_env())


@mcp.tool()
def terraform_init(workdir: str) -> dict[str, Any]:
    """Run `terraform init` (downloads the stackitcloud/stackit provider)."""
    tf_dir = _tf_dir(workdir)
    if is_mock():
        return {
            "simulated": True,
            "output": "Initializing provider plugins...\n- Installing stackitcloud/stackit v0.119.0...\n"
            "Terraform has been successfully initialized!",
        }
    out = _terraform(tf_dir, "init", "-input=false", "-no-color")
    return {"simulated": False, "output": out[-4000:]}


@mcp.tool()
def terraform_validate(workdir: str) -> dict[str, Any]:
    """Run `terraform validate` and return diagnostics."""
    tf_dir = _tf_dir(workdir)
    if is_mock():
        return {
            "simulated": True,
            "valid": True,
            "error_count": 0,
            "warning_count": 0,
            "diagnostics": [],
        }
    env = dict(os.environ)
    result = run(
        ["terraform", "validate", "-json", "-no-color"],
        cwd=tf_dir,
        env=env,
        check=False,
        timeout=600,
    )
    data = json.loads(result.stdout or "{}")
    return {
        "simulated": False,
        "valid": bool(data.get("valid")),
        "error_count": data.get("error_count", 0),
        "warning_count": data.get("warning_count", 0),
        "diagnostics": [
            {
                "severity": d.get("severity"),
                "summary": d.get("summary"),
                "detail": d.get("detail", "")[:500],
            }
            for d in data.get("diagnostics", [])
        ],
    }


@mcp.tool()
def terraform_plan(workdir: str) -> dict[str, Any]:
    """Run `terraform plan -out tfplan` and summarise the changes.

    Writes only inside the run directory: the plan file and plan.txt.
    """
    tf_dir = _tf_dir(workdir)
    plan_file = tf_dir / PLAN_FILE
    if is_mock():
        resources = _declared_resources(tf_dir)
        text = _mock_plan_text(resources)
        changes = [
            {
                "address": f"{r['type']}.{r['name']}",
                "resource_type": r["type"],
                "action": "create",
            }
            for r in resources
        ]
        plan_file.write_text(
            json.dumps({"simulated": True, "changes": changes}, indent=1),
            encoding="utf-8",
        )
    else:
        args = ["plan", "-input=false", "-no-color", f"-out={PLAN_FILE}"]
        if (tf_dir / TFVARS).exists():
            args.append(f"-var-file={TFVARS}")
        _terraform(tf_dir, *args)
        text = _terraform(tf_dir, "show", "-no-color", PLAN_FILE)
        shown = json.loads(_terraform(tf_dir, "show", "-json", PLAN_FILE))
        changes = []
        for rc in shown.get("resource_changes", []):
            actions = rc.get("change", {}).get("actions", [])
            if actions == ["no-op"] or rc.get("mode") == "data":
                continue
            action = (
                "replace" if set(actions) == {"delete", "create"} else "+".join(actions)
            )
            changes.append(
                {
                    "address": rc["address"],
                    "resource_type": rc["type"],
                    "action": action,
                }
            )
    (tf_dir / PLAN_TEXT).write_text(text, encoding="utf-8")
    counts: dict[str, int] = {}
    for change in changes:
        counts[change["action"]] = counts.get(change["action"], 0) + 1
    return {
        "simulated": is_mock(),
        "plan_file": str(plan_file),
        "plan_text_file": str(tf_dir / PLAN_TEXT),
        "plan_sha256": sha256_file(plan_file),
        "changes": changes,
        "counts": counts,
        "summary_line": next(
            (ln for ln in text.splitlines() if ln.startswith("Plan:")), ""
        ),
    }


# --------------------------------------------------------------------------
# mutating tools (approval token required)
# --------------------------------------------------------------------------


@mcp.tool()
def terraform_apply(
    workdir: str, plan_file: str, approval_token: str
) -> dict[str, Any]:
    """Apply the saved, approved plan file. Refuses without a valid approval token."""
    tf_dir = _tf_dir(workdir)
    plan_path = path_inside(tf_dir, plan_file)
    require_approval(
        approval_token, action="terraform.apply", artifact_sha256=sha256_file(plan_path)
    )
    inputs = {
        "workdir": str(tf_dir),
        "plan_file": str(plan_path),
        "plan_sha256": sha256_file(plan_path),
    }
    _audit("terraform_apply", inputs, "before", status="started")
    try:
        if is_mock():
            changes = json.loads(plan_path.read_text(encoding="utf-8"))["changes"]
            output = "\n".join(f"{c['address']}: Creation complete" for c in changes)
            output += f"\n\nApply complete! Resources: {len(changes)} added, 0 changed, 0 destroyed."
            outputs: dict[str, Any] = {
                "note": "simulated apply, no resources were created"
            }
        else:
            output = _terraform(
                tf_dir, "apply", "-input=false", "-no-color", plan_path.name
            )
            raw = json.loads(_terraform(tf_dir, "output", "-json"))
            # Never return sensitive outputs (passwords, keys) to the agent.
            outputs = {
                k: v.get("value") for k, v in raw.items() if not v.get("sensitive")
            }
    except Exception as exc:
        _audit(
            "terraform_apply",
            inputs,
            "after",
            status="error",
            result={"error": str(exc)[:1000]},
        )
        raise
    result = {
        "applied": True,
        "simulated": is_mock(),
        "output": output[-6000:],
        "outputs": outputs,
    }
    _audit(
        "terraform_apply",
        inputs,
        "after",
        result={"applied": True, "simulated": is_mock()},
    )
    return result


_COPY_SUFFIXES = {".tf", ".md", ".json"}


@mcp.tool()
def open_pull_request(
    workdir: str, plan_file: str, title: str, body: str, approval_token: str
) -> dict[str, Any]:
    """Push the generated Terraform to a branch of the client's infrastructure
    repository and open a pull request with `gh`. The client's pipeline applies
    it after review. Refuses without a valid approval token."""
    tf_dir = _tf_dir(workdir)
    plan_path = path_inside(tf_dir, plan_file)
    require_approval(
        approval_token, action="terraform.pr", artifact_sha256=sha256_file(plan_path)
    )
    repo = os.environ.get("PR_REPO") or "example-org/infrastructure"
    branch = f"migrate/{safe_name(run_id(), 'run id')}"
    inputs = {"repo": repo, "branch": branch, "title": title}
    _audit("open_pull_request", inputs, "before", status="started")
    try:
        if is_mock():
            result = {
                "simulated": True,
                "pr_url": f"https://github.com/{repo}/pull/42",
                "branch": branch,
                "files": sorted(
                    p.name for p in tf_dir.iterdir() if p.suffix in _COPY_SUFFIXES
                ),
            }
        else:
            result = _push_and_open_pr(tf_dir, repo, branch, title, body)
    except Exception as exc:
        _audit(
            "open_pull_request",
            inputs,
            "after",
            status="error",
            result={"error": str(exc)[:1000]},
        )
        raise
    _audit("open_pull_request", inputs, "after", result=result)
    return result


def _push_and_open_pr(
    tf_dir: Path, repo: str, branch: str, title: str, body: str
) -> dict[str, Any]:
    clone = os.environ.get("PR_REPO_PATH")
    if not os.environ.get("PR_REPO") or not clone:
        raise RuntimeError(
            "PR mode needs PR_REPO (owner/name) and PR_REPO_PATH (local clone)"
        )
    repo_dir = Path(clone).resolve()
    base = os.environ.get("PR_BASE_BRANCH", "main")
    target = repo_dir / "migrations" / run_id()
    git = ["git", "-C", str(repo_dir)]
    run([*git, "fetch", "origin", base])
    run([*git, "checkout", "-B", branch, f"origin/{base}"])
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in sorted(tf_dir.iterdir()):
        # Code, README and non-secret inputs only: never state, plans or caches.
        if path.is_file() and path.suffix in _COPY_SUFFIXES:
            shutil.copy2(path, target / path.name)
            copied.append(path.name)
    run([*git, "add", str(target)])
    run([*git, "commit", "-m", title])
    run([*git, "push", "-u", "origin", branch])
    out = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=repo_dir,
    ).stdout
    url = next(
        (ln for ln in out.splitlines() if ln.startswith("https://")), out.strip()
    )
    return {"simulated": False, "pr_url": url, "branch": branch, "files": copied}


def main() -> None:
    mcp.run("stdio")
