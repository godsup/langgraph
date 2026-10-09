"""Run external CLIs (terraform, stackit, rclone, pg_*, gh) safely.

Always an argument list, never a shell string, never `shell=True`. Output is
captured and size-limited so a chatty tool cannot flood logs.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 200_000


class ToolNotInstalledError(FileNotFoundError):
    pass


class CommandFailedError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"{args[0]} exited with {returncode}: {stderr.strip()[-2000:]}"
        )


@dataclass
class Result:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise ToolNotInstalledError(f"`{binary}` is not installed or not on PATH")
    return path


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 1800,
    check: bool = True,
    stdin: str | None = None,
) -> Result:
    if isinstance(args, str):  # defensive: never accept a shell string
        raise TypeError("args must be a sequence of strings, not a string")
    argv = [require(args[0]), *[str(a) for a in args[1:]]]
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = Result(
        args=[args[0], *argv[1:]],
        returncode=completed.returncode,
        stdout=completed.stdout[-MAX_OUTPUT:],
        stderr=completed.stderr[-MAX_OUTPUT:],
    )
    if check and completed.returncode != 0:
        raise CommandFailedError(result.args, completed.returncode, result.stderr)
    return result
