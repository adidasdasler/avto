"""RedactionLayer: вычищает персональные данные третьих лиц из ответов провайдера
до того, как они попадут в кэш, базу или отчёт (152-ФЗ).

Две линии защиты:
1. Удаляем поля, имя которых указывает на ПДн (ФИО, дата рождения, паспорт,
   телефон, e-mail, залогодатель, должник и т.п.).
2. Маскируем ПДн в значениях строк: ФИО с отчеством, «Фамилия И.О.», телефоны,
   e-mail, СНИЛС.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "[скрыто]"

# Токены в имени поля, которые означают ПДн физлица.
_PII_TOKENS = {
    "fio", "lastname", "firstname", "middlename", "surname", "patronymic",
    "birth", "birthday", "birthdate", "dob", "passport", "snils",
    "phone", "phones", "tel", "email", "mail",
    "pledgor", "pledgors", "debtor", "debtors", "driver", "drivers", "insurant", "insurer_person",
    "fullname", "person", "persons", "owneraddress", "address_person",
}
# Сочетания, которые ПДн только вместе (ownerName, ownerInn, ownerAddress...).
_PII_PAIRS = {("owner", "name"), ("owner", "inn"), ("owner", "address"), ("seller", "name"),
              ("holder", "name"), ("client", "name")}

_FIO_FULL = re.compile(
    r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|чна|оглы|кызы)\b"
)
_FIO_INITIALS = re.compile(r"\b[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")
_PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}[\s-]\d{2}\b")


def _tokens(key: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return [t for t in re.split(r"[^a-zA-Z0-9]+", s.lower()) if t]


def is_pii_key(key: str) -> bool:
    toks = _tokens(key)
    if not toks:
        return False
    joined = "".join(toks)
    if joined in _PII_TOKENS or any(t in _PII_TOKENS for t in toks):
        return True
    ts = set(toks)
    return any(a in ts and b in ts for a, b in _PII_PAIRS)


def redact_text(text: str) -> str:
    for rx in (_FIO_FULL, _FIO_INITIALS, _PHONE, _EMAIL, _SNILS):
        text = rx.sub(MASK, text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if not is_pii_key(str(k))}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
