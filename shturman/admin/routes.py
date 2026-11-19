"""Панель администратора /admin: только сотрудники и только после двухфакторной проверки.

Роли: support — клиенты, начисления, заказы, модерация, создание бэкапа;
admin — всё то же + восстановление из бэкапа, удаление бэкапов, сотрудники, сброс 2FA.
Посторонним панель отвечает 404, чтобы не раскрывать её существование.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .. import totp
from ..cascade import TIER_TITLES
from ..db import User, WebSession, utcnow
from ..maintenance import cleanup
from ..pros import KINDS
from ..web.routes import COOKIE, auth_of, form_data, orders_of, pros_of, rate_limited, svc
from . import backups
from .service import ROLES, AdminService, effective_role

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))
_env.filters["rub"] = lambda v: f"{int(v or 0):,}".replace(",", " ") + " ₽"
_env.filters["dt"] = lambda v: v.strftime("%d.%m.%Y %H:%M") if v else "—"
_env.filters["details"] = lambda d: ", ".join(f"{k}: {v}" for k, v in (d or {}).items() if v not in (None, ""))
_env.filters["size"] = lambda n: (f"{n / 1024 ** 3:.1f} ГБ" if n >= 1024 ** 3 else
                                  f"{n / 1024 ** 2:.1f} МБ" if n >= 1024 ** 2 else f"{n / 1024:.0f} КБ")


class Staff:
    def __init__(self, user: User, session: WebSession, role: str):
        self.user, self.session, self.role = user, session, role

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def admin_of(request: Request) -> AdminService:
    st = request.app.state
    if not hasattr(st, "admin"):
        st.admin = AdminService(svc(request).db, svc(request).settings)
    return st.admin


def ip_of(request: Request) -> str:
    if svc(request).settings.trust_proxy_headers and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "-"


def go(url: str, msg: str | None = None) -> RedirectResponse:
    path, _, frag = url.partition("#")
    if msg:
        path += ("&" if "?" in path else "?") + "msg=" + quote(msg)
    return RedirectResponse(path + (f"#{frag}" if frag else ""), status_code=303)


async def staff_or_404(request: Request) -> tuple[WebSession, User, str]:
    rec = await auth_of(request).session_record(request.cookies.get(COOKIE))
    role = effective_role(rec[1], svc(request).settings) if rec else None
    if not rec or not role:
        raise HTTPException(404)
    return rec[0], rec[1], role


async def require_staff(request: Request, admin: bool = False) -> Staff:
    sess, user, role = await staff_or_404(request)
    if user.totp_enabled_at is None:
        raise HTTPException(303, headers={"Location": "/admin/2fa/setup"})
    ttl = timedelta(hours=svc(request).settings.staff_mfa_hours)
    if sess.mfa_at is None or sess.mfa_at < utcnow() - ttl:
        raise HTTPException(303, headers={"Location": "/admin/2fa?next=" + quote(request.url.path)})
    if admin and role != "admin":
        raise HTTPException(403, "Действие доступно только администратору")
    return Staff(user, sess, role)


def render(request: Request, name: str, staff: Staff | None = None, **ctx: Any) -> HTMLResponse:
    s = svc(request).settings
    from ..api.app import payment_warnings
    html = _env.get_template(name).render(brand=s.brand_name, staff=staff, roles=ROLES, kinds=KINDS,
                                          tiers=TIER_TITLES, msg=request.query_params.get("msg"),
                                          warnings=payment_warnings(s) if staff else [], path=request.url.path, **ctx)
    return HTMLResponse(html)


async def log_action(request: Request, staff: Staff, action: str, target: str | None = None, **details: Any) -> None:
    await admin_of(request).audit(staff.user.id, action, target, details or None, ip_of(request))


# ---------------------------------------------------------------- 2FA
@router.get("/2fa/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    sess, user, role = await staff_or_404(request)
    if user.totp_enabled_at is not None:
        return go("/admin")
    secret = await auth_of(request).totp_pending_secret(user.id)
    account = user.email or f"user{user.id}"
    uri = totp.provisioning_uri(secret, account, svc(request).settings.brand_name)
    return render(request, "2fa_setup.html", user=user, secret=secret, qr=Markup(totp.qr_svg(uri)),
                  has_password=bool(user.password_hash), error=None)


@router.post("/2fa/setup", response_class=HTMLResponse)
async def setup_confirm(request: Request):
    sess, user, role = await staff_or_404(request)
    f = await form_data(request)
    if rate_limited(request, f"2fa:{user.id}", limit=5, window=300, count=False):
        return go("/admin/2fa/setup", "Слишком много попыток, подождите 5 минут")
    if not user.password_hash or not await auth_of(request).check_user_password(user.id, f.get("password", "")):
        rate_limited(request, f"2fa:{user.id}", limit=5, window=300)
        return go("/admin/2fa/setup", "Неверный пароль")
    codes = await auth_of(request).totp_enable(user.id, f.get("code", ""))
    if not codes:
        rate_limited(request, f"2fa:{user.id}", limit=5, window=300)
        return go("/admin/2fa/setup", "Код не подошёл — проверьте время на телефоне и введите свежий код")
    await auth_of(request).mark_mfa(sess.id)
    await admin_of(request).audit(user.id, "2fa_enabled", f"user:{user.id}", None, ip_of(request))
    return render(request, "2fa_codes.html", codes=codes)


@router.get("/2fa", response_class=HTMLResponse)
async def mfa_page(request: Request):
    sess, user, role = await staff_or_404(request)
    if user.totp_enabled_at is None:
        return go("/admin/2fa/setup")
    return render(request, "2fa.html", next=request.query_params.get("next") or "/admin")


@router.post("/2fa")
async def mfa_check(request: Request):
    sess, user, role = await staff_or_404(request)
    f = await form_data(request)
    nxt = f.get("next") or "/admin"
    nxt = nxt if nxt.startswith("/admin") else "/admin"
    if rate_limited(request, f"2fa:{user.id}", limit=5, window=300, count=False):
        return go(f"/admin/2fa?next={quote(nxt)}", "Слишком много неверных кодов, подождите 5 минут")
    kind = await auth_of(request).totp_check(user.id, f.get("code", ""))
    if kind is None:
        rate_limited(request, f"2fa:{user.id}", limit=5, window=300)
        await admin_of(request).audit(user.id, "2fa_failed", None, None, ip_of(request))
        return go(f"/admin/2fa?next={quote(nxt)}", "Неверный код")
    await auth_of(request).mark_mfa(sess.id)
    await admin_of(request).audit(user.id, "login", None, {"factor": kind}, ip_of(request))
    msg = "Вход по резервному коду. Осталось кодов: " + str(len((await svc(request).get_user(user.id)).recovery_codes or [])) \
        if kind == "recovery" else None
    return go(nxt, msg)


# ---------------------------------------------------------------- сводка
@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    staff = await require_staff(request)
    a = admin_of(request)
    free, total = backups.disk_free(svc(request).settings)
    files = backups.list_backups(svc(request).settings)
    return render(request, "dashboard.html", staff, stats=await a.stats(), errors=await a.provider_errors(8),
                  audit=await a.audit_log(10), last_backup=files[0] if files else None, disk=(free, total),
                  settings=svc(request).settings)


@router.post("/actions/{action}")
async def quick_action(request: Request, action: str):
    staff = await require_staff(request)
    await form_data(request)
    if action == "backup":
        try:
            b = await backups.create_backup(svc(request).settings, "panel")
        except backups.BackupError as e:
            return go("/admin", f"Бэкап не создан: {e}")
        await log_action(request, staff, "backup_create", b.name)
        return go("/admin/backups", f"Бэкап создан: {b.name}")
    if action == "payments":
        orders = orders_of(request)
        await orders.poll_pending()
        from ..web.routes import deliver_web_orders
        await deliver_web_orders(svc(request), orders)
        await log_action(request, staff, "payments_poll")
        return go("/admin/orders", "Статусы оплат обновлены")
    if action == "cleanup":
        out = await cleanup(svc(request).db, svc(request).settings)
        await log_action(request, staff, "cleanup", None, **out)
        return go("/admin", f"Очищено: кэш {out['cache']}, анонимных проверок {out['anon_checks']}")
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
    try:
        delta = int(f.get("delta", "0"))
    except ValueError:
        return go(f"/admin/clients/{user_id}", "Укажите число")
    reason = (f.get("reason") or "").strip()
    if not delta or not reason:
        return go(f"/admin/clients/{user_id}", "Укажите количество и причину")
    if abs(delta) > 500:
        return go(f"/admin/clients/{user_id}", "Не больше 500 отчётов за раз")
    try:
        left = await admin_of(request).adjust_credits(user_id, delta)
    except ValueError as e:
        return go(f"/admin/clients/{user_id}", str(e))
    await log_action(request, staff, "credits", f"user:{user_id}", delta=delta, reason=reason, left=left)
    return go(f"/admin/clients/{user_id}", f"Готово, в пакете {left}")


@router.post("/clients/{user_id}/block")
async def client_block(request: Request, user_id: int):
    staff = await require_staff(request)
    f = await form_data(request)
    if user_id == staff.user.id:
        return go(f"/admin/clients/{user_id}", "Нельзя заблокировать себя")
    target = await svc(request).get_user(user_id)
    if target and effective_role(target, svc(request).settings) and not staff.is_admin:
        raise HTTPException(403, "Блокировать сотрудников может только администратор")
    block = f.get("block") == "1"
    await admin_of(request).set_blocked(user_id, block, f.get("reason"))
    await log_action(request, staff, "block" if block else "unblock", f"user:{user_id}", reason=f.get("reason"))
    return go(f"/admin/clients/{user_id}", "Заблокирован" if block else "Разблокирован")


@router.post("/clients/{user_id}/role")
async def client_role(request: Request, user_id: int):
    staff = await require_staff(request, admin=True)
    f = await form_data(request)
    role = f.get("role") or None
    if user_id == staff.user.id and role != "admin":
        return go(f"/admin/clients/{user_id}", "Нельзя снять роль с себя")
    try:
        await admin_of(request).set_staff_role(user_id, role)
    except ValueError as e:
        return go(f"/admin/clients/{user_id}", str(e))
    await log_action(request, staff, "role", f"user:{user_id}", role=role)
    return go(f"/admin/clients/{user_id}", "Роль изменена")


@router.post("/clients/{user_id}/reset-2fa")
async def client_reset_2fa(request: Request, user_id: int):
    staff = await require_staff(request, admin=True)
    await form_data(request)
    await admin_of(request).reset_2fa(user_id)
    await log_action(request, staff, "2fa_reset", f"user:{user_id}")
    return go(f"/admin/clients/{user_id}", "2FA сброшена — при следующем входе сотрудник подключит её заново")


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
    return go("/admin/orders", f"Заказ №{order_id}: {order.status if order else 'не найден'}")


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
    return go(f"/admin/pros?status={quote(f.get('back') or 'pending')}", "Готово")


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
    return FileResponse(path, filename=name, media_type="application/gzip")


@router.post("/backups/{name}/restore")
async def backup_restore(request: Request, name: str):
    staff = await require_staff(request, admin=True)
    f = await form_data(request)
    if f.get("confirm", "").strip().upper() != "ВОССТАНОВИТЬ":
        return go("/admin/backups", "Для восстановления введите слово ВОССТАНОВИТЬ")
    # Свежий код из приложения (не резервный) — даже при уже пройденной 2FA.
    if await auth_of(request).totp_check(staff.user.id, f.get("code", ""), allow_recovery=False) != "totp":
        await log_action(request, staff, "restore_denied", name)
        return go("/admin/backups", "Неверный код из приложения")
    st = svc(request).settings
    try:
        backups.path_for(st, name)
        safety = await backups.create_backup(st, "before-restore")
        await backups.restore_backup(st, svc(request).db, name)
    except backups.BackupError as e:
        await log_action(request, staff, "restore_failed", name, error=str(e))
        return go("/admin/backups", f"Восстановление не выполнено, база не изменена: {e}")
    await admin_of(request).audit(staff.user.id, "restore", name, {"safety_copy": safety.name}, ip_of(request))
    log.warning("База восстановлена из %s сотрудником #%s (копия перед восстановлением: %s)",
                name, staff.user.id, safety.name)
    return go("/admin/backups", f"База восстановлена из {name}. Копия до восстановления: {safety.name}. "
                                f"Если вас выбросило из панели — войдите заново.")


@router.post("/backups/{name}/delete")
async def backup_delete(request: Request, name: str):
    staff = await require_staff(request, admin=True)
    await form_data(request)
    try:
        backups.delete_backup(svc(request).settings, name)
    except backups.BackupError as e:
        return go("/admin/backups", str(e))
    await log_action(request, staff, "backup_delete", name)
    return go("/admin/backups", "Удалено")


# ---------------------------------------------------------------- сотрудники и журнал
@router.get("/staff", response_class=HTMLResponse)
async def staff_page(request: Request):
    staff = await require_staff(request)
    a = admin_of(request)
    return render(request, "staff.html", staff, members=await a.staff(), audit=await a.audit_log(200))


@router.post("/staff/add")
async def staff_add(request: Request):
    staff = await require_staff(request, admin=True)
    f = await form_data(request)
    who = (f.get("who") or "").strip()
    rows = await admin_of(request).search_clients(who, limit=2)
    exact = [r.user for r in rows if who.lower() in ((r.user.email or "").lower(), str(r.user.id))]
    if len(exact) != 1:
        return go("/admin/staff", "Пользователь не найден: укажите номер в системе или точную почту")
    role = f.get("role") if f.get("role") in ROLES else "support"
    await admin_of(request).set_staff_role(exact[0].id, role)
    await log_action(request, staff, "role", f"user:{exact[0].id}", role=role)
    return go("/admin/staff", f"Пользователь №{exact[0].id} — {ROLES[role]}. При входе в панель он подключит 2FA.")
