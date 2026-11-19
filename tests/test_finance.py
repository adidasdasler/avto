from datetime import date, timedelta

from shturman.admin import finance
from shturman.db import Check, Expense, Payment, ProviderLog, User, utcnow
from tests.test_admin import P, make_panel, staff_login


async def seed(db):
    now = utcnow()
    async with db.session() as s:
        u1, u2 = User(), User()
        s.add_all([u1, u2])
        await s.flush()
        paid = Check(user_id=u1.id, source="web", query="X", kind="vin", vin="X7TEST00000000001", tier="dossier")
        free = Check(user_id=u2.id, source="web", query="X", kind="vin", vin="X7TEST00000000001", tier="express")
        s.add_all([paid, free])
        await s.flush()
        s.add_all([
            Payment(user_id=u1.id, product="dossier", amount_rub=399, provider="robokassa", provider_charge_id="a"),
            Payment(user_id=u1.id, product="pack_10", amount_rub=850, provider="robokassa", provider_charge_id="b"),
            Payment(user_id=u2.id, product="dossier", amount_rub=399, provider="dev", provider_charge_id="c"),  # не деньги
            ProviderLog(check_id=paid.id, method="dossier", charged=60),
            ProviderLog(check_id=free.id, method="express", charged=5),
            ProviderLog(check_id=None, method="balance-probe", charged=0),
        ])
        u1.credits = 9
        await s.commit()
    return now


async def test_pnl_numbers(db, settings):
    await seed(db)
    await finance.set_config(db, 4.0, "usn6", None)
    today = finance.today_msk()
    m = finance.month_start(today)
    await finance.add_entry(db, kind="expense", category="hosting", amount=1000, spent_on=m, monthly=True,
                            until=None, note="VDS", login="boss")
    await finance.add_entry(db, kind="income", category="pros", amount=100, spent_on=today, monthly=False,
                            until=None, note=None, login="boss")
    p = await finance.pnl(db, settings, m, finance.add_months(m, 1))
    assert p.sales == 1249 and p.payments == 2 and p.paying_users == 1
    assert p.by_product == {"dossier": [1, 399.0], "pack_10": [1, 850.0]}
    assert p.acquiring == round(1249 * 0.04, 2)
    assert p.provider_paid == 60 and p.provider_free == 5
    assert p.gross == 1249 - p.acquiring - 60
    assert p.opex == 1000 and p.other_income == 100
    assert p.tax == round(1349 * 0.06, 2)
    assert round(p.net, 2) == round(p.gross + 100 - 5 - 1000 - p.tax, 2)
    assert p.full_checks == 1 and p.free_checks == 1 and p.cost_full == 60
    # УСН 15 %: с прибыли, но не меньше 1 % доходов
    await finance.set_config(db, 4.0, "usn15", None)
    p = await finance.pnl(db, settings, m, finance.add_months(m, 1))
    assert p.tax == round(max((1349 - p.acquiring - 65 - 1000) * 0.15, 13.49), 2)
    o = await finance.overview(db, settings, m, finance.add_months(m, 1))
    assert o["fixed"] == 1000 and o["credits_left"] == 9 and o["liability"] == 9 * 85
    assert o["breakeven"] and len(o["series"]) == 12


def test_monthly_occurrences():
    e = Expense(kind="expense", category="hosting", amount_rub=500, spent_on=date(2026, 1, 31), monthly=True,
                until=date(2026, 3, 1))
    assert finance.occurrences(e, date(2026, 1, 1), date(2026, 2, 1)) == 1
    assert finance.occurrences(e, date(2026, 2, 1), date(2026, 3, 1)) == 1  # 28 февраля
    assert finance.occurrences(e, date(2026, 3, 1), date(2026, 4, 1)) == 1  # март — последний
    assert finance.occurrences(e, date(2026, 4, 1), date(2026, 5, 1)) == 0
    assert finance.occurrences(e, date(2026, 1, 1), date(2027, 1, 1)) == 3
    one = Expense(kind="expense", category="ads", amount_rub=1, spent_on=date(2026, 5, 5), monthly=False)
    assert finance.occurrences(one, date(2026, 5, 1), date(2026, 6, 1)) == 1
    assert finance.occurrences(one, date(2026, 6, 1), date(2026, 7, 1)) == 0
    future = Expense(kind="expense", category="ads", amount_rub=1, spent_on=finance.today_msk() + timedelta(days=40),
                     monthly=True)
    assert finance.occurrences(future, date(2000, 1, 1), date(2100, 1, 1)) == 0  # ещё не наступило


def test_finance_page_admin_only(settings):
    with make_panel(settings) as client:
        staff_login(client, "help")
        assert client.get(P + "/finance").status_code == 403
        assert "Финансы" not in client.get(P).text
        client.cookies.clear()
        staff_login(client, "boss")
        client.portal.call(seed, client.app.state.service.db)
        page = client.get(P + "/finance").text
        assert "Отчёт о прибылях и убытках" in page and "1 249 ₽" in page and "Точка безубыточности" in page
        r = client.post(P + "/finance/entries", data={"kind": "expense", "category": "ads", "amount": "2 500,50",
                                                      "spent_on": finance.today_msk().isoformat(), "note": "Директ"},
                        follow_redirects=True)
        assert "Добавлено" in r.text and "Директ" in r.text
        assert "Не добавлено" in client.post(P + "/finance/entries", data={"kind": "expense", "category": "x",
                                                                            "amount": "1"}, follow_redirects=True).text
        client.post(P + "/finance/settings", data={"acquiring_pct": "3,5", "tax_mode": "npd", "tax_pct": ""})
        cfg = client.portal.call(finance.get_config, client.app.state.service.db)
        assert cfg == {"acquiring_pct": 3.5, "tax_mode": "npd", "tax_pct": 4.0}
        for q in ("?month=2025-01", "?date_from=2026-01-10&date_to=2026-01-01", "?month=bad"):
            assert client.get(P + "/finance" + q).status_code == 200
        csv = client.get(P + "/finance.csv")
        assert csv.status_code == 200 and "Чистая прибыль" in csv.text and len(csv.text.splitlines()) == 25
        entry = client.portal.call(finance.entries, client.app.state.service.db)[0]
        client.post(f"{P}/finance/entries/{entry.id}", data={"action": "delete"})
        assert client.portal.call(finance.entries, client.app.state.service.db) == []
        assert "finance_expense" in client.get(P + "/staff").text
