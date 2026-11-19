"""Telegram-бот (aiogram 3).

Сценарий: /start → роль (разово / подборщик) → VIN/госномер/ссылка → экспресс-результат →
оплата (Telegram Payments, ЮКасса/СБП) или пакет → PDF полного отчёта.
Запуск: python -m shturman.bot
"""
from __future__ import annotations

import html
import logging
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery)

from ..billing import Product, apply_payment, can_use_credit, has_unlimited, products
from ..auth import AuthService
from .. import privacy
from .common import (DELETE_CONFIRM, DELETED, consent_text, privacy_text, BIND_CONFIRM, BIND_DONE, BIND_EXPIRED, HELP, ICONS, LEGAL, LOGIN_CONFIRM, LOGIN_DONE,
                     LOGIN_EXPIRED, Btn, balance_text, cabinet_message,
                     offer_buttons, packs_buttons, report_file, short, site_name, summary_text, tariffs_text)
from ..cascade import TIER_TITLES
from ..config import Settings
from ..db import Check, User
from ..identifiers import Identifier, IdentifierError, parse_many
from ..services import CheckService, LimitExceeded

log = logging.getLogger(__name__)
router = Router()

def _kb(rows: list[list[Btn]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b.text, url=b.url) if b.url else InlineKeyboardButton(text=b.text, callback_data=b.data)
         for b in row] for row in rows])


def offer_keyboard(check: Check, user: User, settings: Settings) -> InlineKeyboardMarkup:
    return _kb(offer_buttons(check, user, settings))


def packs_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return _kb(packs_buttons(settings))


async def send_report(message: Message, service: CheckService, check: Check) -> None:
    name, data = await report_file(service, check)
    await message.answer_document(BufferedInputFile(data, filename=name), caption=summary_text(check)[:1024])


# ---------- команды ----------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id, message.from_user.username)
    arg = (command.args or "").strip()
    if arg.startswith("login_"):
        await message.answer(LOGIN_CONFIRM.format(site=html.escape(site_name(settings))),
                             reply_markup=_kb([[Btn("✅ Подтвердить вход", data=f"login:{arg[6:]}")]]))
        return
    if arg.startswith("bind_"):
        target_id = await AuthService(service.db).peek_token(arg[5:], "bind")
        target = await service.get_user(target_id) if target_id else None
        if target is None:
            await message.answer(BIND_EXPIRED)
            return
        await message.answer(BIND_CONFIRM.format(messenger="Telegram", account=html.escape(target.email or f"№{target.id}"),
                                                 site=html.escape(site_name(settings))),
                             reply_markup=_kb([[Btn("✅ Привязать", data=f"bind:{arg[5:]}")]]))
        return
    if arg.startswith("chk_"):
        check = await service.find_by_short_id(arg[4:])
        if check and check.user_id in (None, user.id):  # чужие проверки не показываем
            await service.attach(check.id, user.id)
            await message.answer(summary_text(check), reply_markup=offer_keyboard(check, user, settings))
            return
    await message.answer(
        f"Здравствуйте! Это <b>{html.escape(settings.brand_name)}</b> — проверка автомобиля по VIN, "
        "госномеру или ссылке на объявление.\n\nКак вы будете пользоваться сервисом?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚗 Проверить одну машину", callback_data="role:buyer")],
            [InlineKeyboardButton(text="🧰 Я автоподборщик", callback_data="role:picker")]]))


@router.callback_query(F.data.startswith("role:"))
async def cb_role(cq: CallbackQuery, service: CheckService) -> None:
    role = cq.data.split(":", 1)[1]
    user = await service.tg_user(cq.from_user.id)
    await service.update_user(user.id, role=role)
    if role == "picker":
        await cq.message.answer("Укажите ваш статус — это нужно для чеков и закрывающих документов:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text=t, callback_data=f"legal:{k}")] for k, t in LEGAL.items()]))
    else:
        await cq.message.answer("Пришлите VIN, госномер или ссылку на объявление.")
    await cq.answer()


@router.callback_query(F.data.startswith("legal:"))
async def cb_legal(cq: CallbackQuery, service: CheckService, settings: Settings) -> None:
    status = cq.data.split(":", 1)[1]
    user = await service.tg_user(cq.from_user.id)
    await service.update_user(user.id, legal_status=status)
    extra = ("\nДля ИП и ООО можем выставить счёт и акт — напишите в поддержку."
             if status in ("ip", "llc") else "")
    await cq.message.answer("Пакеты для подборщиков: отчёты уровня «Досье» дешевле разовых — "
                            "чем больше пакет, тем ниже цена отчёта." + extra,
                            reply_markup=packs_keyboard(settings))
    await cq.message.answer("Присылайте VIN — по одному или списком (каждый с новой строки).")
    await cq.answer()


@router.callback_query(F.data.startswith("login:"))
async def cb_login(cq: CallbackQuery, service: CheckService) -> None:
    user = await service.tg_user(cq.from_user.id)
    ok = await AuthService(service.db).bind_site_token(cq.data.split(":", 1)[1], user.id)
    await cq.message.answer(LOGIN_DONE if ok else LOGIN_EXPIRED)
    await cq.answer()


@router.callback_query(F.data.startswith("bind:"))
async def cb_bind(cq: CallbackQuery, service: CheckService) -> None:
    user = await service.tg_user(cq.from_user.id)
    target_id = await AuthService(service.db).consume_token(cq.data.split(":", 1)[1], "bind")
    if target_id:
        await service.merge_users(target_id, user.id)
    await cq.message.answer(BIND_DONE if target_id else BIND_EXPIRED)
    await cq.answer()


@router.message(Command("cabinet"))
async def cmd_cabinet(message: Message, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id)
    text, buttons = cabinet_message(settings, await AuthService(service.db).new_link_token(user.id))
    await message.answer(text, reply_markup=_kb(buttons) if buttons else None)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


# ---------------------------------------------------------------- 152-ФЗ
@router.callback_query(F.data.startswith("consent:"))
async def cb_consent(cq: CallbackQuery, service: CheckService) -> None:
    user = await service.tg_user(cq.from_user.id)
    if not privacy.has_pd_consent(user):
        for kind in ["offer", "pd"] + (["marketing"] if cq.data == "consent:mkt" else []):
            await privacy.record(service.db, user.id, kind, True, source="telegram")
    await cq.answer()
    await cq.message.answer("Спасибо! " + HELP + "\n\nЕсли вы пришли по ссылке с сайта — откройте её ещё раз.")


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id)
    mkt = (Btn("🔕 Отписаться от новостей", data="privacy:mkt_off") if user.marketing_consent_at
           else Btn("🔔 Получать новости и предложения", data="privacy:mkt_on"))
    await message.answer(privacy_text(user, settings),
                         reply_markup=_kb([[mkt], [Btn("🗑 Удалить мои данные", data="privacy:delete")]]))


@router.callback_query(F.data.startswith("privacy:"))
async def cb_privacy(cq: CallbackQuery, service: CheckService) -> None:
    user = await service.tg_user(cq.from_user.id)
    action = cq.data.split(":", 1)[1]
    await cq.answer()
    if action in ("mkt_on", "mkt_off"):
        await privacy.record(service.db, user.id, "marketing", action == "mkt_on", source="telegram")
        await cq.message.answer("Подписка включена. Отписаться — /privacy." if action == "mkt_on"
                                else "Вы отписались от рекламных и информационных сообщений.")
    elif action == "delete":
        await cq.message.answer(DELETE_CONFIRM, reply_markup=_kb([[Btn("Да, удалить навсегда",
                                                                        data="privacy:delete_yes")],
                                                                   [Btn("Отмена", data="privacy:cancel")]]))
    elif action == "delete_yes":
        await privacy.delete_account(service.db, user.id)
        await cq.message.answer(DELETED)
    else:
        await cq.message.answer("Хорошо, ничего не удаляем.")


@router.message(Command("tariffs"))
async def cmd_tariffs(message: Message, settings: Settings) -> None:
    await message.answer(tariffs_text(settings), reply_markup=packs_keyboard(settings))


@router.message(Command("balance"))
async def cmd_balance(message: Message, service: CheckService) -> None:
    user = await service.tg_user(message.from_user.id)
    await message.answer(balance_text(user, await service.free_left(user=user)))


@router.message(Command("history"))
async def cmd_history(message: Message, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id)
    items = await service.history(user.id)
    if not items:
        await message.answer("Проверок пока нет. Пришлите VIN или госномер.")
        return
    lines = ["<b>Последние проверки</b>"]
    for c in items:
        v = c.vehicle or {}
        lines.append(f"{c.created_at:%d.%m %H:%M} · {html.escape(v.get('title') or c.query)} · "
                     f"{html.escape(c.vin or c.query)} · «{TIER_TITLES[c.tier]}» "
                     f"{ICONS.get(c.overall, '')} /r_{short(c.id)}")
    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"^/r_[0-9a-f]{32}$"))
async def cmd_open(message: Message, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id)
    check = await service.find_by_short_id(message.text[3:])
    if not check or check.user_id != user.id:
        await message.answer("Отчёт не найден.")
        return
    if check.tier == "express":
        await message.answer(summary_text(check), reply_markup=offer_keyboard(check, user, settings))
    else:
        await send_report(message, service, check)


# ---------- проверки ----------
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id, message.from_user.username)
    items = parse_many(message.text)
    if not items:
        return
    if len(items) > 50:
        await message.answer("За один раз — не больше 50 запросов.")
        return
    if len(items) == 1:
        item = items[0]
        if isinstance(item, IdentifierError):
            await message.answer(str(item))
            return
        await single_check(message, service, settings, user, item)
        return
    await batch_check(message, service, settings, user, items)


async def single_check(message: Message, service: CheckService, settings: Settings, user: User,
                       ident: Identifier) -> None:
    wait = await message.answer("🔎 Проверяю…")
    try:
        check = await service.express(ident, user=user, source="telegram",
                                      enforce_limit=not can_use_credit(user, "dossier"))
    except LimitExceeded as e:
        await wait.edit_text(str(e), reply_markup=packs_keyboard(settings))
        return
    await wait.delete()
    await message.answer(summary_text(check), reply_markup=offer_keyboard(check, user, settings))


async def batch_check(message: Message, service: CheckService, settings: Settings, user: User,
                      items: list) -> None:
    use_credit = can_use_credit(user, "dossier")
    await message.answer(f"Принял {len(items)} запросов. "
                         + ("Отчёты «Досье» спишутся из пакета." if use_credit else
                            "Пакета нет — делаю экспресс-проверки."))
    for item in items:
        if isinstance(item, IdentifierError):
            await message.answer(f"⚠️ {html.escape(str(item))}")
            continue
        try:
            check = await service.express(item, user=user, source="telegram", enforce_limit=not use_credit)
            if use_credit:
                user = await service.get_user(user.id)
                if can_use_credit(user, "dossier"):
                    check = await service.upgrade_with_credit(user.id, check.id, "dossier")
                    await send_report(message, service, check)
                    continue
                use_credit = False
            await message.answer(summary_text(check), reply_markup=offer_keyboard(check, user, settings))
        except LimitExceeded as e:
            await message.answer(str(e), reply_markup=packs_keyboard(settings))
            return
        except Exception:  # одна машина не должна ронять всю пачку
            log.exception("batch item failed")
            await message.answer(f"⚠️ {html.escape(item.value)}: не удалось проверить, попробуйте позже.")
    user = await service.get_user(user.id)
    await message.answer(balance_text(user, await service.free_left(user=user)))


@router.callback_query(F.data.startswith("credit:"))
async def cb_credit(cq: CallbackQuery, service: CheckService) -> None:
    user = await service.tg_user(cq.from_user.id)
    check = await service.find_by_short_id(cq.data.split(":", 1)[1])
    if not check:
        await cq.answer("Проверка не найдена", show_alert=True)
        return
    await cq.answer("Собираю отчёт…")
    try:
        check = await service.upgrade_with_credit(user.id, check.id, "dossier")
    except PermissionError as e:
        await cq.message.answer(str(e))
        return
    await send_report(cq.message, service, check)


# ---------- оплата ----------
def _payload(product: Product, check_id: str | None, user: User) -> str:
    return f"{product.code}:{short(check_id) if check_id else '-'}:{user.id}"


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cq: CallbackQuery, service: CheckService, settings: Settings, bot: Bot) -> None:
    _, code, sid = cq.data.split(":", 2)
    product = products(settings).get(code)
    if not product:
        await cq.answer("Неизвестный тариф", show_alert=True)
        return
    user = await service.tg_user(cq.from_user.id)
    check = await service.find_by_short_id(sid) if sid != "-" else None
    if check is not None and check.user_id not in (None, user.id):
        await cq.answer("Проверка не найдена", show_alert=True)
        return
    if settings.dev_fake_payments and not settings.telegram_payment_token:
        await cq.answer("Тестовая оплата")
        await fulfil(cq.message, service, settings, user, product, check.id if check else None,
                     charge_id=f"dev-{uuid.uuid4()}", provider="dev")
        return
    if not settings.telegram_payment_token:
        await cq.answer("Оплата пока не подключена", show_alert=True)
        return
    title = product.title + (f" · {(check.vehicle or {}).get('title', '')}" if check else "")
    await bot.send_invoice(
        chat_id=cq.message.chat.id, title=title[:32], description=product.description[:255] or product.title,
        payload=_payload(product, check.id if check else None, user), provider_token=settings.telegram_payment_token,
        currency="RUB", prices=[LabeledPrice(label=product.title, amount=product.price * 100)])
    await cq.answer()


@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery, settings: Settings) -> None:
    code = pcq.invoice_payload.split(":", 1)[0]
    product = products(settings).get(code)
    ok = product is not None and pcq.currency == "RUB" and pcq.total_amount == product.price * 100
    await pcq.answer(ok=ok, error_message=None if ok else "Тариф изменился, повторите покупку.")


@router.message(F.successful_payment)
async def on_paid(message: Message, service: CheckService, settings: Settings) -> None:
    sp = message.successful_payment
    code, sid, _ = sp.invoice_payload.split(":", 2)
    product = products(settings)[code]
    user = await service.tg_user(message.from_user.id)
    check = await service.find_by_short_id(sid) if sid != "-" else None
    await fulfil(message, service, settings, user, product, check.id if check else None,
                 charge_id=sp.telegram_payment_charge_id, provider="telegram")


async def fulfil(message: Message, service: CheckService, settings: Settings, user: User, product: Product,
                 check_id: str | None, charge_id: str, provider: str) -> None:
    async with service.db.session() as s:
        db_user = await s.get(User, user.id)
        await apply_payment(s, db_user, product, check_id=check_id, charge_id=charge_id, provider=provider)
        await s.commit()
    if product.tier and check_id:
        await message.answer("Оплата получена, собираю отчёт… Обычно это занимает до 2 минут.")
        check = await service.upgrade(check_id, product.tier)
        await send_report(message, service, check)
        return
    user = await service.get_user(user.id)
    await message.answer("Оплата получена ✅\n" + balance_text(user, await service.free_left(user=user)))
    if check_id:
        check = await service.get_check(check_id)
        await message.answer(summary_text(check), reply_markup=offer_keyboard(check, user, settings))


@router.message.outer_middleware()
@router.callback_query.outer_middleware()
async def block_middleware(handler, event, data):
    """Заблокированные в панели пользователи получают отказ, остальное не обрабатывается."""
    service: CheckService = data["service"]
    if event.from_user:
        user = await service.tg_user(event.from_user.id)
        if user.blocked_at is not None:
            text = "Аккаунт заблокирован. Если это ошибка — напишите в поддержку."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return None
        settings: Settings = data["settings"]
        is_consent = isinstance(event, CallbackQuery) and (event.data or "").startswith("consent:")
        if settings.bot_consent_required and not privacy.has_pd_consent(user) and not is_consent:
            target = event.message if isinstance(event, CallbackQuery) else event
            if isinstance(event, CallbackQuery):
                await event.answer()
            await target.answer(consent_text(settings), reply_markup=_kb([
                [Btn("✅ Принимаю", data="consent:yes")],
                [Btn("✅ Принимаю и хочу получать новости", data="consent:mkt")]]))
            return None
    return await handler(event, data)


async def run_polling(service: CheckService, settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(settings.telegram_bot_token, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("Бот @%s запущен", me.username)
    if settings.telegram_bot_username and settings.telegram_bot_username.lstrip("@") != me.username:
        log.warning("TELEGRAM_BOT_USERNAME=%s не совпадает с @%s — ссылки с сайта будут вести не туда",
                    settings.telegram_bot_username, me.username)
    dp = Dispatcher()
    dp.include_router(router)
    dp["service"] = service
    dp["settings"] = settings
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="balance", description="Баланс и пакеты"),
        BotCommand(command="history", description="История проверок"),
        BotCommand(command="cabinet", description="Личный кабинет на сайте"),
        BotCommand(command="tariffs", description="Тарифы"),
        BotCommand(command="privacy", description="Мои данные и удаление"),
        BotCommand(command="help", description="Помощь")])
    await dp.start_polling(bot)
