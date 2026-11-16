"""Правила «светофора»: по ответу каждого метода — статус и короткий вывод."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from . import normalize as n

Status = Literal["ok", "warn", "bad", "info", "na"]


@dataclass
class Flag:
    method: str
    status: Status
    title: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plural(k: int, one: str, few: str, many: str) -> str:
    k10, k100 = k % 10, k % 100
    if k10 == 1 and k100 != 11:
        return one
    if 2 <= k10 <= 4 and not 12 <= k100 <= 14:
        return few
    return many


def records_word(k: int) -> str:
    return f"{k} {_plural(k, 'запись', 'записи', 'записей')}"


def _active(rec: dict[str, Any]) -> bool:
    s = str(n.pick(rec, "status", "state", default="")).lower()
    return any(w in s for w in ("актив", "в залоге", "действ", "active")) and "исключ" not in s


def mileage_points(data: dict[str, Any] | None) -> list[tuple[Any, int]]:
    pts = []
    for r in n.records(data):
        d = n.parse_date(n.pick(r, "date", "checkDate", "dcDate", "dateFrom"))
        m = n.to_int(n.pick(r, "mileage", "odometer", "probeg", "km"))
        if d and m is not None:
            pts.append((d, m))
    return sorted(pts)


def mileage_rollback(points: list[tuple[Any, int]]) -> bool:
    return any(b[1] < a[1] for a, b in zip(points, points[1:]))


def evaluate(method: str, data: dict[str, Any] | None, error: str | None = None) -> Flag:
    from .provider.methods import METHODS

    title = METHODS[method].title if method in METHODS else method
    if error:
        return Flag(method, "na", title, error)
    recs = n.records(data)
    k = n.count(data)

    match method:
        case "pledge":
            active = int((data or {}).get("active_count") or sum(_active(r) for r in recs))
            if active:
                return Flag(method, "bad", "Реестр залогов", f"в залоге, активных записей: {active}")
            return Flag(method, "ok", "Реестр залогов", "не в залоге" + (f" (в истории {records_word(k)})" if k else ""))
        case "score" | "score-body":
            wanted = bool((data or {}).get("wanted")) or any("розыск" in str(n.pick(r, "type", default="")).lower() for r in recs)
            if wanted:
                return Flag(method, "bad", "Розыск", "автомобиль в розыске!")
            active = sum(_active(r) for r in recs) if recs else k
            if active:
                return Flag(method, "bad", "Ограничения", f"{records_word(active)}, не в розыске")
            return Flag(method, "ok", "Ограничения и розыск", "ограничений нет, не в розыске")
        case "taxi":
            if k:
                return Flag(method, "warn", "Работа в такси", f"найдено разрешений: {k}")
            return Flag(method, "ok", "Работа в такси", "разрешений такси не найдено")
        case "elpts":
            if k:
                return Flag(method, "ok", "ЭПТС", "электронный ПТС найден")
            return Flag(method, "info", "ЭПТС", "электронного ПТС нет (бумажный ПТС)")
        case "accidents" | "accidents-rt":
            if k:
                return Flag(method, "bad", title, f"участие в ДТП: {records_word(k)}")
            return Flag(method, "ok", title, "ДТП не найдены")
        case "fines":
            unpaid = (data or {}).get("unpaid_count")
            if unpaid is None:
                unpaid = sum(1 for r in recs if n.pick(r, "paid") is False)
            if unpaid:
                return Flag(method, "warn", "Штрафы", f"неоплаченных: {unpaid} из {k}")
            return Flag(method, "ok", "Штрафы", f"неоплаченных нет (всего {k})" if k else "штрафов нет")
        case "probeg":
            pts = mileage_points(data)
            if not pts:
                return Flag(method, "info", "История пробегов", "данных о пробеге нет")
            last = n.fmt_num(pts[-1][1], "км")
            if mileage_rollback(pts):
                return Flag(method, "bad", "История пробегов", f"возможна скрутка! последний: {last}")
            return Flag(method, "ok", "История пробегов", f"последний пробег {last}")
        case "lizing" | "lawsuits" | "sharing" | "recalls":
            if k:
                return Flag(method, "warn", title, f"найдено: {records_word(k)}")
            return Flag(method, "ok", title, "записей нет")
        case "customs":
            return Flag(method, "info", title, f"найдено: {records_word(k)}" if k else "записей нет")
        case "vin" | "body":
            return Flag(method, "info", "Периоды владения", f"владельцев: {k}" if k else "нет данных")
        case "price":
            r = n.first_record(data)
            lo, hi = n.pick(r, "min", "priceMin"), n.pick(r, "max", "priceMax")
            if lo and hi:
                return Flag(method, "info", "Рыночная стоимость", f"{n.fmt_num(lo)} — {n.fmt_num(hi, '₽')}")
            avg = n.pick(r, "avg", "price", "priceAvg")
            return Flag(method, "info", "Рыночная стоимость", n.fmt_num(avg, "₽") if avg else "нет данных")
        case "nomerogram":
            return Flag(method, "info", "Фотографии", f"найдено {k} фото" if k else "фото не найдены")
        case _:
            return Flag(method, "info", title, records_word(k) if k else "записей нет")


def overall(flags: list[Flag]) -> Status:
    statuses = {f.status for f in flags}
    if "bad" in statuses:
        return "bad"
    if "warn" in statuses:
        return "warn"
    return "ok"
