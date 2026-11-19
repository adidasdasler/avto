"""Учётные записи сотрудников панели — только из .env, отдельно от аккаунтов сайта.

ADMIN_USERS=логин,роль,хеш_пароля,секрет_2FA;логин2,роль,…
  роль — admin (всё) или support (без восстановления/скачивания бэкапов);
  хеш_пароля — scrypt в виде «соль.хеш» (hex), без символа $ (Docker Compose считает $ переменной);
  секрет_2FA — base32 для приложения-аутентификатора.
Создаётся командой `python -m shturman.admin.cli add <логин>` — пароль в .env не попадает.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

ROLES = {"admin": "Администратор", "support": "Поддержка"}
LOGIN_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
_N, _R, _P = 2 ** 14, 8, 1


@dataclass(frozen=True)
class StaffAccount:
    login: str
    role: str
    password_hash: str  # «соль.хеш»
    totp_secret: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def entry(self) -> str:
        return f"{self.login},{self.role},{self.password_hash},{self.totp_secret}"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Пароль сотрудника — не короче 12 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"{salt.hex()}.{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split(".")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password("x" * 16)  # для одинакового времени ответа на несуществующий логин


def parse(value: str) -> dict[str, StaffAccount]:
    """Разбор ADMIN_USERS; некорректные записи пропускаются (с ошибкой в лог при запуске)."""
    out: dict[str, StaffAccount] = {}
    for raw in (value or "").replace("\n", ";").split(";"):
        parts = [p.strip() for p in raw.strip().split(",")]
        if len(parts) != 4:
            continue
        login, role, pw, secret = parts
        if LOGIN_RE.fullmatch(login) and role in ROLES and "." in pw and re.fullmatch(r"[A-Z2-7]{16,}", secret):
            out[login] = StaffAccount(login, role, pw, secret)
    return out


def check(accounts: dict[str, StaffAccount], login: str, password: str) -> StaffAccount | None:
    acc = accounts.get((login or "").strip().lower())
    if acc is None:
        verify_password(password, _DUMMY)
        return None
    return acc if verify_password(password, acc.password_hash) else None
