import hashlib

import pytest
from fastapi.testclient import TestClient

from shturman.api.app import create_app
from shturman.db import Order
from shturman.orders import OrderService, PaymentsNotConfigured
from shturman.payments.robokassa import OpState, Robokassa
from shturman.provider import MockProvider


class FakeRK(Robokassa):
    def __init__(self, paid=False):
        super().__init__("shop", "p1", "p2", receipt=False)
        self.paid = paid

    async def op_state(self, inv_id):
        return OpState(0, 100 if self.paid else 5, 399.0)


async def test_not_configured(db, settings):
    with pytest.raises(PaymentsNotConfigured):
        await OrderService(db, settings).create(1, "dossier", check_id=None, platform="max", chat_id=1)


async def test_dev_fake_payment(db, settings, service):
    settings.dev_fake_payments = True
    user = await service.max_user(5)
    order, url = await OrderService(db, settings).create(user.id, "pack_10", check_id=None, platform="max", chat_id=9)
    assert url is None
    assert (await service.get_user(user.id)).credits == 10


async def test_robokassa_flow_idempotent(db, settings, service):
    rk = FakeRK()
    orders = OrderService(db, settings, rk)
    user = await service.max_user(6)
    order, url = await orders.create(user.id, "pack_10", check_id=None, platform="max", chat_id=1)
    assert url.startswith("https://auth.robokassa.ru/") and f"InvId={order.id}" in url
    await orders.poll_pending()
    assert (await orders.get(order.id)).status == "pending"
    rk.paid = True
    await orders.poll_pending()
    await orders.mark_paid(order.id, "850.000000")  # повторное уведомление
    assert (await orders.get(order.id)).status == "paid"
    assert (await service.get_user(user.id)).credits == 10
    assert [o.id for o in await orders.undelivered("max")] == [order.id]
    await orders.mark_delivered(order.id)
    assert await orders.undelivered("max") == []


async def test_amount_mismatch(db, settings, service):
    orders = OrderService(db, settings, FakeRK())
    user = await service.max_user(7)
    order, _ = await orders.create(user.id, "dossier", check_id=None, platform="max", chat_id=1)
    assert await orders.mark_paid(order.id, "1.00") is None
    assert (await orders.get(order.id)).status == "pending"


def test_result_url_endpoint(settings):
    settings.robokassa_login, settings.robokassa_password1, settings.robokassa_password2 = "shop", "p1", "p2"
    with TestClient(create_app(settings, MockProvider())) as client:
        svc, orders = client.app.state.service, client.app.state.orders

        async def prepare():
            user = await svc.max_user(8)
            order, _ = await orders.create(user.id, "dossier", check_id=None, platform="max", chat_id=1)
            return order.id
        oid = client.portal.call(prepare)
        sig = hashlib.md5(f"399.000000:{oid}:p2".encode()).hexdigest().upper()
        bad = client.post("/pay/robokassa/result", data={"OutSum": "399.000000", "InvId": oid, "SignatureValue": "x"})
        assert bad.status_code == 400
        ok = client.post("/pay/robokassa/result", data={"OutSum": "399.000000", "InvId": oid, "SignatureValue": sig})
        assert ok.status_code == 200 and ok.text == f"OK{oid}"
        assert client.portal.call(orders.get, oid).status == "paid"
        assert "Оплата прошла" in client.get("/pay/robokassa/success").text
