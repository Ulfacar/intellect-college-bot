from app.core.visa_pricing import self_visa_reply, visa_price_reply


def test_visa_price_reply_scopes_to_usa_only():
    reply = visa_price_reply("Сколько стоит виза в США?")

    assert reply is not None
    assert "250$" in reply
    assert "185$" in reply
    assert "Шенген" not in reply
    assert "Китай" not in reply


def test_visa_price_reply_scopes_to_schengen_only():
    reply = visa_price_reply("Цена шенген визы?")

    assert reply is not None
    assert "100€" in reply
    assert "90€" in reply
    assert "30€" in reply
    assert "США" not in reply


def test_visa_price_reply_asks_country_when_missing():
    reply = visa_price_reply("Сколько стоят визы?")

    assert reply == "Подскажите, по какой стране нужна цена? Напишите страну, и я назову официальный прайс только по ней."


def test_non_price_visa_message_not_captured():
    assert visa_price_reply("Хочу визу в США") is None


def test_self_visa_reply_soft_retention_once():
    reply = self_visa_reply("Я сам оформлю визу")

    assert reply is not None
    assert "самостоятельно" in reply
    assert "проверяем анкету" in reply


def test_self_visa_reply_repeat_hands_to_manager():
    reply = self_visa_reply("Все равно без вас сделаю", already_sent=True)

    assert reply is not None
    assert "Передам менеджеру" in reply
