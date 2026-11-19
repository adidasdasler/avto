import re
import time

from shturman import totp
from shturman.db import User
from tests.test_auth_email import register
from tests.test_cabinet import make_client


def code_for(client, user_id, offset=0):
    user = client.portal.call(client.app.state.service.get_user, user_id)
    return totp.code_at(user.totp_secret, int(time.time() // 30) + offset)


def staff_login(client, email="admin@vincv.ru", role="admin"):
    """Регистрация, роль, подключение 2FA через страницу настройки. Возвращает (user_id, резервные коды)."""
    register(client, email=email)
    user = client.portal.call(client.app.state.auth.user_by_email, email)

    async def grant():
        from shturman.admin.service import AdminService
        await AdminService(client.app.state.service.db, client.app.state.service.settings).set_staff_role(user.id, role)
    client.portal.call(grant)
    r = client.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/2fa/setup"
    page = client.get("/admin/2fa/setup").text
    assert "<svg" in page
    secret = re.search(r"<code[^>]*>([A-Z2-7]+)</code>", page).group(1)
    code = totp.code_at(secret, int(time.time() // 30))
    r = client.post("/admin/2fa/setup", data={"password": "secret-123", "code": code})
    codes = re.findall(r"[A-Z2-9]{4}-[A-Z2-9]{4}", r.text)
    assert len(codes) == 8
    return user.id, codes


def test_panel_hidden_from_non_staff(settings):
    with make_client(settings) as client:
        assert client.get("/admin").status_code == 404  # не вошёл
        register(client)
        assert client.get("/admin").status_code == 404  # не сотрудник
        assert client.get("/admin/clients").status_code == 404
        assert client.get("/admin/2fa/setup").status_code == 404


def test_2fa_required_and_codes(settings):
    with make_client(settings) as client:
        uid, recovery = staff_login(client)
        assert client.get("/admin").status_code == 200  # сессия прошла 2FA при подключении
        # новая сессия: пароль есть, но без кода в панель не пускает
        client.cookies.clear()
        client.post("/login", data={"email": "admin@vincv.ru", "password": "secret-123"})
        r = client.get("/admin/clients")
        assert r.status_code == 303 and r.headers["location"].startswith("/admin/2fa?next=")
        assert "Неверный код" in client.post("/admin/2fa", data={"code": "000000"}, follow_redirects=True).text
        code = code_for(client, uid, +1)
        r = client.post("/admin/2fa", data={"code": code, "next": "/admin/clients"})
        assert r.headers["location"] == "/admin/clients"
        # тот же код повторно (перехваченный) — не принимается
        client.cookies.clear()
        client.post("/login", data={"email": "admin@vincv.ru", "password": "secret-123"})
        assert "Неверный код" in client.post("/admin/2fa", data={"code": code}, follow_redirects=True).text
        # резервный код — один раз
        r = client.post("/admin/2fa", data={"code": recovery[0].lower()}, follow_redirects=True)
        assert "Осталось кодов: 7" in r.text
        client.cookies.clear()
        client.post("/login", data={"email": "admin@vincv.ru", "password": "secret-123"})
        assert "Неверный код" in client.post("/admin/2fa", data={"code": recovery[0]}, follow_redirects=True).text


def test_setup_requires_password_and_valid_code(settings):
    with make_client(settings) as client:
        register(client, email="s@vincv.ru")
        user = client.portal.call(client.app.state.auth.user_by_email, "s@vincv.ru")
        client.app.state.service.settings.admin_user_ids = str(user.id)  # первый администратор из .env
        client.get("/admin/2fa/setup")
        secret = client.portal.call(client.app.state.service.get_user, user.id).totp_secret
        good = totp.code_at(secret, int(time.time() // 30))
        assert "Неверный пароль" in client.post("/admin/2fa/setup", data={"password": "nope", "code": good},
                                                follow_redirects=True).text
        assert "Код не подошёл" in client.post("/admin/2fa/setup", data={"password": "secret-123", "code": "123456"},
                                               follow_redirects=True).text
        assert "2FA подключена" in client.post("/admin/2fa/setup", data={"password": "secret-123", "code": good}).text


def test_support_role_limits_and_client_actions(settings):
    with make_client(settings) as client:
        uid, _ = staff_login(client, email="help@vincv.ru", role="support")
        victim = client.portal.call(client.app.state.service.max_user, 4242)
        client.portal.call(lambda: client.app.state.service.express("X7TEST00000000001", user=victim))
        # поиск по VIN и карточка
        assert f"/admin/clients/{victim.id}" in client.get("/admin/clients?q=X7TEST00000000001").text
        assert "LADA Vesta" in client.get(f"/admin/clients/{victim.id}").text
        # начисление и списание отчётов — с причиной
        client.post(f"/admin/clients/{victim.id}/credits", data={"delta": "5", "reason": "компенсация"})
        assert client.portal.call(client.app.state.service.get_user, victim.id).credits == 5
        r = client.post(f"/admin/clients/{victim.id}/credits", data={"delta": "-9", "reason": "ошибка"}, follow_redirects=True)
        assert "Нельзя списать больше" in r.text
        # блокировка
        client.post(f"/admin/clients/{victim.id}/block", data={"block": "1", "reason": "мошенничество"})
        assert client.portal.call(client.app.state.service.get_user, victim.id).blocked_at is not None
        # действия администратора поддержке недоступны
        assert client.post(f"/admin/clients/{victim.id}/role", data={"role": "admin"}).status_code == 403
        assert client.post("/admin/backups/x.sql.gz/restore", data={}).status_code == 403
        # журнал
        page = client.get("/admin/staff").text
        assert "credits" in page and "block" in page and "компенсация" in page


async def test_blocked_user_cannot_use_bot(db, settings, service):
    from shturman.admin.service import AdminService
    from shturman.bot.max import MaxBot
    from shturman.orders import OrderService
    from tests.test_max import FakeAPI, msg, run

    u = await service.max_user(55)
    await AdminService(db, settings).set_blocked(u.id, True, "тест")
    api = FakeAPI()
    bot = MaxBot(api, service, OrderService(db, settings), settings)
    await run(bot, api, msg("X7TEST00000000001", uid=55))
    assert "заблокирован" in api.sent[-1]["text"] and len(api.sent) == 1


def test_backup_create_and_restore_sqlite(settings, tmp_path):
    settings.backup_dir = str(tmp_path / "backups")
    with make_client(settings) as client:
        uid, _ = staff_login(client)
        client.portal.call(client.app.state.service.max_user, 1001)  # данные до бэкапа
        r = client.post("/admin/actions/backup")
        assert "/admin/backups" in r.headers["location"]
        name = re.search(r"(shturman-[\w-]+\.sqlite\.gz)", client.get("/admin/backups").text).group(1)
        client.portal.call(client.app.state.service.max_user, 2002)  # появилось после бэкапа

        # без слова-подтверждения и с неверным кодом — отказ
        assert "ВОССТАНОВИТЬ" in client.post(f"/admin/backups/{name}/restore", data={"code": "1"}, follow_redirects=True).text
        assert "Неверный код" in client.post(f"/admin/backups/{name}/restore", data={"code": "000000", "confirm": "ВОССТАНОВИТЬ"},
                                             follow_redirects=True).text
        r = client.post(f"/admin/backups/{name}/restore",
                        data={"code": code_for(client, uid, +1), "confirm": "восстановить"})
        assert "восстановлена" in r.headers.get("location", "") or r.status_code == 303

        async def users():
            from sqlalchemy import select
            async with client.app.state.service.db.session() as s:
                return set((await s.execute(select(User.max_id))).scalars())
        ids = client.portal.call(users)
        assert 1001 in ids and 2002 not in ids
        files = sorted(p.name for p in (tmp_path / "backups").iterdir())
        assert any("before-restore" in f for f in files)
        # путь за пределы папки не принимается
        assert client.get("/admin/backups/..%2F..%2Fetc%2Fpasswd").status_code in (404, 303)


async def test_backup_file_not_world_readable(settings, tmp_path, db):
    import os
    import stat
    from shturman.admin.backups import create_backup
    settings.backup_dir = str(tmp_path / "b")
    settings.database_url = f"sqlite+aiosqlite:///{tmp_path}/x.db"
    from shturman.db import Database
    d = Database(settings.database_url)
    await d.create_all()
    await d.dispose()
    b = await create_backup(settings, "perm")
    mode = stat.S_IMODE(os.stat(tmp_path / "b" / b.name).st_mode)
    assert mode & 0o007 == 0  # «прочие» не читают и не пишут
