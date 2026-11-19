import json
from datetime import timedelta

import httpx
import respx

from shturman import alerts
from shturman.db import ProviderLog, SystemAlert, User, utcnow
from shturman.provider.client import ProviderResponse


async def add_logs(db, rows):
    """rows: (charged, balance, minutes_ago, error)"""
    async with db.session() as s:
        for charged, balance, ago, error in rows:
            s.add(ProviderLog(method="pledge", charged=charged, balance=balance, error=error,
                              created_at=utcnow() - timedelta(minutes=ago)))
        await s.commit()


def live(settings):
    settings.provider_mode = "live"
    settings.avc_token = "t"
    return settings


async def test_token_leak_detected(db, settings):
    live(settings)
    # наши запросы: 1000 → 990 (−10); затем баланс внезапно 900 — кто-то потратил 90 ₽ мимо нас
    await add_logs(db, [(0, 1000, 50, None), (10, 990, 40, None), (0, 900, 5, None)])
    assert await alerts.external_spend(db, timedelta(hours=1)) == 90
    found = {a.key: a for a in await alerts.evaluate(db, settings)}
    assert found["token_leak"].level == "critical" and "перевыпустите" in found["token_leak"].text


async def test_parallel_requests_no_false_alarm(db, settings):
    live(settings)
    # два параллельных запроса по 15 ₽ записаны «не по порядку»: сначала тот, что видел уже оба списания
    await add_logs(db, [(0, 1000, 30, None), (15, 970, 20, None), (15, 985, 19, None)])
    # крайние точки: 1000 → 985, наших списаний после первой точки 30 → «чужого» нет (в пределах шума)
    ext = await alerts.external_spend(db, timedelta(hours=1))
    assert ext <= 0
    assert "token_leak" not in {a.key for a in await alerts.evaluate(db, settings)}


async def test_top_up_is_not_alarm(db, settings):
    live(settings)
    await add_logs(db, [(0, 100, 30, None), (10, 1090, 10, None)])  # пополнили на 1000
    assert await alerts.external_spend(db, timedelta(hours=1)) < 0
    assert "token_leak" not in {a.key for a in await alerts.evaluate(db, settings)}


async def test_spend_balance_errors(db, settings):
    live(settings)
    settings.alert_spend_hour_rub, settings.alert_errors_per_hour = 100, 3
    await add_logs(db, [(60, 450, 20, None), (60, 390, 10, None)] + [(0, None, 5, "NETWORK")] * 3)
    keys = {a.key for a in await alerts.evaluate(db, settings)}
    assert {"spend_hour", "balance_low", "provider_errors"} <= keys


async def test_mock_mode_has_no_alerts(db, settings):
    await add_logs(db, [(0, 0, 10, None)])
    assert await alerts.evaluate(db, settings) == []


@respx.mock
async def test_dedupe_and_notify_staff(db, settings):
    live(settings)
    settings.max_bot_token = "MAXTOKEN"
    settings.alert_max_user_ids = "777"
    async with db.session() as s:
        s.add(User(max_id=888))  # обычный клиент — ему не пишем
        await s.commit()
    route = respx.post("https://platform-api.max.ru/messages").mock(return_value=httpx.Response(200, json={}))
    await add_logs(db, [(0, 100, 10, None)])  # баланс ниже 500
    fresh = await alerts.run(db, settings)
    assert [a.key for a in fresh] == ["balance_low"]
    req = route.calls.last.request
    assert req.url.params["user_id"] == "777" and req.headers["Authorization"] == "MAXTOKEN"
    assert "осталось 100" in json.loads(req.content)["text"]
    assert route.call_count == 1
    assert await alerts.run(db, settings) == []  # повтор не раньше чем через 3 часа
    async with db.session() as s:
        assert len((await s.execute(SystemAlert.__table__.select())).all()) == 1


async def test_probe_balance(db, settings):
    class P:
        async def call(self, method, **params):
            assert method == "decode" and params == {"vin": "X7TEST00000000001"}
            return ProviderResponse(method, params, {}, test_mode=True, charged=0, balance=481)
    assert await alerts.probe_balance(db, P()) == 481
    async with db.session() as s:
        rows = (await s.execute(ProviderLog.__table__.select())).all()
    assert rows[0].method == "balance-probe" and float(rows[0].balance) == 481


def test_alert_banner_in_panel(settings):
    from tests.test_admin import P, make_panel, staff_login
    with make_panel(settings) as client:
        staff_login(client)

        async def add():
            async with client.app.state.service.db.session() as s:
                s.add(SystemAlert(key="token_leak", level="critical", message="Возможно, токен утёк"))
                await s.commit()
        client.portal.call(add)
        assert "Возможно, токен утёк" in client.get(P).text
