"""Терпимый к схеме разбор ответов провайдера.

Точные поля внутри records у разных методов отличаются и не все задокументированы
в нашем распоряжении, поэтому ищем значения по списку возможных ключей.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable


def records(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not data:
        return []
    for key in ("records", "items", "result", "data", "list"):
        v = data.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
        if isinstance(v, dict):
            inner = records(v)
            if inner:
                return inner
    return []


def count(data: dict[str, Any] | None) -> int:
    if not data:
        return 0
    for key in ("active_count", "found", "count", "total"):
        v = data.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
    return len(records(data))


def pick(d: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not d:
        return default
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = d.get(k, lower.get(k.lower()))
        if v not in (None, "", []):
            return v
    return default


def first_record(data: dict[str, Any] | None) -> dict[str, Any]:
    recs = records(data)
    return recs[0] if recs else (data or {})


def parse_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not isinstance(v, str) or not v:
        return None
    v = v.strip()
    try:
        return datetime.fromisoformat(v).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(v[:10], fmt).date()
        except ValueError:
            continue
    return None


def fmt_date(v: Any) -> str:
    d = parse_date(v)
    return d.strftime("%d.%m.%Y") if d else (str(v) if v not in (None, "") else "—")


def to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v)
        return int(digits) if digits else None
    return None


def fmt_num(v: Any, unit: str = "") -> str:
    n = to_int(v)
    if n is None:
        return "—"
    s = f"{n:,}".replace(",", " ")
    return f"{s} {unit}".strip()


# Подписи для типичных ключей — используются в «универсальном» показе записей.
LABELS = {
    "date": "Дата", "datefrom": "С", "dateto": "По", "from": "С", "to": "По",
    "registrationdate": "Дата регистрации", "exclusiondate": "Дата исключения",
    "number": "Номер", "type": "Тип", "status": "Статус", "region": "Регион",
    "initiator": "Кем наложено", "issuer": "Инициатор", "amount": "Сумма", "paid": "Оплачен",
    "article": "Статья", "company": "Компания", "pledgee": "Залогодержатель",
    "permitnumber": "Номер разрешения", "mileage": "Пробег", "price": "Цена", "site": "Площадка",
    "city": "Город", "plate": "Госномер", "operation": "Операция", "ownertype": "Тип владельца",
    "place": "Место", "state": "Состояние ТС", "participants": "Участников", "dealer": "Дилер",
    "works": "Работы", "series": "Серия", "unlimited": "Без ограничений", "source": "Источник",
    "brand": "Марка", "damagepoints": "Зоны повреждений", "dealer_name": "Дилер",
    "unpaid": "Не оплачено", "currency": "Валюта", "model": "Модель", "year": "Год", "color": "Цвет", "vin": "VIN",
}


def label(key: str) -> str:
    return LABELS.get(key.lower(), re.sub(r"([a-z])([A-Z])", r"\1 \2", key).replace("_", " ").capitalize())


def display_value(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, (list, tuple)):
        return ", ".join(display_value(x) for x in v) if v else "—"
    if isinstance(v, dict):
        return "; ".join(f"{label(k)}: {display_value(x)}" for k, x in v.items())
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T.*)?", v):
        return fmt_date(v)
    return str(v)


def flatten(record: dict[str, Any], skip: Iterable[str] = ()) -> list[tuple[str, str]]:
    skip_l = {s.lower() for s in skip}
    return [(label(k), display_value(v)) for k, v in record.items() if k.lower() not in skip_l]
