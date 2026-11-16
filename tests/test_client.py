import httpx
import pytest
import respx

from shturman.provider import AvtovincodClient, ProviderError, call_accidents_rt

BASE = "https://api.avtovincod.ru"


def ok(body, **extra):
    return httpx.Response(200, json={"success": 1, **body,
                                     "billing": {"charged": extra.get("charged", 0), "currency": "RUB",
                                                 "billed": bool(extra.get("charged")), "balance": 481}},
                          headers={"X-Test-Mode": "1"} if extra.get("test") else {})


@respx.mock
async def test_call_sends_bearer_and_params():
    route = respx.get(f"{BASE}/pledge").mock(return_value=ok({"found": 1, "records": [], "testMode": True}, test=True))
    c = AvtovincodClient("tok", BASE)
    res = await c.call("pledge", vin="X7TEST00000000001")
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.url.params["vin"] == "X7TEST00000000001"
    assert res.test_mode and res.charged == 0 and not res.billed and res.balance == 481
    await c.aclose()


@respx.mock
@pytest.mark.parametrize("payload,status,code", [
    ({"success": 0, "error": "TOKEN_INVALID"}, 401, "TOKEN_INVALID"),
    ({"error": {"code": "NOT_ALLOWED", "message": "no"}}, 403, "NOT_ALLOWED"),
    ({"success": 0, "code": "NO_TOKEN"}, 200, "NO_TOKEN"),
])
async def test_errors(payload, status, code):
    respx.get(f"{BASE}/taxi").mock(return_value=httpx.Response(status, json=payload))
    c = AvtovincodClient("tok", BASE)
    with pytest.raises(ProviderError) as e:
        await c.call("taxi", vin="X")
    assert e.value.code == code
    await c.aclose()


def test_no_token():
    with pytest.raises(ProviderError):
        AvtovincodClient("")


@respx.mock
async def test_accidents_rt_polling():
    route = respx.get(f"{BASE}/accidents-rt")
    route.side_effect = [ok({"status": "pending", "taskId": "TEST-TASK-0001"}, charged=50),
                         ok({"status": "pending", "taskId": "TEST-TASK-0001"}),
                         ok({"status": "done", "found": 0, "records": []})]
    c = AvtovincodClient("tok", BASE)
    res = await call_accidents_rt(c, poll_interval=0, vin="X7TEST00000000001")
    assert res.data["status"] == "done" and res.charged == 50
    assert route.calls[1].request.url.params["task"] == "TEST-TASK-0001"
    await c.aclose()
