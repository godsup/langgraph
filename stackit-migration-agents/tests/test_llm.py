from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from migrate.audit import AuditLog, Redactor
from migrate.config import Settings
from migrate.llm import (
    LLMGateway,
    LLMNotConfiguredError,
    RecordedChatModel,
    SafeChatOpenAI,
    bind_tools_if_any,
    build_chat_model,
)


@tool
def get_metric(name: str) -> str:
    """Return a metric."""
    return name


def _model() -> SafeChatOpenAI:
    return SafeChatOpenAI(
        model="some-open-weight-model",
        api_key="sk-test",
        base_url="http://localhost:9/v1",
    )


def test_no_tools_key_when_no_tools_bound() -> None:
    payload = _model()._get_request_payload([HumanMessage("hi")])
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_empty_tools_list_is_stripped() -> None:
    # Even if some caller passes tools=[], the request must not contain it.
    payload = _model()._get_request_payload(
        [HumanMessage("hi")], tools=[], tool_choice="auto"
    )
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_bound_tools_are_sent() -> None:
    bound = _model().bind_tools([get_metric])
    payload = _model()._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    assert [t["function"]["name"] for t in payload["tools"]] == ["get_metric"]


def test_bind_tools_if_any_does_not_bind_empty_list() -> None:
    model = _model()
    assert bind_tools_if_any(model, []) is model
    assert bind_tools_if_any(model, [get_metric]) is not model


def test_build_chat_model_requires_api_key() -> None:
    with pytest.raises(LLMNotConfiguredError):
        build_chat_model(Settings(_env_file=None, llm_api_key=None))


def test_provider_switch_is_config_only() -> None:
    settings = Settings(
        _env_file=None,
        llm_model="some-model",
        llm_api_key="key-123456",
        llm_base_url="https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1",
    )
    model = build_chat_model(settings)
    assert model.model_name == "some-model"
    assert str(model.openai_api_base).startswith(
        "https://api.openai-compat.model-serving"
    )
    assert Settings(_env_file=None, llm_base_url="").llm_base_url is None


class Choice(BaseModel):
    option: str
    reason: str


def _gateway(tmp_path: Path, responses: dict) -> tuple[LLMGateway, AuditLog]:
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "pick.json").write_text(json.dumps({"responses": responses}))
    audit = AuditLog(tmp_path / "audit.jsonl", Redactor(["supersecretvalue"]))
    return LLMGateway(
        Settings(_env_file=None), audit, recorded_dir=tmp_path / "llm"
    ), audit


def test_recorded_decide_validates_and_audits(tmp_path: Path) -> None:
    gateway, audit = _gateway(tmp_path, {"r1": {"option": "a", "reason": "fits"}})
    decision = asyncio.run(
        gateway.decide(
            agent="t",
            task="pick",
            key="r1",
            system="s",
            prompt="p supersecretvalue",
            schema=Choice,
        )
    )
    assert decision.value == Choice(option="a", reason="fits")
    entry = audit.entries()[-1]
    assert entry["action"] == "llm.decide"
    assert "supersecretvalue" not in json.dumps(entry)


def test_decide_falls_back_on_invalid_response(tmp_path: Path) -> None:
    gateway, audit = _gateway(tmp_path, {"r1": {"wrong": "shape"}})
    fallback = Choice(option="default", reason="rule")
    decision = asyncio.run(
        gateway.decide(
            agent="t",
            task="pick",
            key="r1",
            system="s",
            prompt="p",
            schema=Choice,
            fallback=fallback,
        )
    )
    assert decision.fallback_used and decision.value == fallback
    assert audit.entries()[-1]["status"] == "fallback"


def test_recorded_chat_model_replays_tool_calls() -> None:
    model = RecordedChatModel(
        responses=[
            {
                "content": "",
                "tool_calls": [
                    {"name": "get_metric", "args": {"name": "cpu"}, "id": "c1"}
                ],
            },
            {"content": "done"},
        ]
    )
    first = model.bind_tools([get_metric]).invoke([HumanMessage("x")])
    assert first.tool_calls[0]["name"] == "get_metric"
    assert model.invoke([HumanMessage("x")]).content == "done"
