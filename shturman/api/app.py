"""HTTP API и лендинг (FastAPI)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from ..billing import products
from ..cascade import TIER_TITLES
from ..config import Settings, get_settings
from ..db import Database
from ..identifiers import IdentifierError
from ..provider import Provider, make_provider
from ..provider.methods import METHODS
from ..rules import traffic_light
from ..orders import OrderService, make_robokassa
from ..services import CheckService, LimitExceeded

LANDING_DIR = Path(__file__).resolve().parents[2] / "landing"


class CheckIn(BaseModel):
    query: str = Field(min_length=3, max_length=512)


def create_app(settings: Settings | None = None, provider: Provider | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        await db.create_all()
        prov = provider or make_provider(settings)
        app.state.service = CheckService(db, prov, settings)
        app.state.orders = OrderService(db, settings, make_robokassa(settings))
        yield
        await prov.aclose()
        await db.dispose()

    app = FastAPI(title=settings.brand_name, lifespan=lifespan)
    landing_env = Environment(loader=FileSystemLoader(LANDING_DIR), autoescape=select_autoescape(["html"]))

    def svc(request: Request) -> CheckService:
        return request.app.state.service

    def client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request) -> str:
        s = svc(request)
        return landing_env.get_template("index.html").render(
            brand=settings.brand_name, products=products(settings), bot_link=s.bot_link(), messenger=s.messenger,
            max_link=settings.max_bot_url or None, free_per_day=settings.free_checks_per_day,
            methods_count=len(METHODS))

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "provider": settings.provider_mode}

    @app.get("/api/tariffs")
    async def tariffs() -> list[dict]:
        return [{"code": p.code, "title": p.title, "price": p.price, "description": p.description}
                for p in products(settings).values()]

    @app.post("/api/check")
    async def check(body: CheckIn, request: Request) -> dict:
        s = svc(request)
        try:
            c = await s.express(body.query, client_ip=client_ip(request), source="web")
        except IdentifierError as e:
            raise HTTPException(422, str(e))
        except LimitExceeded as e:
            raise HTTPException(429, str(e))
        return {
            "id": c.id, "vehicle": c.vehicle, "overall": c.overall, "flags": c.flags,
            "light": traffic_light(c.flags or []),
            "report_url": f"/r/{c.id}", "buy_url": s.bot_link(c.id), "messenger": s.messenger,
            "free_left": await s.free_left(client_ip=client_ip(request)),
            "offers": [{"code": p.code, "title": p.title, "price": p.price, "description": p.description}
                       for p in products(settings).values() if p.tier],
        }

    @app.get("/example", response_class=HTMLResponse)
    async def example(request: Request) -> str:
        s = svc(request)
        return s.html(await s.example())

    # ---- Robokassa ----
    async def _rk_params(request: Request) -> dict[str, str]:
        params = dict(request.query_params)
        if request.method == "POST":
            body = (await request.body()).decode("utf-8", "replace")
            params.update({k: v[-1] for k, v in parse_qs(body).items()})
        return params

    @app.api_route("/pay/robokassa/result", methods=["GET", "POST"], response_class=PlainTextResponse)
    async def robokassa_result(request: Request) -> str:
        orders: OrderService = request.app.state.orders
        params = await _rk_params(request)
        if orders.robokassa is None or not orders.robokassa.check_result(params):
            raise HTTPException(400, "bad sign")
        inv_id = int(params["InvId"])
        order = await orders.mark_paid(inv_id, params.get("OutSum"))
        if order is None:
            raise HTTPException(400, "unknown order or amount")
        return f"OK{inv_id}"

    def _pay_page(title: str, text: str) -> str:
        back = settings.max_bot_url or (f"https://t.me/{settings.telegram_bot_username}" if settings.telegram_bot_username else "/")
        return (f"<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{title}</title><style>body{{font:18px/1.5 system-ui,sans-serif;background:#0b1220;color:#e9eef7;display:grid;"
                f"place-items:center;min-height:100vh;margin:0;padding:16px}}div{{max-width:460px;text-align:center}}"
                f"a{{display:inline-block;margin-top:18px;background:#ffb020;color:#2a1d00;padding:12px 20px;border-radius:12px;"
                f"text-decoration:none;font-weight:600}}</style></head><body><div><h1>{title}</h1><p>{text}</p>"
                f"<a href='{back}'>Вернуться в бот</a></div></body></html>")

    @app.api_route("/pay/robokassa/success", methods=["GET", "POST"], response_class=HTMLResponse)
    async def robokassa_success(request: Request) -> str:
        return _pay_page("Оплата прошла", "Спасибо! Отчёт придёт в бот в течение пары минут.")

    @app.api_route("/pay/robokassa/fail", methods=["GET", "POST"], response_class=HTMLResponse)
    async def robokassa_fail(request: Request) -> str:
        return _pay_page("Оплата не прошла", "Деньги не списаны. Попробуйте ещё раз из бота.")

    _id_re = re.compile(r"^[0-9a-f-]{36}$")

    @app.get("/r/{check_id}.pdf")
    async def report_pdf(check_id: str, request: Request) -> Response:
        s = svc(request)
        c = await s.get_check(check_id) if _id_re.match(check_id) else None
        if c is None:
            raise HTTPException(404, "Отчёт не найден")
        pdf = await s.pdf(c)
        name = f"report-{c.vin or c.id[:8]}-{TIER_TITLES[c.tier]}.pdf"
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{_quote(name)}"})

    @app.get("/r/{check_id}", response_class=HTMLResponse)
    async def report_html(check_id: str, request: Request) -> str:
        s = svc(request)
        c = await s.get_check(check_id) if _id_re.match(check_id) else None
        if c is None:
            raise HTTPException(404, "Отчёт не найден")
        return s.html(c)

    return app


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


def get_app() -> FastAPI:  # для uvicorn --factory
    return create_app()
