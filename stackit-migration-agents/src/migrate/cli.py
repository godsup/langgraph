"""`migrate` command-line interface."""

from __future__ import annotations

import asyncio
import getpass
import os
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel

from migrate.config import get_settings
from migrate.console import RichReporter
from migrate.llm import LLMNotConfiguredError
from migrate.runner import GatePendingError, NoPendingGateError, Outcome, Runner
from migrate.runs import RunNotFoundError, list_runs, read_plan
from migrate.state import DeployMode, MigrationPlan, RunOptions, SourceCloud

app = typer.Typer(
    name="migrate",
    help="STACKIT Migration Agents: discover, plan, deploy, migrate data and monitor.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
)
console = Console()

RunId = Annotated[str, typer.Argument(help="Run id, e.g. 20261009-142501-1a2b")]
LiveLlm = Annotated[
    bool,
    typer.Option(
        "--live-llm",
        help="In mock mode, call the configured LLM instead of recorded responses.",
    ),
]
Execute = Annotated[
    bool,
    typer.Option(
        "--execute",
        help="Allow real mutations (still behind the approval gate). Default: dry run.",
    ),
]


def _runner() -> Runner:
    return Runner(get_settings(), RichReporter(console))


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except (
        RunNotFoundError,
        GatePendingError,
        NoPendingGateError,
        LLMNotConfiguredError,
        ValueError,
    ) as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    except PermissionError as exc:
        console.print(f"[bold red]Refused:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        if os.environ.get("MIGRATE_DEBUG") == "1":
            raise
        console.print(f"[bold red]Failed:[/] {type(exc).__name__}: {exc}")
        console.print(
            "[dim]The audit log has the details; set MIGRATE_DEBUG=1 for a traceback.[/]"
        )
        raise typer.Exit(code=1) from exc


def _mock_banner(plan: MigrationPlan) -> None:
    if plan.mock:
        console.print(
            "[black on yellow] MOCK MODE [/] recorded fixtures · no cloud calls · mutations are simulated"
        )


def _show(outcome: Outcome) -> None:
    plan = outcome.plan
    run_id = plan.run_id
    if outcome.pending_gate:
        gate = outcome.pending_gate
        body = gate.get("summary") or ""
        artifact = gate.get("artifact")
        details = [
            f"**Artefact:** `{_display_path(Path(artifact)) if artifact else '-'}`",
            f"**SHA-256:** `{gate.get('artifact_sha256')}`",
        ]
        if gate.get("raw_plan"):
            console.print(
                Panel(
                    gate["raw_plan"],
                    title="Raw terraform plan (excerpt)",
                    border_style="dim",
                    style="dim",
                )
            )
        if gate.get("raw_plan_file"):
            details.append(
                f"**Full plan:** `{_display_path(Path(gate['raw_plan_file']))}`"
            )
        RichReporter(console).panel(
            f"⏸  {gate.get('title', 'Approval required')}",
            body + "\n\n" + "\n\n".join(details),
            style="yellow",
        )
        console.print(
            f"Approve:  [bold]migrate approve {run_id}[/]\n"
            f'Reject:   [bold]migrate approve {run_id} --reject --comment "why"[/]'
        )
        return
    if outcome.halted:
        console.print(f"[yellow]Stopped:[/] {outcome.halted}")
    root = _display_path(get_settings().runs_dir / run_id)
    console.print(
        f"[dim]Plan:[/] {root}/migration-plan.json   [dim]Audit:[/] {root}/audit.jsonl"
    )


def _display_path(path: Path) -> str:
    """Show paths relative to the working directory when possible."""
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _approver(value: str | None) -> str:
    return value or getpass.getuser()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def discover(
    source: Annotated[
        SourceCloud, typer.Option(help="Source cloud.")
    ] = SourceCloud.AWS,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use recorded fixtures; no credentials needed."),
    ] = False,
    region: Annotated[
        str | None, typer.Option(help="Source region (default from config).")
    ] = None,
    live_llm: LiveLlm = False,
) -> None:
    """Inventory the source account (read-only) and create a new run."""
    runner = _runner()
    paths = runner.create_run(source=source, region=region, mock=mock)
    console.print(f"Run [bold]{paths.run_id}[/] created")
    _mock_banner(read_plan(paths))
    outcome = _run(
        runner.advance(paths.run_id, "discover", RunOptions(live_llm=live_llm))
    )
    _show(outcome)
    console.print(f"Next: [bold]migrate plan {paths.run_id}[/]")


@app.command()
def plan(run_id: RunId, live_llm: LiveLlm = False) -> None:
    """Generate Terraform and run terraform plan. Changes nothing."""
    outcome = _run(_runner().advance(run_id, "plan", RunOptions(live_llm=live_llm)))
    _show(outcome)


@app.command()
def deploy(
    run_id: RunId,
    mode: Annotated[
        DeployMode,
        typer.Option(
            help="apply: terraform apply after approval; pr: open a pull request."
        ),
    ] = DeployMode.APPLY,
    execute: Execute = False,
    live_llm: LiveLlm = False,
) -> None:
    """Plan and request approval to apply (or open a PR)."""
    options = RunOptions(execute=execute, deploy_mode=mode, live_llm=live_llm)
    _show(_run(_runner().advance(run_id, "deploy", options)))


@app.command()
def approve(
    run_id: RunId,
    reject: Annotated[
        bool, typer.Option("--reject", help="Reject instead of approve.")
    ] = False,
    comment: Annotated[str, typer.Option(help="Reason, stored in the audit log.")] = "",
    approver: Annotated[
        str | None, typer.Option(help="Approver name (default: current user).")
    ] = None,
    live_llm: LiveLlm = False,
) -> None:
    """Approve (or reject) the pending human gate and continue."""
    outcome = _run(
        _runner().decide(
            run_id,
            approved=not reject,
            approver=_approver(approver),
            comment=comment,
            options=RunOptions(live_llm=live_llm),
        )
    )
    _show(outcome)


@app.command()
def data(run_id: RunId, execute: Execute = False, live_llm: LiveLlm = False) -> None:
    """Copy and verify data, then request approval for cutover."""
    _show(
        _run(
            _runner().advance(
                run_id, "data", RunOptions(execute=execute, live_llm=live_llm)
            )
        )
    )


@app.command()
def monitor(
    run_id: RunId,
    interval: Annotated[
        int | None,
        typer.Option(help="Seconds between polls (default MONITOR_INTERVAL_S)."),
    ] = None,
    iterations: Annotated[
        int, typer.Option(help="Number of polls; 0 = until Ctrl-C.")
    ] = 0,
    live_llm: LiveLlm = False,
) -> None:
    """Poll STACKIT Observability on a schedule. Read-only; never remediates."""
    settings = get_settings()
    runner = _runner()
    every = interval or settings.monitor_interval_s
    count = 0
    try:
        while True:
            _show(
                _run(runner.advance(run_id, "monitor", RunOptions(live_llm=live_llm)))
            )
            count += 1
            if iterations and count >= iterations:
                break
            console.print(f"[dim]Next poll in {every}s (Ctrl-C to stop)[/]")
            time.sleep(every)
    except KeyboardInterrupt:
        console.print("Monitoring stopped.")


@app.command()
def status(run_id: RunId) -> None:
    """Show where a run stands."""
    outcome = _run(_runner().status(run_id))
    plan = outcome.plan
    _mock_banner(plan)
    rows = [
        [
            "Discovery",
            "done" if plan.discovery.completed_at else "pending",
            f"{len(plan.resources)} resources, {len(plan.gaps)} gaps",
        ],
        ["Deployment", plan.deployment.status.value, plan.deployment.mode.value],
        [
            "Data migration",
            plan.data_migration.status.value,
            f"{len(plan.data_migration.tasks)} transfers",
        ],
        [
            "Monitoring",
            f"{plan.monitoring.polls} polls",
            f"{len(plan.monitoring.anomalies)} anomalies",
        ],
    ]
    RichReporter(console).table(
        f"Run {plan.run_id} ({plan.source_cloud.value} → STACKIT {plan.target_region})",
        ["Stage", "Status", "Details"],
        rows,
    )
    if outcome.next_nodes and not outcome.pending_gate:
        console.print(
            f"[yellow]Unfinished work[/] at {', '.join(outcome.next_nodes)}: run [bold]migrate resume {run_id}[/]"
        )
    _show(outcome)


@app.command()
def resume(run_id: RunId, live_llm: LiveLlm = False) -> None:
    """Continue a run that was interrupted by a crash or restart."""
    _show(_run(_runner().resume(run_id, RunOptions(live_llm=live_llm))))


@app.command(name="runs")
def list_all() -> None:
    """List runs, newest first."""
    rows = []
    for paths in list_runs(get_settings().runs_dir):
        try:
            p = read_plan(paths)
        except (OSError, ValueError):
            continue
        rows.append(
            [
                p.run_id,
                p.source_cloud.value,
                "mock" if p.mock else "live",
                p.deployment.status.value,
                p.data_migration.status.value,
            ]
        )
    RichReporter(console).table(
        "Runs", ["Run id", "Source", "Mode", "Deployment", "Data"], rows
    )


@app.command()
def run(
    source: Annotated[
        SourceCloud, typer.Option(help="Source cloud.")
    ] = SourceCloud.AWS,
    mock: Annotated[
        bool,
        typer.Option("--mock", help="Use recorded fixtures; no credentials needed."),
    ] = False,
    region: Annotated[str | None, typer.Option(help="Source region.")] = None,
    mode: Annotated[
        DeployMode, typer.Option(help="Deployment mode.")
    ] = DeployMode.APPLY,
    execute: Execute = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Auto-approve gates (only allowed with --mock)."),
    ] = False,
    live_llm: LiveLlm = False,
) -> None:
    """Full end-to-end flow: discover → deploy → data → monitor, with human gates."""
    if yes and not mock:
        console.print("[bold red]--yes is only allowed together with --mock.[/]")
        raise typer.Exit(code=1)
    runner = _runner()
    paths = runner.create_run(source=source, region=region, mock=mock)
    console.print(f"Run [bold]{paths.run_id}[/] created")
    _mock_banner(read_plan(paths))
    options = RunOptions(execute=execute, deploy_mode=mode, live_llm=live_llm)
    outcome = _run(runner.advance(paths.run_id, "all", options))
    while outcome.pending_gate:
        _show(outcome)
        if yes:
            approved, who = True, "auto-approve (mock demo)"
            console.print("[dim]--yes: auto-approving (mock mode only)[/]")
        else:
            approved, who = typer.confirm("Approve?", default=False), _approver(None)
        comment = (
            ""
            if approved
            else typer.prompt("Reason for rejection", default="rejected in demo")
        )
        outcome = _run(
            runner.decide(
                paths.run_id,
                approved=approved,
                approver=who,
                comment=comment,
                options=options,
            )
        )
    _show(outcome)


def main() -> None:  # pragma: no cover
    app()
