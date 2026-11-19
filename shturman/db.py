"""Модели и подключение к БД. PostgreSQL в проде, SQLite для разработки и тестов."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    max_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="buyer")  # buyer | picker
    legal_status: Mapped[str | None] = mapped_column(String(16))  # person | ip | selfemployed | llc
    email: Mapped[str | None] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(200))
    yandex_id: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    # Сотрудники: support — поддержка, admin — администратор. Вход в панель только с 2FA (TOTP).
    staff_role: Mapped[str | None] = mapped_column(String(16), index=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime)
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger)  # защита от повторного кода
    recovery_codes: Mapped[list[Any] | None] = mapped_column()  # хеши одноразовых резервных кодов
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_reason: Mapped[str | None] = mapped_column(String(300))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    credits: Mapped[int] = mapped_column(Integer, default=0)  # отчёты уровня «Досье» из пакетов
    unlimited_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Check(Base):
    __tablename__ = "checks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16))  # web | telegram | max
    query: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(8))
    vin: Mapped[str | None] = mapped_column(String(17), index=True)
    plate: Mapped[str | None] = mapped_column(String(16))
    tier: Mapped[str] = mapped_column(String(16), default="express")
    overall: Mapped[str] = mapped_column(String(8), default="ok")
    vehicle: Mapped[dict[str, Any]] = mapped_column(default=dict)
    flags: Mapped[list[Any]] = mapped_column(default=list)
    sections: Mapped[dict[str, Any]] = mapped_column(default=dict)  # method -> {data|error}
    provider_cost: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product: Mapped[str] = mapped_column(String(32))
    check_id: Mapped[str | None] = mapped_column(ForeignKey("checks.id"))
    amount_rub: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="paid")
    provider: Mapped[str] = mapped_column(String(32), default="telegram")
    provider_charge_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    """Счёт на оплату во внешней платёжке (Robokassa). id используется как InvId."""
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product: Mapped[str] = mapped_column(String(32))
    check_id: Mapped[str | None] = mapped_column(ForeignKey("checks.id"))
    amount_rub: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | paid | failed
    platform: Mapped[str] = mapped_column(String(16))  # max | telegram | web
    chat_id: Mapped[int | None] = mapped_column(BigInteger)  # куда доставить результат
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class LoginToken(Base):
    """Одноразовый токен входа на сайт через бота.

    kind="site": создан страницей /login, бот привязывает его к пользователю после подтверждения;
    kind="link": создан ботом по команде /cabinet, ведёт сразу в кабинет.
    """
    __tablename__ = "login_tokens"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class WebSession(Base):
    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    mfa_at: Mapped[datetime | None] = mapped_column(DateTime)  # когда в этой сессии пройдена 2FA


class ProProfile(Base):
    """Анкета специалиста в каталоге: подборщик, выездная диагностика, перекуп."""
    __tablename__ = "pro_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # selector | inspector | dealer
    name: Mapped[str] = mapped_column(String(80))
    city: Mapped[str] = mapped_column(String(60), index=True)
    about: Mapped[str] = mapped_column(Text, default="")
    search_text: Mapped[str] = mapped_column(Text, default="")  # имя+о себе+услуги в нижнем регистре
    services: Mapped[list[Any]] = mapped_column(default=list)  # [{"title": ..., "price": ...}]
    phone: Mapped[str | None] = mapped_column(String(32))
    telegram: Mapped[str | None] = mapped_column(String(64))
    max_link: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(200))
    experience_years: Mapped[int | None] = mapped_column(Integer)
    # draft | pending (на модерации) | approved | rejected | hidden
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(300))
    # Задел под монетизацию: тариф анкеты и поднятие в выдаче
    plan: Mapped[str] = mapped_column(String(16), default="free")  # free | pro
    featured_until: Mapped[datetime | None] = mapped_column(DateTime)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)  # согласие на публикацию контактов
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)


class ProStat(Base):
    """Статистика анкеты по дням — основа для будущих платных тарифов."""
    __tablename__ = "pro_stats"
    __table_args__ = (UniqueConstraint("profile_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("pro_profiles.id"), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    views: Mapped[int] = mapped_column(Integer, default=0)
    contacts: Mapped[int] = mapped_column(Integer, default=0)


class CacheEntry(Base):
    __tablename__ = "provider_cache"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)  # уже без ПДн
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ProviderLog(Base):
    """Учёт расходов у провайдера (блок billing из ответа)."""
    __tablename__ = "provider_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_id: Mapped[str | None] = mapped_column(String(36), index=True)
    method: Mapped[str] = mapped_column(String(32))
    charged: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    test_mode: Mapped[bool] = mapped_column(default=False)
    balance: Mapped[float | None] = mapped_column(Numeric(12, 2))  # баланс у провайдера после запроса
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def _add_missing_columns(conn) -> None:
    """Простая миграция для уже созданной базы: досоздаёт новые колонки (всегда допускающие NULL)
    и их индексы. Данные не трогает; удаление и переименование колонок так не делаются."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        added = set()
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=conn.dialect)
            conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}'))
            added.add(col.name)
            log.warning("Миграция: добавлена колонка %s.%s", table.name, col.name)
        for idx in table.indexes:
            if {c.name for c in idx.columns} & added:
                idx.create(conn, checkfirst=True)


class AdminAudit(Base):
    """Журнал действий сотрудников в панели."""
    __tablename__ = "admin_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(120))
    details: Mapped[dict[str, Any] | None] = mapped_column()
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SystemAlert(Base):
    """Тревоги мониторинга (расход и баланс у провайдера, ошибки источников)."""
    __tablename__ = "system_alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(48), index=True)
    level: Mapped[str] = mapped_column(String(16))  # critical | warning
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.sessionmaker()
