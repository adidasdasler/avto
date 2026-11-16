import hashlib
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
import respx

from shturman.payments.robokassa import OPSTATE_URL, Robokassa, RobokassaError, parse_op_state

RK = dict(login="shop", password1="p1", password2="p2")


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def test_payment_url_without_receipt():
    rk = Robokassa(**RK, receipt=False, test=True)
    q = parse_qs(urlparse(rk.payment_url(42, 399, "Досье")).query)
    assert q["OutSum"] == ["399.00"] and q["InvId"] == ["42"] and q["IsTest"] == ["1"]
    assert q["SignatureValue"] == [md5("shop:399.00:42:p1")]


def test_payment_url_with_receipt_and_shp():
    rk = Robokassa(**RK, receipt=True, test=False, sno="npd")
    url = rk.payment_url(7, 199, "Базовый отчёт", shp={"b": 2, "Shp_a": 1})
    q = parse_qs(urlparse(url).query)  # parse_qs снимает один слой кодирования
    receipt = q["Receipt"][0]
    assert receipt.startswith("%7B") and '"sno": "npd"' in unquote(receipt) and '"sum": 199.0' in unquote(receipt)
    assert q["SignatureValue"] == [md5(f"shop:199.00:7:{receipt}:p1:Shp_a=1:Shp_b=2")]
    assert "IsTest" not in q


def test_result_and_success_signatures():
    rk = Robokassa(**RK)
    params = {"OutSum": "399.000000", "InvId": "42", "SignatureValue": md5("399.000000:42:p2").upper()}
    assert rk.check_result(params)
    assert not rk.check_success(params)
    assert rk.check_success({"OutSum": "399.000000", "InvId": "42", "SignatureValue": md5("399.000000:42:p1")})
    assert not rk.check_result({**params, "InvId": "43"})
    assert not rk.check_result({"OutSum": "1"})


def test_sha256():
    rk = Robokassa(**RK, algorithm="sha256", receipt=False)
    q = parse_qs(urlparse(rk.payment_url(1, 10, "x")).query)
    assert q["SignatureValue"] == [hashlib.sha256(b"shop:10.00:1:p1").hexdigest()]


def test_requires_credentials():
    with pytest.raises(RobokassaError):
        Robokassa("", "", "")


XML_PAID = """<?xml version="1.0" encoding="utf-8"?>
<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">
  <Result><Code>0</Code></Result>
  <State><Code>100</Code><RequestDate>2026-11-16T10:00:00</RequestDate><StateDate>2026-11-16T10:01:00</StateDate></State>
  <Info><IncCurrLabel>BankCard</IncCurrLabel><IncSum>399.00</IncSum><OutCurrLabel>BankCard</OutCurrLabel><OutSum>399.00</OutSum></Info>
</OperationStateResponse>"""


def test_parse_op_state():
    st = parse_op_state(XML_PAID)
    assert st.paid and st.out_sum == 399.0
    st = parse_op_state(XML_PAID.replace("<Code>100</Code>", "<Code>5</Code>"))
    assert not st.paid
    st = parse_op_state('<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">'
                        "<Result><Code>3</Code><Description>not found</Description></Result></OperationStateResponse>")
    assert st.result_code == 3 and not st.paid
    with pytest.raises(RobokassaError):
        parse_op_state("not xml")


@respx.mock
async def test_op_state_request():
    route = respx.post(OPSTATE_URL).mock(return_value=httpx.Response(200, text=XML_PAID))
    st = await Robokassa(**RK).op_state(42)
    assert st.paid
    body = parse_qs(route.calls.last.request.content.decode())
    assert body == {"MerchantLogin": ["shop"], "InvoiceID": ["42"], "Signature": [md5("shop:42:p2")]}
