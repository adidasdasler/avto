"""Каскад запросов к провайдеру: какие методы вызывать для каждого тарифа,
гейты для дорогих методов, кэш по VIN и чистка ПДн."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from . import normalize as n
from .db import CacheEntry, Database, ProviderLog, utcnow
from .identifiers import Identifier
from .provider import Provider, ProviderError, ProviderResponse, call_accidents_rt
from .provider.methods import BODY_ALIASES, METHODS
from .redaction import redact
from .rules import Flag, evaluate, overall

log = logging.getLogger(__name__)

TIERS = ("express", "basic", "dossier", "dossier_plus")
TIER_TITLES = {"express": "Экспресс", "basic": "Базовый", "dossier": "Досье", "dossier_plus": "Досье+"}

_EXPRESS = ["decode", "pledge", "taxi", "elpts", "accidents"]
_BASIC = _EXPRESS + ["score", "brief", "probeg", "price", "fines"]
_DOSSIER = _BASIC + ["vin", "osago-history", "offers", "plate-history", "pros-cons", "nomerogram",
                     "recalls", "customs", "lizing", "lawsuits", "sharing"]
_DOSSIER_PLUS = _DOSSIER + ["accidents-rt", "service"]
TIER_METHODS: dict[str, list[str]] = {
    "express": _EXPRESS, "basic": _BASIC, "dossier": _DOSSIER, "dossier_plus": _DOSSIER_PLUS,
}
# Методы, которые запускаются только по условию (зависят от ответов первой волны).
GATED = {"fines", "customs", "lizing", "lawsuits", "sharing", "service"}

ERROR_TEXTS = {
    "NOT_ALLOWED": "метод не подключён к токену",
    "NO_TOKEN": "не настроен доступ к провайдеру",
    "TOKEN_INVALID": "не настроен доступ к провайдеру",
    "NETWORK": "источник временно недоступен",
    "TIMEOUT": "источник не ответил вовремя",
}


def tier_index(tier: str) -> int:
    return TIERS.index(tier)


@dataclass
class CascadeResult:
    vin: str | None
    plate: str | None
    vehicle: dict[str, Any]
    sections: dict[str, dict[str, Any]]
    flags: list[Flag]
    overall: str
    cost: float = 0.0
    errors: list[str] = field(default_factory=list)


def _cache_key(method: str, params: dict[str, str]) -> str:
    raw = method + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return f"{method}:{hashlib.sha1(raw.encode()).hexdigest()}"


def vehicle_summary(sections: dict[str, dict[str, Any]], vin: str | None, plate: str | None) -> dict[str, Any]:
    src: dict[str, Any] = {}
    for m in ("gos2vin", "decode", "brief"):  # brief самый полный — последним
        d = (sections.get(m) or {}).get("data")
        if d:
            src.update({k: v for k, v in n.first_record(d).items() if v not in (None, "")})
    brand = n.pick(src, "brand", "marka", "make", "mark")
    model = n.pick(src, "model")
    return {
        "brand": brand, "model": model,
        "title": " ".join(str(x) for x in (brand, model) if x) or "Автомобиль",
        "year": n.to_int(n.pick(src, "year", "releaseYear", "yearOfManufacture")),
        "color": n.pick(src, "color", "colour"),
        "vin": vin or n.pick(src, "vin"),
        "plate": plate or n.pick(src, "plate", "gosNumber", "regNumber"),
        "body": n.pick(src, "body", "bodyNumber"),
        "chassis": n.pick(src, "chassis", "chassisNumber"),
        "engine_number": n.pick(src, "engineNumber", "engine"),
        "engine_volume": n.pick(src, "engineVolume", "volume"),
        "power_hp": n.pick(src, "powerHp", "power", "hp"),
        "power_kw": n.pick(src, "powerKw", "kw"),
        "vehicle_type": n.pick(src, "vehicleType", "bodyType", "type"),
        "category": n.pick(src, "category"),
        "sts": n.pick(src, "sts", "stsNumber"),
    }


class Cascade:
    def __init__(self, provider: Provider, db: Database, accidents_poll_interval: float = 5.0):
        self.provider = provider
        self.db = db
        self.poll_interval = accidents_poll_interval

    async def fetch(self, method: str, params: dict[str, str], check_id: str | None = None
                    ) -> dict[str, Any]:
        """Возвращает {"data": ...} или {"error": ..., "code": ...}. Данные — уже без ПДн."""
        key = _cache_key(method, params)
        async with self.db.session() as s:
            entry = await s.get(CacheEntry, key)
            if entry and entry.expires_at > utcnow():
                return {"data": entry.payload, "cached": True}

        error: str | None = None
        resp: ProviderResponse | None = None
        try:
            if method == "accidents-rt":
                resp = await call_accidents_rt(self.provider, poll_interval=self.poll_interval, **params)
            else:
                resp = await self.provider.call(method, **params)
        except ProviderError as e:
            error = e.code
            log.warning("provider %s failed: %s", method, e)

        async with self.db.session() as s:
            s.add(ProviderLog(check_id=check_id, method=method, charged=resp.charged if resp else 0,
                              test_mode=resp.test_mode if resp else False, error=error))
            if resp is not None:
                clean = redact({k: v for k, v in resp.data.items() if k != "billing"})
                ttl = METHODS[method].cache_ttl_hours
                await s.merge(CacheEntry(key=key, method=method, payload=clean,
                                         created_at=utcnow(), expires_at=utcnow() + timedelta(hours=ttl)))
            await s.commit()

        if resp is None:
            return {"error": ERROR_TEXTS.get(error or "", "ошибка источника данных"), "code": error,
                    "charged": 0}
        return {"data": clean, "charged": resp.charged}

    # ---- выбор параметра для метода ----
    @staticmethod
    def _params_for(method: str, ident: dict[str, str]) -> tuple[str, dict[str, str]] | None:
        if "vin" not in ident and "body" in ident and method in BODY_ALIASES:
            method = BODY_ALIASES[method]
        accepted = METHODS[method].params
        for p in ("vin", "plate", "body", "sts"):
            if p in ident and p in accepted:
                return method, {p: ident[p]}
        return None

    @staticmethod
    def _gate(method: str, sections: dict[str, dict[str, Any]], vehicle: dict[str, Any],
              vin: str | None) -> str | None:
        """None — запускать; иначе причина пропуска."""
        year = vehicle.get("year")
        age = date.today().year - year if year else None
        data = lambda m: (sections.get(m) or {}).get("data")  # noqa: E731
        if method == "fines":
            return None if vehicle.get("sts") else "нет номера СТС"
        if method == "customs":
            return None if vin and vin[0] not in "XZ" else "автомобиль отечественной сборки"
        if method == "lizing":
            legal_owner = any("юрид" in str(n.pick(r, "ownerType", default="")).lower()
                              for r in n.records(data("vin")))
            return None if legal_owner or (age is not None and age <= 7) else "нет признаков лизинга"
        if method == "lawsuits":
            risky = n.count(data("pledge")) or n.count(data("score"))
            return None if risky else "нет залогов и ограничений"
        if method == "sharing":
            return None if age is None or age <= 6 else "возраст авто больше 6 лет"
        if method == "service":
            return None if age is None or age <= 10 else "возраст авто больше 10 лет"
        return None

    async def _run_wave(self, methods: list[str], ident: dict[str, str],
                        sections: dict[str, dict[str, Any]], check_id: str | None) -> float:
        async def one(m: str) -> tuple[str, dict[str, Any]]:
            target = self._params_for(m, ident)
            if target is None:
                return m, {"skipped": "не поддерживается для этого идентификатора"}
            real, params = target
            return m, await self.fetch(real, params, check_id)

        results = await asyncio.gather(*(one(m) for m in methods))
        cost = 0.0
        for m, res in results:
            cost += float(res.pop("charged", 0) or 0)
            sections[m] = res
        return cost

    async def run(self, ident: Identifier, tier: str, sections: dict[str, dict[str, Any]] | None = None,
                  check_id: str | None = None) -> CascadeResult:
        sections = dict(sections or {})
        cost = 0.0
        vin = ident.value if ident.kind == "vin" else None
        plate = ident.value if ident.kind == "plate" else None

        if ident.kind == "plate":
            if "gos2vin" not in sections or "error" in sections["gos2vin"]:
                sections["gos2vin"] = await self.fetch("gos2vin", {"plate": plate}, check_id)
                cost += float(sections["gos2vin"].pop("charged", 0) or 0)
            g = sections["gos2vin"].get("data")
            vin = n.pick(g, "vin") or n.pick(n.first_record(g), "vin")

        ident_params: dict[str, str] = {}
        if vin:
            ident_params["vin"] = vin
        if plate:
            ident_params["plate"] = plate
        if ident.kind == "body":
            ident_params["body"] = ident.value

        # Повторяем только то, чего ещё нет или что упало с ошибкой.
        todo = [m for m in TIER_METHODS[tier] if m not in sections or "error" in sections[m]]
        cost += await self._run_wave([m for m in todo if m not in GATED], ident_params, sections, check_id)

        vehicle = vehicle_summary(sections, vin, plate)
        if vehicle.get("sts"):
            # Штрафы ищутся по СТС, его отдаёт /brief.
            ident_params = {**ident_params, "sts": str(vehicle["sts"])}
        gated_run = []
        for m in (m for m in todo if m in GATED):
            reason = self._gate(m, sections, vehicle, vin)
            if reason:
                sections[m] = {"skipped": reason}
            else:
                gated_run.append(m)
        cost += await self._run_wave(gated_run, ident_params, sections, check_id)

        flags = build_flags(sections, tier)
        return CascadeResult(vin=vin, plate=plate or vehicle.get("plate"), vehicle=vehicle,
                             sections=sections, flags=flags, overall=overall(flags), cost=cost)


def build_flags(sections: dict[str, dict[str, Any]], tier: str) -> list[Flag]:
    flags = []
    for m in TIER_METHODS[tier]:
        if m in ("decode", "brief"):
            continue
        sec = sections.get(m)
        if not sec or "skipped" in sec:
            continue
        flags.append(evaluate(m, sec.get("data"), sec.get("error")))
    return flags
