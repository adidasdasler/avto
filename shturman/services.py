"""Ядро, общее для лендинга, Telegram- и (позже) MAX-бота."""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from .billing import can_use_credit, consume_credit, free_checks_used_today, has_unlimited
from .cascade import Cascade, tier_index
from .config import Settings
from .db import Check, Database, User
from .identifiers import Identifier, parse
from .provider import Provider
from .report.builder import build_view, render_html, render_pdf


class LimitExceeded(Exception):
    pass


class CheckService:
    def __init__(self, db: Database, provider: Provider, settings: Settings,
                 accidents_poll_interval: float = 5.0):
        self.db = db
        self.provider = provider
        self.settings = settings
        self.cascade = Cascade(provider, db, accidents_poll_interval)
        self._example_id: str | None = None

    # ---- пользователи ----
    async def tg_user(self, tg_id: int, username: str | None = None) -> User:
        async with self.db.session() as s:
            user = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                user = User(tg_id=tg_id, username=username)
                s.add(user)
                await s.commit()
            return user

    async def update_user(self, user_id: int, **fields: Any) -> User:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            for k, v in fields.items():
                setattr(user, k, v)
            await s.commit()
            return user

    async def get_user(self, user_id: int) -> User | None:
        async with self.db.session() as s:
            return await s.get(User, user_id)

    async def free_left(self, *, user: User | None = None, client_ip: str | None = None) -> int:
        async with self.db.session() as s:
            used = await free_checks_used_today(s, user_id=user.id if user else None, client_ip=client_ip)
        return max(self.settings.free_checks_per_day - used, 0)

    # ---- проверки ----
    async def express(self, query: str | Identifier, *, user: User | None = None, client_ip: str | None = None,
                      source: str = "web", enforce_limit: bool = True) -> Check:
        ident = query if isinstance(query, Identifier) else parse(query)
        if enforce_limit and not (user and has_unlimited(user)):
            if await self.free_left(user=user, client_ip=client_ip) <= 0:
                raise LimitExceeded(
                    f"Бесплатный лимит — {self.settings.free_checks_per_day} экспресс-проверки в сутки. "
                    "Купите полный отчёт или пакет, чтобы продолжить.")
        check = Check(user_id=user.id if user else None, client_ip=client_ip, source=source,
                      query=ident.source[:512], kind=ident.kind, tier="express")
        async with self.db.session() as s:
            s.add(check)
            await s.commit()
        return await self._run(check.id, ident, "express")

    async def _run(self, check_id: str, ident: Identifier, tier: str) -> Check:
        async with self.db.session() as s:
            check = await s.get(Check, check_id)
            existing = dict(check.sections or {})
        res = await self.cascade.run(ident, tier, existing, check_id=check_id)
        async with self.db.session() as s:
            check = await s.get(Check, check_id)
            check.tier = tier
            check.vin, check.plate = res.vin, res.plate
            check.vehicle = res.vehicle
            check.sections = res.sections
            check.flags = res.flags
            check.overall = res.overall
            check.provider_cost = float(check.provider_cost or 0) + res.cost
            await s.commit()
            return check

    async def upgrade(self, check_id: str, tier: str) -> Check:
        """Дозапрашивает методы нужного тарифа (уже полученные берутся из проверки/кэша)."""
        check = await self.get_check(check_id)
        if check is None:
            raise LookupError("Проверка не найдена")
        if tier_index(tier) <= tier_index(check.tier):
            return check
        if check.vin:
            ident = Identifier("vin", check.vin, check.query)
        elif check.kind == "plate" and check.plate:
            ident = Identifier("plate", check.plate, check.query)
        else:
            ident = parse(check.query)
        return await self._run(check_id, ident, tier)

    async def upgrade_with_credit(self, user_id: int, check_id: str, tier: str = "dossier") -> Check:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if not can_use_credit(user, tier):
                raise PermissionError("Нет доступных отчётов в пакете")
            consume_credit(user, tier)
            await s.commit()
        try:
            return await self.upgrade(check_id, tier)
        except Exception:
            async with self.db.session() as s:  # вернуть кредит, если отчёт не собрался
                user = await s.get(User, user_id)
                if not has_unlimited(user):
                    user.credits += 1
                await s.commit()
            raise

    async def attach(self, check_id: str, user_id: int) -> Check | None:
        async with self.db.session() as s:
            check = await s.get(Check, check_id)
            if check and check.user_id is None:
                check.user_id = user_id
                await s.commit()
            return check

    async def example(self) -> Check:
        """Демо-отчёт «Досье+» по тестовому VIN провайдера (бесплатно и в боевом режиме)."""
        from .provider.methods import TEST_VALUES

        if self._example_id:
            c = await self.get_check(self._example_id)
            if c:
                return c
        ident = Identifier("vin", TEST_VALUES["vin"], TEST_VALUES["vin"])
        check = Check(source="example", query=ident.source, kind="vin", tier="express")
        async with self.db.session() as s:
            s.add(check)
            await s.commit()
        c = await self._run(check.id, ident, "dossier_plus")
        self._example_id = c.id
        return c

    async def get_check(self, check_id: str) -> Check | None:
        async with self.db.session() as s:
            return await s.get(Check, check_id)

    async def history(self, user_id: int, limit: int = 10) -> list[Check]:
        async with self.db.session() as s:
            rows = await s.execute(select(Check).where(Check.user_id == user_id)
                                   .order_by(Check.created_at.desc()).limit(limit))
            return list(rows.scalars())

    # ---- отчёт ----
    def bot_link(self, check_id: str | None = None) -> str | None:
        if not self.settings.telegram_bot_username:
            return None
        base = f"https://t.me/{self.settings.telegram_bot_username}"
        return f"{base}?start=chk_{check_id.replace('-', '')}" if check_id else base

    def html(self, check: Check) -> str:
        view = build_view(check, self.settings.brand_name, self.settings.public_base_url, self.bot_link(check.id))
        return render_html(view)

    async def pdf(self, check: Check) -> bytes:
        return await asyncio.to_thread(render_pdf, self.html(check), self.settings.public_base_url)

    async def find_by_short_id(self, short: str) -> Check | None:
        """Deep-link Telegram допускает только [A-Za-z0-9_-] до 64 символов: передаём uuid без дефисов."""
        if len(short) != 32:
            return None
        cid = f"{short[:8]}-{short[8:12]}-{short[12:16]}-{short[16:20]}-{short[20:]}"
        return await self.get_check(cid)
