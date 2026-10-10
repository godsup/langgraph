"""Provider-agnostic LLM access.

Any OpenAI-compatible endpoint works (OpenAI, STACKIT AI Model Serving, vLLM,
...). Switching provider is configuration only: `LLM_MODEL`, `LLM_API_KEY`,
`LLM_BASE_URL`.

Known quirk: some open-weight models served on STACKIT treat an empty `tools`
list as "enter tool-calling mode" and hallucinate a function call. We therefore
never send `tools=[]`: `SafeChatOpenAI` strips empty tool parameters from every
request and `bind_tools_if_any` only binds when there is something to bind.

In mock mode the `LLMGateway` replays recorded responses from
`fixtures/llm/<task>.json`, so demos need no API key and are deterministic.
`--live-llm` switches the gateway to the configured model while the clouds
stay mocked.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from migrate.audit import AuditLog, Redactor
from migrate.config import Settings

T = TypeVar("T", bound=BaseModel)

_TOOL_ONLY_PARAMS = ("tools", "tool_choice", "parallel_tool_calls")


class LLMNotConfiguredError(RuntimeError):
    pass


class RecordedResponseMissingError(LookupError):
    pass


class SafeChatOpenAI(ChatOpenAI):
    """`ChatOpenAI` that never sends an empty `tools` list."""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if not payload.get("tools"):
            for key in _TOOL_ONLY_PARAMS:
                payload.pop(key, None)
        return payload


def build_chat_model(settings: Settings) -> SafeChatOpenAI:
    if settings.llm_api_key is None:
        raise LLMNotConfiguredError(
            "LLM_API_KEY is not set. Configure it in .env, or run with --mock "
            "(without --live-llm) to use recorded LLM responses."
        )
    return SafeChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url or None,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        max_retries=2,
        # Chat Completions is the lowest common denominator across
        # OpenAI-compatible servers; the Responses API is OpenAI-only.
        use_responses_api=False,
    )


def bind_tools_if_any(
    model: BaseChatModel, tools: Sequence[BaseTool]
) -> Runnable[LanguageModelInput, BaseMessage]:
    """Bind tools only when there are some. Never binds an empty list."""
    if tools:
        return model.bind_tools(list(tools))
    return model


# --------------------------------------------------------------------------
# Recorded responses (mock mode)
# --------------------------------------------------------------------------


class RecordedChatModel(BaseChatModel):
    """Replays a scripted list of assistant messages (used for tool loops)."""

    responses: list[dict[str, Any]]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "recorded"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> RecordedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.cursor >= len(self.responses):
            message = AIMessage(content="(no further recorded responses)")
        else:
            message = AIMessage(**self.responses[self.cursor])
            self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


class RecordedResponses:
    """Looks up `fixtures/llm/<task>.json` -> {"responses": {key|"*": value}}."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, task: str, key: str) -> Any:
        path = self.directory / f"{task}.json"
        if not path.exists():
            raise RecordedResponseMissingError(
                f"no recorded LLM responses for task {task!r}"
            )
        responses = json.loads(path.read_text(encoding="utf-8")).get("responses", {})
        if key in responses:
            return responses[key]
        if "*" in responses:
            return responses["*"]
        raise RecordedResponseMissingError(
            f"no recorded response for {task!r} / {key!r}"
        )


# --------------------------------------------------------------------------
# Gateway used by all agents
# --------------------------------------------------------------------------


@dataclass
class Decision[T]:
    value: T
    fallback_used: bool = False


class LLMGateway:
    """The single entry point agents use to talk to an LLM.

    - `decide`: structured decision validated against a Pydantic schema.
    - `write`: free text (summaries, explanations).
    - `chat_model`: a raw chat model for tool loops (monitoring triage).

    Every call is audit-logged with redacted inputs, and prompts are scrubbed
    of known secret values before they leave the process.
    """

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        *,
        recorded_dir: Path | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.settings = settings
        self.audit = audit
        self.redactor = redactor or audit.redactor
        self.recorded = RecordedResponses(recorded_dir) if recorded_dir else None
        self._model: SafeChatOpenAI | None = None

    @property
    def mode(self) -> str:
        return "recorded" if self.recorded else f"live:{self.settings.llm_model}"

    def _live_model(self) -> SafeChatOpenAI:
        if self._model is None:
            self._model = build_chat_model(self.settings)
        return self._model

    def _messages(self, system: str, prompt: str) -> list[BaseMessage]:
        return [
            SystemMessage(content=self.redactor.text(system)),
            HumanMessage(content=self.redactor.text(prompt)),
        ]

    async def decide(
        self,
        *,
        agent: str,
        task: str,
        key: str,
        system: str,
        prompt: str,
        schema: type[T],
        fallback: T | None = None,
    ) -> Decision[T]:
        inputs = {"task": task, "key": key, "mode": self.mode, "prompt": prompt}
        try:
            if self.recorded:
                value = schema.model_validate(self.recorded.get(task, key))
            else:
                runnable = self._live_model().with_structured_output(
                    schema, method=self.settings.llm_structured_method
                )
                raw = await runnable.ainvoke(self._messages(system, prompt))
                value = raw if isinstance(raw, schema) else schema.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - any failure may use the fallback
            if fallback is None:
                self.audit.record(
                    agent=agent,
                    action="llm.decide",
                    inputs=inputs,
                    status="error",
                    result={"error": f"{type(exc).__name__}: {exc}"},
                )
                raise
            self.audit.record(
                agent=agent,
                action="llm.decide",
                inputs=inputs,
                status="fallback",
                result={"error": f"{type(exc).__name__}: {exc}", "fallback": fallback},
            )
            return Decision(value=fallback, fallback_used=True)
        self.audit.record(agent=agent, action="llm.decide", inputs=inputs, result=value)
        return Decision(value=value)

    async def write(
        self,
        *,
        agent: str,
        task: str,
        key: str,
        system: str,
        prompt: str,
        fallback: str | None = None,
    ) -> str:
        inputs = {"task": task, "key": key, "mode": self.mode, "prompt": prompt}
        try:
            if self.recorded:
                text = str(self.recorded.get(task, key))
            else:
                message = await self._live_model().ainvoke(
                    self._messages(system, prompt)
                )
                text = str(message.text)
        except Exception as exc:
            if fallback is None:
                self.audit.record(
                    agent=agent,
                    action="llm.write",
                    inputs=inputs,
                    status="error",
                    result={"error": f"{type(exc).__name__}: {exc}"},
                )
                raise
            self.audit.record(
                agent=agent,
                action="llm.write",
                inputs=inputs,
                status="fallback",
                result={"error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback
        text = self.redactor.text(text.strip())
        self.audit.record(agent=agent, action="llm.write", inputs=inputs, result=text)
        return text

    def chat_model(self, *, task: str, key: str) -> BaseChatModel:
        """Chat model for tool-calling loops; recorded script in mock mode."""
        if self.recorded:
            return RecordedChatModel(responses=list(self.recorded.get(task, key)))
        return self._live_model()

    async def investigate(
        self,
        *,
        agent: str,
        task: str,
        key: str,
        system: str,
        prompt: str,
        tools: Sequence[BaseTool],
        execute: Callable[[str, dict[str, Any]], Awaitable[Any]],
        max_steps: int = 4,
        fallback: str,
    ) -> str:
        """Let the LLM call read-only tools before it writes its answer.

        `tools` only describe the allowed tools to the model; every call is
        executed by `execute` (the audited MCP tool box), and names outside the
        allowed set are refused. The final answer is requested from the model
        without tools, so an empty `tools` list is never sent.
        """
        allowed = {tool.name for tool in tools}
        inputs = {
            "task": task,
            "key": key,
            "mode": self.mode,
            "prompt": prompt,
            "tools": sorted(allowed),
        }
        try:
            model = self.chat_model(task=task, key=key)
            bound = bind_tools_if_any(model, tools)
            messages: list[BaseMessage] = self._messages(system, prompt)
            answer: BaseMessage | None = None
            for _ in range(max_steps):
                answer = await bound.ainvoke(messages)
                messages.append(answer)
                calls = getattr(answer, "tool_calls", None) or []
                if not calls:
                    break
                for call in calls:
                    if call["name"] in allowed:
                        try:
                            result: Any = await execute(
                                call["name"], dict(call.get("args") or {})
                            )
                        except Exception as exc:  # report tool errors back to the model
                            result = {"error": str(exc)[:500]}
                    else:
                        result = {"error": f"tool {call['name']!r} is not allowed"}
                    content = self.redactor.text(json.dumps(result, default=str))[:4000]
                    messages.append(
                        ToolMessage(
                            content=content, tool_call_id=call.get("id") or call["name"]
                        )
                    )
                answer = None
            if answer is None:  # tool budget used up: ask for the answer without tools
                answer = await model.ainvoke(messages)
            text = str(answer.text)
        except Exception as exc:  # noqa: BLE001 - triage must not break monitoring
            self.audit.record(
                agent=agent,
                action="llm.investigate",
                inputs=inputs,
                status="fallback",
                result={"error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback
        text = self.redactor.text(text.strip()) or fallback
        self.audit.record(
            agent=agent, action="llm.investigate", inputs=inputs, result=text
        )
        return text
