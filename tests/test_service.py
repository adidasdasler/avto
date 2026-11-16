import pytest

from shturman.billing import apply_payment, products
from shturman.db import ProviderLog, User
from shturman.provider import MockProvider
from shturman.services import CheckService, LimitExceeded
from sqlalchemy import select


async def test_express_by_plate_resolves_vin(service, provider):
    c = await service.express("Т000ТТ00", client_ip="1.1.1.1")
    assert c.vin == "X7TEST00000000001" and c.tier == "express"
    called = [m for m, _ in provider.calls]
    assert called[0] == "gos2vin"
    assert set(called) == {"gos2vin", "decode", "pledge", "taxi", "elpts", "accidents"}
    assert c.overall == "bad"
    # ПДн не сохраняются
    assert "Тестович" not in str(c.sections)


async def test_free_limit(service):
    await service.express("X7TEST00000000001", client_ip="2.2.2.2")
    await service.express("X7TEST00000000001", client_ip="2.2.2.2")
    with pytest.raises(LimitExceeded):
        await service.express("X7TEST00000000001", client_ip="2.2.2.2")
    await service.express("X7TEST00000000001", client_ip="3.3.3.3")


async def test_upgrade_uses_cache_and_gates(service, provider):
    c = await service.express("X7TEST00000000001", client_ip="1.1.1.1")
    n_express = len(provider.calls)
    c = await service.upgrade(c.id, "dossier_plus")
    new_calls = [m for m, _ in provider.calls[n_express:]]
    assert "pledge" not in new_calls  # уже есть в проверке
    assert "customs" not in new_calls  # гейт: VIN на X — отечественная сборка
    assert c.sections["customs"]["skipped"]
    assert ("fines", {"sts": "0000000000"}) in provider.calls[n_express:]
    assert ("accidents-rt", {"task": "TEST-TASK-0001"}) in provider.calls
    assert c.sections["accidents-rt"]["data"]["status"] == "done"


async def test_cache_shared_between_checks(service, provider):
    await service.express("X7TEST00000000001", client_ip="1.1.1.1")
    n = len(provider.calls)
    await service.express("X7TEST00000000001", client_ip="9.9.9.9")
    assert len(provider.calls) == n


async def test_not_allowed_method_is_reported(db, settings):
    svc = CheckService(db, MockProvider(not_allowed={"taxi"}), settings, accidents_poll_interval=0)
    c = await svc.express("X7TEST00000000001", client_ip="1.1.1.1")
    taxi = next(f for f in c.flags if f["method"] == "taxi")
    assert taxi["status"] == "na" and "не подключён" in taxi["summary"]
    async with db.session() as s:
        logs = (await s.execute(select(ProviderLog).where(ProviderLog.method == "taxi"))).scalars().all()
    assert logs[0].error == "NOT_ALLOWED"


async def test_packs_and_credit(service, db, settings):
    user = await service.tg_user(42)
    async with db.session() as s:
        u = await s.get(User, user.id)
        await apply_payment(s, u, products(settings)["pack_10"], check_id=None, charge_id="ch1")
        await apply_payment(s, u, products(settings)["pack_10"], check_id=None, charge_id="ch1")  # повтор
        await s.commit()
    user = await service.get_user(user.id)
    assert user.credits == 10
    c = await service.express("X7TEST00000000001", user=user, source="telegram", enforce_limit=False)
    c = await service.upgrade_with_credit(user.id, c.id, "dossier")
    assert c.tier == "dossier"
    assert (await service.get_user(user.id)).credits == 9


async def test_unlimited(service, db, settings):
    user = await service.tg_user(43)
    async with db.session() as s:
        u = await s.get(User, user.id)
        await apply_payment(s, u, products(settings)["unlimited_day"], check_id=None, charge_id="u1")
        await s.commit()
    user = await service.get_user(user.id)
    for _ in range(4):  # лимит бесплатных не действует
        c = await service.express("X7TEST00000000001", user=user, source="telegram")
        c = await service.upgrade_with_credit(user.id, c.id, "dossier")
    assert (await service.get_user(user.id)).credits == 0


async def test_report_pdf(service):
    c = await service.express("X7TEST00000000001", client_ip="1.1.1.1")
    c = await service.upgrade(c.id, "dossier")
    html = service.html(c)
    assert "История пробегов" in html and "Реестр залогов" in html and "Досье+" in html
    pdf = await service.pdf(c)
    assert pdf[:4] == b"%PDF"
