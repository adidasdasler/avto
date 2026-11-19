"""Панель администратора на секретном адресе ADMIN_PATH.

Вход — только сотрудники из ADMIN_USERS в .env (логин + пароль + код 2FA одной формой); аккаунты
сайта к панели отношения не имеют. Вне ADMIN_PATH панели нет (404), а с IP вне ADMIN_ALLOWED_IPS
(если задан) — 404 даже на секретном адресе. Сессия — отдельная cookie, которая отправляется
браузером только на адрес панели.

Роли: support — клиенты, начисления, блокировки, заказы, анкеты, создание бэкапа;
admin — всё то же + скачивание, восстановление и удаление бэкапов.
"""
from __future__ import annotations

import ipaddress
import logging
import secrets
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import delete

from .. import totp
from ..cascade import TIER_TITLES
from ..db import AdminSession, AdminTotpState, utcnow
from ..maintenance import cleanup
from ..pros import KINDS
from ..web.routes import form_data, orders_of, pros_of, svc
from . import accounts, backups
from .accounts import ROLES, StaffAccount
from .service import AdminService

log = logging.getLogger(__name__)
router = APIRouter()
COOKIE = "vadm"

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))
_env.filters["rub"] = lambda v: f"{int(v or 0):,}".replace(",", " ") + " ₽"
_env.filters["dt"] = lambda v: v.strftime("%d.%m.%Y %H:%M") if v else "—"
_env.filters["details"] = lambda d: ", ".join(f"{k}: {v}" for k, v in (d or {}).items() if v not in (None, ""))
_env.filters["size"] = lambda n: (f"{n / 1024 ** 3:.1f} ГБ" if n >= 1024 ** 3 else
                                  f"{n / 1024 ** 2:.1f} МБ" if n >= 1024 ** 2 else f"{n / 1024:.0f} КБ")


# ---------------------------------------------------------------- доступ
def A(request: Request) -> str:
    return svc(request).settings.admin_prefix


def accounts_of(request: Request) -> dict[str, StaffAccount]:
    return accounts.parse(svc(request).settings.admin_users)


def admin_of(request: Request) -> AdminService:
    st = request.app.state
    if not hasattr(st, "admin"):
        st.admin = AdminService(svc(request).db, svc(request).settings)
    return st.admin


def ip_of(request: Request) -> str:
    if svc(request).settings.trust_proxy_headers and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "-"


def ip_allowed(request: Request) -> bool:
    allowed = [a.strip() for a in svc(request).settings.admin_allowed_ips.split(",") if a.strip()]
    if not allowed:
        return True
    try:
        ip = ipaddress.ip_address(ip_of(request))
        return any(ip in ipaddress.ip_network(a, strict=False) for a in allowed)
    except ValueError:
        return False


def guard(request: Request) -> None:
    """Панель выключена, нет ни одного сотрудника или IP не из белого списка — для всех одинаково 404."""
    if not A(request) or not accounts_of(request) or not ip_allowed(request):
        raise HTTPException(404)


def go(request: Request, path: str = "", msg: str | None = None) -> RedirectResponse:
    url = A(request) + path
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


class Staff:
    def __init__(self, account: StaffAccount, session_id: str):
        self.login, self.role, self.session_id = account.login, account.role, session_id
        self.account = account

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def current_staff(request: Request) -> Staff | None:
    sid = request.cookies.get(COOKIE)
    if not sid:
        return None
    async with svc(request).db.session() as s:
        sess = await s.get(AdminSession, sid)
    if sess is None or sess.expires_at < utcnow():
        return None
    account = accounts_of(request).get(sess.login)  # убрали из .env — сессия больше не действует
    return Staff(account, sid) if account else None


async def require_staff(request: Request, admin: bool = False) -> Staff:
    guard(request)
    staff = await current_staff(request)
    if staff is None:
        raise HTTPException(303, headers={"Location": A(request) + "/login"})
    if admin and not staff.is_admin:
        raise HTTPException(403, "Действие доступно только администратору")
    return staff


async def verify_code(request: Request, account: StaffAccount, code: str) -> bool:
    """Код из приложения; каждый шаг TOTP принимается один раз (перехваченный код бесполезен)."""
    async with svc(request).db.session() as s:
        state = await s.get(AdminTotpState, account.login, with_for_update=True)
        counter = totp.verify(account.totp_secret, code, state.last_counter if state else None)
        if counter is None:
            return False
        if state is None:
            s.add(AdminTotpState(login=account.login, last_counter=counter))
        else:
            state.last_counter = counter
        await s.commit()
        return True


def _failures(request: Request) -> dict[str, deque]:
    st = request.app.state
    if not hasattr(st, "admin_failures"):
        st.admin_failures = defaultdict(deque)
    return st.admin_failures


def locked(request: Request, key: str, limit: int, window: float) -> bool:
    import time
    q = _failures(request)[key]
    while q and time.monotonic() - q[0] > window:
        q.popleft()
    return len(q) >= limit


def fail(request: Request, *keys: str) -> None:
    import time
    for k in keys:
        _failures(request)[k].append(time.monotonic())


def render(request: Request, name: str, staff: Staff | None = None, **ctx: Any) -> HTMLResponse:
    s = svc(request).settings
    from ..api.app import payment_warnings
    html = _env.get_template(name).render(brand=s.brand_name, staff=staff, roles=ROLES, kinds=KINDS, A=A(request),
                                          tiers=TIER_TITLES, msg=request.query_params.get("msg"),
                                          warnings=(payment_warnings(s) + config_warnings(s)) if staff else [],
                                          path=request.url.path, **ctx)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


def config_warnings(s) -> list[str]:
    out = []
    if not (s.legal_name and s.legal_inn and s.contact_email):
        out.append("Не заполнены реквизиты (LEGAL_NAME, LEGAL_INN, CONTACT_EMAIL) — они нужны в оферте и для Robokassa.")
    if not (s.alert_max_user_ids or s.alert_tg_chat_ids):
        out.append("Тревоги некому присылать: укажите ALERT_MAX_USER_IDS (ID в MAX — бот отвечает на /id).")
    return out


async def log_action(request: Request, staff: Staff | None, action: str, target: str | None = None,
                     **details: Any) -> None:
    await admin_of(request).audit(staff.login if staff else details.pop("login", None), action, target,
                                  details or None, ip_of(request))


# ---------------------------------------------------------------- вход
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    guard(request)
    if await current_staff(request):
        return go(request)
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request):
    guard(request)
    f = await form_data(request)
    ip, login_name = ip_of(request), (f.get("login") or "").strip().lower()[:32]
    if locked(request, f"ip:{ip}", 5, 900) or locked(request, f"login:{login_name}", 10, 3600):
        return go(request, "/login", "Слишком много неудачных попыток. Подождите и попробуйте позже.")
    account = accounts.check(accounts_of(request), login_name, f.get("password", ""))
    if account is None or not await verify_code(request, account, f.get("code", "")):
        fail(request, f"ip:{ip}", f"login:{login_name}")
        await log_action(request, None, "login_failed", login_name or None, login=None)
        log.warning("Панель: неудачный вход «%s» с %s", login_name, ip)
        return go(request, "/login", "Неверный логин, пароль или код")  # без подсказки, что именно неверно
    sid = secrets.token_urlsafe(32)
    async with svc(request).db.session() as s:
        s.add(AdminSession(id=sid, login=account.login, ip=ip,
                           expires_at=utcnow() + timedelta(hours=svc(request).settings.staff_mfa_hours)))
        await s.commit()
    await admin_of(request).audit(account.login, "login", None, None, ip)
    resp = go(request)
    resp.set_cookie(COOKIE, sid, max_age=svc(request).settings.staff_mfa_hours * 3600, path=A(request),
                    httponly=True, secure=svc(request).settings.public_base_url.startswith("https://"),
                    samesite="strict")
    return resp


@router.post("/logout")
async def logout(request: Request):
    guard(request)
    await form_data(request)
    sid = request.cookies.get(COOKIE)
    if sid:
        async with svc(request).db.session() as s:
            await s.execute(delete(AdminSession).where(AdminSession.id == sid))
            await s.commit()
    resp = go(request, "/login")
    resp.delete_cookie(COOKIE, path=A(request))
    return resp


# ---------------------------------------------------------------- сводка
@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    staff = await require_staff(request)
    a = admin_of(request)
    free, total = backups.disk_free(svc(request).settings)
    files = backups.list_backups(svc(request).settings)
    from .. import alerts
    return render(request, "dashboard.html", staff, stats=await a.stats(), errors=await a.provider_errors(8),
                  alerts=await alerts.recent(svc(request).db), audit=await a.audit_log(10),
                  last_backup=files[0] if files else None, disk=(free, total), settings=svc(request).settings)


@router.post("/actions/{action}")
async def quick_action(request: Request, action: str):
    staff = await require_staff(request)
    await form_data(request)
    if action == "backup":
        try:
            b = await backups.create_backup(svc(request).settings, "panel")
        except backups.BackupError as e:
            return go(request, "", f"Бэкап не создан: {e}")
        await log_action(request, staff, "backup_create", b.name)
        return go(request, "/backups", f"Бэкап создан: {b.name}")
    if action == "payments":
        orders = orders_of(request)
        await orders.poll_pending()
        from ..web.routes import deliver_web_orders
        await deliver_web_orders(svc(request), orders)
        await log_action(request, staff, "payments_poll")
        return go(request, "/orders", "Статусы оплат обновлены")
    if action == "cleanup":
        out = await cleanup(svc(request).db, svc(request).settings)
        await log_action(request, staff, "cleanup", None, **out)
        return go(request, "", f"Очищено: кэш {out['cache']}, анонимных проверок {out['anon_checks']}")
    raise HTTPException(404)


# ---------------------------------------------------------------- клиенты
@router.get("/clients", response_class=HTMLResponse)
async def clients(request: Request, q: str = ""):
    staff = await require_staff(request)
    return render(request, "clients.html", staff, rows=await admin_of(request).search_clients(q), q=q)


@router.get("/clients/{user_id}", response_class=HTMLResponse)
async def client(request: Request, user_id: int):
    staff = await require_staff(request)
    data = await admin_of(request).client(user_id)
    if data is None:
        raise HTTPException(404)
    from ..rules import traffic_light
    lights = {c.id: traffic_light(c.flags or [], c.sections) for c in data["checks"]}
    return render(request, "client.html", staff, lights=lights, now=utcnow(), **data)


@router.post("/clients/{user_id}/credits")
async def client_credits(request: Request, user_id: int):
    staff = await require_staff(request)
    f = await form_data(request)
    back = f"/clients/{user_id}"
    try:
        delta = int(f.get("delta", "0"))
    except ValueError:
        return go(request, back, "Укажите число")
    reason = (f.get("reason") or "").strip()
    if not delta or not reason:
        return go(request, back, "Укажите количество и причину")
    if abs(delta) > 500:
        return go(request, back, "Не больше 500 отчётов за раз")
    try:
        left = await admin_of(request).adjust_credits(user_id, delta)
    except ValueError as e:
        return go(request, back, str(e))
    await log_action(request, staff, "credits", f"user:{user_id}", delta=delta, reason=reason, left=left)
    return go(request, back, f"Готово, в пакете {left}")


@router.post("/clients/{user_id}/block")
async def client_block(request: Request, user_id: int):
    staff = await require_staff(request)
    f = await form_data(request)
    block = f.get("block") == "1"
    await admin_of(request).set_blocked(user_id, block, f.get("reason"))
    await log_action(request, staff, "block" if block else "unblock", f"user:{user_id}", reason=f.get("reason"))
    return go(request, f"/clients/{user_id}", "Заблокирован" if block else "Разблокирован")


# ---------------------------------------------------------------- заказы
@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, status: str = ""):
    staff = await require_staff(request)
    return render(request, "orders.html", staff, rows=await admin_of(request).orders(status), status=status)


@router.post("/orders/{order_id}/refresh")
async def order_refresh(request: Request, order_id: int):
    staff = await require_staff(request)
    await form_data(request)
    order = await orders_of(request).refresh(order_id)
    await log_action(request, staff, "order_refresh", f"order:{order_id}", status=order.status if order else None)
    return go(request, "/orders", f"Заказ №{order_id}: {order.status if order else 'не найден'}")


# ---------------------------------------------------------------- анкеты
@router.get("/pros", response_class=HTMLResponse)
async def pros_page(request: Request, status: str = "pending"):
    staff = await require_staff(request)
    return render(request, "pros.html", staff, items=await admin_of(request).all_pros(status), status=status,
                  now=utcnow())


@router.post("/pros/{profile_id}")
async def pro_moderate(request: Request, profile_id: int):
    staff = await require_staff(request)
    f = await form_data(request)
    action = f.get("action")
    if action in ("approve", "reject"):
        await pros_of(request).moderate(profile_id, action == "approve", f.get("reason"))
    elif action == "feature":
        await admin_of(request).set_featured(profile_id, int(f.get("days") or 0), f.get("plan"))
    else:
        raise HTTPException(400)
    await log_action(request, staff, f"pro_{action}", f"pro:{profile_id}", reason=f.get("reason"),
                     days=f.get("days"), plan=f.get("plan"))
    return go(request, f"/pros?status={quote(f.get('back') or 'pending')}", "Готово")


# ---------------------------------------------------------------- бэкапы
@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    staff = await require_staff(request)
    free, total = backups.disk_free(svc(request).settings)
    return render(request, "backups.html", staff, files=backups.list_backups(svc(request).settings), disk=(free, total))


@router.get("/backups/{name}")
async def backup_download(request: Request, name: str):
    staff = await require_staff(request, admin=True)
    try:
        path = backups.path_for(svc(request).settings, name)
    except backups.BackupError:
        raise HTTPException(404)
    await log_action(request, staff, "backup_download", name)
    return FileResponse(path, filename=name, media_type="application/gzip", headers={"Cache-Control": "no-store"})


@router.post("/backups/{name}/restore")
async def backup_restore(request: Request, name: str):
    staff = await require_staff(request, admin=True)
    f = await form_data(request)
    if f.get("confirm", "").strip().upper() != "ВОССТАНОВИТЬ":
        return go(request, "/backups", "Для восстановления введите слово ВОССТАНОВИТЬ")
    if not await verify_code(request, staff.account, f.get("code", "")):  # свежий код — даже при открытой сессии
        await log_action(request, staff, "restore_denied", name)
        return go(request, "/backups", "Неверный код из приложения")
    st = svc(request).settings
    try:
        backups.path_for(st, name)
        safety = await backups.create_backup(st, "before-restore")
        await backups.restore_backup(st, svc(request).db, name)
    except backups.BackupError as e:
        await log_action(request, staff, "restore_failed", name, error=str(e))
        return go(request, "/backups", f"Восстановление не выполнено, база не изменена: {e}")
    await admin_of(request).audit(staff.login, "restore", name, {"safety_copy": safety.name}, ip_of(request))
    log.warning("База восстановлена из %s сотрудником %s (копия перед восстановлением: %s)",
                name, staff.login, safety.name)
    return go(request, "/backups", f"База восстановлена из {name}. Копия до восстановления: {safety.name}. "
                                   f"Если вас выбросило из панели — войдите заново.")


@router.post("/backups/{name}/delete")
async def backup_delete(request: Request, name: str):
    staff = await require_staff(request, admin=True)
    await form_data(request)
    try:
        backups.delete_backup(svc(request).settings, name)
    except backups.BackupError as e:
        return go(request, "/backups", str(e))
    await log_action(request, staff, "backup_delete", name)
    return go(request, "/backups", "Удалено")


# ---------------------------------------------------------------- сотрудники и журнал
@router.get("/staff", response_class=HTMLResponse)
async def staff_page(request: Request):
    staff = await require_staff(request)
    return render(request, "staff.html", staff, members=list(accounts_of(request).values()),
                  audit=await admin_of(request).audit_log(200))
