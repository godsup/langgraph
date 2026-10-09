"""Markdown reports rendered from Jinja2 templates (deterministic layout)."""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined


def _money(value: float | None, currency: str = "EUR") -> str:
    return "price not set" if value is None else f"{value:,.2f} {currency}"


def _bytes(value: Any) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return str(value)


def _cell(value: Any) -> str:
    """Make a value safe for a Markdown table cell."""
    return (
        str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")
    )


def environment() -> Environment:
    env = Environment(
        loader=PackageLoader("migrate", "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 - Markdown/HCL output, not HTML
    )
    env.filters.update(money=_money, bytes=_bytes, cell=_cell)
    return env


def render(template: str, **context: Any) -> str:
    return environment().get_template(template).render(**context)
