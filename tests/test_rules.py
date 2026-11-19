import json
from pathlib import Path

import pytest

from shturman.provider.mock import SAMPLES_DIR
from shturman.redaction import redact
from shturman.rules import ANALYZERS, analyze, build_flags, build_sections, traffic_light

SAMPLES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in SAMPLES_DIR.glob("*.json")}


def sections_from_samples():
    out = {}
    for name, body in SAMPLES.items():
        if name == "accidents-rt":
            continue
        method = name.replace(".done", "")
        out[method] = {"error": "источник временно недоступен"} if "error" in body else {"data": redact(body)}
    return out


@pytest.mark.parametrize("method", sorted(ANALYZERS))
def test_every_real_sample_is_parsed(method):
    body = SAMPLES.get("accidents-rt.done" if method == "accidents-rt" else method)
    if body is None or "error" in body:
        pytest.skip("нет ответа-образца")
    s = analyze(method, {"data": body})
    assert s is not None and s.status in ("ok", "warn", "bad", "info") and s.summary


def test_real_samples_traffic_light():
    secs = build_sections(sections_from_samples())
    by_key = {s.key: s for s in secs}
    assert by_key["pledge"].status == "bad"
    assert by_key["score"].status == "bad"
    assert by_key["taxi"].status == "warn"
    assert by_key["owners"].summary == "владельцев: 2"
    assert by_key["fines"].status == "na"
    assert by_key["mileage"].status == "warn"  # в объявлении пробег меньше, чем на техосмотре
    light = traffic_light(build_flags(sections_from_samples()))
    assert light["color"] == "red" and light["score"] < 50
    assert {f["method"] for f in light["red"]} == {"pledge", "score"}


def test_wanted_is_red():
    s = analyze("score", {"data": {"restrictions": [], "searches": [], "status": {"wanted": True}}})
    assert s.status == "bad" and "розыск" in s.summary


def test_clean_car_is_green():
    secs = {"pledge": {"data": {"found": 0, "active_count": 0, "records": []}},
            "taxi": {"data": {"isTaxi": False, "found": 0, "records": []}},
            "accidents": {"data": {"found": 0, "records": []}},
            "score": {"data": {"restrictions": [], "searches": [], "status": {"restricted": False, "wanted": False}}}}
    light = traffic_light(build_flags(secs))
    assert light["color"] == "green" and light["score"] == 100


def test_mileage_rollback_official_is_red():
    secs = {"probeg": {"data": {"records": [
        {"date_of_diagnosis": "2022-01-01", "car_mileage": 90000},
        {"date_of_diagnosis": "2024-01-01", "car_mileage": 40000}]}}}
    s = {x.key: x for x in build_sections(secs)}["mileage"]
    assert s.status == "bad"


def test_active_leasing_is_red():
    s = analyze("lizing", {"data": {"isLeasing": True, "active": 1, "records": [{"isActive": True}]}})
    assert s.status == "bad"


def test_error_section():
    s = analyze("taxi", {"error": "метод не подключён к токену"})
    assert s.status == "na"
    light = traffic_light([s.flag()])
    assert light["color"] == "grey" and light["score"] is None  # ни один источник не ответил


EMPTY = {"pledge": {"data": {"found": 0, "active_count": 0, "records": []}},
         "taxi": {"data": {"isTaxi": False, "found": 0, "records": []}},
         "elpts": {"data": {"found": 0, "records": []}},
         "accidents": {"data": {"found": 0, "records": []}}}


def test_no_data_is_not_green():
    light = traffic_light(build_flags(EMPTY), EMPTY)
    assert light["color"] == "grey" and light["score"] is None
    assert "не найден" in light["verdict"]
    # модель распознана по VIN, но в реестрах записей нет — всё равно серый
    secs = {**EMPTY, "decode": {"data": {"variants": [{"brand": "LADA", "model": "Vesta"}]}},
            "price": {"data": {"price": {"min": 1, "max": 2}}}}
    light = traffic_light(build_flags(secs), secs)
    assert light["color"] == "grey" and "реестрах не найдено" in light["verdict"]


def test_registry_record_allows_green():
    secs = {**EMPTY, "score": {"data": {"record": {"model": "LADA"}, "restrictions": [], "searches": [],
                                        "status": {"restricted": False, "wanted": False}}}}
    light = traffic_light(build_flags(secs), secs)
    assert light["color"] == "green" and light["score"] == 100


def test_key_check_failed_caps_at_yellow():
    secs = {**EMPTY, "elpts": {"data": {"found": 1, "records": [{"restrictions_status": "нет ограничений"}]}},
            "pledge": {"error": "источник временно недоступен"}}
    light = traffic_light(build_flags(secs), secs)
    assert light["color"] == "yellow" and any(f["title"] == "Не все проверки выполнены" for f in light["yellow"])


def test_red_wins_even_without_registry_data():
    secs = {**EMPTY, "pledge": {"data": {"found": 1, "active_count": 1, "records": [{"status": "active"}]}}}
    assert traffic_light(build_flags(secs), secs)["color"] == "red"
