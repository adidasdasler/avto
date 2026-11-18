"""Вход на сайт через бота — без паролей и почты.

1) Сайт → бот: страница /login создаёт токен и показывает ссылку на бота `?start=login_<токен>`.
   Бот спрашивает «Войти на сайт?» и после подтверждения привязывает токен к пользователю.
   Страница опрашивает /auth/poll и получает сессию. Подтверждение в боте защищает от того,
   что злоумышленник подсунет свою ссылку входа и получит доступ к чужому кабинету.
2) Бот → сайт: команда /cabinet присылает одноразовую ссылку /auth/link?t=<токен>.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import delete

from .db import Database, LoginToken, User, WebSession, utcnow

TOKEN_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=30)


def new_token() -> str:
    return secrets.token_urlsafe(24)  # 32 символа [A-Za-z0-9_-] — помещается в deep-link Telegram


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    async def _token(self, s, token: str, kind: str) -> LoginToken | None:
        t = await s.get(LoginToken, token)
        if t is None or t.kind != kind or t.used_at is not None or t.created_at < utcnow() - TOKEN_TTL:
            return None
        return t

    async def new_site_token(self) -> str:
        async with self.db.session() as s:
            t = LoginToken(token=new_token(), kind="site")
            s.add(t)
            await s.commit()
            return t.token

    async def bind_site_token(self, token: str, user_id: int) -> bool:
        async with self.db.session() as s:
            t = await self._token(s, token, "site")
            if t is None or t.user_id is not None:
                return False
            t.user_id = user_id
            await s.commit()
            return True

    async def poll_site_token(self, token: str) -> str | None:
        """Сессия, если вход подтверждён в боте; токен одноразовый."""
        async with self.db.session() as s:
            t = await self._token(s, token, "site")
            if t is None or t.user_id is None:
                return None
            t.used_at = utcnow()
            sid = await self._new_session(s, t.user_id)
            await s.commit()
            return sid

    async def new_link_token(self, user_id: int) -> str:
        async with self.db.session() as s:
            t = LoginToken(token=new_token(), kind="link", user_id=user_id)
            s.add(t)
            await s.commit()
            return t.token

    async def consume_link_token(self, token: str) -> str | None:
        async with self.db.session() as s:
            t = await self._token(s, token, "link")
            if t is None:
                return None
            t.used_at = utcnow()
            sid = await self._new_session(s, t.user_id)
            await s.commit()
            return sid

    @staticmethod
    async def _new_session(s, user_id: int) -> str:
        sess = WebSession(id=secrets.token_urlsafe(32), user_id=user_id, expires_at=utcnow() + SESSION_TTL)
        s.add(sess)
        return sess.id

    async def session_user(self, sid: str | None) -> User | None:
        if not sid:
            return None
        async with self.db.session() as s:
            sess = await s.get(WebSession, sid)
            if sess is None or sess.expires_at < utcnow():
                return None
            return await s.get(User, sess.user_id)

    async def logout(self, sid: str | None) -> None:
        if not sid:
            return
        async with self.db.session() as s:
            await s.execute(delete(WebSession).where(WebSession.id == sid))
            await s.commit()

    async def cleanup(self) -> None:
        async with self.db.session() as s:
            await s.execute(delete(LoginToken).where(LoginToken.created_at < utcnow() - timedelta(days=1)))
            await s.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))
            await s.commit()
