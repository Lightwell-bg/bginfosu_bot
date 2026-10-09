"""Асинхронный клиент OpenRouter (JEV systemone и chat/completions со строгой JSON-схемой).

В лог попадают только тип вызова, модель, статус и задержка: ни ключ, ни тексты запросов.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

from bginfo.logsafe import safe_exc

log = logging.getLogger(__name__)

_RETRY_PAUSES = (0.5, 1.5)


@dataclass
class AiUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int


class AiError(Exception):
    """kind: timeout | http | invalid | budget | limit | disabled."""

    def __init__(self, kind: str, message: str = "", usage: AiUsage | None = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message
        self.usage = usage


def _num(value, cast, default):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_s: float,
        retries: int,
        app_title: str = "",
        http_referer: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if http_referer:
            headers["HTTP-Referer"] = http_referer
        if app_title:
            headers["X-Title"] = app_title
        self._base = base_url.rstrip("/")
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout_s, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, call: str, model: str, path: str, body: dict) -> tuple[dict, int]:
        """POST с повторами на таймаут, 429 и 5xx. Возвращает (json, latency_ms)."""
        url = f"{self._base}{path}"
        last: AiError | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(_RETRY_PAUSES[min(attempt - 1, len(_RETRY_PAUSES) - 1)])
            started = time.monotonic()
            try:
                resp = await self._client.post(url, json=body)
            except httpx.TimeoutException:
                ms = int((time.monotonic() - started) * 1000)
                log.warning("ai %s model=%s timeout latency_ms=%d attempt=%d", call, model, ms, attempt + 1)
                last = AiError("timeout", "request timed out")
                continue
            except httpx.HTTPError as exc:
                ms = int((time.monotonic() - started) * 1000)
                log.warning("ai %s model=%s transport error latency_ms=%d: %s", call, model, ms, safe_exc(exc))
                raise AiError("http", type(exc).__name__) from exc
            ms = int((time.monotonic() - started) * 1000)
            log.info("ai %s model=%s status=%d latency_ms=%d", call, model, resp.status_code, ms)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = AiError("http", f"status {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise AiError("http", f"status {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise AiError("invalid", "response is not JSON") from exc
            if not isinstance(data, dict):
                raise AiError("invalid", "response is not an object")
            return data, ms
        assert last is not None
        raise last

    async def systemone(
        self, model: str, state: dict | str, questions: dict, session_id: str | None = None
    ) -> tuple[dict, AiUsage]:
        body: dict = {"model": model, "state": state, "questions": questions}
        if session_id:
            body["session_id"] = session_id
        data, ms = await self._post("systemone", model, "/systemone", body)
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = u.get("cost")
        usage = AiUsage(
            model=str(data.get("model") or model),
            input_tokens=_num(u.get("input_tokens"), int, 0),
            output_tokens=_num(u.get("output_tokens"), int, 0),
            cost_usd=_num(cost, float, None) if cost is not None else None,
            latency_ms=ms,
        )
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise AiError("invalid", "answers missing", usage=usage)
        return answers, usage

    async def chat_json(
        self,
        model: str,
        messages: list[dict],
        json_schema: dict,
        schema_name: str,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> tuple[dict, AiUsage]:
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            },
            "usage": {"include": True},
        }
        data, ms = await self._post("chat_json", model, "/chat/completions", body)
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = u.get("cost")
        usage = AiUsage(
            model=str(data.get("model") or model),
            input_tokens=_num(u.get("prompt_tokens"), int, 0),
            output_tokens=_num(u.get("completion_tokens"), int, 0),
            cost_usd=_num(cost, float, None) if cost is not None else None,
            latency_ms=ms,
        )
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AiError("invalid", "bad completion content", usage=usage) from exc
        if not isinstance(parsed, dict):
            raise AiError("invalid", "completion is not an object", usage=usage)
        return parsed, usage
