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
