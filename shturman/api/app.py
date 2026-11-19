"""HTTP API и лендинг (FastAPI)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import parse_qs, quote
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
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
from ..auth import AuthService
from ..maintenance import cleanup
from ..orders import OrderService, make_robokassa
from ..pros import ProService
from ..admin.routes import router as admin_router
from ..web.routes import COOKIE, deliver_web_orders, rate_limited, router as web_router
from ..services import CheckService, LimitExceeded

log = logging.getLogger(__name__)
LANDING_DIR = Path(__file__).resolve().parents[2] / "landing"


class CheckIn(BaseModel):
    query: str = Field(min_length=3, max_length=512)


def payment_warnings(settings: Settings) -> list[str]:
    """Опасные для боевого сайта настройки оплаты."""
    if not settings.public_base_url.startswith("https://"):
        return []
    out = []
    if settings.dev_fake_payments:
        out.append("DEV_FAKE_PAYMENTS=true — кнопки покупки выдают отчёты бесплатно. Выключите на боевом сайте.")
    if settings.robokassa_login and settings.robokassa_test:
        out.append("ROBOKASSA_TEST=true — принимаются тестовые платежи без денег. "
                   "Для приёма оплат поставьте false и боевые пароли.")
    return out


async def background(app: FastAPI, interval: float = 15.0) -> None:
    """Подтверждение оплат с сайта (опрос Robokassa), выполнение веб-заказов, ежедневная очистка."""
    last_cleanup = 0.0
    while True:
        try:
            await app.state.orders.poll_pending()
            await deliver_web_orders(app.state.service, app.state.orders)
            if time.monotonic() - last_cleanup > 24 * 3600:
                await cleanup(app.state.service.db, app.state.service.settings)
                last_cleanup = time.monotonic()
        except Exception:
            log.exception("background")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None, provider: Provider | None = None,
               run_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        await db.create_all()
        prov = provider or make_provider(settings)
        app.state.service = CheckService(db, prov, settings)
        app.state.orders = OrderService(db, settings, make_robokassa(settings))
        app.state.auth = AuthService(db)
        for warning in payment_warnings(settings):
            log.warning("ВНИМАНИЕ: %s", warning)
        app.state.pros = ProService(db)
        bg = asyncio.create_task(background(app)) if run_background else None
        yield
        if bg:
            bg.cancel()
        await prov.aclose()
        await db.dispose()

    app = FastAPI(title=settings.brand_name, lifespan=lifespan)
    app.include_router(admin_router)
    app.include_router(web_router)
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
            user = await request.app.state.auth.session_user(request.cookies.get(COOKIE))
            c = await s.express(body.query, user=user, client_ip=client_ip(request), source="web")
        except IdentifierError as e:
            raise HTTPException(422, str(e))
        except LimitExceeded as e:
            raise HTTPException(429, str(e))
        return {
            "id": c.id, "vehicle": c.vehicle, "overall": c.overall, "flags": c.flags,
            "light": traffic_light(c.flags or [], c.sections),
            "report_url": f"/r/{c.id}", "buy_url": s.bot_link(c.id), "messenger": s.messenger,
            "free_left": await s.free_left(user=user, client_ip=client_ip(request)),
            "cabinet": bool(user),
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
        ip = client_ip(request)
        if orders.robokassa is None or not orders.robokassa.check_result(params):
            log.warning("Robokassa ResultURL: неверная подпись от %s, InvId=%s", ip, params.get("InvId"))
            raise HTTPException(400, "bad sign")
        if str(params.get("IsTest", "")).lower() in ("1", "true") and not settings.robokassa_test:
            # Тестовые платежи на боевом магазине не засчитываем.
            log.error("Robokassa ResultURL: тестовый платёж при выключенном тестовом режиме, InvId=%s, IP %s",
                      params.get("InvId"), ip)
            raise HTTPException(400, "test payment")
        if not str(params.get("InvId", "")).isdigit():
            raise HTTPException(400, "bad InvId")
        inv_id = int(params["InvId"])
        order = await orders.mark_paid(inv_id, params.get("OutSum"))
        if order is None:
            log.error("Robokassa ResultURL: заказ #%s не найден или сумма %s не совпала (IP %s)",
                      inv_id, params.get("OutSum"), ip)
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

    @app.api_route("/pay/robokassa/success", methods=["GET", "POST"])
    async def robokassa_success(request: Request):
        orders: OrderService = request.app.state.orders
        params = await _rk_params(request)
        if orders.robokassa and orders.robokassa.check_success(params) and str(params.get("InvId", "")).isdigit():
            order = await orders.refresh(int(params["InvId"]))
            if order and order.platform == "web":
                if order.status == "paid":
                    await deliver_web_orders(request.app.state.service, orders)
                    msg = "Оплата получена — отчёт в списке проверок"
                else:
                    msg = "Оплата обрабатывается — обновите страницу через минуту"
                return RedirectResponse("/cabinet?msg=" + quote(msg), status_code=303)
        return HTMLResponse(_pay_page("Оплата прошла", "Спасибо! Отчёт придёт в бот или появится в личном кабинете "
                                                          "в течение пары минут."))

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
        if rate_limited(request, "pdf", limit=30, window=600):  # сборка PDF нагружает процессор
            raise HTTPException(429, "Слишком много запросов, попробуйте через несколько минут")
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
