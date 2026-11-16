"""Разбор ответов avtovincod.ru и «светофор».

Для каждого метода — анализатор, который по реальной схеме ответа (см.
shturman/provider/samples/) возвращает Section: статус, короткий вывод для сводки
и строки для отчёта. Из статусов разделов складывается общий светофор:
красный — юридические риски (залог, розыск, ограничения, лизинг, скрутка пробега),
жёлтый — замечания (такси, ДТП, штрафы, каршеринг, суды), зелёный — рисков нет.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Literal

from . import normalize as n

Status = Literal["ok", "warn", "bad", "info", "na"]
WEIGHTS = {"bad": 30, "warn": 10}

D = n.fmt_date


def N(v: Any, unit: str = "") -> str:
    return n.fmt_num(v, unit)


def dig(d: Any, path: str, default: Any = None) -> Any:
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def kv(*pairs: tuple[str, Any]) -> list[tuple[str, str]]:
    out = []
    for label, value in pairs:
        if value in (None, "", [], "—"):
            continue
        out.append((label, value if isinstance(value, str) else n.display_value(value)))
    return out


def plural(k: int, one: str, few: str, many: str) -> str:
    k10, k100 = k % 10, k % 100
    if k10 == 1 and k100 != 11:
        return f"{k} {one}"
    if 2 <= k10 <= 4 and not 12 <= k100 <= 14:
        return f"{k} {few}"
    return f"{k} {many}"


def records_word(k: int) -> str:
    return plural(k, "запись", "записи", "записей")


def _list(d: Any, *keys: str) -> list[dict[str, Any]]:
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _names(items: Any) -> str:
    return ", ".join(str(x.get("name")) for x in items or [] if isinstance(x, dict) and x.get("name"))


def _period(a: Any, b: Any) -> str:
    return f"{D(a)} — {D(b) if b else 'по настоящее время'}"


@dataclass
class Section:
    key: str
    title: str
    status: Status
    summary: str
    note: str | None = None
    kind: str = "cards"  # cards | table | mileage | proscons | price | photos
    rows: list[Any] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    weight: int | None = None

    @property
    def effective_weight(self) -> int:
        return self.weight if self.weight is not None else WEIGHTS.get(self.status, 0)

    def flag(self) -> dict[str, Any]:
        return {"method": self.key, "status": self.status, "title": self.title,
                "summary": self.summary, "weight": self.effective_weight}


# ---------------------------------------------------------------- анализаторы

def _score(d: dict) -> Section:
    restr = _list(d, "restrictions")
    searches = _list(d, "searches")
    st = d.get("status") or {}
    wanted = bool(st.get("wanted") or st.get("spec_wanted") or searches)
    rows = [kv(("Вид ограничения", r.get("restriction_name")), ("Кем наложено", r.get("organization_name")),
               ("Регион", r.get("region")), ("Дата", D(r.get("enforcement_date")) if r.get("enforcement_date") else None),
               ("Основание", r.get("reasons")), ("Исполнительное производство", r.get("enforcement_number")))
            for r in restr]
    rows += [[("Розыск", "")] + n.flatten(s) for s in searches]
    title = "Ограничения и розыск"
    if wanted:
        return Section("score", title, "bad", "автомобиль в розыске!", rows=rows, weight=60,
                       note="Автомобиль числится в розыске — покупка невозможна.")
    if restr or st.get("restricted"):
        k = len(restr) or 1
        return Section("score", title, "bad", f"{plural(k, 'ограничение', 'ограничения', 'ограничений')}, не в розыске",
                       rows=rows, note="Наложены ограничения: перерегистрация будет невозможна до их снятия.")
    return Section("score", title, "ok", "ограничений нет, не в розыске", note="Ограничений и розыска нет.")


def owners_section(rec: dict) -> Section | None:
    periods = _list(rec, "ownershipPeriods")
    status = str(rec.get("recordStatus") or "")
    if not periods and not status:
        return None
    k = len(periods)
    rows = [kv((f"{i}-й владелец", _period(p.get("from"), p.get("to"))), ("Тип владельца", p.get("personType")))
            for i, p in enumerate(periods, 1)]
    first = n.parse_date(periods[0].get("from")) if periods else None
    years = (date.today() - first).days // 365 if first else None
    note = f"Количество владельцев: {k}" + (f". Срок эксплуатации: {plural(years, 'год', 'года', 'лет')}" if years else "")
    if rec.get("lastRegAction"):
        note += f". Последнее действие: {rec['lastRegAction']}"
    title = "Периоды владения"
    if status and "действ" not in status.lower():
        return Section("owners", title, "bad", f"регистрация: {status}", note=note, rows=rows)
    if k >= 4:
        return Section("owners", title, "warn", f"владельцев: {k} — много", note=note, rows=rows, weight=5)
    return Section("owners", title, "ok", f"владельцев: {k}", note=note, rows=rows)


def _pledge(d: dict) -> Section:
    recs = _list(d, "records")
    is_active = lambda r: str(r.get("status", "")).lower() in ("active", "действует", "в залоге")  # noqa: E731
    active = int(d.get("active_count") or sum(map(is_active, recs)))
    rows = [kv(("Статус", "В залоге" if is_active(r) else "Залог прекращён"),
               ("Дата регистрации", D(r.get("register_date") or r.get("start_date"))),
               ("Дата прекращения", D(r.get("end_date")) if r.get("end_date") else None),
               ("Номер уведомления", r.get("number")), ("Залогодержатель", _names(r.get("pledgees"))),
               ("История", "; ".join(f"{h.get('date')} — {h.get('type')}" for h in r.get("history") or [])))
            for r in recs]
    title = "Реестр залогов"
    if active:
        return Section("pledge", title, "bad", f"в залоге ({records_word(active)})", rows=rows, weight=40,
                       note="ВНИМАНИЕ: автомобиль находится в залоге! Залог переходит к новому владельцу.")
    hist = f" (в истории {records_word(len(recs))})" if recs else ""
    return Section("pledge", title, "ok", "не в залоге" + hist, rows=rows, note="Действующих залогов нет.")


def _taxi(d: dict) -> Section:
    recs = _list(d, "records")
    active = any(bool(r.get("isActual")) for r in recs)
    rows = [kv(("Регион", r.get("regionName")), ("Номер разрешения", r.get("permitNumber")),
               ("Действует", _period(r.get("dateFrom"), r.get("dateTo"))),
               ("Статус", "действующее" if r.get("isActual") else "недействующее"),
               ("Перевозчик", _names(r.get("carriers"))))
            for r in recs]
    title = "Работа в такси"
    if active:
        return Section("taxi", title, "warn", "действующее разрешение такси", rows=rows, weight=15,
                       note="Есть действующее разрешение на работу в такси — высокий износ, возможен перекрас.")
    if d.get("isTaxi") or recs:
        return Section("taxi", title, "warn", f"был в такси ({records_word(len(recs))})", rows=rows)
    return Section("taxi", title, "ok", "в такси не числится")


def _elpts(d: dict) -> Section:
    recs = _list(d, "records")
    title = "Информация о ПТС"
    if not (d.get("found") and recs):
        return Section("elpts", title, "info", "электронного ПТС нет (бумажный ПТС)",
                       note="Транспортное средство не имеет электронного ПТС.")
    r = recs[0]
    restr, cust = r.get("restrictions_status"), r.get("customs_restrictions_status")
    fee = r.get("displosal_fee") or r.get("disposal_fee")
    rows = [kv(("Страна", r.get("country")), ("Тип ТС", r.get("type")), ("Год выпуска", r.get("production")),
               ("Утилизационный сбор", fee), ("Ограничения", restr), ("Таможенные ограничения", cust),
               ("Последнее регистрационное действие", r.get("last_reg_action")))]
    has = lambda s: bool(s) and not str(s).lower().startswith("нет")  # noqa: E731
    if has(restr) or has(cust):
        return Section("elpts", title, "bad", f"ограничения в ЭПТС: {restr if has(restr) else cust}", rows=rows)
    if fee and str(fee).lower().startswith("не"):
        return Section("elpts", title, "warn", "утилизационный сбор не оплачен", rows=rows)
    return Section("elpts", title, "ok", "электронный ПТС, ограничений нет", rows=rows,
                   note="Электронный ПТС найден, ограничений нет.")


def _accidents(key: str) -> Callable[[dict], Section]:
    def analyze(d: dict) -> Section:
        title = "ДТП по базе ГИБДД (онлайн)" if key == "accidents-rt" else "ДТП (архив ГИБДД)"
        if d.get("status") == "pending":
            return Section(key, title, "na", "проверка не завершилась")
        recs = _list(d, "records")
        k = len(recs) or (int(d["found"]) if isinstance(d.get("found"), int) and not isinstance(d.get("found"), bool) else 0)
        rows = [kv(("Дата", D(r.get("date"))), ("Тип", r.get("type")), ("Регион", r.get("region")),
                   ("Место", r.get("place")), ("Состояние ТС", r.get("damageState")),
                   ("Участников", r.get("vehiclesInvolved")),
                   ("Зоны повреждений", ", ".join(map(str, r.get("damagePoints") or [])) or None),
                   ("Описание", r.get("damageDescription") or r.get("damage")), ("Номер ДТП", r.get("number")))
                for r in recs]
        note = d.get("dataNote")
        if k == 0:
            return Section(key, title, "ok", "ДТП не найдены", note=note or "ДТП не найдены.")
        status: Status = "bad" if k >= 2 else "warn"
        return Section(key, title, status, plural(k, "ДТП", "ДТП", "ДТП"), rows=rows,
                       note=(note + " " if note else "") + f"Количество происшествий: {k}.",
                       weight=None if k >= 2 else 15)
    return analyze


def _fines(d: dict) -> Section:
    recs = n.records(d)
    unpaid = d.get("unpaid_count")
    if unpaid is None:
        unpaid = sum(1 for r in recs if n.pick(r, "paid", "isPaid") is False
                     or "не оплач" in str(n.pick(r, "status", default="")).lower())
    total = sum(n.to_int(n.pick(r, "amount", "sum")) or 0 for r in recs
                if n.pick(r, "paid", "isPaid") is False)
    rows = [n.flatten(r) for r in recs]
    title = "Штрафы ГИБДД"
    if unpaid:
        return Section("fines", title, "warn", f"неоплаченных: {unpaid}" + (f" на {N(total, '₽')}" if total else ""),
                       rows=rows)
    return Section("fines", title, "ok", f"неоплаченных нет (всего {len(recs)})" if recs else "штрафов нет", rows=rows)


def _policy_rows(p: dict) -> list[tuple[str, str]]:
    drivers = None
    if p.get("driversLimited") is not None:
        drivers = (f"ограничен ({p['driversCount']} вод.)" if p.get("driversCount") else "ограничен") \
            if p["driversLimited"] else "без ограничений"
    return kv(("Период", _period(p.get("startDate"), p.get("endDate"))), ("Страховая", p.get("companyName")),
              ("Полис", " ".join(str(x) for x in (p.get("policySerial"), p.get("policyNumber")) if x)),
              ("Статус", p.get("status")), ("Цель использования", p.get("purpose")),
              ("Допуск к управлению", drivers), ("Собственник", p.get("ownerType")),
              ("Госномер", p.get("regNumber") or p.get("licensePlate")), ("Регион", p.get("region")))


def _is_active_policy(p: dict) -> bool:
    return bool(p.get("isActive")) or str(p.get("status", "")).lower().startswith("действ")


def _osago(d: dict) -> Section:
    pols = _list(d, "policies", "records")
    active = [p for p in pols if _is_active_policy(p)]
    rows = [_policy_rows(p) for p in pols]
    if active:
        p = active[0]
        return Section("osago", "Полис ОСАГО", "ok", f"действует до {D(p.get('endDate'))}", rows=rows)
    return Section("osago", "Полис ОСАГО", "info", "действующего полиса нет", rows=rows)


def _osago_history(d: dict) -> Section:
    pols = _list(d, "policies", "records")
    taxi = [p for p in pols if p.get("purposeType") == "TAXI" or "такси" in str(p.get("purpose", "")).lower()]
    rows = [_policy_rows(p) for p in pols]
    title = "История полисов ОСАГО"
    if taxi:
        return Section("osago-history", title, "warn", f"полис с целью «такси»: {len(taxi)}", rows=rows,
                       note="В истории ОСАГО есть полисы для работы в такси.")
    active = any(_is_active_policy(p) for p in pols)
    return Section("osago-history", title, "ok" if active else "info",
                   f"полисов: {len(pols)}" + (", есть действующий" if active else ", действующего нет"), rows=rows)


def _offers(d: dict) -> Section:
    recs = _list(d, "records")
    rows = [[D(r.get("date")), N(r.get("price"), "₽"), N(r.get("mileage"), "км"), r.get("city") or "—",
             r.get("url") or ""] for r in sorted(recs, key=lambda r: str(r.get("date")), reverse=True)]
    photos = [u for r in recs for u in (r.get("photos") or []) if isinstance(u, str)]
    summary = f"объявлений: {len(recs)}" if recs else "объявлений не найдено"
    return Section("offers", "Объявления о продаже", "info", summary, kind="table", rows=rows,
                   columns=["Дата", "Цена", "Пробег", "Город", "Ссылка"], extra={"photos": photos})


def _plates(d: dict) -> Section:
    plates = d.get("plates") or [r.get("plate") or r.get("regNumber") for r in _list(d, "records")]
    plates = [p if isinstance(p, str) else n.display_value(p) for p in plates if p]
    return Section("plate-history", "История госномеров", "info",
                   plural(len(plates), "госномер", "госномера", "госномеров") if plates else "нет данных",
                   kind="table", rows=[[p] for p in plates], columns=["Госномер"])


def _proscons(d: dict) -> Section:
    split = lambda s: [x.strip().capitalize() for x in str(s).rstrip(".").split(",") if x.strip()]  # noqa: E731
    pros = d.get("prosList") or (split(d["pros"]) if isinstance(d.get("pros"), str) else d.get("pros") or [])
    cons = d.get("consList") or (split(d["cons"]) if isinstance(d.get("cons"), str) else d.get("cons") or [])
    summary = ", ".join(f"{k}: {v.lower()}" for k, v in (("надёжность", d.get("reliabilityLevel")),
                                                          ("ликвидность", d.get("liquidityLevel"))) if v)
    return Section("pros-cons", "Плюсы и минусы модели", "info", summary or "есть описание", kind="proscons",
                   extra={"model": d.get("model"), "pros": pros, "cons": cons,
                          "facts": kv(("Надёжность", d.get("reliability")), ("Ликвидность", d.get("liquidity")),
                                      ("Кузов и коррозия", d.get("bodyCorrosion")))})


def _photos(d: dict) -> Section:
    photos = [p if isinstance(p, str) else n.pick(p, "url", "photo", "image", "src")
              for p in (d.get("photos") or n.records(d))]
    photos = [p for p in photos if p]
    return Section("nomerogram", "Фотографии автомобиля", "info",
                   f"найдено {len(photos)} фото" if photos else "фото не найдены", kind="photos",
                   extra={"photos": photos})


def _recalls(d: dict) -> Section:
    recs = _list(d, "records")
    rows = [kv(("Дата", D(r.get("date"))), ("Производитель", r.get("manufacturer")), ("Марка", r.get("brand")),
               ("Причина", r.get("reason"))) for r in recs]
    if recs:
        return Section("recalls", "Отзывные кампании", "warn", f"кампаний: {len(recs)} — уточните у дилера",
                       rows=rows, weight=5, note="Проверьте у официального дилера, выполнены ли работы.")
    return Section("recalls", "Отзывные кампании", "ok", "отзывных кампаний нет")


def _customs(d: dict) -> Section:
    recs = _list(d, "records")
    rows = [kv(("Страна", r.get("country")), ("Дата выпуска", D(r.get("releaseDate")) if r.get("releaseDate") else None),
               ("Модель", r.get("model")), ("Источник", r.get("source"))) for r in recs]
    if recs:
        return Section("customs", "Таможня", "info", f"есть сведения: {recs[0].get('country') or '—'}", rows=rows)
    return Section("customs", "Таможня", "ok", "сведений нет")


def _lizing(d: dict) -> Section:
    recs = _list(d, "records")
    rows = [kv(("Статус", "действует" if r.get("isActive") else "завершён"),
               ("Лизингодатель", _names(r.get("lessors"))), ("Лизингополучатель", r.get("lessee")),
               ("Договор", f"№ {dig(r, 'contract.number')} от {D(dig(r, 'contract.date'))}" if r.get("contract") else None),
               ("Период", _period(r.get("dateStart"), r.get("dateEnd") or r.get("plannedEnd"))),
               ("Причина прекращения", r.get("stopReason"))) for r in recs]
    active = int(d.get("active") or 0) or any(r.get("isActive") for r in recs)
    title = "Лизинг"
    if active:
        return Section("lizing", title, "bad", "в действующем лизинге", rows=rows, weight=40,
                       note="Автомобиль в лизинге: продажа без согласия лизингодателя невозможна.")
    if recs or d.get("isLeasing"):
        return Section("lizing", title, "warn", "был в лизинге, договор завершён", rows=rows, weight=5)
    return Section("lizing", title, "ok", "в лизинге не числится")


def _lawsuits(d: dict) -> Section:
    items = _list(d, "items", "records")
    rows = [[n.pick(i, "RegistrationDate", "date", default="—"), n.pick(i, "CaseNumber", "number", default="—"),
             n.pick(i, "Court", "court", default="—"), n.pick(i, "Type", "type", default="—"),
             n.pick(i, "CaseUrl", "url", default="")] for i in items]
    if items:
        return Section("lawsuits", "Судебные дела", "warn", f"дел: {len(items)}", kind="table", rows=rows,
                       columns=["Дата", "Дело", "Суд", "Документ", "Ссылка"])
    return Section("lawsuits", "Судебные дела", "ok", "судебных дел нет")


def _sharing(d: dict) -> Section:
    if d.get("isCarsharing") or n.records(d):
        company = d.get("company")
        summary = "использовался в каршеринге" + (f" ({company})" if company else "")
        return Section("sharing", "Каршеринг", "warn", summary,
                       note=f"Автомобиль {summary}. {d.get('dataNote') or ''}".strip())
    return Section("sharing", "Каршеринг", "ok", "в каршеринге не числится", note=d.get("dataNote"))


def _service(d: dict) -> Section:
    recs = sorted(_list(d, "records"), key=lambda r: str(r.get("date")), reverse=True)
    rows = [[D(r.get("date")), N(r.get("mileage"), "км"), r.get("type") or "—", r.get("description") or "—",
             r.get("city") or "—"] for r in recs]
    return Section("service", "Сервисная история", "info",
                   records_word(len(recs)) if recs else "записей нет", kind="table", rows=rows,
                   columns=["Дата", "Пробег", "Тип", "Работы", "Город"])


def _price(d: dict) -> Section:
    p = d.get("price") if isinstance(d.get("price"), dict) else n.first_record(d)
    lo, hi = n.pick(p, "min", "priceMin"), n.pick(p, "max", "priceMax")
    avg = n.pick(p, "average", "avg", "price")
    summary = f"{N(lo)} — {N(hi, '₽')}" if lo and hi else (N(avg, "₽") if avg else "нет данных")
    by_year = dig(d, "dynamics.byYear") or {}
    return Section("price", "Рыночная стоимость", "info", summary, kind="price",
                   extra={"min": N(lo, "₽"), "max": N(hi, "₽"), "avg": N(avg, "₽"), "count": n.pick(p, "count"),
                          "by_year": [(y, N(v, "₽")) for y, v in sorted(by_year.items())]})


ANALYZERS: dict[str, Callable[[dict], Section]] = {
    "score": _score, "score-body": _score, "pledge": _pledge, "taxi": _taxi, "elpts": _elpts,
    "accidents": _accidents("accidents"), "accidents-rt": _accidents("accidents-rt"), "fines": _fines,
    "osago": _osago, "osago-history": _osago_history, "offers": _offers, "plate-history": _plates,
    "pros-cons": _proscons, "nomerogram": _photos, "recalls": _recalls, "customs": _customs,
    "lizing": _lizing, "lawsuits": _lawsuits, "sharing": _sharing, "service": _service, "price": _price,
}

TITLES = {
    "fines": "Штрафы ГИБДД", "score": "Ограничения и розыск", "pledge": "Реестр залогов", "taxi": "Работа в такси",
    "elpts": "Информация о ПТС", "accidents": "ДТП (архив ГИБДД)", "accidents-rt": "ДТП по базе ГИБДД (онлайн)",
    "lizing": "Лизинг", "lawsuits": "Судебные дела", "sharing": "Каршеринг", "recalls": "Отзывные кампании",
    "customs": "Таможня", "osago-history": "История полисов ОСАГО", "offers": "Объявления о продаже",
    "plate-history": "История госномеров", "service": "Сервисная история", "price": "Рыночная стоимость",
    "pros-cons": "Плюсы и минусы модели", "nomerogram": "Фотографии автомобиля", "vin": "Периоды владения",
    "owners": "Периоды владения", "probeg": "История пробегов", "mileage": "История пробегов",
}

# Порядок разделов в отчёте (как в образце).
ORDER = ["elpts", "owners", "mileage", "accidents", "accidents-rt", "pledge", "score", "fines", "taxi", "lizing",
         "osago-history", "plate-history", "offers", "sharing", "lawsuits", "customs", "recalls", "service",
         "price", "pros-cons", "nomerogram"]


# ---------------------------------------------------------------- пробег

def mileage_points(sections: dict[str, dict[str, Any]]) -> list[tuple[date, int, str]]:
    pts: list[tuple[date, int, str]] = []

    def data(m: str) -> dict:
        return (sections.get(m) or {}).get("data") or {}

    for r in _list(data("probeg"), "records"):
        d = n.parse_date(r.get("date_of_diagnosis") or r.get("date"))
        km = n.to_int(r.get("car_mileage") if r.get("car_mileage") is not None else r.get("mileage"))
        if d and km:
            pts.append((d, km, "Техосмотр (ЕАИСТО)"))
    for r in _list(data("offers"), "records"):
        d, km = n.parse_date(r.get("date")), n.to_int(r.get("mileage"))
        if d and km:
            pts.append((d, km, "Объявление о продаже"))
    for r in _list(data("service"), "records"):
        d, km = n.parse_date(r.get("date")), n.to_int(r.get("mileage"))
        if d and km:
            pts.append((d, km, f"Сервис: {r.get('type') or 'обслуживание'}"))
    return sorted(set(pts))


def mileage_rollback(points: list[tuple[Any, int, Any]] | list[tuple[Any, int]]) -> bool:
    return any(b[1] < a[1] for a, b in zip(points, points[1:]))


def mileage_section(sections: dict[str, dict[str, Any]]) -> Section | None:
    if not any(m in sections and "data" in sections[m] for m in ("probeg", "offers", "service")):
        if "error" in (sections.get("probeg") or {}):
            return Section("mileage", "История пробегов", "na", sections["probeg"]["error"])
        return None
    pts = mileage_points(sections)
    rows = [[d.strftime("%d.%m.%Y"), N(km, "км"), src] for d, km, src in reversed(pts)]
    diag = next((r for r in _list((sections.get("probeg") or {}).get("data") or {}, "records")
                 if r.get("is_valid")), None)
    note = f"Диагностическая карта действует до {D(diag.get('date_of_validity'))}." if diag else None
    sec = Section("mileage", "История пробегов", "ok", "", kind="mileage", rows=rows,
                  columns=["Дата", "Пробег", "Источник"], extra={"points": [(d, km) for d, km, _ in pts]}, note=note)
    official = [p for p in pts if not p[2].startswith("Объявление")]
    max_km = max((p[1] for p in pts), default=0)
    if not pts:
        sec.status, sec.summary = "info", "данных о пробеге нет"
    elif mileage_rollback(official):
        sec.status, sec.summary, sec.weight = "bad", f"возможна скрутка! максимум был {N(max_km, 'км')}", 35
        sec.note = "Пробег по техосмотрам/сервису уменьшался — возможна скрутка одометра. " + (note or "")
    elif mileage_rollback(pts):
        sec.status, sec.summary, sec.weight = "warn", f"в объявлении пробег меньше, чем ранее ({N(max_km, 'км')})", 15
        sec.note = ("Продавец указал в объявлении пробег меньше, чем зафиксирован раньше, — "
                    "уточните пробег и проверьте одометр. " + (note or ""))
    else:
        sec.summary = f"{N(pts[-1][1], 'км')} на {pts[-1][0]:%d.%m.%Y}"
    return sec


# ---------------------------------------------------------------- сборка

def analyze(method: str, sec: dict[str, Any]) -> Section | None:
    if not sec or "skipped" in sec:
        return None
    title = TITLES.get(method, method)
    if "error" in sec:
        return Section(method, title, "na", sec["error"])
    fn = ANALYZERS.get(method)
    if fn is None:
        return None
    return fn(sec.get("data") or {})


def build_sections(sections: dict[str, dict[str, Any]]) -> list[Section]:
    out: list[Section] = []
    for key in ORDER:
        if key == "owners":
            rec = dig(sections, "vin.data.record") or dig(sections, "score.data.record")
            if "vin" in sections and "error" in sections["vin"] and not rec:
                out.append(Section("owners", "Периоды владения", "na", sections["vin"]["error"]))
            elif rec:
                s = owners_section(rec)
                if s:
                    out.append(s)
        elif key == "mileage":
            s = mileage_section(sections)
            if s:
                out.append(s)
        elif key == "score" and "score" not in sections and "score-body" in sections:
            s = analyze("score-body", sections["score-body"])
            if s:
                out.append(s)
        elif key in sections:
            s = analyze(key, sections[key])
            if s:
                out.append(s)
    return out


def build_flags(sections: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [s.flag() for s in build_sections(sections)]


VERDICTS = {
    "green": "Серьёзных рисков не найдено",
    "yellow": "Есть замечания — проверьте перед покупкой",
    "red": "Высокий риск — не покупайте без юридической проверки",
}


def traffic_light(flags: list[dict[str, Any]]) -> dict[str, Any]:
    red = [f for f in flags if f["status"] == "bad"]
    yellow = [f for f in flags if f["status"] == "warn"]
    green = [f for f in flags if f["status"] == "ok"]
    na = [f for f in flags if f["status"] == "na"]
    score = max(0, 100 - sum(int(f.get("weight", WEIGHTS.get(f["status"], 0))) for f in flags))
    color = "red" if red else "yellow" if yellow else "green"
    return {"color": color, "score": score, "verdict": VERDICTS[color], "red": red, "yellow": yellow,
            "green": green, "na": na, "checked": len(flags) - len(na)}


def overall(flags: list[dict[str, Any]]) -> str:
    return {"red": "bad", "yellow": "warn", "green": "ok"}[traffic_light(flags)["color"]]
