"""Run directories: `runs/<run-id>/` holds everything a run produces."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from migrate.state import MigrationPlan

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


class RunNotFoundError(FileNotFoundError):
    pass


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def run_id(self) -> str:
        return self.root.name

    @property
    def plan_file(self) -> Path:
        return self.root / "migration-plan.json"

    @property
    def audit_file(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def terraform_dir(self) -> Path:
        return self.root / "terraform"

    @property
    def discovery_report(self) -> Path:
        return self.root / "discovery-report.md"

    @property
    def verification_report(self) -> Path:
        return self.root / "data-verification.md"

    @property
    def cutover_runbook(self) -> Path:
        return self.root / "cutover-runbook.md"

    @property
    def monitoring_report(self) -> Path:
        return self.root / "monitoring-report.md"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


def run_paths(runs_dir: Path, run_id: str, *, must_exist: bool = True) -> RunPaths:
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"invalid run id {run_id!r} (expected YYYYMMDD-HHMMSS-xxxx)")
    paths = RunPaths(runs_dir / run_id)
    if must_exist and not paths.plan_file.exists():
        raise RunNotFoundError(f"run {run_id} not found in {runs_dir}")
    return paths


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_plan(paths: RunPaths, plan: MigrationPlan) -> None:
    write_atomic(paths.plan_file, plan.model_dump_json(indent=2) + "\n")


def read_plan(paths: RunPaths) -> MigrationPlan:
    return MigrationPlan.model_validate_json(
        paths.plan_file.read_text(encoding="utf-8")
    )


def list_runs(runs_dir: Path) -> list[RunPaths]:
    if not runs_dir.exists():
        return []
    return sorted(
        (RunPaths(p) for p in runs_dir.iterdir() if RUN_ID_RE.match(p.name)),
        key=lambda r: r.run_id,
        reverse=True,
    )
