"""Optional anomaly notifications to Slack or Microsoft Teams.

Slack: incoming-webhook JSON `{"text": ...}`.
Teams: a message with an Adaptive Card, as accepted by Teams Workflows
("When a Teams webhook request is received"). The webhook URL is a secret:
it comes from WEBHOOK_URL and is redacted from the audit log.
"""

from __future__ import annotations

from typing import Any

import httpx

from migrate.state import Anomaly


def _text(run_id: str, anomalies: list[Anomaly]) -> str:
    lines = [
        f"STACKIT migration {run_id}: {len(anomalies)} anomaly(ies) detected (nothing changed automatically)"
    ]
    for a in anomalies:
        target = ", ".join(f"{k}={v}" for k, v in a.labels.items())
        lines.append(
            f"- [{a.severity}] {a.rule} {target}: {a.value:g} (threshold {a.threshold:g})"
        )
    return "\n".join(lines)


def payload(kind: str, run_id: str, anomalies: list[Anomaly]) -> dict[str, Any]:
    text = _text(run_id, anomalies)
    if kind == "teams":
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": line, "wrap": True}
                            for line in text.splitlines()
                        ],
                    },
                }
            ],
        }
    return {"text": text}


def send(
    url: str, kind: str, run_id: str, anomalies: list[Anomaly], *, timeout: float = 10.0
) -> int:
    response = httpx.post(url, json=payload(kind, run_id, anomalies), timeout=timeout)
    response.raise_for_status()
    return response.status_code
