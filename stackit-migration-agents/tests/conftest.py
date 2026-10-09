from __future__ import annotations

from pathlib import Path

import pytest

from migrate.config import PROJECT_ROOT, Settings
from migrate.console import NullReporter
from migrate.runner import Runner


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests never see real credentials or a developer's .env.
    for name in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "APPROVAL_SECRET",
        "WEBHOOK_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        runs_dir=tmp_path / "runs",
        fixtures_dir=PROJECT_ROOT / "fixtures",
        config_dir=PROJECT_ROOT / "config",
    )


@pytest.fixture
def reporter() -> NullReporter:
    return NullReporter()


@pytest.fixture
def runner(settings: Settings, reporter: NullReporter) -> Runner:
    return Runner(settings, reporter)
