"""Данные и действия панели администратора. Каждое действие сотрудника пишется в журнал (AdminAudit)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from ..billing import add_credits
from ..config import Settings
from ..db import (AdminAudit, Check, Database, Order, Payment, ProProfile, ProviderLog, User, WebSession,
                  utcnow)

ROLES = {"support": "Поддержка", "admin": "Администратор"}


def effective_role(user: User | None, settings: Settings) -> str | None:
    """Роль сотрудника. ADMIN_USER_IDS из .env — администраторы по умолчанию (первый вход в панель)."""
    if user is None or user.blocked_at is not None:
        return None
    if user.staff_role in ROLES:
        return user.staff_role
    return "admin" if user.id in settings.admins else None


@dataclass
class ClientRow:
    user: User
    checks: int
    paid_rub: int
    last_activity: Any


class AdminService:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    # ---------------------------------------------------------------- журнал
    async def audit(self, staff_id: int | None, action: str, target: str | None = None,
                    details: dict[str, Any] | None = None, ip: str | None = None) -> None:
        async with self.db.session() as s:
            s.add(AdminAudit(staff_user_id=staff_id, action=action, target=target, details=details, ip=ip))
            await s.commit()

    async def audit_log(self, limit: int = 100, staff_id: int | None = None) -> list[AdminAudit]:
        stmt = select(AdminAudit).order_by(AdminAudit.id.desc()).limit(limit)
        if staff_id:
            stmt = stmt.where(AdminAudit.staff_user_id == staff_id)
        async with self.db.session() as s:
            return list((await s.execute(stmt)).scalars())

    # ---------------------------------------------------------------- сводка
    async def stats(self) -> dict[str, Any]:
        now = utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        d7, d30 = now - timedelta(days=7), now - timedelta(days=30)

        async with self.db.session() as s:
            async def scalar(stmt):
                return (await s.execute(stmt)).scalar_one()

            def paid(since=None):
                q = select(func.count(), func.coalesce(func.sum(Order.amount_rub), 0)).where(Order.status == "paid")
                return q.where(Order.paid_at >= since) if since else q

            spend = lambda since: select(func.coalesce(func.sum(ProviderLog.charged), 0)).where(  # noqa: E731
                ProviderLog.created_at >= since)
            last_balance = (await s.execute(select(ProviderLog.balance, ProviderLog.created_at)
                                            .where(ProviderLog.balance.is_not(None), ProviderLog.test_mode.is_(False))
                                            .order_by(ProviderLog.id.desc()).limit(1))).first()
            return {
                "users": await scalar(select(func.count()).select_from(User)),
                "users_7d": await scalar(select(func.count()).select_from(User).where(User.created_at >= d7)),
                "checks_today": await scalar(select(func.count()).select_from(Check).where(Check.created_at >= today)),
                "checks_7d": await scalar(select(func.count()).select_from(Check).where(Check.created_at >= d7)),
                "full_7d": await scalar(select(func.count()).select_from(Check).where(
                    Check.created_at >= d7, Check.tier != "express")),
                "paid_today": (await s.execute(paid(today))).one(),
                "paid_30d": (await s.execute(paid(d30))).one(),
                "paid_total": (await s.execute(paid())).one(),
                "pending_orders": await scalar(select(func.count()).select_from(Order).where(
                    Order.status == "pending", Order.created_at >= now - timedelta(hours=2))),
                "undelivered": await scalar(select(func.count()).select_from(Order).where(
                    Order.status == "paid", Order.delivered_at.is_(None))),
                "spend_today": float(await scalar(spend(today))),
                "spend_30d": float(await scalar(spend(d30))),
                "provider_errors_24h": await scalar(select(func.count()).select_from(ProviderLog).where(
                    ProviderLog.error.is_not(None), ProviderLog.created_at >= now - timedelta(hours=24))),
                "provider_balance": (float(last_balance[0]), last_balance[1]) if last_balance else None,
                "pros_pending": await scalar(select(func.count()).select_from(ProProfile).where(
                    ProProfile.status == "pending")),
                "pros_live": await scalar(select(func.count()).select_from(ProProfile).where(
                    ProProfile.status == "approved")),
            }

    async def provider_errors(self, limit: int = 20) -> list[ProviderLog]:
        async with self.db.session() as s:
            return list((await s.execute(select(ProviderLog).where(ProviderLog.error.is_not(None))
                                         .order_by(ProviderLog.id.desc()).limit(limit))).scalars())

    # ---------------------------------------------------------------- клиенты
    async def search_clients(self, q: str = "", limit: int = 50) -> list[ClientRow]:
        q = (q or "").strip()
        async with self.db.session() as s:
            stmt = select(User)
            if q:
                conds = [func.lower(User.email).like(f"%{q.lower().replace('%', '')}%"),
                         func.lower(User.username).like(f"%{q.lower().replace('%', '')}%")]
                if q.lstrip("#№").isdigit():
                    n = int(q.lstrip("#№"))
                    conds += [User.id == n, User.max_id == n, User.tg_id == n]
                vin_users = select(Check.user_id).where(or_(Check.vin == q.upper(), Check.plate == q.upper(),
                                                            Check.query == q))
                conds.append(User.id.in_(vin_users))
                stmt = stmt.where(or_(*conds))
            users = list((await s.execute(stmt.order_by(User.id.desc()).limit(limit))).scalars())
            rows = []
            for u in users:
                checks, last = (await s.execute(select(func.count(), func.max(Check.created_at))
                                                .where(Check.user_id == u.id))).one()
                paid = (await s.execute(select(func.coalesce(func.sum(Order.amount_rub), 0))
                                        .where(Order.user_id == u.id, Order.status == "paid"))).scalar_one()
                rows.append(ClientRow(u, checks, int(paid), last or u.created_at))
            return rows

    async def client(self, user_id: int) -> dict[str, Any] | None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if user is None:
                return None
            checks = list((await s.execute(select(Check).where(Check.user_id == user_id)
                                           .order_by(Check.created_at.desc()).limit(50))).scalars())
            orders = list((await s.execute(select(Order).where(Order.user_id == user_id)
                                           .order_by(Order.id.desc()).limit(50))).scalars())
            payments = list((await s.execute(select(Payment).where(Payment.user_id == user_id)
                                             .order_by(Payment.id.desc()).limit(50))).scalars())
            profile = (await s.execute(select(ProProfile).where(ProProfile.user_id == user_id))).scalar_one_or_none()
            sessions = (await s.execute(select(func.count()).select_from(WebSession).where(
                WebSession.user_id == user_id, WebSession.expires_at > utcnow()))).scalar_one()
        return {"user": user, "checks": checks, "orders": orders, "payments": payments, "profile": profile,
                "sessions": sessions, "role": effective_role(user, self.settings)}

    async def adjust_credits(self, user_id: int, delta: int) -> int:
        """Начислить (delta > 0) или списать отчёты из пакета; ниже нуля не уходит."""
        async with self.db.session() as s:
            if delta >= 0:
                await add_credits(s, user_id, delta)
            else:
                res = await s.execute(update(User).where(User.id == user_id, User.credits + delta >= 0)
                                      .values(credits=User.credits + delta))
                if res.rowcount != 1:
                    raise ValueError("Нельзя списать больше, чем есть в пакете")
            await s.commit()
            return (await s.get(User, user_id)).credits

    async def set_blocked(self, user_id: int, blocked: bool, reason: str | None = None) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            user.blocked_at, user.blocked_reason = (utcnow(), (reason or "")[:300]) if blocked else (None, None)
            if blocked:  # выкинуть из всех сессий
                await s.execute(delete(WebSession).where(WebSession.user_id == user_id))
            await s.commit()

    async def staff(self) -> list[User]:
        async with self.db.session() as s:
            rows = await s.execute(select(User).where(or_(User.staff_role.in_(list(ROLES)),
                                                          User.id.in_(self.settings.admins or {-1})))
                                   .order_by(User.id))
            return list(rows.scalars())

    async def set_staff_role(self, user_id: int, role: str | None) -> None:
        if role is not None and role not in ROLES:
            raise ValueError("Неизвестная роль")
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            user.staff_role = role
            if role is None:  # без роли — без доступа к панели в текущих сессиях
                await s.execute(update(WebSession).where(WebSession.user_id == user_id).values(mfa_at=None))
            await s.commit()

    async def reset_2fa(self, user_id: int) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            user.totp_secret = user.totp_enabled_at = user.totp_last_counter = user.recovery_codes = None
            await s.execute(update(WebSession).where(WebSession.user_id == user_id).values(mfa_at=None))
            await s.commit()

    async def orders(self, status: str = "", limit: int = 100) -> list[tuple[Order, User | None]]:
        stmt = select(Order, User).join(User, User.id == Order.user_id, isouter=True).order_by(Order.id.desc()).limit(limit)
        if status == "undelivered":
            stmt = stmt.where(Order.status == "paid", Order.delivered_at.is_(None))
        elif status:
            stmt = stmt.where(Order.status == status)
        async with self.db.session() as s:
            return [(o, u) for o, u in (await s.execute(stmt)).all()]

    async def set_featured(self, profile_id: int, days: int, plan: str | None = None) -> None:
        async with self.db.session() as s:
            p = await s.get(ProProfile, profile_id)
            p.featured_until = utcnow() + timedelta(days=days) if days > 0 else None
            if plan in ("free", "pro"):
                p.plan = plan
            await s.commit()

    async def all_pros(self, status: str = "") -> list[ProProfile]:
        stmt = select(ProProfile).order_by(ProProfile.updated_at.desc()).limit(200)
        if status:
            stmt = stmt.where(ProProfile.status == status)
        async with self.db.session() as s:
            return list((await s.execute(stmt)).scalars())
