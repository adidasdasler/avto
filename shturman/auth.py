"""Вход на сайт через бота — без паролей и почты.

1) Сайт → бот: страница /login создаёт токен и показывает ссылку на бота `?start=login_<токен>`.
   Бот спрашивает «Войти на сайт?» и после подтверждения привязывает токен к пользователю.
   Страница опрашивает /auth/poll и получает сессию. Подтверждение в боте защищает от того,
   что злоумышленник подсунет свою ссылку входа и получит доступ к чужому кабинету.
2) Бот → сайт: команда /cabinet присылает одноразовую ссылку /auth/link?t=<токен>.
3) Почта и пароль: регистрация, вход, подтверждение адреса и сброс пароля по ссылке из письма.
4) Привязка мессенджера к кабинету: ссылка `?start=bind_<токен>` — бот объединяет аккаунты.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select

from .db import Database, LoginToken, User, WebSession, utcnow

TOKEN_TTL = timedelta(minutes=10)
TTL = {"site": TOKEN_TTL, "link": TOKEN_TTL, "bind": TOKEN_TTL,
       "verify": timedelta(days=3), "reset": timedelta(hours=1)}
SESSION_TTL = timedelta(days=30)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
MIN_PASSWORD = 8


class AuthError(ValueError):
    pass


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Проверьте адрес почты.")
    return email


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Пароль — не короче {MIN_PASSWORD} символов.")
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(24)  # 32 символа [A-Za-z0-9_-] — помещается в deep-link Telegram


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    async def _token(self, s, token: str, kind: str) -> LoginToken | None:
        t = await s.get(LoginToken, token)
        if t is None or t.kind != kind or t.used_at is not None or t.created_at < utcnow() - TTL[kind]:
            return None
        return t

    async def new_token(self, kind: str, user_id: int | None) -> str:
        async with self.db.session() as s:
            t = LoginToken(token=new_token(), kind=kind, user_id=user_id)
            s.add(t)
            await s.commit()
            return t.token

    async def consume_token(self, token: str, kind: str) -> int | None:
        """Одноразовый токен (подтверждение почты, сброс пароля, привязка) → id пользователя."""
        async with self.db.session() as s:
            t = await self._token(s, token, kind)
            if t is None or t.user_id is None:
                return None
            t.used_at = utcnow()
            await s.commit()
            return t.user_id

    async def peek_token(self, token: str, kind: str) -> int | None:
        async with self.db.session() as s:
            t = await self._token(s, token, kind)
            return t.user_id if t else None

    async def create_session(self, user_id: int) -> str:
        async with self.db.session() as s:
            sid = await self._new_session(s, user_id)
            await s.commit()
            return sid

    # ---- почта и пароль ----
    async def register(self, email: str, password: str) -> User:
        email = normalize_email(email)
        pw = hash_password(password)
        async with self.db.session() as s:
            if (await s.execute(select(User.id).where(User.email == email))).first():
                raise AuthError("Этот адрес уже зарегистрирован — войдите или восстановите пароль.")
            user = User(email=email, password_hash=pw)
            s.add(user)
            await s.commit()
            return user

    async def set_email_password(self, user_id: int, email: str, password: str) -> User:
        """Добавить вход по почте аккаунту, созданному через мессенджер."""
        email = normalize_email(email)
        pw = hash_password(password)
        async with self.db.session() as s:
            other = (await s.execute(select(User.id).where(User.email == email, User.id != user_id))).first()
            if other:
                raise AuthError("Этот адрес уже привязан к другому аккаунту.")
            user = await s.get(User, user_id)
            if user.email != email:
                user.email_verified_at = None
            user.email, user.password_hash = email, pw
            await s.commit()
            return user

    async def check_password(self, email: str, password: str) -> User | None:
        try:
            email = normalize_email(email)
        except AuthError:
            return None
        async with self.db.session() as s:
            user = (await s.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
        if user is None:
            verify_password(password, "scrypt$16384$8$1$00$00")  # одинаковое время ответа
            return None
        return user if verify_password(password, user.password_hash) else None

    async def set_password(self, user_id: int, password: str) -> None:
        pw = hash_password(password)
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            user.password_hash = pw
            await s.execute(delete(WebSession).where(WebSession.user_id == user_id))  # выйти везде
            await s.commit()

    async def mark_verified(self, user_id: int) -> None:
        async with self.db.session() as s:
            user = await s.get(User, user_id)
            if user and user.email_verified_at is None:
                user.email_verified_at = utcnow()
                await s.commit()

    async def user_by_email(self, email: str) -> User | None:
        try:
            email = normalize_email(email)
        except AuthError:
            return None
        async with self.db.session() as s:
            return (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()

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
            await s.execute(delete(LoginToken).where(LoginToken.created_at < utcnow() - timedelta(days=4)))
            await s.execute(delete(WebSession).where(WebSession.expires_at < utcnow()))
            await s.commit()
