"""The one place this system talks to OpenAI.

Same shape as providers/prava.py, for the same reasons: a synchronous module
with an injectable httpx client, so every test drives it with a MockTransport
and the async app wraps calls in a threadpool. No SDK — the API is one POST,
and a dependency would only put distance between us and the request body we
are contractually obliged to inspect.

That inspection is the point of this file: `complete` asserts the redaction
denylist against the serialized body *at the send*, after every layer above
has had its say. A redaction bug upstream becomes a loud AgentError here, not
a tenant's email in a third party's logs.

The payload is deliberately minimal — model, messages, tools. No temperature:
gpt-5 rejects non-default values, and defaults are what we want anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import config

DEFAULT_TIMEOUT = 120.0  # reasoning models legitimately think for a while


class AgentError(RuntimeError):
    """The agent could not complete a model call — configuration, transport,
    or the send-time privacy assert."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall]
    tokens_in: int
    tokens_out: int
    # The assistant message exactly as the API returned it, because the next
    # request must echo it back verbatim for the tool protocol to hold.
    raw_message: dict[str, Any] = field(default_factory=dict)


def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError as exc:
            raise AgentError(
                f"model returned unparseable tool arguments: {function.get('arguments')!r:.200}"
            ) from exc
        if not isinstance(arguments, dict):
            raise AgentError("model returned tool arguments that are not an object")
        calls.append(
            ToolCall(
                id=str(call.get("id", "")), name=str(function.get("name", "")), arguments=arguments
            )
        )
    return calls


def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    denylist: tuple[str, ...] = (),
    model: str | None = None,
    http: httpx.Client | None = None,
) -> LLMReply:
    """One chat-completions call. Raises AgentError on anything unusual —
    an agent that guesses at a half-answer is worse than one that says why."""
    key = config.openai_api_key()
    if not key:
        raise AgentError(
            "OPENAI_API_KEY is unset — the ops agent is not configured on this deployment"
        )
    body: dict[str, Any] = {"model": model or config.agent_model(), "messages": messages}
    if tools:
        body["tools"] = tools
    payload = json.dumps(body)

    # The privacy boundary's last line: nothing on the denylist leaves, ever.
    # This runs on the serialized payload so no nesting can hide a value.
    for value in denylist:
        if value and value in payload:
            raise AgentError(
                "redaction failure: a denylisted value reached the outgoing request body —"
                " refusing to send"
            )

    client = http or httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        response = client.post(
            f"{config.openai_api_base()}/v1/chat/completions",
            content=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    finally:
        if http is None:
            client.close()
    if response.status_code != 200:
        raise AgentError(f"OpenAI returned {response.status_code}: {response.text[:200]}")

    parsed = response.json()
    choices = parsed.get("choices") or []
    if not choices:
        raise AgentError("OpenAI returned no choices")
    message = choices[0].get("message") or {}
    usage = parsed.get("usage") or {}
    return LLMReply(
        content=str(message.get("content") or ""),
        tool_calls=_parse_tool_calls(message),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        raw_message=message,
    )
