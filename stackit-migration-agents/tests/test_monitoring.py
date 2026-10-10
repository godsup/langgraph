from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from migrate import config, notify
from migrate.audit import AuditLog
from migrate.cli import app
from migrate.config import PROJECT_ROOT, Settings
from migrate.runner import Runner
from migrate.runs import run_paths
from migrate.state import Anomaly, RunOptions, SourceCloud


def _monitor(runner: Runner) -> tuple[str, Any]:
    run_id = runner.create_run(source=SourceCloud.AWS, region=None, mock=True).run_id
    return run_id, asyncio.run(runner.advance(run_id, "monitor", RunOptions())).plan


def test_rules_are_evaluated_and_anomalies_triaged(runner: Runner) -> None:
    run_id, plan = _monitor(runner)
    mon = plan.monitoring
    assert mon.polls == 1 and len(mon.last_evaluations) == 6
    breaches = {e.rule: e.breaches for e in mon.last_evaluations}
    assert breaches == {
        "cpu_high": 1,
        "memory_high": 0,
        "disk_full": 0,
        "db_connections_high": 1,
        "http_error_rate_high": 0,
        "target_down": 0,
    }
    by_rule = {a.rule: a for a in mon.anomalies}
    assert by_rule["cpu_high"].labels == {"instance": "shop-worker-1"}
    assert "nightly export" in by_rule["cpu_high"].triage
    assert "PgBouncer" in by_rule["db_connections_high"].triage
    report = Path(mon.report_path).read_text()
    assert "nothing is changed automatically" in report and "cpu_high" in report

    entries = AuditLog(run_paths(runner.settings.runs_dir, run_id).audit_file).entries()
    actions = [e["action"] for e in entries]
    # The LLM's own investigation queries went through the audited tool box.
    assert "observability.query_range" in actions
    assert actions.count("llm.investigate") == 2
    # No mutating action exists for monitoring.
    assert not [
        e for e in entries if e["agent"] == "monitoring" and e.get("phase") == "before"
    ]


def test_polls_accumulate_history(runner: Runner) -> None:
    run_id, _ = _monitor(runner)
    plan = asyncio.run(runner.advance(run_id, "monitor", RunOptions())).plan
    assert plan.monitoring.polls == 2 and len(plan.monitoring.anomalies) == 4


def test_llm_cannot_call_tools_outside_the_read_only_set(
    tmp_path: Path, reporter
) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(PROJECT_ROOT / "fixtures", fixtures)
    triage_file = fixtures / "llm" / "monitoring_triage.json"
    triage = json.loads(triage_file.read_text())
    triage["responses"]["cpu_high"] = [
        {
            "content": "",
            "tool_calls": [
                {"name": "terraform_apply", "id": "c1", "args": {"workdir": "x"}}
            ],
        },
        {"content": "- **What:** tried something not allowed"},
    ]
    triage_file.write_text(json.dumps(triage))
    settings = Settings(
        _env_file=None,
        runs_dir=tmp_path / "runs",
        fixtures_dir=fixtures,
        config_dir=PROJECT_ROOT / "config",
    )
    runner = Runner(settings, reporter)
    run_id, plan = _monitor(runner)
    assert any(
        a.triage.startswith("- **What:** tried") for a in plan.monitoring.anomalies
    )
    entries = AuditLog(run_paths(settings.runs_dir, run_id).audit_file).entries()
    assert not [e for e in entries if "terraform_apply" in e["action"]]


def test_tool_budget_exhausted_asks_without_tools(tmp_path: Path) -> None:
    from langchain_core.tools import tool

    from migrate.llm import LLMGateway

    @tool
    def query_promql(query: str) -> str:
        """Run a query."""
        return query

    (tmp_path / "llm").mkdir()
    loop = [
        {
            "content": "",
            "tool_calls": [
                {"name": "query_promql", "id": f"c{i}", "args": {"query": "up"}}
            ],
        }
        for i in range(3)
    ]
    (tmp_path / "llm" / "t.json").write_text(
        json.dumps({"responses": {"k": loop + [{"content": "final answer"}]}})
    )
    gateway = LLMGateway(
        Settings(_env_file=None),
        AuditLog(tmp_path / "a.jsonl"),
        recorded_dir=tmp_path / "llm",
    )
    calls: list[str] = []

    async def execute(name: str, args: dict) -> dict:
        calls.append(name)
        return {"ok": True}

    text = asyncio.run(
        gateway.investigate(
            agent="m",
            task="t",
            key="k",
            system="s",
            prompt="p",
            tools=[query_promql],
            execute=execute,
            max_steps=3,
            fallback="fb",
        )
    )
    assert text == "final answer" and calls == ["query_promql"] * 3


def _anomaly() -> Anomaly:
    return Anomaly(
        rule="cpu_high",
        severity="warning",
        query="q",
        value=93.4,
        threshold=85,
        labels={"instance": "w1"},
    )


def test_webhook_payloads() -> None:
    slack = notify.payload("slack", "r1", [_anomaly()])
    assert (
        "cpu_high" in slack["text"] and "nothing changed automatically" in slack["text"]
    )
    teams = notify.payload("teams", "r1", [_anomaly()])
    card = teams["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert any("cpu_high" in block["text"] for block in card["content"]["body"])


def test_webhook_is_sent_and_url_redacted(
    tmp_path: Path, reporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict] = []

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:
        sent.append({"url": url, "json": json})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    hook = "https://hooks.slack.com/services/T000/B000/abcdefSECRET"
    settings = Settings(
        _env_file=None,
        runs_dir=tmp_path / "runs",
        fixtures_dir=PROJECT_ROOT / "fixtures",
        config_dir=PROJECT_ROOT / "config",
        webhook_url=hook,
        webhook_kind="slack",
    )
    runner = Runner(settings, reporter)
    run_id, plan = _monitor(runner)
    assert len(sent) == 1 and sent[0]["url"] == hook
    assert plan.monitoring.notifications_sent == 1
    audit_text = run_paths(settings.runs_dir, run_id).audit_file.read_text()
    assert "abcdefSECRET" not in audit_text and "notify.webhook" in audit_text


def test_observability_live_queries_use_prometheus_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_servers.observability import server

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"instance": "a"}, "value": [1, "1"]}],
                },
            },
        )

    monkeypatch.setenv("MIGRATE_MOCK", "0")
    monkeypatch.setattr(
        server,
        "_client",
        lambda: httpx.Client(
            base_url="https://metrics.example",
            auth=("u", "p"),
            transport=httpx.MockTransport(handler),
        ),
    )
    result = server.query_promql(query="up")
    assert result["result"][0]["metric"] == {"instance": "a"}
    assert seen[0].url.path == "/api/v1/query" and seen[0].url.params["query"] == "up"
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_monitor_cli_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)
    config.get_settings.cache_clear()
    try:
        cli = CliRunner()
        out = cli.invoke(app, ["discover", "--mock"])
        run_id = re.search(r"Run (\S+) created", out.output).group(1)
        result = cli.invoke(
            app, ["monitor", run_id, "--interval", "0", "--iterations", "2"]
        )
        assert result.exit_code == 0, result.output
        assert result.output.count("Poll #") == 2
    finally:
        config.get_settings.cache_clear()
