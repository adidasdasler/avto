"""Одноразовые коды TOTP (RFC 6238) для двухфакторного входа в панель.

Совместимо с Яндекс Ключом, Google Authenticator, 1Password, Bitwarden и др.:
SHA-1, 6 цифр, шаг 30 секунд. Принимаем код текущего и соседних шагов (±30 с на расхождение часов),
но каждый шаг — только один раз (защита от повторного использования перехваченного кода).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlencode

STEP = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, last_counter: int | None = None, now: float | None = None,
           window: int = 1) -> int | None:
    """Номер шага, на котором код верен (его нужно сохранить как last_counter), иначе None."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = int((time.time() if now is None else now) // STEP)
    for counter in range(current - window, current + window + 1):
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                                                    "digits": DIGITS, "period": STEP})


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return img.to_string(encoding="unicode")


# ---- резервные коды: показываются один раз, хранятся только хеши ----
def new_recovery_codes(n: int = 8) -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)) for _ in range(n)]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().upper().replace(" ", "").encode()).hexdigest()


def use_recovery(code: str, hashes: list[str] | None) -> list[str] | None:
    """Новый список хешей без использованного кода, если код верный; иначе None."""
    h = hash_recovery(code)
    hashes = list(hashes or [])
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            hashes.remove(stored)
            return hashes
    return None
