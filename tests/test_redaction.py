from shturman.redaction import MASK, is_pii_key, redact


def test_keys():
    for k in ("ownerFio", "pledgor", "pledgorBirth", "debtorFio", "sellerPhone", "owner_name", "email", "passport"):
        assert is_pii_key(k), k
    for k in ("pledgee", "ownerType", "owners", "vin", "brand", "model", "region", "date"):
        assert not is_pii_key(k), k


def test_values_masked():
    data = {"records": [{"pledgee": "АО «Банк»", "pledgor": "Иванов Иван Иванович",
                         "note": "Должник Петров Пётр Петрович, тел. +7 (900) 123-45-67, a@b.ru, Сидоров А.Б."}]}
    out = redact(data)
    rec = out["records"][0]
    assert "pledgor" not in rec and rec["pledgee"] == "АО «Банк»"
    assert "Петров" not in rec["note"] and "900" not in rec["note"] and "a@b.ru" not in rec["note"]
    assert "Сидоров" not in rec["note"] and MASK in rec["note"]
