"""Каталог методов API avtovincod.ru.

Источник: раздел документации «Тестовые VIN, госномер и СТС для отладки API».
Для каждого метода указаны параметры, которые он принимает, заголовок раздела
отчёта и TTL кэша (сколько часов ответ считается свежим).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MethodSpec:
    name: str
    title: str
    params: tuple[str, ...]
    cache_ttl_hours: int


_H = 1
_DAY = 24
_WEEK = 24 * 7
_MONTH = 24 * 30

METHODS: dict[str, MethodSpec] = {
    m.name: m
    for m in [
        MethodSpec("vin", "История регистрации", ("vin",), _WEEK),
        MethodSpec("body", "История регистрации по кузову", ("body",), _WEEK),
        MethodSpec("score", "Ограничения и розыск", ("vin",), _DAY),
        MethodSpec("score-body", "Ограничения и розыск по кузову", ("body",), _DAY),
        MethodSpec("gos2vin", "VIN по госномеру", ("plate",), _MONTH),
        MethodSpec("vin2gos", "Госномер по VIN", ("vin",), _WEEK),
        MethodSpec("plate-history", "История госномеров", ("vin", "body"), _WEEK),
        MethodSpec("brief", "Краткие данные ТС", ("vin", "plate"), _MONTH),
        MethodSpec("decode", "Расшифровка VIN", ("vin",), _MONTH * 12),
        MethodSpec("autopark", "Номер двигателя", ("vin", "body", "chassis"), _MONTH),
        MethodSpec("pros-cons", "Плюсы и минусы модели", ("vin", "body"), _MONTH * 3),
        MethodSpec("taxi", "Работа в такси", ("vin", "plate"), _DAY),
        MethodSpec("pledge", "Реестр залогов", ("vin", "body", "chassis"), _DAY),
        MethodSpec("fines", "Штрафы ГИБДД", ("sts", "driverLicense", "numPost"), 6 * _H),
        MethodSpec("elpts", "Электронный ПТС", ("vin", "pts"), _WEEK),
        MethodSpec("probeg", "Техосмотр и пробег", ("vin", "plate", "body"), _WEEK),
        MethodSpec("osago", "Полисы ОСАГО", ("vin", "plate", "body"), _DAY),
        MethodSpec("osago-history", "История полисов ОСАГО", ("vin", "plate", "body"), _WEEK),
        MethodSpec("nomerogram", "Фотографии автомобиля", ("vin", "plate"), _WEEK),
        MethodSpec("sharing", "Каршеринг", ("vin", "plate"), _WEEK),
        MethodSpec("service", "Сервисная история", ("vin",), _WEEK),
        MethodSpec("lizing", "Лизинг", ("vin",), _DAY),
        MethodSpec("accidents", "ДТП (архив)", ("vin",), _WEEK),
        MethodSpec("accidents-rt", "ДТП по базе ГИБДД", ("vin", "body", "task"), _DAY),
        MethodSpec("customs", "Таможня", ("vin",), _MONTH),
        MethodSpec("offers", "Объявления о продаже", ("vin", "plate"), 3 * _DAY),
        MethodSpec("recalls", "Отзывные кампании", ("vin",), _MONTH),
        MethodSpec("price", "Рыночная оценка", ("vin",), _WEEK),
        MethodSpec("lawsuits", "Судебные дела", ("vin", "plate"), _DAY),
        MethodSpec("invalid-reg", "Реестр инвалидов", ("plate",), _WEEK),
        MethodSpec("truck-pass", "Пропуска на МКАД", ("plate", "pass"), _DAY),
        MethodSpec("rnis", "ТС в РНИС", ("plate",), _WEEK),
        MethodSpec("ktg-permit", "Разрешения КТГ", ("plate", "permit"), _DAY),
    ]
}

# Методы, у которых для номера кузова есть отдельный «двойник».
BODY_ALIASES = {"vin": "body", "score": "score-body"}

# Тестовые значения из документации: по ним любой метод отвечает образцом,
# бесплатно и даже при нулевом балансе.
TEST_VALUES = {
    "vin": "X7TEST00000000001",
    "plate": "Т000ТТ00",
    "body": "TEST-0000001",
    "chassis": "TEST-0000001",
    "sts": "0000000000",
    "driverLicense": "0000000000",
    "numPost": "18800000000000000000",
    "pts": "000000000000000",
    "task": "TEST-TASK-0001",
    "pass": "ТТ 0000000",
}
