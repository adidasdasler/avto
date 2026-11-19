"""Отправка писем по SMTP (подтверждение почты, сброс пароля).

Подойдёт любой SMTP: почта для домена в Яндекс 360 или Mail.ru для бизнеса, Unisender, SendPulse.
Если SMTP не настроен, письма не отправляются, а ссылка пишется в лог — удобно для разработки.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Settings

log = logging.getLogger(__name__)


def configured(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _send_sync(settings: Settings, to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.brand_name, settings.smtp_from))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=settings.smtp_from.split("@")[-1])
    msg.set_content(text)
    ctx = ssl.create_default_context()
    if settings.smtp_ssl:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=20) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            s.starttls(context=ctx)
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


async def send(settings: Settings, to: str, subject: str, text: str) -> bool:
    if not configured(settings):
        log.warning("SMTP не настроен — письмо «%s» для %s не отправлено:\n%s", subject, to, text)
        return False
    try:
        await asyncio.to_thread(_send_sync, settings, to, subject, text)
        return True
    except Exception:
        log.exception("Не удалось отправить письмо на %s", to)
        return False
