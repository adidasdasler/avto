"""MAX-бот: тот же сценарий, что в Telegram, с оплатой через Robokassa.

/start → роль (разово / подборщик) → VIN/госномер/ссылка → экспресс-результат со светофором →
кнопка тарифа → ссылка на оплату Robokassa → после оплаты бот присылает отчёт (PDF или HTML).

Оплата подтверждается уведомлением Robokassa на ResultURL (если сайт доступен из интернета)
или опросом статуса счёта — второй путь работает на домашнем компьютере без публичного сервера.
Запуск: python -m shturman.bot.max
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from ..billing import can_use_credit, products
from ..config import Settings
from ..db import Check, Order, User
from ..identifiers import Identifier, IdentifierError, parse_many
from ..orders import OrderService, PaymentsNotConfigured
from ..services import CheckService, LimitExceeded
from ..auth import AuthService
from .common import (HELP, ICONS, LEGAL, LOGIN_CONFIRM, LOGIN_DONE, LOGIN_EXPIRED, Btn, balance_text, cabinet_message,
                     offer_buttons, packs_buttons, report_file, short, site_name, summary_text, tariffs_text)
from .max_api import MaxAPI, MaxError

log = logging.getLogger(__name__)

MAX_BATCH = 50


def dig(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


class Ctx:
    """Куда отвечать: в чат (chat_id) или пользователю (user_id)."""

    def __init__(self, user: User, chat_id: int | None, user_id: int):
        self.user, self.chat_id, self.user_id = user, chat_id, user_id

    @property
    def target(self) -> dict[str, int]:
        return {"chat_id": self.chat_id} if self.chat_id is not None else {"user_id": self.user_id}


class MaxBot:
    def __init__(self, api: MaxAPI, service: CheckService, orders: OrderService, settings: Settings,
                 payments_interval: float = 15.0):
        self.api, self.service, self.orders, self.settings = api, service, orders, settings
        self.payments_interval = payments_interval
        self.marker: int | None = None
        self._deliver_lock = asyncio.Lock()
        self.auth = AuthService(service.db)
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- отправка
    async def say(self, ctx: Ctx, text: str, buttons: list[list[Btn]] | None = None) -> None:
        await self.api.send_message(**ctx.target, text=text, buttons=buttons)

    async def send_report(self, ctx: Ctx, check: Check) -> None:
        name, data = await report_file(self.service, check)
        await self.api.send_file(**ctx.target, data=data, filename=name, text=summary_text(check))

    async def show_offer(self, ctx: Ctx, check: Check) -> None:
        user = await self.service.get_user(ctx.user.id)
        await self.say(ctx, summary_text(check), offer_buttons(check, user, self.settings))

    # ---------------------------------------------------------------- разбор событий
    async def handle(self, upd: dict[str, Any]) -> None:
        kind = upd.get("update_type")
        if kind == "bot_started":
            u = upd.get("user") or {}
            ctx = await self._ctx(u, upd.get("chat_id"))
            await self.on_start(ctx, upd.get("payload"))
        elif kind == "message_created":
            msg = upd.get("message") or {}
            u = msg.get("sender") or {}
            if not u.get("user_id") or u.get("is_bot"):
                return
            ctx = await self._ctx(u, dig(msg, "recipient", "chat_id"))
            text = (dig(msg, "body", "text") or "").strip()
            await self.on_text(ctx, text)
        elif kind == "message_callback":
            cb = upd.get("callback") or {}
            u = cb.get("user") or {}
            if cb.get("callback_id"):
                await self.api.answer_callback(cb["callback_id"])
            ctx = await self._ctx(u, dig(upd, "message", "recipient", "chat_id"))
            await self.on_callback(ctx, cb.get("payload") or "")

    async def _ctx(self, u: dict[str, Any], chat_id: int | None) -> Ctx:
        uid = int(u["user_id"])
        user = await self.service.max_user(uid, u.get("username") or u.get("name"))
        return Ctx(user, chat_id, uid)

    # ---------------------------------------------------------------- сценарии
    async def on_start(self, ctx: Ctx, payload: str | None) -> None:
        payload = (payload or "").strip()
        if payload.startswith("login_"):
            await self.say(ctx, LOGIN_CONFIRM.format(site=html.escape(site_name(self.settings))),
                           [[Btn("✅ Подтвердить вход", data=f"login:{payload[6:]}")]])
            return
        if payload.startswith("chk_"):
            check = await self.service.find_by_short_id(payload[4:])
            if check:
                await self.service.attach(check.id, ctx.user.id)
                await self.show_offer(ctx, check)
                return
        await self.say(ctx, f"Здравствуйте! Это <b>{html.escape(self.settings.brand_name)}</b> — проверка автомобиля "
                            "по VIN, госномеру или ссылке на объявление.\n\nКак вы будете пользоваться сервисом?",
                       [[Btn("🚗 Проверить одну машину", data="role:buyer")],
                        [Btn("🧰 Я автоподборщик", data="role:picker")]])

    async def on_text(self, ctx: Ctx, text: str) -> None:
        if not text:
            return
        cmd = text.split()[0].lower() if text.startswith("/") else ""
        if cmd == "/start":
            await self.on_start(ctx, text[6:].strip())
        elif cmd == "/help":
            await self.say(ctx, HELP)
        elif cmd == "/tariffs":
            await self.say(ctx, tariffs_text(self.settings), packs_buttons(self.settings))
        elif cmd == "/balance":
            user = await self.service.get_user(ctx.user.id)
            await self.say(ctx, balance_text(user, await self.service.free_left(user=user)))
        elif cmd == "/history":
            await self.history(ctx)
        elif cmd == "/cabinet":
            text, buttons = cabinet_message(self.settings, await self.auth.new_link_token(ctx.user.id))
            await self.say(ctx, text, buttons)
        elif cmd.startswith("/r_"):
            check = await self.service.find_by_short_id(cmd[3:])
            if not check or check.user_id != ctx.user.id:
                await self.say(ctx, "Отчёт не найден.")
            elif check.tier == "express":
                await self.show_offer(ctx, check)
            else:
                await self.send_report(ctx, check)
        elif cmd:
            await self.say(ctx, "Не знаю такой команды. " + HELP)
        else:
            await self.checks(ctx, text)

    async def history(self, ctx: Ctx) -> None:
        from ..cascade import TIER_TITLES

        items = await self.service.history(ctx.user.id)
        if not items:
            await self.say(ctx, "Проверок пока нет. Пришлите VIN или госномер.")
            return
        lines = ["<b>Последние проверки</b>"]
        for c in items:
            v = c.vehicle or {}
            lines.append(f"{c.created_at:%d.%m %H:%M} · {html.escape(v.get('title') or c.query)} · "
                         f"«{TIER_TITLES[c.tier]}» {ICONS.get(c.overall, '')} /r_{short(c.id)}")
        await self.say(ctx, "\n".join(lines))

    async def checks(self, ctx: Ctx, text: str) -> None:
        items = parse_many(text)
        if len(items) > MAX_BATCH:
            await self.say(ctx, f"За один раз — не больше {MAX_BATCH} запросов.")
            return
        if len(items) == 1:
            if isinstance(items[0], IdentifierError):
                await self.say(ctx, html.escape(str(items[0])))
                return
            await self.single(ctx, items[0])
        else:
            await self.batch(ctx, items)

    async def single(self, ctx: Ctx, ident: Identifier) -> None:
        user = await self.service.get_user(ctx.user.id)
        try:
            check = await self.service.express(ident, user=user, source="max",
                                               enforce_limit=not can_use_credit(user, "dossier"))
        except LimitExceeded as e:
            await self.say(ctx, html.escape(str(e)), packs_buttons(self.settings))
            return
        await self.show_offer(ctx, check)

    async def batch(self, ctx: Ctx, items: list) -> None:
        user = await self.service.get_user(ctx.user.id)
        use_credit = can_use_credit(user, "dossier")
        await self.say(ctx, f"Принял {len(items)} запросов. " + ("Отчёты «Досье» спишутся из пакета."
                                                                if use_credit else "Пакета нет — делаю экспресс-проверки."))
        for item in items:
            if isinstance(item, IdentifierError):
                await self.say(ctx, f"⚠️ {html.escape(str(item))}")
                continue
            try:
                check = await self.service.express(item, user=user, source="max", enforce_limit=not use_credit)
                if use_credit:
                    user = await self.service.get_user(user.id)
                    if can_use_credit(user, "dossier"):
                        check = await self.service.upgrade_with_credit(user.id, check.id, "dossier")
                        await self.send_report(ctx, check)
                        continue
                    use_credit = False
                await self.show_offer(ctx, check)
            except LimitExceeded as e:
                await self.say(ctx, html.escape(str(e)), packs_buttons(self.settings))
                return
            except Exception:
                log.exception("batch item failed")
                await self.say(ctx, f"⚠️ {html.escape(item.value)}: не удалось проверить, попробуйте позже.")
        user = await self.service.get_user(user.id)
        await self.say(ctx, balance_text(user, await self.service.free_left(user=user)))

    async def on_callback(self, ctx: Ctx, data: str) -> None:
        kind, _, rest = data.partition(":")
        if kind == "role":
            await self.service.update_user(ctx.user.id, role=rest)
            if rest == "picker":
                await self.say(ctx, "Укажите ваш статус — это нужно для чеков и закрывающих документов:",
                               [[Btn(t, data=f"legal:{k}")] for k, t in LEGAL.items()])
            else:
                await self.say(ctx, "Пришлите VIN, госномер или ссылку на объявление.")
        elif kind == "legal":
            await self.service.update_user(ctx.user.id, legal_status=rest)
            extra = "\nДля ИП и ООО можем выставить счёт и акт — напишите в поддержку." if rest in ("ip", "llc") else ""
            await self.say(ctx, "Пакеты для подборщиков: отчёты «Досье» дешевле разовых, а безлимит на сутки — "
                                "для активного дня просмотров." + extra, packs_buttons(self.settings))
            await self.say(ctx, "Присылайте VIN — по одному или списком (каждый с новой строки).")
        elif kind == "credit":
            check = await self.service.find_by_short_id(rest)
            if not check:
                await self.say(ctx, "Проверка не найдена.")
                return
            await self.say(ctx, "Собираю отчёт…")
            try:
                check = await self.service.upgrade_with_credit(ctx.user.id, check.id, "dossier")
            except PermissionError as e:
                await self.say(ctx, html.escape(str(e)))
                return
            await self.send_report(ctx, check)
        elif kind == "buy":
            code, _, sid = rest.partition(":")
            await self.buy(ctx, code, sid)
        elif kind == "login":
            ok = await self.auth.bind_site_token(rest, ctx.user.id)
            await self.say(ctx, LOGIN_DONE if ok else LOGIN_EXPIRED)
        elif kind == "paid":
            await self.check_paid(ctx, int(rest) if rest.isdigit() else 0)

    # ---------------------------------------------------------------- оплата
    async def buy(self, ctx: Ctx, code: str, sid: str) -> None:
        product = products(self.settings).get(code)
        if not product:
            await self.say(ctx, "Неизвестный тариф.")
            return
        check = await self.service.find_by_short_id(sid) if sid and sid != "-" else None
        try:
            order, url = await self.orders.create(ctx.user.id, code, check_id=check.id if check else None,
                                                  platform="max", chat_id=ctx.chat_id or ctx.user_id)
        except PaymentsNotConfigured:
            await self.say(ctx, "Оплата пока не подключена.")
            return
        if url is None:  # DEV_FAKE_PAYMENTS: заказ уже оплачен
            await self.deliver_pending()
            return
        what = f"{product.title}" + (f" · {html.escape((check.vehicle or {}).get('title', ''))}" if check else "")
        await self.say(ctx, f"Счёт № {order.id}: <b>{what}</b> — {product.price} ₽.\n"
                            "Оплатите картой или через СБП на странице Робокассы. "
                            "После оплаты отчёт придёт сюда автоматически, обычно в течение минуты.",
                       [[Btn(f"💳 Оплатить {product.price} ₽", url=url)],
                        [Btn("Я оплатил — проверить", data=f"paid:{order.id}")]])

    async def check_paid(self, ctx: Ctx, order_id: int) -> None:
        order = await self.orders.get(order_id)
        if not order or order.user_id != ctx.user.id:
            await self.say(ctx, "Счёт не найден.")
            return
        order = await self.orders.refresh(order_id)
        if order and order.status == "paid":
            await self.deliver_pending()
        else:
            await self.say(ctx, "Оплата пока не поступила. Если вы уже заплатили — подождите минуту, "
                                "я пришлю отчёт сам.")

    async def deliver(self, order: Order) -> None:
        user = await self.service.get_user(order.user_id)
        if user is None or user.max_id is None:
            await self.orders.mark_delivered(order.id)
            return
        ctx = Ctx(user, order.chat_id, user.max_id)
        product = products(self.settings)[order.product]
        if product.tier and order.check_id:
            await self.say(ctx, "Оплата получена ✅ Собираю отчёт… Обычно это до 2 минут.")
            check = await self.service.upgrade(order.check_id, product.tier)
            await self.send_report(ctx, check)
        else:
            await self.say(ctx, "Оплата получена ✅\n" + balance_text(user, await self.service.free_left(user=user)))
            if order.check_id:
                check = await self.service.get_check(order.check_id)
                await self.show_offer(ctx, check)
        await self.orders.mark_delivered(order.id)

    async def deliver_pending(self) -> None:
        async with self._deliver_lock:  # фоновый цикл и кнопка «Я оплатил» не доставят дважды
            for order in await self.orders.undelivered("max"):
                try:
                    await self.deliver(order)
                except Exception:
                    log.exception("Не удалось доставить заказ #%s, повторю позже", order.id)

    async def payments_loop(self) -> None:
        while True:
            try:
                await self.orders.poll_pending()
                await self.deliver_pending()
            except Exception:
                log.exception("payments loop")
            await asyncio.sleep(self.payments_interval)

    # ---------------------------------------------------------------- polling
    async def _safe_handle(self, upd: dict[str, Any]) -> None:
        try:
            await self.handle(upd)
        except MaxError as e:
            log.error("MAX API: %s", e)
        except Exception:
            log.exception("Ошибка обработки события %s", upd.get("update_type"))

    async def poll_once(self, timeout: int = 30, concurrent: bool = True) -> int:
        data = await self.api.get_updates(marker=self.marker, timeout=timeout)
        updates = data.get("updates") or []
        for upd in updates:
            if concurrent:  # сборка отчёта долгая — не задерживаем остальных пользователей
                task = asyncio.create_task(self._safe_handle(upd))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                await self._safe_handle(upd)
        if data.get("marker") is not None:
            self.marker = data["marker"]
        return len(updates)

    async def run(self) -> None:
        me = await self.api.get_me()
        log.info("MAX-бот запущен: %s (id %s)", me.get("name") or me.get("username"), me.get("user_id"))
        if not self.orders.robokassa:
            log.warning("Robokassa не настроена%s", " — включены тестовые оплаты" if self.settings.dev_fake_payments else
                        " — кнопки оплаты будут отвечать «Оплата пока не подключена»")
        payments = asyncio.create_task(self.payments_loop())
        try:
            while True:
                try:
                    await self.poll_once()
                except MaxError as e:
                    log.error("Ошибка получения событий: %s — повтор через 5 с", e)
                    await asyncio.sleep(5)
                await asyncio.sleep(0.3)  # не чаще 300 мс между запросами
        finally:
            payments.cancel()


async def main() -> None:
    from ..config import get_settings
    from ..db import Database
    from ..orders import make_robokassa
    from ..provider import make_provider

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.max_bot_token:
        raise SystemExit("Не задан MAX_BOT_TOKEN в .env")
    db = Database(settings.database_url)
    await db.create_all()
    provider = make_provider(settings)
    api = MaxAPI(settings.max_bot_token, settings.max_api_url)
    bot = MaxBot(api, CheckService(db, provider, settings), OrderService(db, settings, make_robokassa(settings)),
                 settings)
    try:
        await bot.run()
    finally:
        await api.aclose()
        await provider.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
