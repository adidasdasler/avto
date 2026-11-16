"""Telegram-бот (aiogram 3).

Сценарий: /start → роль (разово / подборщик) → VIN/госномер/ссылка → экспресс-результат →
оплата (Telegram Payments, ЮКасса/СБП) или пакет → PDF полного отчёта.
Запуск: python -m shturman.bot
"""
from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery)

from ..billing import Product, apply_payment, can_use_credit, has_unlimited, products
from ..cascade import TIER_TITLES
from ..config import Settings
from ..db import Check, User
from ..identifiers import Identifier, IdentifierError, parse_many
from ..services import CheckService, LimitExceeded

log = logging.getLogger(__name__)
router = Router()

ICONS = {"ok": "✅", "bad": "❌", "warn": "⚠️", "info": "ℹ️", "na": "➖"}
VERDICT = {"ok": "🟢 Серьёзных проблем не найдено", "warn": "🟡 Есть замечания", "bad": "🔴 Найдены риски"}
LEGAL = {"person": "Физлицо", "selfemployed": "Самозанятый", "ip": "ИП", "llc": "ООО"}

HELP = (
    "<b>Как пользоваться</b>\n"
    "Пришлите VIN (17 символов), госномер (А123ВС77) или ссылку на объявление — "
    "я сделаю бесплатную экспресс-проверку: залог, такси, ЭПТС, ДТП.\n\n"
    "Подборщикам: пришлите несколько VIN — по одному в строке.\n\n"
    "/balance — остаток пакета и безлимита\n/history — последние проверки\n"
    "/tariffs — тарифы и пакеты\n/help — эта справка"
)


def short(check_id: str) -> str:
    return check_id.replace("-", "")


def summary_text(check: Check) -> str:
    v = check.vehicle or {}
    head = html.escape(v.get("title") or "Автомобиль") + (f", {v['year']}" if v.get("year") else "")
    ids = " · ".join(html.escape(str(x)) for x in (v.get("vin"), v.get("plate")) if x)
    lines = [f"<b>{head}</b>", ids, "", VERDICT.get(check.overall, ""), ""]
    for f in check.flags or []:
        lines.append(f"{ICONS.get(f['status'], '')} <b>{html.escape(f['title'])}</b>: {html.escape(f['summary'])}")
    lines.append(f"\nТариф: «{TIER_TITLES[check.tier]}»")
    return "\n".join(lines)


def offer_keyboard(check: Check, user: User, settings: Settings) -> InlineKeyboardMarkup:
    rows = []
    sid = short(check.id)
    if check.tier in ("express", "basic") and can_use_credit(user, "dossier"):
        label = "безлимит" if has_unlimited(user) else f"осталось {user.credits}"
        rows.append([InlineKeyboardButton(text=f"📄 «Досье» из пакета ({label})", callback_data=f"credit:{sid}")])
    order = ["basic", "dossier", "dossier_plus"]
    for code in order[order.index(check.tier) + 1:] if check.tier in order else order:
        p = products(settings)[code]
        rows.append([InlineKeyboardButton(text=f"{p.title} — {p.price} ₽", callback_data=f"buy:{code}:{sid}")])
    rows.append([InlineKeyboardButton(text="🌐 Открыть отчёт", url=f"{settings.public_base_url.rstrip('/')}/r/{check.id}")]
                if settings.public_base_url.startswith("https://") else [])
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def packs_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    ps = products(settings)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{ps[c].title} — {ps[c].price} ₽", callback_data=f"buy:{c}:-")]
        for c in ("pack_10", "pack_25", "pack_50", "unlimited_day")])


def balance_text(user: User, free_left: int) -> str:
    parts = [f"Отчётов «Досье» в пакете: <b>{user.credits}</b>"]
    if has_unlimited(user):
        until = user.unlimited_until.replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m %H:%M")
        parts.append(f"Безлимит активен до <b>{until}</b>")
    parts.append(f"Бесплатных экспресс-проверок сегодня: <b>{free_left}</b>")
    return "\n".join(parts)


async def send_report(message: Message, service: CheckService, check: Check) -> None:
    pdf = await service.pdf(check)
    name = f"{(check.vehicle or {}).get('title', 'auto')}-{check.vin or short(check.id)[:8]}-{TIER_TITLES[check.tier]}.pdf"
    await message.answer_document(BufferedInputFile(pdf, filename=name.replace(" ", "_")),
                                  caption=summary_text(check)[:1024])


# ---------- команды ----------
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, service: CheckService, settings: Settings) -> None:
    user = await service.tg_user(message.from_user.id, message.from_user.username)
    arg = (command.args or "").strip()
    if arg.startswith("chk_"):
        check = await service.find_by_short_id(arg[4:])
        if check:
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
    await cq.message.answer("Пакеты для подборщиков: отчёты уровня «Досье» дешевле разовых, "
                            "а безлимит на сутки — для активного дня просмотров." + extra,
                            reply_markup=packs_keyboard(settings))
    await cq.message.answer("Присылайте VIN — по одному или списком (каждый с новой строки).")
    await cq.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("tariffs"))
async def cmd_tariffs(message: Message, settings: Settings) -> None:
    lines = ["<b>Тарифы</b>", "Экспресс — 0 ₽: залог, такси, ЭПТС, архив ДТП"]
    lines += [f"{p.title} — {p.price} ₽: {html.escape(p.description)}" for p in products(settings).values()]
    await message.answer("\n".join(lines), reply_markup=packs_keyboard(settings))


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


async def run_polling(service: CheckService, settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp["service"] = service
    dp["settings"] = settings
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="balance", description="Баланс и пакеты"),
        BotCommand(command="history", description="История проверок"),
        BotCommand(command="tariffs", description="Тарифы"),
        BotCommand(command="help", description="Помощь")])
    log.info("bot started at %s", datetime.now())
    await dp.start_polling(bot)
