"""Render the Terraform of the mock demo account into a directory (for CI).

    uv run python -m migrate.tools.render_fixture_terraform build/terraform

Runs mock discovery in a temporary run directory, renders the Terraform with
the same code as the deployment agent and writes it to the target directory,
so CI can run `terraform fmt -check` and `terraform validate` on it.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from migrate import terraform
from migrate.config import PROJECT_ROOT, Settings
from migrate.runner import Runner
from migrate.state import RunOptions, SourceCloud


def render_fixture(out: Path, source: SourceCloud = SourceCloud.AWS) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            _env_file=None,
            runs_dir=Path(tmp) / "runs",
            fixtures_dir=PROJECT_ROOT / "fixtures",
            config_dir=PROJECT_ROOT / "config",
        )
        runner = Runner(settings)
        run_id = runner.create_run(source=source, region=None, mock=True).run_id
        plan = asyncio.run(runner.advance(run_id, "discover", RunOptions())).plan
    rendered = terraform.render(plan)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, content in rendered.files.items():
        (out / name).write_text(content, encoding="utf-8")
    return rendered.addresses


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/terraform")
    addresses = render_fixture(out)
    print(
        f"rendered {len(addresses)} resources/data sources into {out}", file=sys.stderr
    )


if __name__ == "__main__":
    main()
