"""Заказы с оплатой через Robokassa.

Жизненный цикл: create() → пользователь платит по ссылке → заказ становится «paid»
одним из двух путей:
  1) Robokassa присылает уведомление на ResultURL (/pay/robokassa/result) — нужен публичный сервер;
  2) бот сам опрашивает статус счёта (OpStateExt) — работает и без публичного сервера.
Затем бот доставляет результат (отчёт или пополнение пакета) и ставит delivered_at.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .billing import apply_payment, products
from .config import Settings
from .db import Database, Order, User, utcnow
from .payments.robokassa import Robokassa, RobokassaError

log = logging.getLogger(__name__)


def make_robokassa(settings: Settings) -> Robokassa | None:
    if not settings.robokassa_login:
        return None
    return Robokassa(settings.robokassa_login, settings.robokassa_password1, settings.robokassa_password2,
                     test=settings.robokassa_test, algorithm=settings.robokassa_hash,
                     receipt=settings.robokassa_receipt, sno=settings.robokassa_sno, tax=settings.robokassa_tax)


class PaymentsNotConfigured(Exception):
    pass


class OrderService:
    def __init__(self, db: Database, settings: Settings, robokassa: Robokassa | None = None):
        self.db = db
        self.settings = settings
        self.robokassa = robokassa

    async def create(self, user_id: int, product_code: str, *, check_id: str | None, platform: str,
                     chat_id: int | None) -> tuple[Order, str | None]:
        """Создаёт заказ и возвращает ссылку на оплату (None — при тестовой «оплате» без платёжки)."""
        product = products(self.settings)[product_code]
        if not self.robokassa and not self.settings.dev_fake_payments:
            raise PaymentsNotConfigured("Оплата пока не подключена")
        async with self.db.session() as s:
            order = Order(user_id=user_id, product=product.code, check_id=check_id, amount_rub=product.price,
                          platform=platform, chat_id=chat_id)
            s.add(order)
            await s.commit()
        if self.robokassa is None:  # DEV_FAKE_PAYMENTS
            await self.mark_paid(order.id)
            return order, None
        title = f"{self.settings.brand_name}: {product.title}"
        url = self.robokassa.payment_url(order.id, product.price, title)
        return order, url

    async def get(self, order_id: int) -> Order | None:
        async with self.db.session() as s:
            return await s.get(Order, order_id)

    async def mark_paid(self, order_id: int, out_sum: str | float | None = None) -> Order | None:
        """Идемпотентно: повторное уведомление не начислит второй раз."""
        try:
            return await self._mark_paid(order_id, out_sum)
        except IntegrityError:  # параллельное уведомление уже записало эту оплату
            return await self.get(order_id)

    async def _mark_paid(self, order_id: int, out_sum: str | float | None) -> Order | None:
        async with self.db.session() as s:
            # Блокируем строку заказа: параллельные уведомления ждут, а не начисляют второй раз.
            order = await s.get(Order, order_id, with_for_update=True)
            if order is None:
                return None
            if order.status == "paid":
                return order
            if out_sum is not None and abs(float(out_sum) - order.amount_rub) > 0.009:
                log.error("Сумма оплаты %s не совпадает с заказом #%s (%s)", out_sum, order_id, order.amount_rub)
                return None
            user = await s.get(User, order.user_id)
            product = products(self.settings)[order.product]
            await apply_payment(s, user, product, check_id=order.check_id, charge_id=f"robokassa:{order.id}",
                                provider="robokassa" if self.robokassa else "dev")
            order.status = "paid"
            order.paid_at = utcnow()
            await s.commit()
            return order

    async def refresh(self, order_id: int) -> Order | None:
        """Спрашивает у Robokassa статус счёта и отмечает оплату."""
        order = await self.get(order_id)
        if order is None or order.status != "pending" or self.robokassa is None:
            return order
        try:
            state = await self.robokassa.op_state(order.id)
        except (RobokassaError, Exception) as e:  # сеть, формат — повторим в следующий раз
            log.warning("OpStateExt для заказа #%s не удался: %s", order_id, e)
            return order
        if state.paid:
            return await self.mark_paid(order.id, state.out_sum)
        return order

    async def poll_pending(self, max_age: timedelta = timedelta(hours=2)) -> None:
        if not (self.robokassa and self.settings.robokassa_poll_status):
            return
        async with self.db.session() as s:
            ids = (await s.execute(select(Order.id).where(
                Order.status == "pending", Order.created_at > utcnow() - max_age))).scalars().all()
        for oid in ids:
            await self.refresh(oid)

    async def undelivered(self, platform: str) -> list[Order]:
        async with self.db.session() as s:
            rows = await s.execute(select(Order).where(Order.status == "paid", Order.platform == platform,
                                                       Order.delivered_at.is_(None)).order_by(Order.id))
            return list(rows.scalars())

    async def mark_delivered(self, order_id: int) -> None:
        async with self.db.session() as s:
            order = await s.get(Order, order_id)
            if order:
                order.delivered_at = utcnow()
                await s.commit()
