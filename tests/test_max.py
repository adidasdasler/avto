import json

import httpx
import respx

from shturman.bot.common import Btn
from shturman.bot.max import MaxBot
from shturman.bot.max_api import MaxAPI, MaxError, keyboard
from shturman.orders import OrderService
from shturman.payments.robokassa import OpState, Robokassa

API = "https://platform-api2.max.ru"


def test_keyboard_format():
    kb = keyboard([[Btn("Купить", data="buy:dossier:x")], [Btn("Сайт", url="https://example.com")]])
    assert kb == {"type": "inline_keyboard", "payload": {"buttons": [
        [{"type": "callback", "text": "Купить", "payload": "buy:dossier:x"}],
        [{"type": "link", "text": "Сайт", "url": "https://example.com"}]]}}


@respx.mock
async def test_api_auth_send_and_errors():
    route = respx.post(f"{API}/messages").mock(return_value=httpx.Response(200, json={"message": {}}))
    api = MaxAPI("tok")
    await api.send_message(chat_id=5, text="<b>hi</b>", buttons=[[Btn("a", data="x")]])
    req = route.calls.last.request
    assert req.headers["Authorization"] == "tok" and req.url.params["chat_id"] == "5"
    body = json.loads(req.content)
    assert body["format"] == "html" and body["attachments"][0]["type"] == "inline_keyboard"
    respx.get(f"{API}/me").mock(return_value=httpx.Response(401, json={"code": "verify.token", "message": "Invalid"}))
    try:
        await api.get_me()
        raise AssertionError
    except MaxError as e:
        assert e.code == "verify.token" and e.status == 401
    await api.aclose()


@respx.mock
async def test_upload_and_retry_not_ready():
    respx.post(f"{API}/uploads").mock(return_value=httpx.Response(200, json={"url": "https://upload.example/u1"}))
    up = respx.post("https://upload.example/u1").mock(return_value=httpx.Response(200, json={"token": "T1"}))
    msgs = respx.post(f"{API}/messages")
    msgs.side_effect = [httpx.Response(400, json={"code": "attachment.not.ready", "message": "wait"}),
                        httpx.Response(200, json={"message": {}})]
    api = MaxAPI("tok")
    import shturman.bot.max_api as m

    async def no_sleep(_):
        return None
    m.asyncio.sleep, orig = no_sleep, m.asyncio.sleep
    try:
        await api.send_file(user_id=3, data=b"%PDF", filename="r.pdf", text="отчёт")
    finally:
        m.asyncio.sleep = orig
    assert b'name="data"' in up.calls.last.request.content
    sent = json.loads(msgs.calls.last.request.content)
    assert {"type": "file", "payload": {"token": "T1"}} in sent["attachments"]
    assert msgs.call_count == 2
    await api.aclose()


class FakeAPI:
    def __init__(self):
        self.sent, self.files, self.updates = [], [], []

    async def send_message(self, *, chat_id=None, user_id=None, text="", buttons=None, attachments=None):
        self.sent.append({"chat_id": chat_id, "user_id": user_id, "text": text, "buttons": buttons or []})

    async def send_file(self, *, chat_id=None, user_id=None, data, filename, text="", buttons=None):
        self.files.append({"chat_id": chat_id, "filename": filename, "data": data, "text": text})

    async def answer_callback(self, callback_id, notification=None):
        pass

    async def get_updates(self, marker=None, timeout=30, limit=100):
        ups, self.updates = self.updates, []
        return {"updates": ups, "marker": (marker or 0) + len(ups)}

    def buttons(self, i=-1):
        return [b for row in self.sent[i]["buttons"] for b in row]


def msg(text, uid=100, chat=500):
    return {"update_type": "message_created", "timestamp": 1,
            "message": {"sender": {"user_id": uid, "name": "Денис"}, "recipient": {"chat_id": chat, "chat_type": "dialog"},
                        "body": {"mid": "m1", "seq": 1, "text": text}}}


def press(payload, uid=100, chat=500):
    return {"update_type": "message_callback", "timestamp": 1,
            "callback": {"timestamp": 1, "callback_id": "cb1", "payload": payload, "user": {"user_id": uid}},
            "message": {"recipient": {"chat_id": chat}}}


class FakeRK(Robokassa):
    def __init__(self):
        super().__init__("shop", "p1", "p2", receipt=False)
        self.paid = False

    async def op_state(self, inv_id):
        return OpState(0, 100 if self.paid else 5, None)


async def run(bot, api, *updates):
    api.updates = list(updates)
    await bot.poll_once(concurrent=False)


async def test_max_full_flow(db, settings, service):
    api, rk = FakeAPI(), FakeRK()
    bot = MaxBot(api, service, OrderService(db, settings, rk), settings)

    await run(bot, api, {"update_type": "bot_started", "chat_id": 500, "user": {"user_id": 100}, "payload": None})
    assert "Как вы будете пользоваться" in api.sent[-1]["text"]
    assert [b.data for b in api.buttons()] == ["role:buyer", "role:picker"]

    await run(bot, api, msg("X7TEST00000000001"))
    assert "🔴" in api.sent[-1]["text"] and "Реестр залогов" in api.sent[-1]["text"]
    buy = next(b for b in api.buttons() if b.data and b.data.startswith("buy:dossier:"))

    await run(bot, api, press(buy.data))
    pay = api.buttons()
    assert pay[0].url.startswith("https://auth.robokassa.ru/") and pay[1].data.startswith("paid:")

    await run(bot, api, press(pay[1].data))  # ещё не оплачено
    assert "пока не поступила" in api.sent[-1]["text"]

    rk.paid = True
    await run(bot, api, press(pay[1].data))
    assert api.files and api.files[-1]["chat_id"] == 500 and api.files[-1]["data"][:4] == b"%PDF"
    assert "«Досье»" in api.files[-1]["text"]
    n = len(api.files)
    await bot.deliver_pending()  # повторно не доставляет
    assert len(api.files) == n


async def test_max_start_deeplink_history_and_batch(db, settings, service):
    settings.dev_fake_payments = True
    api = FakeAPI()
    bot = MaxBot(api, service, OrderService(db, settings), settings)
    c = await service.express("Т000ТТ00", client_ip="9.9.9.9", source="web")
    await run(bot, api, {"update_type": "bot_started", "chat_id": 501, "user": {"user_id": 101},
                         "payload": "chk_" + c.id.replace("-", "")})
    assert "LADA Vesta" in api.sent[-1]["text"]

    await run(bot, api, press("buy:pack_10:-", uid=101, chat=501))  # тестовая оплата
    assert "Оплата получена" in api.sent[-1]["text"] and "<b>10</b>" in api.sent[-1]["text"]

    await run(bot, api, msg("X7TEST00000000001\nТ000ТТ00\nмусор", uid=101, chat=501))
    assert len(api.files) == 2 and any("⚠️" in m["text"] for m in api.sent)
    assert "<b>8</b>" in api.sent[-1]["text"]

    await run(bot, api, msg("/history", uid=101, chat=501))
    assert "Последние проверки" in api.sent[-1]["text"]
    await run(bot, api, msg("/balance", uid=101, chat=501))
    assert "в пакете" in api.sent[-1]["text"]
    await run(bot, api, msg("/foo", uid=101, chat=501))
    assert "Не знаю такой команды" in api.sent[-1]["text"]
