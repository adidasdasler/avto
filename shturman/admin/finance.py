"""Управленческий учёт: P&L, маржа, юнит-экономика.

Метод — кассовый (выручка в день оплаты; пакеты отчётов — тоже в день оплаты, а неизрасходованные отчёты
показываются отдельно как обязательство). Месяцы — по московскому времени.

Откуда цифры:
- выручка — оплаты (таблица payments; фиктивные dev-оплаты не считаются);
- эквайринг — % от выручки (ставка из настроек);
- себестоимость — фактические списания avtovincod.ru по каждому запросу (provider_log): платные отчёты отдельно,
  бесплатные экспресс-проверки — отдельно как расход на привлечение;
- операционные расходы и прочие доходы — ручные записи (в том числе ежемесячные);
- налог — по выбранному режиму (оценка: без уменьшения на взносы и без НДС).
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select

from ..billing import products
from ..config import Settings
from ..db import AppSetting, Check, Database, Expense, Payment, ProviderLog, User, utcnow

MSK = timedelta(hours=3)

EXPENSE_CATS = {"hosting": "Сервер, домен, хостинг", "ads": "Реклама и продвижение",
                "staff": "Зарплаты и подрядчики", "services": "Сервисы и подписки", "bank": "Банк и РКО",
                "fixed_tax": "Фикс. взносы и налоги", "other": "Прочее"}
INCOME_CATS = {"pros": "Размещение анкет", "other_income": "Прочий доход"}
TAX_MODES = {"usn6": ("УСН «Доходы»", 6.0), "usn15": ("УСН «Доходы минус расходы»", 15.0),
             "npd": ("Самозанятый (НПД)", 4.0), "none": ("Не учитывать", 0.0)}
DEFAULTS = {"acquiring_pct": "3.9", "tax_mode": "usn6", "tax_pct": "6"}


# ---------------------------------------------------------------- настройки
async def get_config(db: Database) -> dict[str, Any]:
    async with db.session() as s:
        rows = {r.key: r.value for r in (await s.execute(select(AppSetting).where(
            AppSetting.key.in_(list(DEFAULTS))))).scalars()}
    cfg = {**DEFAULTS, **rows}
    return {"acquiring_pct": float(cfg["acquiring_pct"]), "tax_mode": cfg["tax_mode"] if cfg["tax_mode"] in TAX_MODES
            else "usn6", "tax_pct": float(cfg["tax_pct"])}


async def set_config(db: Database, acquiring_pct: float, tax_mode: str, tax_pct: float | None) -> None:
    if not 0 <= acquiring_pct <= 20:
        raise ValueError("Эквайринг — от 0 до 20 %")
    if tax_mode not in TAX_MODES:
        raise ValueError("Неизвестный налоговый режим")
    if tax_pct is None:
        tax_pct = TAX_MODES[tax_mode][1]
    if not 0 <= tax_pct <= 30:
        raise ValueError("Ставка налога — от 0 до 30 %")
    async with db.session() as s:
        for k, v in {"acquiring_pct": acquiring_pct, "tax_mode": tax_mode, "tax_pct": tax_pct}.items():
            row = await s.get(AppSetting, k)
            if row is None:
                s.add(AppSetting(key=k, value=str(v)))
            else:
                row.value = str(v)
        await s.commit()


# ---------------------------------------------------------------- ручные записи
async def add_entry(db: Database, *, kind: str, category: str, amount: float, spent_on: date, monthly: bool,
                    until: date | None, note: str | None, login: str | None) -> Expense:
    cats = EXPENSE_CATS if kind == "expense" else INCOME_CATS if kind == "income" else None
    if cats is None or category not in cats:
        raise ValueError("Неизвестная категория")
    if not 0 < amount < 100_000_000:
        raise ValueError("Сумма должна быть больше нуля")
    if until and until < spent_on:
        raise ValueError("«До» раньше даты начала")
    async with db.session() as s:
        e = Expense(kind=kind, category=category, amount_rub=round(amount, 2), spent_on=spent_on, monthly=monthly,
                    until=until if monthly else None, note=(note or "").strip()[:200] or None, staff_login=login)
        s.add(e)
        await s.commit()
        return e


async def entries(db: Database, limit: int = 300) -> list[Expense]:
    async with db.session() as s:
        return list((await s.execute(select(Expense).order_by(Expense.spent_on.desc(), Expense.id.desc())
                                     .limit(limit))).scalars())


async def stop_or_delete(db: Database, entry_id: int, action: str) -> Expense | None:
    """delete — удалить запись; stop — ежемесячную закончить прошлым месяцем (история остаётся)."""
    async with db.session() as s:
        e = await s.get(Expense, entry_id)
        if e is None:
            return None
        if action == "stop" and e.monthly:
            last = month_start(today_msk()) - timedelta(days=1)
            e.until = max(last, e.spent_on)
        else:
            await s.delete(e)
        await s.commit()
        return e


def occurrences(e: Expense, start: date, end: date) -> int:
    """Сколько раз запись попадает в [start, end). Ежемесячная — в тот же день каждого месяца."""
    if not e.monthly:
        return int(start <= e.spent_on < end)
    n, y, m = 0, e.spent_on.year, e.spent_on.month
    last = min(end - timedelta(days=1), today_msk())  # будущие платежи ещё не случились
    while True:
        d = date(y, m, min(e.spent_on.day, calendar.monthrange(y, m)[1]))
        if d > last:
            return n
        if d >= start and (e.until is None or (d.year, d.month) <= (e.until.year, e.until.month)):
            n += 1
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ---------------------------------------------------------------- периоды
def today_msk() -> date:
    return (utcnow() + MSK).date()


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day) - MSK


MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]


def month_title(d: date) -> str:
    return f"{MONTHS[d.month - 1].capitalize()} {d.year}"


# ---------------------------------------------------------------- отчёт
@dataclass
class PnL:
    start: date
    end: date  # не включительно
    sales: float = 0
    payments: int = 0
    by_product: dict[str, list] = field(default_factory=dict)  # code -> [кол-во, сумма]
    other_income: float = 0
    income_by_cat: dict[str, float] = field(default_factory=dict)
    acquiring: float = 0
    provider_paid: float = 0
    provider_free: float = 0
    opex: float = 0
    opex_by_cat: dict[str, float] = field(default_factory=dict)
    tax: float = 0
    tax_mode: str = "usn6"
    paying_users: int = 0
    new_paying: int = 0
    new_users: int = 0
    new_users_paid: int = 0
    full_checks: int = 0
    free_checks: int = 0

    @property
    def income(self) -> float:
        return self.sales + self.other_income

    @property
    def gross(self) -> float:
        return self.sales - self.acquiring - self.provider_paid

    @property
    def operating(self) -> float:
        return self.gross + self.other_income - self.provider_free - self.opex

    @property
    def net(self) -> float:
        return self.operating - self.tax

    @property
    def expenses(self) -> float:
        return self.acquiring + self.provider_paid + self.provider_free + self.opex + self.tax

    def pct(self, value: float, base: float | None = None) -> float | None:
        base = self.sales if base is None else base
        return value / base * 100 if base else None

    @property
    def avg_check(self) -> float | None:
        return self.sales / self.payments if self.payments else None

    @property
    def arppu(self) -> float | None:
        return self.sales / self.paying_users if self.paying_users else None

    @property
    def cost_full(self) -> float | None:
        return self.provider_paid / self.full_checks if self.full_checks else None

    @property
    def cost_free(self) -> float | None:
        return self.provider_free / self.free_checks if self.free_checks else None

    @property
    def conversion(self) -> float | None:
        return self.new_users_paid / self.new_users * 100 if self.new_users else None


async def pnl(db: Database, settings: Settings, start: date, end: date, cfg: dict | None = None,
              all_entries: list[Expense] | None = None) -> PnL:
    cfg = cfg or await get_config(db)
    a, b = utc(start), utc(end)
    r = PnL(start, end, tax_mode=cfg["tax_mode"])
    real = Payment.provider != "dev"
    async with db.session() as s:
        for code, n, total in (await s.execute(
                select(Payment.product, func.count(), func.coalesce(func.sum(Payment.amount_rub), 0))
                .where(real, Payment.created_at >= a, Payment.created_at < b).group_by(Payment.product))).all():
            r.by_product[code] = [n, float(total)]
            r.sales += float(total)
            r.payments += n
        r.paying_users = (await s.execute(select(func.count(func.distinct(Payment.user_id))).where(
            real, Payment.created_at >= a, Payment.created_at < b))).scalar_one()
        first = select(Payment.user_id, func.min(Payment.created_at).label("first")).where(real) \
            .group_by(Payment.user_id).subquery()
        r.new_paying = (await s.execute(select(func.count()).select_from(first).where(
            first.c.first >= a, first.c.first < b))).scalar_one()
        new_users = select(User.id).where(User.created_at >= a, User.created_at < b)
        r.new_users = (await s.execute(select(func.count()).select_from(new_users.subquery()))).scalar_one()
        r.new_users_paid = (await s.execute(select(func.count(func.distinct(Payment.user_id))).where(
            real, Payment.user_id.in_(new_users)))).scalar_one()

        is_free = case((Check.tier == "express", 1), (Check.id.is_(None), 1), else_=0)
        for free, total in (await s.execute(
                select(is_free, func.coalesce(func.sum(ProviderLog.charged), 0))
                .select_from(ProviderLog).join(Check, Check.id == ProviderLog.check_id, isouter=True)
                .where(ProviderLog.created_at >= a, ProviderLog.created_at < b).group_by(is_free))).all():
            if free:
                r.provider_free += float(total)
            else:
                r.provider_paid += float(total)
        real_check = (Check.created_at >= a, Check.created_at < b, Check.source != "example")
        r.full_checks = (await s.execute(select(func.count()).select_from(Check).where(
            *real_check, Check.tier != "express"))).scalar_one()
        r.free_checks = (await s.execute(select(func.count()).select_from(Check).where(
            *real_check, Check.tier == "express"))).scalar_one()

    for e in all_entries if all_entries is not None else await entries(db, 100_000):
        k = occurrences(e, start, end)
        if not k:
            continue
        amount = float(e.amount_rub) * k
        if e.kind == "income":
            r.other_income += amount
            r.income_by_cat[e.category] = r.income_by_cat.get(e.category, 0) + amount
        else:
            r.opex += amount
            r.opex_by_cat[e.category] = r.opex_by_cat.get(e.category, 0) + amount

    r.acquiring = round(r.sales * cfg["acquiring_pct"] / 100, 2)
    rate = cfg["tax_pct"] / 100
    if cfg["tax_mode"] == "usn15":
        base = r.income - (r.acquiring + r.provider_paid + r.provider_free + r.opex)
        r.tax = max(base * rate, r.income * 0.01) if r.income > 0 else 0
    elif cfg["tax_mode"] in ("usn6", "npd"):
        r.tax = r.income * rate
    r.tax = round(r.tax, 2)
    return r


async def overview(db: Database, settings: Settings, start: date, end: date, months: int = 12) -> dict[str, Any]:
    cfg = await get_config(db)
    all_entries = await entries(db, 100_000)
    current = await pnl(db, settings, start, end, cfg, all_entries)
    span = (end - start).days
    previous = await pnl(db, settings, start - timedelta(days=span), start, cfg, all_entries) \
        if not (start.day == 1 and end == add_months(start, 1)) else \
        await pnl(db, settings, add_months(start, -1), start, cfg, all_entries)
    this_month = month_start(today_msk())
    series = [await pnl(db, settings, add_months(this_month, -i), add_months(this_month, -i + 1), cfg, all_entries)
              for i in range(months - 1, -1, -1)]

    # постоянные расходы в месяц и точка безубыточности
    m0, m1 = this_month, add_months(this_month, 1)
    fixed = sum(float(e.amount_rub) for e in all_entries if e.kind == "expense" and e.monthly
                and e.spent_on < m1 and (e.until is None or e.until >= m0))
    recent = [p for p in series[-3:] if p.payments]
    margin_per_sale = (sum(p.gross for p in recent) / sum(p.payments for p in recent)) if recent else None
    tax_k = cfg["tax_pct"] / 100 if cfg["tax_mode"] in ("usn6", "npd") else 0
    avg = (sum(p.sales for p in recent) / sum(p.payments for p in recent)) if recent else None
    unit = (margin_per_sale - (avg or 0) * tax_k) if margin_per_sale is not None else None
    breakeven = math.ceil(fixed / unit) if unit and unit > 0 and fixed else None

    async with db.session() as s:
        credits_left = int((await s.execute(select(func.coalesce(func.sum(User.credits), 0)))).scalar_one())
        packs = {c: p.credits for c, p in products(settings).items() if p.credits}
        sold = (await s.execute(select(Payment.product, func.count(), func.coalesce(func.sum(Payment.amount_rub), 0))
                                .where(Payment.provider != "dev", Payment.product.in_(list(packs)))
                                .group_by(Payment.product))).all()
    sold_credits = sum(packs[c] * n for c, n, _ in sold)
    per_credit = (sum(float(t) for _, _, t in sold) / sold_credits) if sold_credits else \
        settings.price_pack_25 / 25
    return {"cfg": cfg, "p": current, "prev": previous, "series": series, "fixed": fixed,
            "unit": unit, "breakeven": breakeven, "credits_left": credits_left,
            "liability": credits_left * per_credit, "per_credit": per_credit}


def csv_rows(series: list[PnL]) -> list[list[Any]]:
    head = ["Месяц", "Выручка", "Оплат", "Прочие доходы", "Эквайринг", "Провайдер (платные)",
            "Провайдер (бесплатные)", "Операционные расходы", "Валовая прибыль", "Операционная прибыль",
            "Налог", "Чистая прибыль", "Чистая маржа, %"]
    rows = [head]
    for p in series:
        rows.append([p.start.strftime("%Y-%m"), p.sales, p.payments, p.other_income, p.acquiring,
                     round(p.provider_paid, 2), round(p.provider_free, 2), p.opex, round(p.gross, 2),
                     round(p.operating, 2), p.tax, round(p.net, 2),
                     round(p.pct(p.net, p.income), 1) if p.income else ""])
    return rows
