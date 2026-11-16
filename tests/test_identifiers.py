import pytest

from shturman.identifiers import IdentifierError, parse, parse_many


@pytest.mark.parametrize("text,kind,value", [
    ("X7TEST00000000001", "vin", "X7TEST00000000001"),
    (" xwegu411bj0006942 ", "vin", "XWEGU411BJ0006942"),
    ("Т000ТТ00", "plate", "Т000ТТ00"),
    ("t000tt00", "plate", "Т000ТТ00"),
    ("о 803 ув 40", "plate", "О803УВ40"),
    ("А123ВС777", "plate", "А123ВС777"),
    ("TEST-0000001", "body", "TEST-0000001"),
    ("https://auto.example/cars/kia/optima/XWEGU411BJ0006942/", "vin", "XWEGU411BJ0006942"),
    ("https://site.example/lot?plate=%D0%9E803%D0%A3%D0%9240", "plate", "О803УВ40"),
])
def test_parse(text, kind, value):
    ident = parse(text)
    assert (ident.kind, ident.value) == (kind, value)


@pytest.mark.parametrize("text", ["", "hello", "XWEGU411BJ000694I", "https://auto.example/cars/123"])
def test_parse_errors(text):
    with pytest.raises(IdentifierError):
        parse(text)


def test_parse_many():
    items = parse_many("X7TEST00000000001\nТ000ТТ00; мусор")
    assert [getattr(i, "kind", None) for i in items] == ["vin", "plate", None]
