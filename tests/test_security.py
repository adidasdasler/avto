"""Тесты найденных при аудите уязвимостей и входа через Яндекс ID."""
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from shturman.oauth_yandex import INFO_URL, TOKEN_URL
from shturman.services import LimitExceeded
from tests.test_auth_email import register
from tests.test_cabinet import login_as, make_client


# ---------------------------------------------------------------- Robokassa
def _sig(out_sum, inv_id, pw="p2"):
    return hashlib.md5(f"{out_sum}:{inv_id}:{pw}".encode()).hexdigest()


def _rk_client(settings, **kw):
    return make_client(settings, robokassa_login="shop", robokassa_password1="p1", robokassa_password2="p2", **kw)


def _new_order(client, product="pack_10"):
    async def go():
        u = await client.app.state.service.max_user(31337)
        order, _ = await client.app.state.orders.create(u.id, product, check_id=None, platform="max", chat_id=1)
        return order.id, u.id
    return client.portal.call(go)


def test_result_url_rejects_forgery_and_wrong_amount(settings):
    with _rk_client(settings, robokassa_test=False) as client:
        oid, uid = _new_order(client)
        # подпись не тем паролем (паролем №1 из ссылки на оплату)
        r = client.post("/pay/robokassa/result", data={"OutSum": "850.00", "InvId": oid, "SignatureValue": _sig("850.00", oid, "p1")})
        assert r.status_code == 400
        # корректная подпись, но сумма меньше цены заказа
        r = client.post("/pay/robokassa/result", data={"OutSum": "1.00", "InvId": oid, "SignatureValue": _sig("1.00", oid)})
        assert r.status_code == 400
        # тестовый платёж при выключенном тестовом режиме
        r = client.post("/pay/robokassa/result", data={"OutSum": "850.00", "InvId": oid, "IsTest": "1",
                                                      "SignatureValue": _sig("850.00", oid)})
        assert r.status_code == 400
        assert client.portal.call(client.app.state.service.get_user, uid).credits == 0
        r = client.post("/pay/robokassa/result", data={"OutSum": "850.00", "InvId": oid, "SignatureValue": _sig("850.00", oid)})
        assert r.text == f"OK{oid}"
        r = client.post("/pay/robokassa/result", data={"OutSum": "850.00", "InvId": oid, "SignatureValue": _sig("850.00", oid)})
        assert r.text == f"OK{oid}"  # повтор не начисляет второй раз
        assert client.portal.call(client.app.state.service.get_user, uid).credits == 10


def test_payment_warnings_on_prod(settings):
    from shturman.api.app import payment_warnings
    settings.public_base_url = "https://vincv.ru"
    settings.robokassa_login, settings.robokassa_test, settings.dev_fake_payments = "shop", True, True
    w = payment_warnings(settings)
    assert any("ROBOKASSA_TEST" in x for x in w) and any("DEV_FAKE_PAYMENTS" in x for x in w)
    settings.robokassa_test = settings.dev_fake_payments = False
    assert payment_warnings(settings) == []


# ---------------------------------------------------------------- чужие проверки и пакеты
async def test_cannot_use_someone_elses_check(db, settings, service):
    from shturman.billing import apply_payment, products
    from shturman.bot.max import MaxBot
    from shturman.db import User
    from shturman.orders import OrderService
    from tests.test_max import FakeAPI, press, run

    victim = await service.max_user(1)
    check = await service.express("X7TEST00000000001", user=victim)
    attacker = await service.max_user(2)
    async with db.session() as s:
        await apply_payment(s, await s.get(User, attacker.id), products(settings)["pack_10"], check_id=None, charge_id="a")
        await s.commit()
    with pytest.raises(PermissionError):
        await service.upgrade_with_credit(attacker.id, check.id, "dossier")
    assert (await service.get_user(attacker.id)).credits == 10

    settings.dev_fake_payments = True
    api = FakeAPI()
    bot = MaxBot(api, service, OrderService(db, settings), settings)
    sid = check.id.replace("-", "")
    await run(bot, api, {"update_type": "bot_started", "chat_id": 2, "user": {"user_id": 2}, "payload": f"chk_{sid}"})
    assert "LADA" not in api.sent[-1]["text"]  # сводка чужой проверки не показана
    await run(bot, api, press(f"buy:dossier:{sid}", uid=2, chat=2))
    await run(bot, api, press(f"credit:{sid}", uid=2, chat=2))
    assert not api.files and (await service.get_check(check.id)).tier == "express"


async def test_credit_race_single_spend(db, settings, service):
    import asyncio
    from shturman.db import User
    u = await service.max_user(7)
    async with db.session() as s:
        (await s.get(User, u.id)).credits = 1
        await s.commit()
    checks = [await service.express(q, user=u, enforce_limit=False) for q in ("X7TEST00000000001", "Т000ТТ00")]
    res = await asyncio.gather(*(service.upgrade_with_credit(u.id, c.id) for c in checks), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in res) == 1
    assert (await service.get_user(u.id)).credits == 0


# ---------------------------------------------------------------- бесплатные проверки
async def test_free_limit_per_ip_across_accounts(db, settings, service):
    settings.free_checks_per_day, settings.free_checks_per_ip_per_day = 3, 4
    for i in range(2):
        u = await service.max_user(100 + i)
        for _ in range(2):
            await service.express("X7TEST00000000001", user=u, client_ip="6.6.6.6")
    fresh = await service.max_user(200)
    with pytest.raises(LimitExceeded, match="подключения"):
        await service.express("X7TEST00000000001", user=fresh, client_ip="6.6.6.6")
    await service.express("X7TEST00000000001", user=fresh)  # из бота (без IP) — свой лимит


async def test_free_global_budget(db, settings, service):
    settings.free_checks_global_per_day = 2
    await service.express("X7TEST00000000001", client_ip="1.1.1.1")
    await service.express("X7TEST00000000001", client_ip="2.2.2.2")
    with pytest.raises(LimitExceeded, match="закончились"):
        await service.express("X7TEST00000000001", client_ip="3.3.3.3")


# ---------------------------------------------------------------- сессии
def test_password_change_logs_out_other_sessions(settings):
    with make_client(settings) as a, make_client(settings) as _:
        register(a)
        other = a.portal.call(a.app.state.auth.create_session, 1)  # «чужая» сессия того же аккаунта
        r = a.post("/cabinet/credentials", data={"email": "ivan@example.com", "current": "secret-123",
                                                "password": "new-secret-1", "password2": "new-secret-1"})
        assert r.status_code == 303
        assert a.portal.call(a.app.state.auth.session_user, other) is None
        assert a.get("/cabinet").status_code == 200  # текущая сессия осталась


# ---------------------------------------------------------------- Яндекс ID
def _ya_client(settings):
    return make_client(settings, yandex_client_id="cid", yandex_client_secret="secret")


def _ya_login(client, ya_id="1001", email="ivan@yandex.ru", next_="/cabinet"):
    r = client.get(f"/auth/yandex?next={next_}")
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "oauth.yandex.ru"
    q = parse_qs(loc.query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"] == ["http://localhost:8000/auth/yandex/callback"]
    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "AT"}))
        info = respx.get(INFO_URL).mock(return_value=httpx.Response(200, json={
            "id": ya_id, "login": "ivan", "default_email": email, "emails": [email]}))
        cb = client.get(f"/auth/yandex/callback?code=c&state={q['state'][0]}")
        assert info.calls.last.request.headers["Authorization"] == "OAuth AT"
    return cb


def test_yandex_login_creates_and_reuses_account(settings):
    with _ya_client(settings) as client:
        cb = _ya_login(client)
        assert cb.status_code == 303 and cb.headers["location"] == "/cabinet"
        page = client.get("/cabinet").text
        assert "ivan@yandex.ru" in page and "подтверждена" in page and "Яндекс ID привязан" in page
        client.cookies.clear()
        _ya_login(client)
        assert "Номер в системе: 1" in client.get("/cabinet").text


def test_yandex_state_must_match(settings):
    with _ya_client(settings) as client:
        client.get("/auth/yandex")
        r = client.get("/auth/yandex/callback?code=c&state=forged", follow_redirects=True)
        assert "устарела" in r.text
        assert client.get("/cabinet").status_code == 303


def test_yandex_does_not_take_over_unverified_email(settings):
    with _ya_client(settings) as client:
        register(client, email="victim@yandex.ru")  # злоумышленник занял чужой адрес без подтверждения
        client.cookies.clear()
        _ya_login(client, email="victim@yandex.ru")
        page = client.get("/cabinet").text
        assert "Номер в системе: 2" in page and "victim@yandex.ru" in page  # владелец получил свой аккаунт
        client.cookies.clear()
        assert client.post("/login", data={"email": "victim@yandex.ru", "password": "secret-123"}).status_code == 400


def test_yandex_links_verified_email_and_merges_when_logged_in(settings):
    with _ya_client(settings) as client:
        register(client, email="ivan@yandex.ru")
        auth = client.app.state.auth
        user = client.portal.call(auth.user_by_email, "ivan@yandex.ru")
        client.portal.call(auth.mark_verified, user.id)
        client.cookies.clear()
        _ya_login(client)  # подтверждённый адрес — тот же аккаунт
        assert f"Номер в системе: {user.id}" in client.get("/cabinet").text

        # из кабинета MAX-аккаунта привязываем другой Яндекс ID — аккаунты объединяются
        client.cookies.clear()
        login_as(client, max_id=777)
        _ya_login(client, ya_id="2002", email="other@yandex.ru")
        page = client.get("/cabinet").text
        assert "Яндекс ID привязан" in page and "MAX привязан" in page


def test_yandex_not_configured(settings):
    with make_client(settings) as client:
        assert "Яндекс ID" not in client.get("/login").text
        assert "не подключён" in client.get("/auth/yandex", follow_redirects=True).text
