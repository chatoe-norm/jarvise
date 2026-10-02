import json
from unittest.mock import MagicMock

import httpx
import pytest

from jarvise_paper.llm_openrouter import OPENROUTER_URL, OpenRouterError, chat_json

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "{}"}]


def _resp(status: int, content: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": content}}]} if content is not None else {}
    return resp


def test_missing_key_raises_without_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = MagicMock(spec=httpx.Client)
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", client=client)
    assert client.post.call_count == 0


def test_ok_json_and_headers() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(200, json.dumps({"decision": "approve", "reason": "ok"}))
    out = chat_json(MSGS, model="anthropic/claude-sonnet-4.5", api_key="k", client=client)
    assert out == {"decision": "approve", "reason": "ok"}
    args, kwargs = client.post.call_args
    assert args[0] == OPENROUTER_URL
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert kwargs["json"]["model"] == "anthropic/claude-sonnet-4.5"
    assert kwargs["json"]["response_format"] == {"type": "json_object"}


def test_fenced_json_is_accepted() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(200, '```json\n{"decision": "defer", "reason": "x"}\n```')
    assert chat_json(MSGS, model="m", api_key="k", client=client)["decision"] == "defer"


def test_4xx_raises_immediately() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(401)
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
    assert client.post.call_count == 1


def test_5xx_retries_once_then_raises() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(503)
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
    assert client.post.call_count == 2


def test_timeout_then_success() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = [httpx.ReadTimeout("slow"), _resp(200, '{"decision":"reject","reason":"r"}')]
    assert chat_json(MSGS, model="m", api_key="k", client=client)["decision"] == "reject"
    assert client.post.call_count == 2


def test_non_json_and_non_object_raise() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp(200, "sure, approve it")
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
    client.post.return_value = _resp(200, "[1, 2]")
    with pytest.raises(OpenRouterError):
        chat_json(MSGS, model="m", api_key="k", client=client)
