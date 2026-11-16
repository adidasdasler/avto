"""Офлайн-провайдер для разработки и тестов.

Отдаёт ответы-образцы в общей обёртке avtovincod.ru (success/query/found/records/
testMode/billing). Поля внутри records — наше предположение о схеме: перед
боевым запуском сверить их с настоящими тестовыми ответами
(scripts/dump_test_responses.py сохраняет их в tests/fixtures/live/).
"""
from __future__ import annotations

import copy
from typing import Any

from .client import ProviderError, ProviderResponse, build_response
from .methods import METHODS

_VIN = "X7TEST00000000001"

SAMPLES: dict[str, dict[str, Any]] = {
    "decode": {"found": 1, "records": [{
        "brand": "LADA", "model": "VESTA", "year": 2019, "manufacturer": "АО «АвтоВАЗ»",
        "country": "Россия", "bodyType": "Седан", "engineVolume": 1596, "fuel": "Бензин"}]},
    "brief": {"found": 1, "records": [{
        "brand": "LADA", "model": "VESTA", "year": 2019, "color": "Белый", "vin": _VIN,
        "plate": "Т000ТТ00", "body": "нет данных", "engineNumber": "21129 0000001",
        "engineVolume": 1596, "powerHp": 106, "powerKw": 78, "category": "B",
        "vehicleType": "Седан", "sts": "0000000000"}]},
    "gos2vin": {"found": 1, "vin": _VIN, "records": [{"vin": _VIN, "brand": "LADA", "model": "VESTA"}]},
    "vin2gos": {"found": 1, "records": [{"plate": "Т000ТТ00"}]},
    "vin": {"found": 2, "records": [
        {"from": "2019-07-03", "to": "2021-04-05", "ownerType": "Физическое лицо",
         "operation": "Первичная регистрация", "ownerFio": "Тестов Тест Тестович"},
        {"from": "2021-04-05", "to": None, "ownerType": "Юридическое лицо",
         "operation": "Регистрация в связи со сменой собственника", "ownerInn": "0000000000"}]},
    "score": {"found": 1, "wanted": False, "records": [
        {"type": "Запрет на регистрационные действия", "status": "Активно",
         "region": "Город Москва", "initiator": "Судебный пристав", "date": "2025-01-15",
         "number": "000000/7741", "debtorFio": "Тестов Тест Тестович"}]},
    "taxi": {"found": 1, "records": [
        {"permitNumber": "ТЕСТ-0001", "region": "Москва", "status": "Аннулировано",
         "dateFrom": "2020-03-01", "dateTo": "2023-03-01", "company": "ООО «Тестовое такси»"}]},
    "pledge": {"found": 2, "active_count": 1, "records": [
        {"registrationDate": "2021-06-07", "number": "2021-000-000000-000",
         "type": "Возникновение", "status": "В залоге", "pledgee": "АО «Тестбанк»",
         "pledgor": "Тестов Тест Тестович", "pledgorBirth": "1980-01-01"},
        {"registrationDate": "2020-06-29", "exclusionDate": "2021-01-26",
         "number": "2020-000-000000-000", "type": "Исключение", "status": "Исключён из залога",
         "pledgee": "ПАО «Тестовый банк»"}]},
    "elpts": {"found": 0, "records": [], "paperPts": "00ТТ000000"},
    "probeg": {"found": 4, "records": [
        {"date": "2020-02-04", "mileage": 26000, "type": "Диагностическая карта", "number": "000000000000001"},
        {"date": "2021-02-09", "mileage": 61000, "type": "Диагностическая карта", "number": "000000000000002"},
        {"date": "2022-03-01", "mileage": 98500, "type": "Диагностическая карта", "number": "000000000000003"},
        {"date": "2023-03-12", "mileage": 87000, "type": "Объявление", "number": None}]},
    "accidents": {"found": 1, "records": [
        {"date": "2022-01-26", "number": "000000001", "type": "Столкновение", "state": "Повреждено",
         "participants": 2, "place": "Москва", "damagePoints": ["06", "07"]}]},
    "accidents-rt": {"status": "done", "found": 0, "records": []},
    "fines": {"found": 2, "unpaid_count": 1, "records": [
        {"date": "2025-01-14T12:00:00", "article": "12.9 ч.2 — Превышение скорости на 20–40 км/ч",
         "amount": 500, "paid": False, "issuer": "ЦАФАП ГИБДД"},
        {"date": "2024-09-01T17:35:00", "article": "12.6 — Ремень безопасности",
         "amount": 1000, "paid": True, "issuer": "ГИБДД"}]},
    "osago": {"found": 1, "records": [{"company": "АО «Тест-страхование»", "series": "ХХХ",
                                         "number": "0000000000", "status": "Действует", "dateTo": "2026-05-01"}]},
    "osago-history": {"found": 2, "records": [
        {"company": "АО «Тест-страхование»", "dateFrom": "2025-05-02", "dateTo": "2026-05-01", "unlimited": False},
        {"company": "СПАО «Тест»", "dateFrom": "2024-05-02", "dateTo": "2025-05-01", "unlimited": True}]},
    "plate-history": {"found": 2, "records": [{"plate": "А000АА00", "dateFrom": "2019-07-03", "dateTo": "2021-04-05"},
                                               {"plate": "Т000ТТ00", "dateFrom": "2021-04-05", "dateTo": None}]},
    "pros-cons": {"found": 1, "records": [{"pros": ["Недорогое обслуживание", "Просторный салон"],
                                            "cons": ["Слабая шумоизоляция", "Вариатор/робот на части версий"]}]},
    "nomerogram": {"found": 2, "records": [
        {"date": "2023-05-10", "url": "https://example.invalid/photo1.jpg", "source": "Номерограм"},
        {"date": "2022-08-01", "url": "https://example.invalid/photo2.jpg", "source": "Номерограм"}]},
    "sharing": {"found": 0, "records": []},
    "service": {"found": 1, "records": [{"date": "2021-02-01", "mileage": 60000, "dealer": "Тест-Авто",
                                          "works": "ТО-4"}]},
    "lizing": {"found": 0, "records": []},
    "customs": {"found": 0, "records": []},
    "offers": {"found": 1, "records": [{"date": "2023-03-12", "price": 950000, "mileage": 87000,
                                         "site": "auto.example", "city": "Москва",
                                         "sellerPhone": "+7 900 000-00-00"}]},
    "recalls": {"found": 0, "records": []},
    "price": {"found": 1, "records": [{"min": 870000, "max": 1080000, "avg": 965000, "currency": "RUB"}]},
    "lawsuits": {"found": 0, "records": []},
    "autopark": {"found": 1, "records": [{"engineNumber": "21129 0000001"}]},
    "body": {"found": 0, "records": []},
    "score-body": {"found": 0, "records": []},
    "invalid-reg": {"found": 0, "records": []},
    "truck-pass": {"found": 0, "records": []},
    "rnis": {"found": 0, "records": []},
    "ktg-permit": {"found": 0, "records": []},
}


class MockProvider:
    """Совместим с AvtovincodClient. not_allowed — методы, на которые эмулируем NOT_ALLOWED."""

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
        if method == "accidents-rt" and "task" not in params:
            body: dict[str, Any] = {"status": "pending", "taskId": "TEST-TASK-0001"}
        else:
            body = copy.deepcopy(self.overrides.get(method, SAMPLES.get(method, {"found": 0, "records": []})))
        data = {"success": 1, "query": dict(params), **body, "testMode": True,
                "billing": {"charged": 0, "currency": "RUB", "billed": False, "balance": 0, "requestId": None}}
        return build_response(method, dict(params), data, {"X-Test-Mode": "1"})
