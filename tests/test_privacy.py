"""152-ФЗ: согласия, выгрузка данных, удаление аккаунта, бот, документы."""
import json

from sqlalchemy import select

from shturman import privacy
from shturman.db import Check, ConsentLog, Order, Payment, User
from tests.test_auth_email import register
from tests.test_cabinet import PRO, login_as, make_client


def consents(client, user_id):
    async def q():
        async with client.app.state.service.db.session() as s:
            return [(c.kind, c.granted, c.source) for c in (await s.execute(
                select(ConsentLog).where(ConsentLog.user_id == user_id).order_by(ConsentLog.id))).scalars()]
    return client.portal.call(q)


def test_register_records_separate_consents(settings):
    with make_client(settings) as client:
        register(client, email="a@vincv.ru", marketing="on")
        user = client.portal.call(client.app.state.auth.user_by_email, "a@vincv.ru")
        assert consents(client, user.id) == [("offer", True, "web"), ("pd", True, "web"), ("marketing", True, "web")]
        assert user.pd_consent_version == privacy.PD_VERSION and user.marketing_consent_at
        client.cookies.clear()
        register(client, email="b@vincv.ru")  # без рекламы
        user = client.portal.call(client.app.state.auth.user_by_email, "b@vincv.ru")
        assert [k for k, _, _ in consents(client, user.id)] == ["offer", "pd"] and user.marketing_consent_at is None
        client.cookies.clear()
        page = client.get("/register").text
        for name in ('name="offer"', 'name="consent"', 'name="marketing"'):
            assert name in page
        assert "checked" not in page  # ничего не отмечено заранее


def test_new_document_version_requires_consent_again(settings, monkeypatch):
    with make_client(settings) as client:
        register(client)
        assert client.get("/cabinet").status_code == 200
        monkeypatch.setattr(privacy, "PD_VERSION", "2099-01-01")
        r = client.get("/cabinet/pro")
        assert r.status_code == 303 and r.headers["location"] == "/cabinet/consent?next=/cabinet/pro"
        assert "Обновили документы" in client.get("/cabinet/consent").text
        r = client.post("/cabinet/consent", data={"consent": "on", "next": "/cabinet/pro"})
        assert r.headers["location"] == "/cabinet/pro" and client.get("/cabinet/pro").status_code == 200


def test_marketing_toggle_and_export(settings):
    with make_client(settings) as client:
        user = login_as(client, max_id=900)
        client.portal.call(lambda: client.app.state.service.express("X7TEST00000000001", user=user))
        assert "не подписаны" in client.get("/cabinet").text
        client.post("/cabinet/marketing", data={"on": "1"})
        assert client.portal.call(client.app.state.service.get_user, user.id).marketing_consent_at
        client.post("/cabinet/marketing", data={"on": "0"})
        assert client.portal.call(client.app.state.service.get_user, user.id).marketing_consent_at is None
        r = client.get("/cabinet/export.json")
        assert "attachment" in r.headers["content-disposition"]
        data = json.loads(r.text)
        assert data["account"]["max_id"] == 900 and data["checks"][0]["vin"] == "X7TEST00000000001"
        assert [c["kind"] for c in data["consents"]] == ["pd", "marketing", "marketing"]


def test_delete_account(settings):
    with make_client(settings) as client:
        register(client, email="del@vincv.ru")
        user = client.portal.call(client.app.state.auth.user_by_email, "del@vincv.ru")
        client.post("/cabinet/pro", data={**PRO, "consent": "on"})
        svc = client.app.state.service

        async def seed():
            free = await svc.express("X7TEST00000000001", user=user)
            paid = await svc.express("X7TEST00000000001", user=user, enforce_limit=False)
            async with svc.db.session() as s:
                s.add(Order(user_id=user.id, product="basic", check_id=paid.id, amount_rub=199, status="paid",
                            platform="web"))
                s.add(Payment(user_id=user.id, product="basic", check_id=paid.id, amount_rub=199,
                              provider="robokassa", provider_charge_id="x1"))
                await s.commit()
            return free.id, paid.id
        free_id, paid_id = client.portal.call(seed)

        assert "УДАЛИТЬ" in client.post("/cabinet/delete", data={"confirm": "нет", "password": "secret-123"},
                                        follow_redirects=True).text
        assert "Пароль указан неверно" in client.post("/cabinet/delete", data={"confirm": "удалить", "password": "x"},
                                                      follow_redirects=True).text
        r = client.post("/cabinet/delete", data={"confirm": "удалить", "password": "secret-123"})
        assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
        assert client.get("/cabinet").status_code == 303  # сессия завершена

        async def state():
            async with svc.db.session() as s:
                u = await s.get(User, user.id)
                checks = {c.id: c.user_id for c in (await s.execute(select(Check))).scalars()}
                pays = list((await s.execute(select(Payment))).scalars())
                return u, checks, pays
        u, checks, pays = client.portal.call(state)
        assert u.deleted_at and u.email is None and u.password_hash is None and u.pd_consent_at is None
        assert free_id not in checks and checks[paid_id] is None  # без оплаты — удалена, с оплатой — обезличена
        assert len(pays) == 1 and pays[0].user_id == user.id  # учёт оплат сохраняется
        assert client.portal.call(client.app.state.pros.by_user, user.id) is None
        assert ("pd", False, "delete") in consents(client, user.id)
        # тот же адрес можно зарегистрировать заново, старый пароль не входит
        assert client.post("/login", data={"email": "del@vincv.ru", "password": "secret-123"}).status_code == 400
        assert register(client, email="del@vincv.ru").status_code == 303


def test_pro_public_consent_and_delete(settings):
    with make_client(settings) as client:
        user = login_as(client, max_id=901)
        assert "/consent-public" in client.get("/cabinet/pro").text
        client.post("/cabinet/pro", data={**PRO, "consent": "on"})
        client.post("/cabinet/pro/delete")
        assert client.portal.call(client.app.state.pros.by_user, user.id) is None
        assert consents(client, user.id)[-2:] == [("public", True, "web"), ("public", False, "web")]


def test_legal_documents_and_cookie_notice(settings):
    settings.legal_name, settings.legal_inn, settings.contact_email = "ИП Иванов И. И.", "770000000000", "pd@vincv.ru"
    with make_client(settings) as client:
        for path, words in [("/consent", "Перечень данных"), ("/consent-marketing", "38-ФЗ"),
                            ("/consent-public", "10.1"), ("/privacy", "Трансграничная передача")]:
            page = client.get(path).text
            assert words in page and "ИП Иванов И. И." in page and "pd@vincv.ru" in page, path
        assert "cookie-notice" in client.get("/").text and "cookie-notice" in client.get("/pros").text
        assert "/consent" in client.get("/sitemap.xml").text


def test_admin_deletes_on_request(settings):
    from tests.test_admin import P, staff_env, staff_login
    staff_env(settings)
    with make_client(settings) as client:
        victim = client.portal.call(client.app.state.service.max_user, 4343)
        staff_login(client, "help")
        assert client.post(f"{P}/clients/{victim.id}/delete", data={"confirm": "УДАЛИТЬ", "reason": "x"}).status_code == 403
        client.cookies.clear()
        staff_login(client, "boss")
        client.post(f"{P}/clients/{victim.id}/delete", data={"confirm": "УДАЛИТЬ", "reason": "письмо 01.12"})
        assert client.portal.call(client.app.state.service.get_user, victim.id).deleted_at
        assert "аккаунт удалён" in client.get(f"{P}/clients/{victim.id}").text
        assert "pd_delete" in client.get(P + "/staff").text


async def test_max_bot_asks_consent_first(db, settings, service):
    from shturman.bot.max import MaxBot
    from shturman.orders import OrderService
    from tests.test_max import FakeAPI, msg, press, run

    settings.bot_consent_required = True
    api = FakeAPI()
    bot = MaxBot(api, service, OrderService(db, settings), settings)
    await run(bot, api, msg("X7TEST00000000001", uid=77))
    assert "согласие на обработку персональных данных" in api.sent[-1]["text"] and len(api.sent) == 1
    async with db.session() as s:
        assert (await s.execute(select(Check))).first() is None  # без согласия ничего не проверяем
    data = api.sent[-1]["buttons"][0][0].data
    assert data == "consent:yes:"
    await run(bot, api, msg("/start chk_abc", uid=78))
    assert api.sent[-1]["buttons"][1][0].data == "consent:mkt:chk_abc"  # ссылку выполним после согласия
    await run(bot, api, press("consent:yes:", uid=77))
    user = await service.max_user(77)
    assert privacy.has_pd_consent(user) and user.marketing_consent_at is None
    await run(bot, api, msg("X7TEST00000000001", uid=77))
    async with db.session() as s:
        assert (await s.execute(select(Check))).first() is not None

    # /privacy → отписка/подписка → удаление
    await run(bot, api, msg("/privacy", uid=77))
    assert "Мои данные" in api.sent[-1]["text"]
    await run(bot, api, press("privacy:mkt_on", uid=77))
    assert (await service.get_user(user.id)).marketing_consent_at
    await run(bot, api, press("privacy:delete", uid=77))
    assert api.sent[-1]["buttons"][0][0].data == "privacy:delete_yes"
    await run(bot, api, press("privacy:delete_yes", uid=77))
    assert "данные удалены" in api.sent[-1]["text"]
    assert (await service.get_user(user.id)).deleted_at
    await run(bot, api, msg("привет", uid=77))  # новый человек с тем же ID — снова спрашиваем согласие
    assert "согласие" in api.sent[-1]["text"] and (await service.max_user(77)).id != user.id
