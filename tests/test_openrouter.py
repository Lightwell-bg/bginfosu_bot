import json

import httpx
import pytest

from bginfo.ai import openrouter
from bginfo.ai.openrouter import AiError, OpenRouterClient


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _s(_):
        return None

    monkeypatch.setattr(openrouter.asyncio, "sleep", _s)


def client(handler, retries=2):
    return OpenRouterClient(
        "k", base_url="https://x/api", timeout_s=5, retries=retries, app_title="T", http_referer="R",
        transport=httpx.MockTransport(handler),
    )


def chat_resp(content, usage=None):
    body = {"choices": [{"message": {"content": content}}], "model": "m"}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


SCHEMA = {"type": "object"}


async def test_retry_503_then_success():
    calls = []

    def h(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503)
        return chat_resp('{"a": 1}', {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.01})

    c = client(h)
    out, usage = await c.chat_json("m", [{"role": "user", "content": "x"}], SCHEMA, "s", 100)
    assert out == {"a": 1} and len(calls) == 2
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (3, 4, 0.01)
    req = calls[0]
    assert req.headers["authorization"] == "Bearer k" and req.headers["x-title"] == "T"
    body = json.loads(req.content)
    assert body["response_format"]["json_schema"]["strict"] is True and body["usage"] == {"include": True}
    await c.aclose()


async def test_no_retry_on_400():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(400)

    c = client(h)
    with pytest.raises(AiError) as e:
        await c.chat_json("m", [], SCHEMA, "s", 10)
    assert e.value.kind == "http" and len(calls) == 1
    await c.aclose()


async def test_retries_limited_on_503():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503)

    c = client(h, retries=2)
    with pytest.raises(AiError) as e:
        await c.chat_json("m", [], SCHEMA, "s", 10)
    assert e.value.kind == "http" and len(calls) == 3
    await c.aclose()


async def test_timeout():
    def h(req):
        raise httpx.ReadTimeout("t", request=req)

    c = client(h, retries=1)
    with pytest.raises(AiError) as e:
        await c.chat_json("m", [], SCHEMA, "s", 10)
    assert e.value.kind == "timeout"
    await c.aclose()


async def test_invalid_json_content():
    c = client(lambda r: chat_resp("not json", {"prompt_tokens": 1, "completion_tokens": 1}))
    with pytest.raises(AiError) as e:
        await c.chat_json("m", [], SCHEMA, "s", 10)
    assert e.value.kind == "invalid" and e.value.usage is not None
    await c.aclose()


async def test_cost_missing_is_none():
    c = client(lambda r: chat_resp("{}", {"prompt_tokens": 1, "completion_tokens": 2}))
    _, usage = await c.chat_json("m", [], SCHEMA, "s", 10)
    assert usage.cost_usd is None
    await c.aclose()


async def test_systemone_ok_and_invalid():
    def h(req):
        assert req.url.path.endswith("/systemone")
        body = json.loads(req.content)
        if body["state"] == "bad":
            return httpx.Response(200, json={"usage": {}})
        return httpx.Response(
            200,
            json={"answers": {"q": 1}, "usage": {"input_tokens": 5, "output_tokens": 6, "cost": 0.5}, "model": "j"},
        )

    c = client(h)
    ans, usage = await c.systemone("j", "ok", {"q": {}}, session_id="s1")
    assert ans == {"q": 1} and usage.cost_usd == 0.5 and usage.input_tokens == 5
    with pytest.raises(AiError) as e:
        await c.systemone("j", "bad", {})
    assert e.value.kind == "invalid"
    await c.aclose()
