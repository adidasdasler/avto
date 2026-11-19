"""Каталог специалистов: подборщики, выездная диагностика, перекупы.

Анкеты проходят модерацию (pending → approved/rejected). Для будущей монетизации
у анкеты есть тариф (plan) и поднятие в выдаче (featured_until), а по дням копится
статистика просмотров и кликов «Показать контакты» (ProStat).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, func, select

from .db import Database, ProProfile, ProStat, utcnow

KIND_GROUPS = {
    "Покупка и продажа": {
        "selector": "Автоподборщик",
        "inspector": "Выездная диагностика",
        "dealer": "Продажа авто (перекуп)",
        "appraiser": "Оценка автомобиля",
        "registration": "Оформление и постановка на учёт",
        "lawyer": "Автоюрист",
        "insurance": "Автострахование",
        "import": "Пригон авто из-за рубежа",
        "driver": "Перегон автомобилей",
    },
    "Помощь на дороге": {
        "tow": "Эвакуатор",
        "unlock": "Аварийное вскрытие авто",
        "keys": "Изготовление автоключей",
        "mobile_tires": "Выездной шиномонтаж",
    },
    "Ремонт и уход": {
        "service": "Автосервис",
        "electrician": "Автоэлектрик",
        "bodywork": "Кузовной ремонт и покраска",
        "detailing": "Детейлинг и предпродажная подготовка",
    },
}
KINDS = {k: t for group in KIND_GROUPS.values() for k, t in group.items()}
# доступ к чужой машине: публикуем, только если мастер проверяет документы владельца
SENSITIVE_KINDS = {"unlock", "keys"}

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya"})


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(_TRANSLIT)).strip("-")
    return s[:48] or "pro"


def normalize_city(city: str) -> str:
    """«москва» → «Москва», «санкт-петербург» → «Санкт-Петербург»: одинаковое написание для фильтра."""
    words = city.strip().split()
    return " ".join("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")) for w in words)[:60]


class ValidationError(ValueError):
    pass


@dataclass
class ProForm:
    kind: str
    name: str
    city: str
    about: str = ""
    services: str = ""  # по строке на услугу: «Название — цена»
    phone: str = ""
    telegram: str = ""
    max_link: str = ""
    website: str = ""
    experience_years: str = ""
    consent: bool = False

    def clean(self) -> dict[str, Any]:
        errors = []
        kind = self.kind if self.kind in KINDS else None
        if not kind:
            errors.append("Выберите вид услуг.")
        name = self.name.strip()[:80]
        if len(name) < 2:
            errors.append("Укажите имя или название.")
        city = normalize_city(self.city)
        if len(city) < 2:
            errors.append("Укажите город.")
        services = []
        for line in self.services.splitlines():
            line = line.strip(" -•\t")
            if not line:
                continue
            parts = re.split(r"\s+[—–-]\s+", line, maxsplit=1)
            title, price = parts[0], parts[1] if len(parts) > 1 else ""
            services.append({"title": title.strip()[:100], "price": price.strip()[:40]})
        phone = re.sub(r"[^\d+]", "", self.phone)[:16]
        if phone and not re.fullmatch(r"\+?\d{10,15}", phone):
            errors.append("Телефон в формате +79991234567.")
        tg = self.telegram.strip().lstrip("@").removeprefix("https://t.me/")[:64]
        if tg and not re.fullmatch(r"[A-Za-z0-9_]{4,64}", tg):
            errors.append("Telegram — имя пользователя, например @ivan_podbor.")
        links = {}
        for field in ("max_link", "website"):
            v = getattr(self, field).strip()[:200]
            if v and not re.match(r"^https?://", v):
                v = "https://" + v
            links[field] = v or None
        exp = int(self.experience_years) if self.experience_years.strip().isdigit() else None
        if not (phone or tg or links["max_link"] or links["website"]):
            errors.append("Укажите хотя бы один контакт.")
        if not self.consent:
            errors.append("Нужно согласие на публикацию анкеты и контактов.")
        if errors:
            raise ValidationError(" ".join(errors))
        search = " ".join([name, self.about, *[f"{x['title']} {x['price']}" for x in services]]).lower()
        return {"kind": kind, "name": name, "city": city, "about": self.about.strip()[:2000], "search_text": search,
                "services": services[:20], "phone": phone or None, "telegram": tg or None,
                "experience_years": exp if exp is None or 0 <= exp <= 60 else None, **links}


class ProService:
    def __init__(self, db: Database):
        self.db = db

    async def by_user(self, user_id: int) -> ProProfile | None:
        async with self.db.session() as s:
            return (await s.execute(select(ProProfile).where(ProProfile.user_id == user_id))).scalar_one_or_none()

    async def by_slug(self, slug: str) -> ProProfile | None:
        async with self.db.session() as s:
            return (await s.execute(select(ProProfile).where(ProProfile.slug == slug))).scalar_one_or_none()

    async def get(self, profile_id: int) -> ProProfile | None:
        async with self.db.session() as s:
            return await s.get(ProProfile, profile_id)

    async def save(self, user_id: int, form: ProForm) -> ProProfile:
        """Создаёт или обновляет анкету; любое изменение отправляет её на модерацию."""
        data = form.clean()
        async with self.db.session() as s:
            p = (await s.execute(select(ProProfile).where(ProProfile.user_id == user_id))).scalar_one_or_none()
            if p is None:
                base = slugify(f"{data['name']}-{data['city']}")
                slug, i = base, 1
                while (await s.execute(select(ProProfile.id).where(ProProfile.slug == slug))).first():
                    i += 1
                    slug = f"{base}-{i}"
                p = ProProfile(user_id=user_id, slug=slug, **data)
                s.add(p)
            else:
                for k, v in data.items():
                    setattr(p, k, v)
            p.status, p.reject_reason, p.consent_at = "pending", None, utcnow()
            await s.commit()
            return p

    async def set_hidden(self, user_id: int, hidden: bool) -> None:
        async with self.db.session() as s:
            p = (await s.execute(select(ProProfile).where(ProProfile.user_id == user_id))).scalar_one_or_none()
            if p and p.status in ("approved", "hidden"):
                p.status = "hidden" if hidden else "approved"
                await s.commit()

    async def moderate(self, profile_id: int, approve: bool, reason: str | None = None) -> ProProfile | None:
        async with self.db.session() as s:
            p = await s.get(ProProfile, profile_id)
            if p is None:
                return None
            p.status = "approved" if approve else "rejected"
            p.reject_reason = None if approve else (reason or "Анкета не прошла модерацию")[:300]
            if approve and p.published_at is None:
                p.published_at = utcnow()
            await s.commit()
            return p

    async def pending(self) -> list[ProProfile]:
        async with self.db.session() as s:
            rows = await s.execute(select(ProProfile).where(ProProfile.status == "pending").order_by(ProProfile.updated_at))
            return list(rows.scalars())

    async def catalog(self, city: str | None = None, kind: str | None = None, q: str | None = None) -> list[ProProfile]:
        now = utcnow()
        stmt = select(ProProfile).where(ProProfile.status == "approved")
        if city:
            stmt = stmt.where(ProProfile.city == normalize_city(city))
        if kind in KINDS:
            stmt = stmt.where(ProProfile.kind == kind)
        if q:
            like = "%" + q.strip().lower()[:50].replace("%", "").replace("_", "") + "%"
            stmt = stmt.where(ProProfile.search_text.like(like))
        featured = case((ProProfile.featured_until > now, 0), else_=1)
        pro = case((ProProfile.plan == "pro", 0), else_=1)
        stmt = stmt.order_by(featured, pro, ProProfile.published_at.desc()).limit(200)
        async with self.db.session() as s:
            return list((await s.execute(stmt)).scalars())

    async def cities(self) -> list[str]:
        async with self.db.session() as s:
            rows = await s.execute(select(ProProfile.city, func.count()).where(ProProfile.status == "approved")
                                   .group_by(ProProfile.city).order_by(func.count().desc(), ProProfile.city))
            return [r[0] for r in rows]

    async def track(self, profile_id: int, field: str) -> None:
        day = date.today().isoformat()
        async with self.db.session() as s:
            st = (await s.execute(select(ProStat).where(ProStat.profile_id == profile_id, ProStat.day == day))
                  ).scalar_one_or_none()
            if st is None:
                st = ProStat(profile_id=profile_id, day=day, views=0, contacts=0)
                s.add(st)
            setattr(st, field, getattr(st, field) + 1)
            await s.commit()

    async def stats(self, profile_id: int, days: int = 30) -> dict[str, int]:
        async with self.db.session() as s:
            rows = (await s.execute(select(func.sum(ProStat.views), func.sum(ProStat.contacts))
                                    .where(ProStat.profile_id == profile_id)
                                    .where(ProStat.day >= (date.today() - timedelta(days=days)).isoformat())
                                    )).one()
        return {"views": int(rows[0] or 0), "contacts": int(rows[1] or 0)}
