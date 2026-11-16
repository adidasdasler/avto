from shturman.rules import evaluate, overall


def test_pledge():
    assert evaluate("pledge", {"found": 2, "active_count": 1, "records": [{}, {}]}).status == "bad"
    assert evaluate("pledge", {"found": 1, "active_count": 0, "records": [{"status": "Исключён из залога"}]}).status == "ok"


def test_mileage_rollback():
    data = {"records": [{"date": "2020-01-01", "mileage": 50000}, {"date": "2021-01-01", "mileage": 40000}]}
    assert evaluate("probeg", data).status == "bad"
    data["records"][1]["mileage"] = 70000
    assert evaluate("probeg", data).status == "ok"


def test_error_and_overall():
    f = evaluate("taxi", None, "метод не подключён к токену")
    assert f.status == "na"
    assert overall([f, evaluate("taxi", {"found": 1, "records": [{}]})]) == "warn"
