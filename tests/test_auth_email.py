import asyncio
import re
import sqlite3

from shturman.auth import AuthService
from shturman.db import Database
from tests.test_cabinet import login_as, make_client


def register(client, email="ivan@example.com", password="secret-123", **extra):
    data = {"email": email, "password": password, "password2": password, "offer": "on", "consent": "on",
            **extra}
    return client.post("/register", data=data)


def test_register_login_logout(settings):
    with make_client(settings) as client:
        r = register(client)
        assert r.status_code == 303 and r.headers["location"].startswith("/cabinet")
        page = client.get("/cabinet").text
        assert "ivan@example.com" in page and "не подтверждена" in page
        client.post("/logout")
        assert client.get("/cabinet").status_code == 303
        bad = client.post("/login", data={"email": "ivan@example.com", "password": "wrong-pass"})
        assert bad.status_code == 400 and "Неверная почта или пароль" in bad.text
        ok = client.post("/login", data={"email": " IVAN@example.com ", "password": "secret-123", "next": "/pros"})
        assert ok.status_code == 303 and ok.headers["location"] == "/pros"
        evil = client.post("/login", data={"email": "ivan@example.com", "password": "secret-123", "next": "//evil.example"})
        assert evil.headers["location"] == "/cabinet"


def test_register_validation(settings):
    with make_client(settings) as client:
        assert "не совпадают" in register(client, password2="other-123").text
        assert "условия оферты" in client.post("/register", data={"email": "a@b.ru", "password": "secret-123",
                                                                   "password2": "secret-123"}).text
        assert "Нужно согласие" in client.post("/register", data={"email": "a@b.ru", "password": "secret-123",
                                                                    "password2": "secret-123", "offer": "on"}).text
        assert "не короче 8" in register(client, password="short").text
        assert "Проверьте адрес" in register(client, email="not-an-email").text
        register(client)
        client.cookies.clear()
        assert "уже зарегистрирован" in register(client).text


def test_login_rate_limit(settings):
    with make_client(settings) as client:
        codes = [client.post("/login", data={"email": "x@y.ru", "password": "nope-nope"}).status_code
                 for _ in range(11)]
        assert codes[-1] == 429 and codes[0] == 400


def test_verify_and_reset_by_token(settings):
    with make_client(settings) as client:
        register(client)
        auth: AuthService = client.app.state.auth
        user = client.portal.call(auth.user_by_email, "ivan@example.com")
        t = client.portal.call(auth.new_token, "verify", user.id)
        assert "подтверждён" in client.get(f"/verify?t={t}", follow_redirects=True).text
        assert "устарела" in client.get(f"/verify?t={t}", follow_redirects=True).text

        # без SMTP восстановление по почте недоступно — подсказываем вход через бота
        assert "недоступно" in client.get("/forgot").text
        rt = client.portal.call(auth.new_token, "reset", user.id)
        client.cookies.clear()
        assert "Новый пароль" in client.get(f"/reset?t={rt}").text
        r = client.post("/reset", data={"token": rt, "password": "new-secret-9", "password2": "new-secret-9"})
        assert r.status_code == 303
        client.cookies.clear()
        assert client.post("/login", data={"email": "ivan@example.com", "password": "secret-123"}).status_code == 400
        assert client.post("/login", data={"email": "ivan@example.com", "password": "new-secret-9"}).status_code == 303
        assert "устарела" in client.get(f"/reset?t={rt}").text


def test_forgot_with_smtp_does_not_reveal_accounts(settings, monkeypatch):
    sent = []

    async def fake_send(settings, to, subject, text):
        sent.append((to, text))
        return True
    import shturman.mailer as mailer
    monkeypatch.setattr(mailer, "send", fake_send)
    with make_client(settings, smtp_host="smtp.example", smtp_from="noreply@vincv.ru") as client:
        register(client)
        assert sent and "/verify?t=" in sent[-1][1]
        client.cookies.clear()
        a = client.post("/forgot", data={"email": "ivan@example.com"}).text
        b = client.post("/forgot", data={"email": "nobody@example.com"}).text
        assert "Если этот адрес зарегистрирован" in a and "Если этот адрес зарегистрирован" in b
        assert len(sent) == 2 and "/reset?t=" in sent[-1][1]


def test_messenger_user_adds_email_and_changes_password(settings):
    with make_client(settings) as client:
        login_as(client, max_id=500)
        r = client.post("/cabinet/credentials", data={"email": "max@example.com", "password": "secret-123",
                                                     "password2": "secret-123"})
        assert "#security" in r.headers["location"]
        assert "max@example.com" in client.get("/cabinet").text
        # смена пароля требует текущий
        r = client.post("/cabinet/credentials", data={"email": "max@example.com", "current": "bad",
                                                     "password": "x" * 9, "password2": "x" * 9})
        assert "%D0%BD%D0%B5%D0%B2%D0%B5%D1%80%D0%BD%D0%BE" in r.headers["location"]  # «неверно»
        client.cookies.clear()
        assert client.post("/login", data={"email": "max@example.com", "password": "secret-123"}).status_code == 303


async def test_bind_max_merges_accounts(db, settings, service):
    from shturman.billing import apply_payment, products
    from shturman.bot.max import MaxBot
    from shturman.db import User
    from shturman.orders import OrderService
    from tests.test_max import FakeAPI, press, run

    auth = AuthService(db)
    web = await auth.register("web@example.com", "secret-123")
    await service.express("X7TEST00000000001", user=web)
    bot_user = await service.max_user(900)
    await service.express("Т000ТТ00", user=bot_user)
    async with db.session() as s:
        await apply_payment(s, await s.get(User, bot_user.id), products(settings)["pack_10"], check_id=None,
                            charge_id="b1")
        await s.commit()

    api = FakeAPI()
    bot = MaxBot(api, service, OrderService(db, settings), settings)
    token = await auth.new_token("bind", web.id)
    await run(bot, api, {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 900}, "payload": f"bind_{token}"})
    assert "web@example.com" in api.sent[-1]["text"]
    await run(bot, api, press(api.buttons()[0].data, uid=900, chat=1))
    assert "привязан" in api.sent[-1]["text"]

    merged = await service.get_user(web.id)
    assert merged.max_id == 900 and merged.credits == 10 and merged.email == "web@example.com"
    assert await service.get_user(bot_user.id) is None
    assert len(await service.history(web.id)) == 2
    assert (await service.max_user(900)).id == web.id  # дальше бот видит объединённый аккаунт


def test_legal_pages_robots_sitemap(settings):
    with make_client(settings, legal_name="Иванов Иван Иванович", legal_inn="771234567890",
                     contact_email="support@vincv.ru", legal_status="Самозанятый") as client:
        offer = client.get("/offer").text
        assert "Публичная оферта" in offer and "771234567890" in offer and "Досье — 399 ₽" in offer
        assert "Робокасса" in offer and "Возврат" in offer
        assert "152-ФЗ" in client.get("/privacy").text
        assert "support@vincv.ru" in client.get("/contacts").text
        assert "Disallow: /cabinet" in client.get("/robots.txt").text
        assert "<loc>http://localhost:8000/offer</loc>" in client.get("/sitemap.xml").text
        assert 'href="/offer"' in client.get("/").text


def test_legal_missing_requisites_visible(settings):
    with make_client(settings) as client:
        assert "LEGAL_INN" in client.get("/offer").text


def test_migration_adds_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, tg_id BIGINT, max_id BIGINT, username VARCHAR(64), "
                "role VARCHAR(16), legal_status VARCHAR(16), credits INTEGER, unlimited_until DATETIME, created_at DATETIME)")
    con.execute("INSERT INTO users (id, max_id, role, credits) VALUES (1, 77, 'buyer', 5)")
    con.commit()
    con.close()

    async def go():
        db = Database(f"sqlite+aiosqlite:///{path}")
        await db.create_all()
        await db.create_all()  # повторно — без ошибок
        await db.dispose()
    asyncio.run(go())
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("pragma table_info(users)")]
    assert {"email", "password_hash", "email_verified_at"} <= set(cols)
    assert list(con.execute("select credits from users where id=1")) == [(5,)]
    unique = {r[1]: r[2] for r in con.execute("pragma index_list(users)")}
    assert unique.get("ix_users_email") == 1
