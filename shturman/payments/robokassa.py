"""Robokassa: ссылка на оплату, проверка уведомлений и статуса счёта.

Формулы (как в официальном SDK robokassa на PyPI):
- ссылка:   hash("MerchantLogin:OutSum:InvId[:Receipt]:Пароль1[:Shp_a=1:Shp_b=2]"),
            Receipt — URL-кодированный JSON; Shp_-параметры по алфавиту после пароля;
- ResultURL: hash("OutSum:InvId:Пароль2[:Shp_...]"), ответ магазина — "OK<InvId>";
- SuccessURL / FailURL: hash("OutSum:InvId:Пароль1[:Shp_...]");
- OpStateExt: hash("MerchantLogin:InvoiceID:Пароль2"), State/Code = 100 — оплачено.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlencode

import httpx

from ..net import ssl_context

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
OPSTATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"
_NS = {"ns": "http://merchant.roboxchange.com/WebService/"}

STATE_PAID = 100
STATES = {5: "создан", 10: "отменён", 20: "холдирование", 50: "в обработке", 60: "отказ", 80: "приостановлен",
          100: "оплачен"}


class RobokassaError(Exception):
    pass


@dataclass
class OpState:
    result_code: int
    state_code: int | None
    out_sum: float | None = None

    @property
    def paid(self) -> bool:
        return self.result_code == 0 and self.state_code == STATE_PAID


def _shp(params: Mapping[str, Any]) -> list[str]:
    return sorted(f"{k}={v}" for k, v in params.items() if k.lower().startswith("shp_"))


class Robokassa:
    def __init__(self, login: str, password1: str, password2: str, *, test: bool = True,
                 algorithm: str = "md5", receipt: bool = True, sno: str = "", tax: str = "none",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not (login and password1 and password2):
            raise RobokassaError("Не заданы ROBOKASSA_LOGIN / ROBOKASSA_PASSWORD1 / ROBOKASSA_PASSWORD2")
        if algorithm.lower() not in hashlib.algorithms_available:
            raise RobokassaError(f"Неизвестный алгоритм хеширования: {algorithm}")
        self.login, self.password1, self.password2 = login, password1, password2
        self.test, self.algorithm = test, algorithm.lower()
        self.receipt, self.sno, self.tax = receipt, sno, tax
        self._transport = transport

    def _hash(self, *parts: Any) -> str:
        return hashlib.new(self.algorithm, ":".join(str(p) for p in parts).encode()).hexdigest()

    @staticmethod
    def out_sum(amount: int | float) -> str:
        return f"{amount:.2f}"

    def receipt_json(self, name: str, amount: int | float) -> str:
        data: dict[str, Any] = {"items": [{
            "name": name[:128], "quantity": 1, "sum": round(float(amount), 2),
            "payment_method": "full_payment", "payment_object": "service", "tax": self.tax}]}
        if self.sno:
            data = {"sno": self.sno, **data}
        return quote(json.dumps(data, ensure_ascii=False), safe="")

    def payment_url(self, inv_id: int, amount: int | float, description: str, *,
                    email: str | None = None, shp: Mapping[str, Any] | None = None) -> str:
        out_sum = self.out_sum(amount)
        shp = {k if k.lower().startswith("shp_") else f"Shp_{k}": v for k, v in (shp or {}).items()}
        receipt = self.receipt_json(description, amount) if self.receipt else None
        sign_parts: list[Any] = [self.login, out_sum, inv_id]
        if receipt:
            sign_parts.append(receipt)
        sign_parts.append(self.password1)
        sign_parts += _shp(shp)
        params: dict[str, Any] = {"MerchantLogin": self.login, "OutSum": out_sum, "InvId": inv_id,
                                  "Description": description[:100], "Culture": "ru", "Encoding": "utf-8"}
        if receipt:
            params["Receipt"] = receipt  # urlencode закодирует ещё раз — так и требует Robokassa
        if email:
            params["Email"] = email
        params.update(shp)
        params["SignatureValue"] = self._hash(*sign_parts)
        if self.test:
            params["IsTest"] = 1
        return f"{PAY_URL}?{urlencode(params)}"

    def _check(self, params: Mapping[str, Any], password: str) -> bool:
        try:
            out_sum, inv_id, sig = params["OutSum"], params["InvId"], params["SignatureValue"]
        except KeyError:
            return False
        expected = self._hash(out_sum, inv_id, password, *_shp(params))
        return hmac.compare_digest(expected.lower(), str(sig).lower())

    def check_result(self, params: Mapping[str, Any]) -> bool:
        """Подпись уведомления на ResultURL (пароль №2)."""
        return self._check(params, self.password2)

    def check_success(self, params: Mapping[str, Any]) -> bool:
        """Подпись редиректа на SuccessURL (пароль №1)."""
        return self._check(params, self.password1)

    async def op_state(self, inv_id: int) -> OpState:
        data = {"MerchantLogin": self.login, "InvoiceID": inv_id,
                "Signature": self._hash(self.login, inv_id, self.password2)}
        async with httpx.AsyncClient(timeout=20, transport=self._transport, verify=ssl_context()) as http:
            resp = await http.post(OPSTATE_URL, data=data)
        return parse_op_state(resp.text)


def parse_op_state(text: str) -> OpState:
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError as e:
        raise RobokassaError("Не удалось разобрать ответ OpStateExt") from e

    def num(path: str, cast=int):
        el = root.find(path, _NS)
        return cast(el.text) if el is not None and el.text else None

    result = num("ns:Result/ns:Code")
    if result is None:
        raise RobokassaError("В ответе OpStateExt нет Result/Code")
    return OpState(result_code=result, state_code=num("ns:State/ns:Code"), out_sum=num("ns:Info/ns:OutSum", float))
