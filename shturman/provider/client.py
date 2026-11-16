"""HTTP-клиент API avtovincod.ru.

Протокол (по документации провайдера):
  GET https://api.avtovincod.ru/<method>?<param>=<value>
  Authorization: Bearer <token>
Ответ — JSON с полями success, query, found, records, billing{charged, billed,
balance, ...}; в тестовом режиме дополнительно "testMode": true и заголовок
X-Test-Mode: 1. Ошибки доступа: NO_TOKEN, TOKEN_INVALID, NOT_ALLOWED.
Лимит — 60 запросов в минуту на токен.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .methods import METHODS


class ProviderError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None):
        self.code = code
        self.message = message or code
        self.status = status
        super().__init__(f"{code}: {self.message}")


@dataclass
class ProviderResponse:
    method: str
    params: dict[str, str]
    data: dict[str, Any]
    test_mode: bool = False
    charged: float = 0.0
    billed: bool = False
    balance: float | None = None
    from_cache: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    async def call(self, method: str, **params: str) -> ProviderResponse: ...

    async def aclose(self) -> None: ...


class RateLimiter:
    """Скользящее окно: не больше `limit` вызовов за `period` секунд."""

    def __init__(self, limit: int, period: float = 60.0):
        self.limit = limit
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._calls[0]) + 0.01)


def _error_from_payload(data: dict[str, Any]) -> tuple[str, str] | None:
    """Достаёт код ошибки из ответа. Формат ошибки в документации не зафиксирован,
    поэтому разбираем несколько типичных вариантов."""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or "ERROR"), str(err.get("message") or err.get("text") or "")
    if isinstance(err, str) and err:
        return err, str(data.get("message") or data.get("error_text") or "")
    if data.get("success") in (0, False, "0"):
        code = data.get("code") or data.get("error_code") or "ERROR"
        return str(code), str(data.get("message") or "")
    return None


def build_response(method: str, params: dict[str, str], data: dict[str, Any],
                   headers: httpx.Headers | dict | None = None) -> ProviderResponse:
    billing = data.get("billing") or {}
    test_header = (headers or {}).get("X-Test-Mode") == "1" if headers is not None else False
    return ProviderResponse(
        method=method,
        params=params,
        data=data,
        test_mode=bool(data.get("testMode")) or test_header,
        charged=float(billing.get("charged") or 0),
        billed=bool(billing.get("billed")),
        balance=billing.get("balance"),
    )


class AvtovincodClient:
    def __init__(self, token: str, base_url: str = "https://api.avtovincod.ru",
                 rate_per_minute: int = 60, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise ProviderError("NO_TOKEN", "Не задан токен avtovincod.ru (AVC_TOKEN)")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._limiter = RateLimiter(rate_per_minute)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, **params: str) -> ProviderResponse:
        if method not in METHODS:
            raise ValueError(f"Неизвестный метод провайдера: {method}")
        await self._limiter.acquire()
        try:
            resp = await self._http.get(f"/{method}", params=params)
        except httpx.HTTPError as e:
            raise ProviderError("NETWORK", str(e)) from e
        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("BAD_RESPONSE", f"HTTP {resp.status_code}: не JSON", resp.status_code)
        if not isinstance(data, dict):
            raise ProviderError("BAD_RESPONSE", "Ответ не является объектом JSON", resp.status_code)
        err = _error_from_payload(data)
        if err or resp.status_code >= 400:
            code, msg = err or (f"HTTP_{resp.status_code}", "")
            raise ProviderError(code, msg, resp.status_code)
        return build_response(method, dict(params), data, resp.headers)


async def call_accidents_rt(provider: Provider, poll_interval: float = 5.0,
                            max_wait: float = 180.0, **params: str) -> ProviderResponse:
    """Асинхронный метод /accidents-rt: первый вызов возвращает status=pending и
    taskId, затем опрашиваем ?task=<taskId> до status=done."""
    first = await provider.call("accidents-rt", **params)
    status = first.data.get("status")
    task_id = first.data.get("taskId")
    if status != "pending" or not task_id:
        return first
    deadline = time.monotonic() + max_wait
    while True:
        res = await provider.call("accidents-rt", task=str(task_id))
        if res.data.get("status") != "pending":
            res.charged += first.charged
            return res
        if time.monotonic() >= deadline:
            raise ProviderError("TIMEOUT", "Задача ДТП по базе ГИБДД не завершилась вовремя")
        await asyncio.sleep(poll_interval)
