"""Тонкий HTTP-клиент MAX Bot API.

Протокол сверён с исходниками официально рекомендованного Python-SDK maxapi (1.2.4):
- базовый адрес https://platform-api2.max.ru, токен в заголовке Authorization без «Bearer»;
- GET /updates?marker=&timeout=&limit= → {"updates": [...], "marker": N} (long polling, до 90 с);
- POST /messages?chat_id=|user_id= с телом {text, format: "html", attachments: [...]};
  инлайн-клавиатура — вложение {"type": "inline_keyboard", "payload": {"buttons": [[...]]}},
  кнопки {"type": "callback", "text", "payload"} и {"type": "link", "text", "url"};
- POST /answers?callback_id= — ответ на нажатие кнопки;
- файл: POST /uploads?type=file → {url, token?}; multipart на url в поле "data" → {"token"};
  затем вложение {"type": "file", "payload": {"token"}}; пока файл обрабатывается, API
  отвечает ошибкой attachment.not.ready — повторяем.
Разработчики отмечают, что реальные ответы иногда расходятся с документацией, поэтому
разбор событий в shturman/bot/max.py сделан терпимым.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any

import httpx

from .common import Btn

log = logging.getLogger(__name__)

TEXT_LIMIT = 4000


class MaxError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None):
        self.code, self.message, self.status = code, message, status
        super().__init__(f"{code}: {message} (HTTP {status})")


def keyboard(rows: list[list[Btn]]) -> dict[str, Any]:
    buttons = [[{"type": "link", "text": b.text[:64], "url": b.url} if b.url
                else {"type": "callback", "text": b.text[:64], "payload": (b.data or "")[:256]}
                for b in row] for row in rows if row]
    return {"type": "inline_keyboard", "payload": {"buttons": buttons}}


class MaxAPI:
    def __init__(self, token: str, base_url: str = "https://platform-api2.max.ru",
                 transport: httpx.AsyncBaseTransport | None = None, proxy: str | None = None):
        if not token:
            raise MaxError("NO_TOKEN", "Не задан MAX_BOT_TOKEN")
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": token},
                                       timeout=httpx.Timeout(120, connect=15), transport=transport, proxy=proxy)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> dict:
        try:
            resp = await self._http.request(method, path, params={k: v for k, v in (params or {}).items()
                                                                   if v is not None}, json=json)
        except httpx.HTTPError as e:
            raise MaxError("NETWORK", str(e)) from e
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {"message": resp.text[:300]}
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            raise MaxError(str(data.get("code") or f"HTTP_{resp.status_code}"), str(data.get("message") or ""),
                           resp.status_code)
        return data if isinstance(data, dict) else {"result": data}

    async def get_me(self) -> dict:
        return await self._req("GET", "/me")

    async def get_updates(self, marker: int | None = None, timeout: int = 30, limit: int = 100) -> dict:
        return await self._req("GET", "/updates", params={"marker": marker, "timeout": timeout, "limit": limit})

    async def send_message(self, *, chat_id: int | None = None, user_id: int | None = None, text: str = "",
                           buttons: list[list[Btn]] | None = None, attachments: list[dict] | None = None,
                           fmt: str = "html") -> dict:
        att = list(attachments or [])
        if buttons:
            att.append(keyboard(buttons))
        body = {"text": text[:TEXT_LIMIT], "format": fmt, "attachments": att}
        params = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}
        for attempt in range(6):
            try:
                return await self._req("POST", "/messages", params=params, json=body)
            except MaxError as e:
                if e.code == "attachment.not.ready" and attempt < 5:
                    await asyncio.sleep(2)
                    continue
                raise
        raise MaxError("attachment.not.ready")  # pragma: no cover

    async def answer_callback(self, callback_id: str, notification: str | None = None) -> None:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        try:
            await self._req("POST", "/answers", params={"callback_id": callback_id}, json=body)
        except MaxError as e:  # подтверждение не критично
            log.debug("answer_callback: %s", e)

    async def upload_file(self, data: bytes, filename: str) -> str:
        info = await self._req("POST", "/uploads", params={"type": "file"})
        url = info.get("url")
        if not url:
            raise MaxError("UPLOAD", "API не вернул url для загрузки")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            resp = await self._http.post(url, files={"data": (filename, data, mime)})
        except httpx.HTTPError as e:
            raise MaxError("NETWORK", str(e)) from e
        if resp.status_code >= 400:
            raise MaxError("UPLOAD", resp.text[:300], resp.status_code)
        try:
            token = (resp.json() or {}).get("token")
        except ValueError:
            token = None
        token = token or info.get("token")
        if not token:
            raise MaxError("UPLOAD", "Сервер загрузки не вернул token")
        return str(token)

    async def send_file(self, *, chat_id: int | None = None, user_id: int | None = None, data: bytes,
                        filename: str, text: str = "", buttons: list[list[Btn]] | None = None) -> dict:
        token = await self.upload_file(data, filename)
        return await self.send_message(chat_id=chat_id, user_id=user_id, text=text, buttons=buttons,
                                       attachments=[{"type": "file", "payload": {"token": token}}])
