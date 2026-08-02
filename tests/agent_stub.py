"""A scripted OpenAI, for driving the agent without a network or a bill.

Mirrors how the Prava tests use MockTransport: the stub replays canned
chat-completions responses in order and records every request body it saw,
so a test can assert both what the agent did and what it sent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


def completion(
    content: str = "",
    tool_calls: list[tuple[str, dict[str, Any]]] | None = None,
    *,
    tokens_in: int = 10,
    tokens_out: int = 5,
) -> dict[str, Any]:
    """One chat-completions response body, in the API's own shape."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (name, arguments) in enumerate(tool_calls)
        ]
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


class OpenAIStub:
    def __init__(self, replies: list[dict[str, Any]] | None = None, *, status: int = 200) -> None:
        self.replies = list(replies or [])
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode()))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        if self.replies:
            return httpx.Response(200, json=self.replies.pop(0))
        # Running past the script means the loop asked more than the test
        # planned for; answer something terminal so the failure is readable.
        return httpx.Response(200, json=completion(content="(past the scripted replies)"))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handler))
