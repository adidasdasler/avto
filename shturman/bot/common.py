"""Общее для Telegram- и MAX-бота: тексты, кнопки (в нейтральном виде), файл отчёта."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import timezone

from ..billing import can_use_credit, has_unlimited, products
from ..cascade import TIER_TITLES
from ..config import Settings
from ..db import Check, User
from ..rules import traffic_light

log = logging.getLogger(__name__)

ICONS = {"ok": "✅", "bad": "❌", "warn": "⚠️", "info": "ℹ️", "na": "➖"}
LAMP = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
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
    light = traffic_light(check.flags or [])
    lines = [f"<b>{head}</b>", ids, "",
             f"{LAMP[light['color']]} <b>{html.escape(light['verdict'])}</b> · {light['score']}/100", ""]
    for title, key in (("Критично", "red"), ("Внимание", "yellow"), ("В порядке", "green"), ("Нет данных", "na")):
        if light[key]:
            lines.append(f"<b>{title}:</b>")
            lines += [f"{ICONS[f['status']]} {html.escape(f['title'])}: {html.escape(f['summary'])}" for f in light[key]]
    lines.append(f"\nТариф: «{TIER_TITLES[check.tier]}»")
    return "\n".join(lines)


def balance_text(user: User, free_left: int) -> str:
    parts = [f"Отчётов «Досье» в пакете: <b>{user.credits}</b>"]
    if has_unlimited(user):
        until = user.unlimited_until.replace(tzinfo=timezone.utc).astimezone().strftime("%d.%m %H:%M")
        parts.append(f"Безлимит активен до <b>{until}</b>")
    parts.append(f"Бесплатных экспресс-проверок сегодня: <b>{free_left}</b>")
    return "\n".join(parts)


@dataclass
class Btn:
    """Кнопка без привязки к мессенджеру: callback (data) или ссылка (url)."""
    text: str
    data: str | None = None
    url: str | None = None


def offer_buttons(check: Check, user: User, settings: Settings) -> list[list[Btn]]:
    rows: list[list[Btn]] = []
    sid = short(check.id)
    if check.tier in ("express", "basic") and can_use_credit(user, "dossier"):
        label = "безлимит" if has_unlimited(user) else f"осталось {user.credits}"
        rows.append([Btn(f"📄 «Досье» из пакета ({label})", data=f"credit:{sid}")])
    order = ["basic", "dossier", "dossier_plus"]
    for code in order[order.index(check.tier) + 1:] if check.tier in order else order:
        p = products(settings)[code]
        rows.append([Btn(f"{p.title} — {p.price} ₽", data=f"buy:{code}:{sid}")])
    if settings.public_base_url.startswith("https://"):
        rows.append([Btn("🌐 Открыть отчёт", url=f"{settings.public_base_url.rstrip('/')}/r/{check.id}")])
    return rows


def packs_buttons(settings: Settings) -> list[list[Btn]]:
    ps = products(settings)
    return [[Btn(f"{ps[c].title} — {ps[c].price} ₽", data=f"buy:{c}:-")]
            for c in ("pack_10", "pack_25", "pack_50", "unlimited_day")]


def tariffs_text(settings: Settings) -> str:
    lines = ["<b>Тарифы</b>", "Экспресс — 0 ₽: залог, такси, ЭПТС, архив ДТП"]
    lines += [f"{p.title} — {p.price} ₽: {html.escape(p.description)}" for p in products(settings).values()]
    return "\n".join(lines)


async def report_file(service, check: Check) -> tuple[str, bytes]:
    """PDF отчёта, а без системных библиотек WeasyPrint (Windows без GTK) — HTML."""
    base = f"{(check.vehicle or {}).get('title', 'auto')}-{check.vin or short(check.id)[:8]}-{TIER_TITLES[check.tier]}"
    base = base.replace(" ", "_")
    try:
        return f"{base}.pdf", await service.pdf(check)
    except (OSError, ImportError) as e:
        log.warning("PDF не собран (%s), отправляю HTML", e.__class__.__name__)
        return f"{base}.html", service.html(check).encode("utf-8")
