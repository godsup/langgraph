"""Human-facing progress output (rich ships with Typer)."""

from __future__ import annotations

from typing import Protocol

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

AGENT_STYLE = {
    "orchestrator": "bold white",
    "discovery": "bold cyan",
    "deployment": "bold magenta",
    "data_migration": "bold yellow",
    "monitoring": "bold green",
}


class Reporter(Protocol):
    def stage(self, agent: str, title: str) -> None: ...
    def step(self, agent: str, message: str) -> None: ...
    def ok(self, agent: str, message: str) -> None: ...
    def warn(self, agent: str, message: str) -> None: ...
    def panel(self, title: str, markdown: str, *, style: str = "cyan") -> None: ...
    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None: ...


class RichReporter:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def _tag(self, agent: str) -> str:
        style = AGENT_STYLE.get(agent, "bold")
        return f"[{style}]{agent:>14}[/]"

    def stage(self, agent: str, title: str) -> None:
        self.console.rule(f"[{AGENT_STYLE.get(agent, 'bold')}]{title}")

    def step(self, agent: str, message: str) -> None:
        self.console.print(f"{self._tag(agent)}  {message}", highlight=False)

    def ok(self, agent: str, message: str) -> None:
        self.console.print(
            f"{self._tag(agent)}  [green]✔[/] {message}", highlight=False
        )

    def warn(self, agent: str, message: str) -> None:
        self.console.print(
            f"{self._tag(agent)}  [yellow]⚠[/] {message}", highlight=False
        )

    def panel(self, title: str, markdown: str, *, style: str = "cyan") -> None:
        self.console.print(Panel(Markdown(markdown), title=title, border_style=style))

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        table = Table(title=title, title_justify="left", header_style="bold")
        for column in columns:
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        self.console.print(table)


class NullReporter:
    """Silent reporter for tests; keeps messages for assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def stage(self, agent: str, title: str) -> None:
        self.messages.append(f"[{agent}] == {title}")

    def step(self, agent: str, message: str) -> None:
        self.messages.append(f"[{agent}] {message}")

    def ok(self, agent: str, message: str) -> None:
        self.messages.append(f"[{agent}] OK {message}")

    def warn(self, agent: str, message: str) -> None:
        self.messages.append(f"[{agent}] WARN {message}")

    def panel(self, title: str, markdown: str, *, style: str = "cyan") -> None:
        self.messages.append(f"[panel] {title}")

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        self.messages.append(f"[table] {title} ({len(rows)} rows)")
