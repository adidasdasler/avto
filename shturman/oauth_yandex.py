"""Вход через Яндекс ID (OAuth 2.0, authorization code).

1) /auth/yandex → https://oauth.yandex.ru/authorize?response_type=code&client_id=…&redirect_uri=…&state=…
2) Яндекс возвращает на /auth/yandex/callback?code=…&state=…; state сверяется с cookie (защита от CSRF-входа).
3) code меняется на access_token (POST https://oauth.yandex.ru/token),
   профиль — GET https://login.yandex.ru/info?format=json с заголовком «Authorization: OAuth <token>».
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .config import Settings
from .net import ssl_context

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
INFO_URL = "https://login.yandex.ru/info"


class YandexError(Exception):
    pass


@dataclass
class YandexProfile:
    id: str
    email: str | None
    login: str | None
    name: str | None


def configured(settings: Settings) -> bool:
    return bool(settings.yandex_client_id and settings.yandex_client_secret)


def redirect_uri(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/") + "/auth/yandex/callback"


def authorize_url(settings: Settings, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": settings.yandex_client_id,
        "redirect_uri": redirect_uri(settings), "state": state})


async def fetch_profile(settings: Settings, code: str, transport: httpx.AsyncBaseTransport | None = None
                        ) -> YandexProfile:
    async with httpx.AsyncClient(timeout=15, verify=ssl_context(), transport=transport) as http:
        try:
            tok = await http.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                                   "client_id": settings.yandex_client_id,
                                                   "client_secret": settings.yandex_client_secret})
            token = tok.json().get("access_token") if tok.status_code == 200 else None
            if not token:
                raise YandexError(f"Яндекс не выдал токен: HTTP {tok.status_code}")
            info = await http.get(INFO_URL, params={"format": "json"}, headers={"Authorization": f"OAuth {token}"})
        except httpx.HTTPError as e:
            raise YandexError(str(e)) from e
    if info.status_code != 200:
        raise YandexError(f"Яндекс не отдал профиль: HTTP {info.status_code}")
    d = info.json()
    if not d.get("id"):
        raise YandexError("В профиле Яндекса нет id")
    email = d.get("default_email") or next(iter(d.get("emails") or []), None)
    return YandexProfile(id=str(d["id"]), email=(email or "").strip().lower() or None, login=d.get("login"),
                         name=d.get("real_name") or d.get("display_name"))
