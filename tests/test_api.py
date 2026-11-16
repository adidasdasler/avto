from fastapi.testclient import TestClient

from shturman.api.app import create_app
from shturman.provider import MockProvider


def test_landing_check_and_report(settings):
    app = create_app(settings, MockProvider())
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200 and "Проверить бесплатно" in r.text and "t.me/test_bot" in r.text
        r = client.post("/api/check", json={"query": "Т000ТТ00"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["vehicle"]["vin"] == "X7TEST00000000001"
        assert data["buy_url"].startswith("https://t.me/test_bot?start=chk_")
        assert data["free_left"] == 1
        assert client.get(data["report_url"]).status_code == 200
        pdf = client.get(data["report_url"] + ".pdf")
        assert pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
        assert client.post("/api/check", json={"query": "X7TEST00000000001"}).status_code == 200
        assert client.post("/api/check", json={"query": "X7TEST00000000001"}).status_code == 429
        assert client.post("/api/check", json={"query": "непонятно что"}).status_code == 422
        assert client.get("/r/not-a-uuid").status_code == 404
        r = client.get("/example")
        assert r.status_code == 200 and "Досье+" in r.text
