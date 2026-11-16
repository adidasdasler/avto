from shturman.bot.telegram import offer_keyboard, summary_text


async def test_summary_and_offer(service, settings):
    user = await service.tg_user(7)
    c = await service.express("X7TEST00000000001", user=user, source="telegram")
    text = summary_text(c)
    assert "LADA Vesta" in text and "Реестр залогов" in text and "«Экспресс»" in text
    assert "🔴" in text and "Критично" in text
    kb = offer_keyboard(c, user, settings)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert [d.split(":")[1] for d in datas] == ["basic", "dossier", "dossier_plus"]
    c = await service.upgrade(c.id, "dossier")
    kb = offer_keyboard(c, user, settings)
    assert [b.callback_data.split(":")[1] for row in kb.inline_keyboard for b in row] == ["dossier_plus"]


class FakeMessage:
    def __init__(self):
        self.docs, self.texts = [], []

    async def answer_document(self, doc, caption=None):
        self.docs.append((doc.filename, doc.data, caption))

    async def answer(self, text, reply_markup=None):
        self.texts.append(text)


async def test_send_report_pdf_and_html_fallback(service, monkeypatch):
    from shturman.bot.telegram import send_report

    c = await service.express("X7TEST00000000001", client_ip="1")
    msg = FakeMessage()
    await send_report(msg, service, c)
    assert msg.docs[0][0].endswith(".pdf") and msg.docs[0][1][:4] == b"%PDF"

    async def no_pdf(check):
        raise OSError("cannot load library 'gobject-2.0-0'")
    monkeypatch.setattr(service, "pdf", no_pdf)
    await send_report(msg, service, c)
    name, data, caption = msg.docs[1]
    assert name.endswith(".html") and "Реестр залогов".encode() in data and "Критично" in caption


async def test_fake_payment_delivers_report(service, settings):
    from shturman.billing import products
    from shturman.bot.telegram import fulfil

    user = await service.tg_user(99)
    c = await service.express("X7TEST00000000001", user=user, source="telegram")
    msg = FakeMessage()
    await fulfil(msg, service, settings, user, products(settings)["dossier"], c.id, charge_id="dev-1", provider="dev")
    assert msg.docs and (await service.get_check(c.id)).tier == "dossier"
