"""Офлайн-провайдер для разработки и тестов.

Отдаёт настоящие ответы avtovincod.ru на тестовые значения (VIN X7TEST00000000001 и др.),
сохранённые scripts/dump_test_responses.py в shturman/provider/samples/.
Исключения:
- accidents-rt.done.json — ответ на опрос ?task=... мы не сохраняли, он собран по описанию;
- fines.json — на момент снятия провайдер отвечал «Метод сейчас недоступен», мок повторяет это.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .client import ProviderError, ProviderResponse, _error_from_payload, build_response
from .methods import METHODS

SAMPLES_DIR = Path(__file__).parent / "samples"


@lru_cache
def load_sample(name: str) -> dict[str, Any] | None:
    path = SAMPLES_DIR / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class MockProvider:
    """Совместим с AvtovincodClient.

    not_allowed — методы, для которых эмулируем NOT_ALLOWED;
    overrides — подменить ответ метода (тело без billing).
    """

    def __init__(self, not_allowed: set[str] | None = None, overrides: dict[str, dict] | None = None):
        self.not_allowed = not_allowed or set()
        self.overrides = overrides or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def aclose(self) -> None:  # pragma: no cover - нечего закрывать
        return None

    async def call(self, method: str, **params: str) -> ProviderResponse:
        if method not in METHODS:
            raise ValueError(f"Неизвестный метод провайдера: {method}")
        self.calls.append((method, dict(params)))
        if method in self.not_allowed:
            raise ProviderError("NOT_ALLOWED", "Метод не подключён токену", 403)

        if method in self.overrides:
            body = {"success": 1, "query": dict(params), **copy.deepcopy(self.overrides[method])}
        else:
            name = "accidents-rt.done" if method == "accidents-rt" and "task" in params else method
            body = copy.deepcopy(load_sample(name) or {"success": 1, "found": 0, "records": []})
        err = _error_from_payload(body)
        if err:
            raise ProviderError(*err)
        body.setdefault("testMode", True)
        body.setdefault("billing", {"charged": 0, "currency": "RUB", "billed": False, "balance": 0,
                                    "requestId": None})
        return build_response(method, dict(params), body, {"X-Test-Mode": "1"})
