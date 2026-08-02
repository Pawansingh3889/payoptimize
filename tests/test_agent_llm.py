"""The OpenAI wrapper: parses what matters, refuses what must not leave."""

from __future__ import annotations

import pytest
from agent_stub import OpenAIStub, completion

from payoptimize.agent import llm

MESSAGES = [{"role": "user", "content": "why did pay_1 fail?"}]


def test_parses_a_plain_answer() -> None:
    stub = OpenAIStub([completion(content="it was declined", tokens_in=42, tokens_out=7)])
    with stub.client() as http:
        reply = llm.complete(MESSAGES, http=http)

    assert reply.content == "it was declined"
    assert reply.tool_calls == []
    assert (reply.tokens_in, reply.tokens_out) == (42, 7)
    body = stub.requests[0]
    assert body["model"] == "gpt-5"
    assert "temperature" not in body
    assert "tools" not in body


def test_parses_tool_calls_and_keeps_the_raw_message() -> None:
    stub = OpenAIStub([completion(tool_calls=[("get_payment", {"payment_id": "pay_1"})])])
    tools = [{"type": "function", "function": {"name": "get_payment", "parameters": {}}}]
    with stub.client() as http:
        reply = llm.complete(MESSAGES, tools, http=http)

    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call.name == "get_payment"
    assert call.arguments == {"payment_id": "pay_1"}
    assert reply.raw_message.get("tool_calls")  # verbatim, for the echo-back
    assert stub.requests[0]["tools"] == tools


def test_denylisted_value_never_leaves_the_process() -> None:
    stub = OpenAIStub([completion(content="never reached")])
    poisoned = [{"role": "user", "content": "tenant email is leak@secret.example"}]
    with stub.client() as http, pytest.raises(llm.AgentError, match="redaction failure"):
        llm.complete(poisoned, denylist=("leak@secret.example",), http=http)

    assert stub.requests == []  # refused before any bytes moved


def test_unset_key_is_a_sentence_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(llm.AgentError, match="OPENAI_API_KEY"):
        llm.complete(MESSAGES)


def test_api_errors_raise_with_the_status() -> None:
    stub = OpenAIStub(status=500)
    with stub.client() as http, pytest.raises(llm.AgentError, match="500"):
        llm.complete(MESSAGES, http=http)


def test_unparseable_tool_arguments_raise() -> None:
    reply = completion()
    reply["choices"][0]["message"]["tool_calls"] = [
        {"id": "call_0", "type": "function", "function": {"name": "x", "arguments": "{not json"}}
    ]
    stub = OpenAIStub([reply])
    with stub.client() as http, pytest.raises(llm.AgentError, match="unparseable"):
        llm.complete(MESSAGES, http=http)


def test_model_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYOPTIMIZE_AGENT_MODEL", "gpt-4.1")
    stub = OpenAIStub([completion(content="ok")])
    with stub.client() as http:
        llm.complete(MESSAGES, http=http)
        llm.complete(MESSAGES, model="gpt-5", http=http)
    assert [request["model"] for request in stub.requests] == ["gpt-4.1", "gpt-5"]
