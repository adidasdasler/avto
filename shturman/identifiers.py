"""Распознавание того, что ввёл пользователь: VIN, госномер, номер кузова или ссылка."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

Kind = Literal["vin", "plate", "body"]

# Буквы, допустимые в российских госномерах, и их латинские двойники.
_LAT2CYR = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")
_PLATE_LETTERS = "АВЕКМНОРСТУХ"

VIN_RE = re.compile(r"(?<![A-Z0-9])([A-HJ-NPR-Z0-9]{17})(?![A-Z0-9])")
# Х000ХХ00 / Х000ХХ000 (обычный) и ХХ00000 (такси, прицепы)
PLATE_RE = re.compile(
    rf"(?<![{_PLATE_LETTERS}0-9])"
    rf"([{_PLATE_LETTERS}]\d{{3}}[{_PLATE_LETTERS}]{{2}}\d{{2,3}}|[{_PLATE_LETTERS}]{{2}}\d{{5,6}})"
    rf"(?![{_PLATE_LETTERS}0-9])"
)
BODY_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{4,24}$")
URL_RE = re.compile(r"^https?://", re.I)


class IdentifierError(ValueError):
    pass


@dataclass(frozen=True)
class Identifier:
    kind: Kind
    value: str
    source: str  # что ввёл пользователь

    @property
    def param(self) -> dict[str, str]:
        return {self.kind: self.value}


def normalize_plate(text: str) -> str:
    return re.sub(r"[\s-]", "", text.upper()).translate(_LAT2CYR)


def _find_vin(text: str) -> str | None:
    m = VIN_RE.search(text.upper())
    return m.group(1) if m else None


def _find_plate(text: str) -> str | None:
    m = PLATE_RE.search(normalize_plate(text))
    return m.group(1) if m else None


def parse(text: str) -> Identifier:
    raw = (text or "").strip()
    if not raw:
        raise IdentifierError("Пустой запрос. Пришлите VIN, госномер или ссылку на объявление.")

    if URL_RE.match(raw):
        decoded = unquote(raw)
        if vin := _find_vin(decoded):
            return Identifier("vin", vin, raw)
        if plate := _find_plate(decoded.split("://", 1)[1]):
            return Identifier("plate", plate, raw)
        raise IdentifierError(
            "В ссылке не нашлось VIN или госномера. Скопируйте VIN из объявления и пришлите его."
        )

    compact = re.sub(r"\s", "", raw.upper())
    if VIN_RE.fullmatch(compact):
        return Identifier("vin", compact, raw)
    if len(compact) == 17 and re.fullmatch(r"[A-Z0-9]{17}", compact):
        raise IdentifierError("VIN не может содержать буквы I, O и Q. Проверьте ввод.")

    plate = normalize_plate(raw)
    if PLATE_RE.fullmatch(plate):
        return Identifier("plate", plate, raw)

    if BODY_RE.fullmatch(compact) and re.search(r"\d", compact):
        return Identifier("body", compact, raw)

    raise IdentifierError(
        "Не удалось распознать запрос. Пришлите VIN (17 символов), госномер (например, А123ВС77) "
        "или ссылку на объявление."
    )


def parse_many(text: str) -> list[Identifier | IdentifierError]:
    """Пачка запросов от подборщика: по одному на строку (или через запятую/;)."""
    parts = [p.strip() for p in re.split(r"[\n,;]+", text or "") if p.strip()]
    out: list[Identifier | IdentifierError] = []
    for p in parts:
        try:
            out.append(parse(p))
        except IdentifierError as e:
            out.append(e)
    return out
