"""Личный кабинет, вход через бота, каталог специалистов и модерация."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select

from ..auth import SESSION_TTL, AuthService
from ..billing import can_use_credit, has_unlimited, products
from ..cascade import TIER_TITLES, TIERS
from ..db import Check, Order, ProProfile, User, utcnow
from ..orders import OrderService, PaymentsNotConfigured
from ..pros import KINDS, ProForm, ProService, ValidationError
from ..rules import traffic_light
from ..services import CheckService

log = logging.getLogger(__name__)
COOKIE = "sid"
_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))

router = APIRouter()


# ---------------------------------------------------------------- помощники
def svc(request: Request) -> CheckService:
    return request.app.state.service


def orders_of(request: Request) -> OrderService:
    return request.app.state.orders


def auth_of(request: Request) -> AuthService:
    return request.app.state.auth


def pros_of(request: Request) -> ProService:
    return request.app.state.pros


async def current_user(request: Request) -> User | None:
    return await auth_of(request).session_user(request.cookies.get(COOKIE))


async def require_user(request: Request) -> User:
    user = await current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login?next=" + quote(request.url.path)})
    return user


def is_admin(request: Request, user: User | None) -> bool:
    return bool(user and user.id in svc(request).settings.admins)


async def form_data(request: Request) -> dict[str, str]:
    """Разбор application/x-www-form-urlencoded без python-multipart; заодно проверка Origin (CSRF)."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin and urlparse(origin).netloc and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Запрос с чужого сайта")
    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


def render(request: Request, name: str, user: User | None = None, **ctx: Any) -> HTMLResponse:
    s = svc(request).settings
    html = _env.get_template(name).render(brand=s.brand_name, user=user, is_admin=is_admin(request, user),
                                          msg=request.query_params.get("msg"), kinds=KINDS, **ctx)
    return HTMLResponse(html)


def back(url: str, msg: str | None = None) -> RedirectResponse:
    return RedirectResponse(url + (("&" if "?" in url else "?") + "msg=" + quote(msg) if msg else ""), status_code=303)


def set_session(resp: Response, request: Request, sid: str) -> Response:
    secure = svc(request).settings.public_base_url.startswith("https://")
    resp.set_cookie(COOKIE, sid, max_age=int(SESSION_TTL.total_seconds()), httponly=True, secure=secure,
                    samesite="lax")
    return resp


def messenger_links(request: Request, payload: str) -> list[tuple[str, str]]:
    s = svc(request).settings
    links = []
    if s.max_bot_url:
        links.append(("MAX", f"{s.max_bot_url.rstrip('/')}?start={payload}"))
    if s.telegram_bot_username:
        links.append(("Telegram", f"https://t.me/{s.telegram_bot_username}?start={payload}"))
    return links


# ---------------------------------------------------------------- вход
@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    if await current_user(request):
        return RedirectResponse("/cabinet", status_code=303)
    token = await auth_of(request).new_site_token()
    nxt = request.query_params.get("next") or "/cabinet"
    if not nxt.startswith("/") or nxt.startswith("//"):  # только свои страницы
        nxt = "/cabinet"
    return render(request, "login.html", token=token, links=messenger_links(request, f"login_{token}"), next=nxt)


@router.get("/auth/poll")
async def auth_poll(request: Request, t: str):
    sid = await auth_of(request).poll_site_token(t)
    if not sid:
        return JSONResponse({"ok": False})
    return set_session(JSONResponse({"ok": True}), request, sid)


@router.get("/auth/link")
async def auth_link(request: Request, t: str):
    sid = await auth_of(request).consume_link_token(t)
    if not sid:
        return back("/login", "Ссылка для входа устарела или уже использована — запросите новую в боте командой /cabinet")
    return set_session(RedirectResponse("/cabinet", status_code=303), request, sid)


@router.post("/logout")
async def logout(request: Request):
    await form_data(request)
    await auth_of(request).logout(request.cookies.get(COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------- кабинет
@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request):
    user = await require_user(request)
    s = svc(request)
    checks = await s.history(user.id, limit=50)
    rows = []
    for c in checks:
        light = traffic_light(c.flags or [], c.sections)
        higher = [products(s.settings)[t] for t in TIERS[TIERS.index(c.tier) + 1:]]
        rows.append({"c": c, "light": light, "tier": TIER_TITLES[c.tier], "higher": higher,
                     "credit": c.tier in ("express", "basic") and can_use_credit(user, "dossier")})
    profile = await pros_of(request).by_user(user.id)
    ps = products(s.settings)
    return render(request, "cabinet.html", user=user, rows=rows, profile=profile,
                  packs=[ps[k] for k in ("pack_10", "pack_25", "pack_50", "unlimited_day")],
                  unlimited=has_unlimited(user), free_left=await s.free_left(user=user),
                  payments=bool(orders_of(request).robokassa or s.settings.dev_fake_payments))


@router.post("/cabinet/buy")
async def cabinet_buy(request: Request):
    user = await require_user(request)
    f = await form_data(request)
    code, check_id = f.get("product", ""), f.get("check_id") or None
    if code not in products(svc(request).settings):
        return back("/cabinet", "Неизвестный тариф")
    if check_id:
        c = await svc(request).get_check(check_id)
        if not c or c.user_id != user.id:
            return back("/cabinet", "Проверка не найдена")
    try:
        order, url = await orders_of(request).create(user.id, code, check_id=check_id, platform="web", chat_id=None)
    except PaymentsNotConfigured:
        return back("/cabinet", "Оплата пока не подключена")
    if url:
        return RedirectResponse(url, status_code=303)
    await deliver_web_orders(svc(request), orders_of(request))  # тестовая оплата — сразу
    return back("/cabinet", "Оплата получена")


@router.post("/cabinet/credit")
async def cabinet_credit(request: Request):
    user = await require_user(request)
    f = await form_data(request)
    c = await svc(request).get_check(f.get("check_id", ""))
    if not c or c.user_id != user.id:
        return back("/cabinet", "Проверка не найдена")
    try:
        await svc(request).upgrade_with_credit(user.id, c.id, "dossier")
    except PermissionError as e:
        return back("/cabinet", str(e))
    return back("/cabinet", "Отчёт «Досье» готов")


async def deliver_web_orders(service: CheckService, orders: OrderService) -> None:
    """Оплаченные с сайта заказы: дособрать отчёт (пакеты уже начислены при оплате)."""
    for order in await orders.undelivered("web"):
        try:
            product = products(service.settings)[order.product]
            if product.tier and order.check_id:
                await service.upgrade(order.check_id, product.tier)
            await orders.mark_delivered(order.id)
        except Exception:
            log.exception("Не удалось выполнить веб-заказ #%s", order.id)


# ---------------------------------------------------------------- анкета специалиста
@router.get("/cabinet/pro", response_class=HTMLResponse)
async def pro_edit(request: Request):
    user = await require_user(request)
    profile = await pros_of(request).by_user(user.id)
    stats = await pros_of(request).stats(profile.id) if profile else None
    return render(request, "pro_edit.html", user=user, p=profile, stats=stats, form=None, error=None)


@router.post("/cabinet/pro", response_class=HTMLResponse)
async def pro_save(request: Request):
    user = await require_user(request)
    f = await form_data(request)
    form = ProForm(kind=f.get("kind", ""), name=f.get("name", ""), city=f.get("city", ""), about=f.get("about", ""),
                   services=f.get("services", ""), phone=f.get("phone", ""), telegram=f.get("telegram", ""),
                   max_link=f.get("max_link", ""), website=f.get("website", ""),
                   experience_years=f.get("experience_years", ""), consent=f.get("consent") == "on")
    try:
        await pros_of(request).save(user.id, form)
    except ValidationError as e:
        profile = await pros_of(request).by_user(user.id)
        return render(request, "pro_edit.html", user=user, p=profile, stats=None, form=form, error=str(e))
    return back("/cabinet/pro", "Анкета отправлена на модерацию")


@router.post("/cabinet/pro/visibility")
async def pro_visibility(request: Request):
    user = await require_user(request)
    f = await form_data(request)
    await pros_of(request).set_hidden(user.id, f.get("hidden") == "1")
    return back("/cabinet/pro")


# ---------------------------------------------------------------- каталог
@router.get("/pros", response_class=HTMLResponse)
async def pros_catalog(request: Request, city: str = "", kind: str = "", q: str = ""):
    user = await current_user(request)
    items = await pros_of(request).catalog(city or None, kind or None, q or None)
    return render(request, "pros.html", user=user, items=items, cities=await pros_of(request).cities(),
                  city=city, kind=kind, q=q, now=utcnow())


async def _visible_profile(request: Request, slug: str) -> tuple[ProProfile, User | None]:
    p = await pros_of(request).by_slug(slug)
    user = await current_user(request)
    if p is None or (p.status != "approved" and not (user and (user.id == p.user_id or is_admin(request, user)))):
        raise HTTPException(404, "Анкета не найдена")
    return p, user


@router.get("/pros/{slug}", response_class=HTMLResponse)
async def pro_page(request: Request, slug: str):
    p, user = await _visible_profile(request, slug)
    if p.status == "approved" and not (user and user.id == p.user_id):
        await pros_of(request).track(p.id, "views")
    return render(request, "pro.html", user=user, p=p)


@router.post("/pros/{slug}/contacts")
async def pro_contacts(request: Request, slug: str):
    p, user = await _visible_profile(request, slug)
    if p.status == "approved" and not (user and user.id == p.user_id):
        await pros_of(request).track(p.id, "contacts")
    return {"phone": p.phone, "telegram": p.telegram, "max_link": p.max_link, "website": p.website}


# ---------------------------------------------------------------- админка
@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = await require_user(request)
    if not is_admin(request, user):
        raise HTTPException(404)
    s = svc(request)
    async with s.db.session() as db:
        totals = {
            "users": (await db.execute(select(func.count()).select_from(User))).scalar_one(),
            "checks": (await db.execute(select(func.count()).select_from(Check))).scalar_one(),
            "paid": (await db.execute(select(func.count(), func.coalesce(func.sum(Order.amount_rub), 0))
                                      .where(Order.status == "paid"))).one(),
            "pros": (await db.execute(select(func.count()).select_from(ProProfile)
                                      .where(ProProfile.status == "approved"))).scalar_one(),
        }
    return render(request, "admin.html", user=user, pending=await pros_of(request).pending(), totals=totals)


@router.post("/admin/pros/{profile_id}")
async def admin_moderate(request: Request, profile_id: int):
    user = await require_user(request)
    if not is_admin(request, user):
        raise HTTPException(404)
    f = await form_data(request)
    await pros_of(request).moderate(profile_id, f.get("action") == "approve", f.get("reason"))
    return back("/admin", "Готово")
