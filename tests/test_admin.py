import re
import time

import pytest

from shturman import totp
from shturman.admin import accounts
from shturman.db import User
from tests.test_auth_email import register
from tests.test_cabinet import make_client

P = "/ops-test0123456789"
SECRETS = {"boss": totp.new_secret(), "help": totp.new_secret()}
PASSWORD = "very-long-pass-1"


def staff_env(settings, ips: str = "") -> None:
    """Два сотрудника в ADMIN_USERS, как их создаёт команда shturman.admin.cli add."""
    settings.admin_path = P
    settings.admin_allowed_ips = ips
    h = accounts.hash_password(PASSWORD)
    settings.admin_users = f"boss,admin,{h},{SECRETS['boss']};help,support,{h},{SECRETS['help']}"


def code(login: str, offset: int = 0) -> str:
    return totp.code_at(SECRETS[login], int(time.time() // 30) + offset)


def staff_login(client, login: str = "boss", offset: int = 0):
    r = client.post(P + "/login", data={"login": login, "password": PASSWORD, "code": code(login, offset)})
    assert r.status_code == 303 and r.headers["location"] == P, r.text
    return r


def make_panel(settings, **kw):
    staff_env(settings, **kw)
    return make_client(settings)


def test_panel_only_on_secret_path(settings):
    staff_env(settings)
    with make_client(settings) as client:
        assert client.get("/admin").status_code == 404
        assert client.get("/admin/login").status_code == 404
        assert client.get(P).headers["location"] == P + "/login"
        assert "Вход для сотрудников" in client.get(P + "/login").text
        assert "/admin" not in client.get("/robots.txt").text and P not in client.get("/robots.txt").text
        # аккаунт сайта — даже с тем же логином — в панель не пускает
        register(client, email="boss@vincv.ru")
        assert client.get(P + "/clients").headers["location"] == P + "/login"


def test_panel_disabled_without_path_or_users(settings):
    with make_client(settings) as client:  # ADMIN_PATH пуст — панели нет
        assert client.get("/admin").status_code == 404
    settings.admin_path = P
    with make_client(settings) as client:  # адрес есть, сотрудников нет — тоже 404
        assert client.get(P + "/login").status_code == 404


def test_login_requires_password_and_code(settings):
    with make_panel(settings) as client:
        bad = [{"login": "boss", "password": "wrong-password", "code": code("boss")},
               {"login": "boss", "password": PASSWORD, "code": "000000"},
               {"login": "nobody", "password": PASSWORD, "code": code("boss")}]
        for data in bad:
            r = client.post(P + "/login", data=data, follow_redirects=True)
            assert "Неверный логин, пароль или код" in r.text
        assert client.get(P).status_code == 303
        staff_login(client)
        page = client.get(P).text
        assert "Сводка" in page and "boss" in page
        cookie = next(c for c in client.cookies.jar if c.name == "vadm")
        assert cookie.path == P and cookie.has_nonstandard_attr("HttpOnly")
        # тот же код второй раз (перехвачен) — не принимается
        client.cookies.clear()
        r = client.post(P + "/login", data={"login": "boss", "password": PASSWORD, "code": code("boss")},
                        follow_redirects=True)
        assert "Неверный логин" in r.text
        # выход завершает сессию на сервере
        staff_login(client, offset=1)
        sid = client.cookies.get("vadm")
        client.post(P + "/logout")
        client.cookies.set("vadm", sid, path=P)
        assert client.get(P).status_code == 303


def test_bruteforce_lockout(settings):
    with make_panel(settings) as client:
        for _ in range(5):
            client.post(P + "/login", data={"login": "boss", "password": "x" * 12, "code": "111111"})
        r = client.post(P + "/login", data={"login": "boss", "password": PASSWORD, "code": code("boss")},
                        follow_redirects=True)
        assert "Слишком много" in r.text  # даже с верными данными — блок на 15 минут


def test_ip_allowlist(settings):
    with make_panel(settings, ips="10.0.0.0/8, 192.168.1.5") as client:  # TestClient ходит с «testclient»
        assert client.get(P + "/login").status_code == 404
        assert client.post(P + "/login", data={"login": "boss"}).status_code == 404
    with make_panel(settings, ips="testclient") as client:  # нераспознанный адрес — тоже мимо
        assert client.get(P + "/login").status_code == 404


def test_removed_user_session_dies(settings):
    with make_panel(settings) as client:
        staff_login(client)
        assert client.get(P).status_code == 200
        settings.admin_users = settings.admin_users.split(";")[1]  # boss убран из .env
        assert client.get(P).status_code == 303


def test_support_role_limits_and_client_actions(settings):
    with make_panel(settings) as client:
        staff_login(client, "help")
        victim = client.portal.call(client.app.state.service.max_user, 4242)
        client.portal.call(lambda: client.app.state.service.express("X7TEST00000000001", user=victim))
        # поиск по VIN и карточка
        assert f"{P}/clients/{victim.id}" in client.get(P + "/clients?q=X7TEST00000000001").text
        assert "LADA Vesta" in client.get(f"{P}/clients/{victim.id}").text
        # начисление и списание отчётов — с причиной
        client.post(f"{P}/clients/{victim.id}/credits", data={"delta": "5", "reason": "компенсация"})
        assert client.portal.call(client.app.state.service.get_user, victim.id).credits == 5
        r = client.post(f"{P}/clients/{victim.id}/credits", data={"delta": "-9", "reason": "ошибка"},
                        follow_redirects=True)
        assert "Нельзя списать больше" in r.text
        # блокировка
        client.post(f"{P}/clients/{victim.id}/block", data={"block": "1", "reason": "мошенничество"})
        assert client.portal.call(client.app.state.service.get_user, victim.id).blocked_at is not None
        # действия администратора поддержке недоступны
        assert client.post(P + "/backups/x.sql.gz/restore", data={}).status_code == 403
        assert client.get(P + "/backups/x.sql.gz").status_code == 403
        # журнал с логином сотрудника
        page = client.get(P + "/staff").text
        assert "credits" in page and "block" in page and "компенсация" in page and "boss" in page


def test_cli_add_writes_env(tmp_path, monkeypatch, capsys):
    from shturman.admin import cli
    env = tmp_path / ".env"
    env.write_text("BRAND_NAME=VinCv\nPUBLIC_BASE_URL=https://vincv.ru\n")
    monkeypatch.setattr(cli, "ENV", env)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
    secrets_seen = []
    orig = cli.show_qr
    monkeypatch.setattr(cli, "show_qr", lambda acc, brand: (secrets_seen.append(acc.totp_secret), orig(acc, brand)))
    monkeypatch.setattr("builtins.input", lambda prompt="": totp.code_at(secrets_seen[-1], int(time.time() // 30)))
    cli.main(["add", "Ivan", "--role", "admin"])
    out = capsys.readouterr().out
    assert "Код верный" in out and any(ch in out for ch in "█▀▄#")  # QR в терминале
    text = env.read_text()
    assert "ADMIN_PATH=/ops-" in text and PASSWORD not in text and "$" not in text
    users = accounts.parse(re.search(r"ADMIN_USERS=(.*)", text).group(1))
    assert users["ivan"].role == "admin" and accounts.check(users, "ivan", PASSWORD)
    assert accounts.check(users, "ivan", "wrong-password!") is None
    assert oct(env.stat().st_mode)[-3:] == "600"
    with pytest.raises(SystemExit):
        cli.main(["add", "ivan"])  # уже есть
    cli.main(["remove", "ivan"])
    assert accounts.parse(re.search(r"ADMIN_USERS=(.*)", env.read_text()).group(1)) == {}


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
    with make_panel(settings) as client:
        staff_login(client)
        client.portal.call(client.app.state.service.max_user, 1001)  # данные до бэкапа
        r = client.post(P + "/actions/backup")
        assert P + "/backups" in r.headers["location"]
        name = re.search(r"(shturman-[\w-]+\.sqlite\.gz)", client.get(P + "/backups").text).group(1)
        client.portal.call(client.app.state.service.max_user, 2002)  # появилось после бэкапа

        # без слова-подтверждения и с неверным кодом — отказ
        assert "ВОССТАНОВИТЬ" in client.post(f"{P}/backups/{name}/restore", data={"code": "1"}, follow_redirects=True).text
        assert "Неверный код" in client.post(f"{P}/backups/{name}/restore", data={"code": "000000", "confirm": "ВОССТАНОВИТЬ"},
                                             follow_redirects=True).text
        r = client.post(f"{P}/backups/{name}/restore",
                        data={"code": code("boss", +1), "confirm": "восстановить"})
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
        assert client.get(P + "/backups/..%2F..%2Fetc%2Fpasswd").status_code in (404, 303)


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
