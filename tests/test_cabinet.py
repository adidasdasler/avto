import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from shturman.api.app import create_app
from shturman.db import CacheEntry, Check, utcnow
from shturman.maintenance import cleanup
from shturman.provider import MockProvider


def make_client(settings, **kw):
    settings.max_bot_url = "https://max.ru/shturman_bot"
    for k, v in kw.items():
        setattr(settings, k, v)
    return TestClient(create_app(settings, MockProvider(), run_background=False), follow_redirects=False)


def login_as(client, max_id=100):
    """Вход как через бота: страница /login → подтверждение в боте → опрос."""
    page = client.get("/login").text
    token = re.search(r"/auth/poll\?t=([A-Za-z0-9_-]+)", page).group(1)
    assert f"https://max.ru/shturman_bot?start=login_{token}" in page
    assert client.get(f"/auth/poll?t={token}").json() == {"ok": False}
    app = client.app

    async def confirm():
        from shturman import privacy
        user = await app.state.service.max_user(max_id)
        if not privacy.has_pd_consent(user):  # в боте согласие дают до входа на сайт
            await privacy.record(app.state.service.db, user.id, "pd", True, source="max")
        assert await app.state.auth.bind_site_token(token, user.id)
        return await app.state.service.get_user(user.id)
    user = client.portal.call(confirm)
    assert client.get(f"/auth/poll?t={token}").json() == {"ok": True}
    assert client.get(f"/auth/poll?t={token}").json() == {"ok": False}  # одноразовый
    return user


def test_login_and_cabinet(settings):
    with make_client(settings) as client:
        r = client.get("/cabinet")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        user = login_as(client)
        page = client.get("/cabinet").text
        assert "Личный кабинет" in page and f"Номер в системе: {user.id}" in page
        # проверка с сайта под сессией попадает в кабинет
        assert client.post("/api/check", json={"query": "X7TEST00000000001"}).json()["cabinet"] is True
        assert "LADA Vesta" in client.get("/cabinet").text
        r = client.post("/logout", headers={"origin": "http://testserver"})
        assert r.status_code == 303
        assert client.get("/cabinet").status_code == 303


def test_link_token_and_open_redirect(settings):
    with make_client(settings) as client:
        async def token():
            u = await client.app.state.service.max_user(7)
            return await client.app.state.auth.new_link_token(u.id)
        t = client.portal.call(token)
        r = client.get(f"/auth/link?t={t}")
        assert r.status_code == 303 and r.headers["location"] == "/cabinet" and "sid=" in r.headers["set-cookie"]
        client.cookies.clear()
        assert "устарела" in client.get(f"/auth/link?t={t}", follow_redirects=True).text
        page = client.get("/login?next=//evil.example/x").text
        assert '"//evil.example' not in page and '"/cabinet"' in page


def test_csrf_origin_check(settings):
    with make_client(settings) as client:
        login_as(client)
        r = client.post("/cabinet/buy", data={"product": "pack_10"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403


def test_buy_from_cabinet_dev_payments(settings):
    with make_client(settings, dev_fake_payments=True) as client:
        user = login_as(client)
        cid = client.post("/api/check", json={"query": "X7TEST00000000001"}).json()["id"]
        r = client.post("/cabinet/buy", data={"product": "dossier", "check_id": cid})
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        check = client.portal.call(client.app.state.service.get_check, cid)
        assert check.tier == "dossier"
        client.post("/cabinet/buy", data={"product": "pack_10"})
        assert client.portal.call(client.app.state.service.get_user, user.id).credits == 10
        cid2 = client.post("/api/check", json={"query": "Т000ТТ00"}).json()["id"]
        client.post("/cabinet/credit", data={"check_id": cid2})
        assert client.portal.call(client.app.state.service.get_check, cid2).tier == "dossier"


def test_buy_requires_payments(settings):
    with make_client(settings) as client:
        login_as(client)
        r = client.post("/cabinet/buy", data={"product": "pack_10"})
        assert "%D0%BD%D0%B5" in r.headers["location"] or "msg=" in r.headers["location"]


PRO = {"kind": "selector", "name": "Иван Подбор", "city": "москва", "about": "Подбираю немецкие авто",
       "services": "Подбор под ключ — от 15 000 ₽\nДиагностика — 3 500 ₽", "phone": "+7 (999) 123-45-67",
       "telegram": "@ivan_podbor", "experience_years": "7"}


def test_pro_profile_moderation_catalog(settings):
    from tests.test_admin import staff_env
    staff_env(settings)
    with make_client(settings) as client:
        owner = login_as(client, max_id=200)
        r = client.post("/cabinet/pro", data=PRO)  # без согласия
        assert r.status_code == 200 and "согласие" in r.text
        r = client.post("/cabinet/pro", data={**PRO, "consent": "on"})
        assert r.status_code == 303
        assert "На модерации" in client.get("/cabinet/pro").text
        assert "Иван Подбор" not in client.get("/pros").text
        slug = client.portal.call(client.app.state.pros.by_user, owner.id).slug
        assert slug.startswith("ivan-podbor-moskva")
        assert client.get(f"/pros/{slug}").status_code == 200  # владельцу видно

        client.cookies.clear()
        assert client.get(f"/pros/{slug}").status_code == 404  # остальным — нет
        from tests.test_admin import P, staff_login
        staff_login(client, "help")
        assert "Иван Подбор" in client.get(P + "/pros").text
        client.post(f"{P}/pros/{client.portal.call(client.app.state.pros.by_user, owner.id).id}",
                    data={"action": "approve"})
        client.cookies.clear()

        assert "Иван Подбор" in client.get("/pros?q=НЕМЕЦКИЕ").text
        cat = client.get("/pros?city=москва").text
        assert "Иван Подбор" in cat and "Подбор под ключ" in cat
        assert "Иван Подбор" not in client.get("/pros?city=Казань").text
        page = client.get(f"/pros/{slug}").text
        assert "+79991234567" not in page  # контакты не в HTML
        c = client.post(f"/pros/{slug}/contacts").json()
        assert c["phone"] == "+79991234567" and c["telegram"] == "ivan_podbor"
        stats = client.portal.call(client.app.state.pros.stats, client.portal.call(client.app.state.pros.by_slug, slug).id)
        assert stats == {"views": 1, "contacts": 1}


def test_admin_forbidden_for_users(settings):
    with make_client(settings) as client:
        login_as(client)
        assert client.get("/admin").status_code == 404


async def test_cleanup(db, settings, service):
    old = utcnow() - timedelta(days=40)
    anon = await service.express("X7TEST00000000001", client_ip="1.1.1.1")
    user = await service.max_user(1)
    mine = await service.express("X7TEST00000000001", user=user)
    async with db.session() as s:
        for cid in (anon.id, mine.id):
            (await s.get(Check, cid)).created_at = old
        s.add(CacheEntry(key="old", method="taxi", payload={}, expires_at=old))
        await s.commit()
    out = await cleanup(db, settings)
    assert out["anon_checks"] == 1 and out["cache"] == 1
    assert await service.get_check(anon.id) is None and await service.get_check(mine.id) is not None


async def test_max_bot_login(db, settings, service):
    from shturman.auth import AuthService
    from shturman.bot.max import MaxBot
    from shturman.orders import OrderService
    from tests.test_max import FakeAPI, msg, press, run

    api = FakeAPI()
    bot = MaxBot(api, service, OrderService(db, settings), settings)
    token = await AuthService(db).new_site_token()
    await run(bot, api, {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 55}, "payload": f"login_{token}"})
    assert "Подтвердите" in api.sent[-1]["text"]
    await run(bot, api, press(api.buttons()[0].data, uid=55, chat=1))
    assert "Готово" in api.sent[-1]["text"]
    assert await AuthService(db).poll_site_token(token)
    await run(bot, api, msg("/cabinet", uid=55, chat=1))
    assert "/auth/link?t=" in api.sent[-1]["text"]


def test_search_and_city_normalization():
    from shturman.pros import ProForm, normalize_city
    assert normalize_city("  санкт-петербург ") == "Санкт-Петербург"
    data = ProForm(kind="inspector", name="Диагност", city="казань", services="Толщиномер — 1 000 ₽",
                   phone="89991234567", consent=True).clean()
    assert data["city"] == "Казань" and "толщиномер" in data["search_text"]


def test_unlock_kind_in_catalog(settings):
    from tests.test_admin import P, staff_env, staff_login
    staff_env(settings)
    with make_client(settings) as client:
        owner = login_as(client, max_id=300)
        form = client.get("/cabinet/pro").text
        assert "Аварийное вскрытие авто" in form and "unlock-hint" in form
        client.post("/cabinet/pro", data={**PRO, "kind": "unlock", "name": "Мастер Ключ", "consent": "on"})
        pid = client.portal.call(client.app.state.pros.by_user, owner.id).id
        client.cookies.clear()
        staff_login(client, "help")
        assert "проверяет документы владельца" in client.get(P + "/pros").text
        client.post(f"{P}/pros/{pid}", data={"action": "approve"})
        client.cookies.clear()
        assert "Мастер Ключ" in client.get("/pros?kind=unlock").text
        assert "Мастер Ключ" not in client.get("/pros?kind=selector").text


def test_all_kinds_listed_and_keys_sensitive(settings):
    from shturman.pros import KIND_GROUPS, KINDS, SENSITIVE_KINDS
    assert all(len(k) <= 16 for k in KINDS) and SENSITIVE_KINDS <= set(KINDS)
    assert sum(len(g) for g in KIND_GROUPS.values()) == len(KINDS)
    with make_client(settings) as client:
        page = client.get("/pros").text
        for title in ("Эвакуатор", "Автоюрист", "Изготовление автоключей", "Детейлинг и предпродажная подготовка"):
            assert title in page
        assert '<optgroup label="Помощь на дороге">' in page
        login_as(client, max_id=301)
        assert 'data-sensitive="keys unlock"' in client.get("/cabinet/pro").text
