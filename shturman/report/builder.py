"""Сборка отчёта: данные проверки → HTML (Jinja2) → PDF (WeasyPrint)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .. import normalize as n
from ..cascade import TIER_METHODS, TIER_TITLES, TIERS
from ..provider.methods import METHODS
from ..rules import TITLES, build_sections, traffic_light

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))



def mileage_chart(points: list[tuple[Any, int]], color: str = "#2e9e5b", width: int = 720,
                  height: int = 260) -> Markup:
    if len(points) < 2:
        return Markup("")
    pad_l, pad_r, pad_t, pad_b = 80, 40, 16, 40
    xs = [p[0].toordinal() for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y1 = y0 + 1
    sx = lambda x: pad_l + (x - x0) / max(x1 - x0, 1) * (width - pad_l - pad_r)  # noqa: E731
    sy = lambda y: pad_t + (1 - (y - y0) / (y1 - y0)) * (height - pad_t - pad_b)  # noqa: E731
    coords = [(sx(x), sy(y)) for x, y in zip(xs, ys)]
    line = color
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" class="chart">']
    for i in range(5):
        y = pad_t + i * (height - pad_t - pad_b) / 4
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e3e3e3"/>')
    parts.append('<polyline fill="none" stroke="%s" stroke-width="3" points="%s"/>'
                 % (line, " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)))
    for (x, y), (d, m) in zip(coords, points):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{line}"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11">'
                     f'{n.fmt_num(m, "км")}</text>')
        parts.append(f'<text x="{x:.1f}" y="{height - 14}" text-anchor="middle" font-size="10">'
                     f'{d.strftime("%d.%m.%Y")}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


TECH_FIELDS = [
    ("Марка и модель", "title"), ("Год выпуска", "year"), ("Цвет", "color"), ("Госномер", "plate"),
    ("VIN", "vin"), ("Номер кузова", "body"), ("Номер двигателя", "engine_number"), ("Объём двигателя", "engine_volume"),
    ("Мощность", "power"), ("Топливо", "fuel"), ("Коробка передач", "transmission"), ("Привод", "drive"),
    ("Тип кузова", "vehicle_type"), ("Категория", "category"), ("Сборка", "assembly"), ("Модификация", "modification"),
]


def tech_rows(v: dict[str, Any]) -> list[tuple[str, str]]:
    v = dict(v)
    if v.get("engine_volume"):
        v["engine_volume"] = n.fmt_num(v["engine_volume"], "см³")
    if v.get("power_hp"):
        v["power"] = f"{v['power_hp']} л. с." + (f" / {v['power_kw']} кВт" if v.get("power_kw") else "")
    if v.get("title") == "Автомобиль":  # модель не распознана — не показываем заглушку
        v["title"] = None
    return [(label, str(v[key])) for label, key in TECH_FIELDS if v.get(key)]


def build_view(check: Any, brand: str, public_base_url: str, bot_link: str | None = None) -> dict[str, Any]:
    sections_data: dict[str, dict[str, Any]] = check.sections or {}
    tier = check.tier
    sections = build_sections(sections_data)
    for s in sections:
        if s.kind == "mileage":
            color = {"bad": "#d64545", "warn": "#d08a00"}.get(s.status, "#2e9e5b")
            s.extra["chart"] = mileage_chart(s.extra.get("points") or [], color)
    flags = [s.flag() for s in sections]
    light = traffic_light(flags, sections_data)
    skipped = [(TITLES.get(m, METHODS[m].title), s["skipped"]) for m, s in sections_data.items()
               if "skipped" in s and m in METHODS]
    locked = []
    if tier != "dossier_plus":
        seen = set(TIER_METHODS[tier])
        for t in TIERS[TIERS.index(tier) + 1:]:
            for m in TIER_METHODS[t]:
                if m not in seen and m not in HIDDEN_METHODS:
                    seen.add(m)
                    locked.append((TITLES.get(m, METHODS[m].title), TIER_TITLES[t]))
    v = dict(check.vehicle or {})
    created = check.updated_at or check.created_at or datetime.now()
    return {
        "brand": brand, "check": check, "tier_title": TIER_TITLES[tier], "tier": tier,
        "vehicle": v, "tech": tech_rows(v), "light": light, "sections": sections, "skipped": skipped,
        "locked": locked, "generated": created.strftime("%d.%m.%Y %H:%M"),
        "query_label": {"vin": "VIN", "plate": "госномеру", "body": "номеру кузова"}.get(check.kind, "запросу"),
        "report_url": f"{public_base_url.rstrip('/')}/r/{check.id}", "bot_link": bot_link,
    }


# Методы, которые не дают своего раздела (идут в техданные или в «Периоды владения»/«Пробег»).
HIDDEN_METHODS = {"decode", "brief", "vin2gos", "gos2vin", "autopark"}


def render_html(view: dict[str, Any]) -> str:
    return _env.get_template("report.html").render(**view)


def render_pdf(html: str, base_url: str | None = None) -> bytes:
    from weasyprint import HTML  # тяжёлый импорт — только когда нужен PDF

    return HTML(string=html, base_url=base_url).write_pdf()
