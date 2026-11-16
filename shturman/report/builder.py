"""Сборка отчёта: данные проверки → HTML (Jinja2) → PDF (WeasyPrint)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .. import normalize as n
from ..cascade import TIER_METHODS, TIER_TITLES, TIERS, build_flags
from ..provider.methods import METHODS
from ..rules import mileage_points, mileage_rollback, records_word

TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

# Порядок и способ показа разделов. kind: special-шаблон или generic-список записей.
SECTION_ORDER = [
    ("elpts", "pts"), ("vin", "owners"), ("probeg", "mileage"), ("accidents", "accidents"),
    ("accidents-rt", "accidents"), ("pledge", "pledge"), ("score", "restrictions"),
    ("fines", "fines"), ("taxi", "generic"), ("osago-history", "generic"), ("plate-history", "generic"),
    ("offers", "generic"), ("lizing", "generic"), ("customs", "generic"), ("lawsuits", "generic"),
    ("sharing", "generic"), ("service", "generic"), ("recalls", "generic"), ("price", "price"),
    ("pros-cons", "proscons"), ("nomerogram", "photos"),
]

ICONS = {"ok": "✔", "bad": "✖", "warn": "!", "info": "i", "na": "—"}


def mileage_chart(points: list[tuple[Any, int]], width: int = 720, height: int = 260) -> Markup:
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
    rollback = mileage_rollback(points)
    line = "#d64545" if rollback else "#2e9e5b"
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


def _section(method: str, kind: str, sec: dict[str, Any]) -> dict[str, Any]:
    data = sec.get("data")
    recs = n.records(data)
    out: dict[str, Any] = {"method": method, "kind": kind, "title": METHODS[method].title,
                           "error": sec.get("error"), "count": n.count(data), "records": recs}
    if kind == "generic":
        out["rows"] = [n.flatten(r) for r in recs]
    elif kind == "owners":
        out["title"] = "Периоды владения"
        out["owners"] = [{"from": n.fmt_date(n.pick(r, "from", "dateFrom", "startDate")),
                          "to": n.fmt_date(n.pick(r, "to", "dateTo", "endDate")) if n.pick(r, "to", "dateTo", "endDate") else "по настоящее время",
                          "owner_type": n.pick(r, "ownerType", "personType", default=""),
                          "operation": n.pick(r, "operation", "lastOperation", default="—")} for r in recs]
    elif kind == "mileage":
        pts = mileage_points(data)
        out["title"] = "История пробегов"
        out["chart"] = mileage_chart(pts)
        out["rollback"] = mileage_rollback(pts)
        out["rows"] = [{"date": n.fmt_date(n.pick(r, "date", "checkDate")),
                        "mileage": n.fmt_num(n.pick(r, "mileage", "odometer", "probeg"), "км"),
                        "type": n.pick(r, "type", "source", default="")} for r in recs]
    elif kind == "pts":
        out["title"] = "Информация о ПТС"
        out["has_epts"] = bool(out["count"])
        out["paper"] = n.pick(data, "paperPts", "pts")
        out["rows"] = [n.flatten(r) for r in recs]
    elif kind in ("accidents", "pledge", "restrictions", "fines"):
        out["rows"] = [n.flatten(r) for r in recs]
        if kind == "fines":
            out["unpaid"] = sum(1 for r in recs if n.pick(r, "paid") is False)
            out["total_unpaid"] = sum(n.to_int(n.pick(r, "amount", "sum")) or 0 for r in recs
                                      if n.pick(r, "paid") is False)
        if kind == "pledge":
            out["active"] = int((data or {}).get("active_count") or 0)
    elif kind == "price":
        r = n.first_record(data)
        out["min"] = n.fmt_num(n.pick(r, "min", "priceMin"), "₽")
        out["max"] = n.fmt_num(n.pick(r, "max", "priceMax"), "₽")
        out["avg"] = n.fmt_num(n.pick(r, "avg", "price", "priceAvg"), "₽")
    elif kind == "proscons":
        r = n.first_record(data)
        out["pros"] = n.pick(r, "pros", "plus", default=[]) or []
        out["cons"] = n.pick(r, "cons", "minus", default=[]) or []
    elif kind == "photos":
        out["photos"] = [{"url": n.pick(r, "url", "photo", "image", "src"),
                          "date": n.fmt_date(n.pick(r, "date")), "source": n.pick(r, "source", default="")}
                         for r in recs if n.pick(r, "url", "photo", "image", "src")]
    return out


def build_view(check: Any, brand: str, public_base_url: str, bot_link: str | None = None) -> dict[str, Any]:
    sections_data: dict[str, dict[str, Any]] = check.sections or {}
    tier = check.tier
    flags = [f.to_dict() for f in build_flags(sections_data, tier)]
    sections = []
    for method, kind in SECTION_ORDER:
        sec = sections_data.get(method)
        if not sec or "skipped" in sec or method not in TIER_METHODS[tier]:
            continue
        sections.append(_section(method, kind, sec))
    skipped = [(METHODS[m].title, s["skipped"]) for m, s in sections_data.items()
               if "skipped" in s and m in METHODS]
    locked = []
    if tier != "dossier_plus":
        nxt = TIERS[TIERS.index(tier) + 1:]
        seen = set(TIER_METHODS[tier])
        for t in nxt:
            for m in TIER_METHODS[t]:
                if m not in seen and m not in ("brief", "decode"):
                    seen.add(m)
                    locked.append((METHODS[m].title, TIER_TITLES[t]))
    v = dict(check.vehicle or {})
    created = check.updated_at or check.created_at or datetime.now()
    return {
        "brand": brand, "check": check, "tier_title": TIER_TITLES[tier], "tier": tier,
        "vehicle": v, "flags": flags, "icons": ICONS, "sections": sections, "skipped": skipped,
        "locked": locked, "generated": created.strftime("%d.%m.%Y %H:%M"),
        "query_label": {"vin": "VIN", "plate": "госномеру", "body": "номеру кузова"}.get(check.kind, "запросу"),
        "report_url": f"{public_base_url.rstrip('/')}/r/{check.id}", "bot_link": bot_link,
        "records_word": records_word, "n": n,
    }


def render_html(view: dict[str, Any]) -> str:
    return _env.get_template("report.html").render(**view)


def render_pdf(html: str, base_url: str | None = None) -> bytes:
    from weasyprint import HTML  # тяжёлый импорт — только когда нужен PDF

    return HTML(string=html, base_url=base_url).write_pdf()
