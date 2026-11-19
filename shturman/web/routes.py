"""Личный кабинет, вход через бота, каталог специалистов и модерация."""
from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select

from .. import mailer, oauth_yandex
from ..auth import MIN_PASSWORD, SESSION_TTL, AuthError, AuthService
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
    """Сотрудник (поддержка или администратор) — для ссылки «Панель» и просмотра неопубликованных анкет."""
    from ..admin.service import effective_role
    return effective_role(user, svc(request).settings) is not None


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
    path, _, frag = url.partition("#")
    if msg:
        path += ("&" if "?" in path else "?") + "msg=" + quote(msg)
    return RedirectResponse(path + (f"#{frag}" if frag else ""), status_code=303)


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


def rate_limited(request: Request, bucket: str, limit: int = 10, window: float = 900, count: bool = True) -> bool:
    """Не больше limit попыток за window секунд с одного IP (вход, регистрация, восстановление).
    count=False — только проверить, не засчитывая попытку (для 2FA считаем лишь неудачные коды)."""
    s = svc(request).settings
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() if s.trust_proxy_headers else ""
    key = f"{bucket}:{ip or (request.client.host if request.client else '-')}"
    attempts = getattr(request.app.state, "attempts", None)
    if attempts is None:
        attempts = request.app.state.attempts = defaultdict(deque)
    q, now = attempts[key], time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    if count:
        q.append(now)
    return False


def safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/cabinet"


def site_url(request: Request, path: str) -> str:
    return svc(request).settings.public_base_url.rstrip("/") + path


async def send_verify_email(request: Request, user: User) -> bool:
    token = await auth_of(request).new_token("verify", user.id)
    brand = svc(request).settings.brand_name
    return await mailer.send(svc(request).settings, user.email, f"Подтвердите адрес — {brand}",
                             f"Здравствуйте!\n\nПодтвердите адрес почты для входа в {brand}:\n"
                             f"{site_url(request, '/verify?t=' + token)}\n\nСсылка действует 3 дня. "
                             f"Если вы не регистрировались — просто проигнорируйте письмо.")


# ---------------------------------------------------------------- вход
async def login_page(request: Request, error: str | None = None, email: str = "", status: int = 200) -> HTMLResponse:
    token = await auth_of(request).new_site_token()
    nxt = safe_next(request.query_params.get("next"))
    resp = render(request, "login.html", token=token, links=messenger_links(request, f"login_{token}"), next=nxt,
                  error=error, email=email, yandex=oauth_yandex.configured(svc(request).settings))
    resp.status_code = status
    return resp


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    if await current_user(request):
        return RedirectResponse("/cabinet", status_code=303)
    return await login_page(request)


@router.post("/login", response_class=HTMLResponse)
async def login_password(request: Request):
    f = await form_data(request)
    if rate_limited(request, "login"):
        return await login_page(request, "Слишком много попыток. Подождите 15 минут.", f.get("email", ""), 429)
    user = await auth_of(request).check_password(f.get("email", ""), f.get("password", ""))
    if user is None:
        return await login_page(request, "Неверная почта или пароль.", f.get("email", ""), 400)
    if user.blocked_at is not None:
        return await login_page(request, "Аккаунт заблокирован. Напишите в поддержку.", f.get("email", ""), 403)
    sid = await auth_of(request).create_session(user.id)
    return set_session(RedirectResponse(safe_next(f.get("next")), status_code=303), request, sid)


@router.get("/auth/yandex")
async def yandex_start(request: Request):
    st = svc(request).settings
    if not oauth_yandex.configured(st):
        return back("/login", "Вход через Яндекс ID пока не подключён")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(oauth_yandex.authorize_url(st, state), status_code=303)
    resp.set_cookie("ya_state", f"{state}|{safe_next(request.query_params.get('next'))}", max_age=600, httponly=True,
                    secure=st.public_base_url.startswith("https://"), samesite="lax")
    return resp


@router.get("/auth/yandex/callback")
async def yandex_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    st = svc(request).settings
    saved, _, nxt = (request.cookies.get("ya_state") or "").partition("|")
    if error or not code:
        return back("/login", "Вход через Яндекс ID отменён")
    if not saved or not secrets.compare_digest(saved, state):  # защита от подмены входа (CSRF)
        return back("/login", "Сессия входа устарела — попробуйте ещё раз")
    try:
        profile = await oauth_yandex.fetch_profile(st, code, getattr(request.app.state, "yandex_transport", None))
    except oauth_yandex.YandexError as e:
        log.warning("Яндекс ID: %s", e)
        return back("/login", "Яндекс не подтвердил вход — попробуйте ещё раз")
    current = await current_user(request)
    user_id = await auth_of(request).yandex_account(profile.id, profile.email, current.id if current else None,
                                                    svc(request).merge_users)
    resp = RedirectResponse(safe_next(nxt), status_code=303)
    resp.delete_cookie("ya_state")
    if current is None:
        set_session(resp, request, await auth_of(request).create_session(user_id))
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if await current_user(request):
        return RedirectResponse("/cabinet", status_code=303)
    return render(request, "register.html", error=None, email="", yandex=oauth_yandex.configured(svc(request).settings))


@router.post("/register", response_class=HTMLResponse)
async def register(request: Request):
    f = await form_data(request)
    email = f.get("email", "")

    def fail(msg: str, code: int = 400) -> HTMLResponse:
        r = render(request, "register.html", error=msg, email=email)
        r.status_code = code
        return r

    if rate_limited(request, "register"):
        return fail("Слишком много попыток. Подождите 15 минут.", 429)
    if f.get("password") != f.get("password2"):
        return fail("Пароли не совпадают.")
    if f.get("consent") != "on":
        return fail("Нужно согласие на обработку персональных данных.")
    try:
        user = await auth_of(request).register(email, f.get("password", ""))
    except AuthError as e:
        return fail(str(e))
    sent = await send_verify_email(request, user)
    sid = await auth_of(request).create_session(user.id)
    msg = ("Готово! Мы отправили письмо для подтверждения адреса." if sent else "Готово! Добро пожаловать.")
    return set_session(back("/cabinet", msg), request, sid)


@router.get("/verify")
async def verify(request: Request, t: str):
    user_id = await auth_of(request).consume_token(t, "verify")
    if not user_id:
        return back("/cabinet", "Ссылка для подтверждения устарела — отправьте письмо ещё раз из кабинета.")
    await auth_of(request).mark_verified(user_id)
    return back("/cabinet", "Адрес почты подтверждён.")


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    return render(request, "forgot.html", sent=False, smtp=mailer.configured(svc(request).settings), error=None)


@router.post("/forgot", response_class=HTMLResponse)
async def forgot(request: Request):
    f = await form_data(request)
    smtp = mailer.configured(svc(request).settings)
    if rate_limited(request, "forgot", limit=5):
        return render(request, "forgot.html", sent=False, smtp=smtp, error="Слишком много попыток. Подождите 15 минут.")
    user = await auth_of(request).user_by_email(f.get("email", ""))
    if user and smtp:
        token = await auth_of(request).new_token("reset", user.id)
        await mailer.send(svc(request).settings, user.email, f"Восстановление пароля — {svc(request).settings.brand_name}",
                          f"Здравствуйте!\n\nЧтобы задать новый пароль, откройте ссылку:\n"
                          f"{site_url(request, '/reset?t=' + token)}\n\nСсылка действует 1 час. "
                          f"Если вы не запрашивали восстановление — просто проигнорируйте письмо.")
    # Ответ одинаковый, есть такой адрес или нет — чтобы не раскрывать, кто зарегистрирован.
    return render(request, "forgot.html", sent=True, smtp=smtp, error=None)


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, t: str):
    valid = await auth_of(request).peek_token(t, "reset") is not None
    return render(request, "reset.html", token=t, valid=valid, error=None)


@router.post("/reset", response_class=HTMLResponse)
async def reset(request: Request):
    f = await form_data(request)
    token = f.get("token", "")
    if f.get("password") != f.get("password2"):
        return render(request, "reset.html", token=token, valid=True, error="Пароли не совпадают.")
    if len(f.get("password", "")) < MIN_PASSWORD:
        return render(request, "reset.html", token=token, valid=True,
                      error=f"Пароль — не короче {MIN_PASSWORD} символов.")
    user_id = await auth_of(request).consume_token(token, "reset")
    if not user_id:
        return render(request, "reset.html", token=token, valid=False, error=None)
    await auth_of(request).set_password(user_id, f["password"])
    await auth_of(request).mark_verified(user_id)  # письмо дошло — адрес рабочий
    sid = await auth_of(request).create_session(user_id)
    return set_session(back("/cabinet", "Пароль изменён."), request, sid)


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
    bind = []
    st = s.settings
    if not user.max_id and st.max_bot_url:
        bind.append(("MAX", "max"))
    if not user.tg_id and st.telegram_bot_username:
        bind.append(("Telegram", "telegram"))
    return render(request, "cabinet.html", user=user, rows=rows, profile=profile, bind=bind,
                  yandex=oauth_yandex.configured(st),
                  smtp=mailer.configured(st), min_password=MIN_PASSWORD,
                  packs=[ps[k] for k in ("pack_10", "pack_25", "pack_50")],
                  unlimited=has_unlimited(user), free_left=await s.free_left(user=user),
                  payments=bool(orders_of(request).robokassa or s.settings.dev_fake_payments))


@router.post("/cabinet/credentials")
async def cabinet_credentials(request: Request):
    user = await require_user(request)
    f = await form_data(request)
    if user.password_hash and not await auth_of(request).check_password(user.email or "", f.get("current", "")):
        return back("/cabinet#security", "Текущий пароль указан неверно")
    if f.get("password") != f.get("password2"):
        return back("/cabinet#security", "Пароли не совпадают")
    try:
        updated = await auth_of(request).set_email_password(user.id, f.get("email", ""), f.get("password", ""),
                                                            keep_session=request.cookies.get(COOKIE))
    except AuthError as e:
        return back("/cabinet#security", str(e))
    if updated.email_verified_at is None:
        sent = await send_verify_email(request, updated)
        return back("/cabinet#security", "Сохранено. " + ("Подтвердите адрес по ссылке из письма." if sent else ""))
    return back("/cabinet#security", "Сохранено")


@router.post("/cabinet/verify")
async def cabinet_verify_resend(request: Request):
    user = await require_user(request)
    await form_data(request)
    if rate_limited(request, "verify", limit=3):
        return back("/cabinet#security", "Письмо уже отправлено, проверьте почту и папку «Спам»")
    if user.email and not user.email_verified_at:
        sent = await send_verify_email(request, user)
        return back("/cabinet#security", "Письмо отправлено" if sent else "Отправка писем пока не настроена")
    return back("/cabinet#security")


@router.post("/cabinet/bind")
async def cabinet_bind(request: Request):
    """Привязать мессенджер: ссылка на бота с одноразовым токеном, бот объединит аккаунты."""
    user = await require_user(request)
    f = await form_data(request)
    token = await auth_of(request).new_token("bind", user.id)
    links = dict(messenger_links(request, f"bind_{token}"))
    url = links.get({"max": "MAX", "telegram": "Telegram"}.get(f.get("messenger", ""), ""))
    if not url:
        return back("/cabinet#security", "Этот мессенджер не подключён")
    return RedirectResponse(url, status_code=303)


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


# ---------------------------------------------------------------- документы
LEGAL_PAGES = {"offer": "Публичная оферта", "privacy": "Политика обработки персональных данных",
               "contacts": "Контакты и реквизиты"}
LEGAL_UPDATED = "19.11.2026"


async def legal_page(request: Request, page: str) -> HTMLResponse:
    user = await current_user(request)
    st = svc(request).settings
    required = {"LEGAL_NAME": st.legal_name, "LEGAL_INN": st.legal_inn, "CONTACT_EMAIL": st.contact_email}
    missing = [k for k, val in required.items() if not val] if is_admin(request, user) else []
    bots = [(n, u.split("?")[0]) for n, u in messenger_links(request, "x")]
    return render(request, "legal.html", user=user, page=page, title=LEGAL_PAGES[page], updated=LEGAL_UPDATED,
                  L=st, site=st.public_base_url.split("://")[-1].rstrip("/"), missing=missing, bots=bots,
                  products=list(products(st).values()))


@router.get("/offer", response_class=HTMLResponse)
async def offer(request: Request):
    return await legal_page(request, "offer")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return await legal_page(request, "privacy")


@router.get("/contacts", response_class=HTMLResponse)
async def contacts(request: Request):
    return await legal_page(request, "contacts")


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots(request: Request) -> str:
    return ("User-agent: *\nDisallow: /cabinet\nDisallow: /admin\nDisallow: /r/\nDisallow: /auth/\n"
            "Disallow: /pay/\nDisallow: /api/\n"
            f"Sitemap: {site_url(request, '/sitemap.xml')}\n")


@router.get("/sitemap.xml")
async def sitemap(request: Request) -> Response:
    paths = ["/", "/pros", "/offer", "/privacy", "/contacts"]
    paths += [f"/pros/{p.slug}" for p in await pros_of(request).catalog()]
    urls = "".join(f"<url><loc>{site_url(request, p)}</loc></url>" for p in paths)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")
