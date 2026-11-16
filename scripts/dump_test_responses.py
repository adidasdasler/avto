"""Сохраняет настоящие ответы avtovincod.ru по тестовым значениям (бесплатно, без списаний)
в tests/fixtures/live/<method>.json — чтобы сверить разбор полей в shturman/normalize.py,
shturman/rules.py и шаблоне отчёта с реальной схемой.

    AVC_TOKEN=... python scripts/dump_test_responses.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shturman.provider import AvtovincodClient, ProviderError  # noqa: E402
from shturman.provider.methods import METHODS, TEST_VALUES  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"


async def main() -> None:
    token = os.environ.get("AVC_TOKEN")
    if not token:
        raise SystemExit("Задайте AVC_TOKEN")
    OUT.mkdir(parents=True, exist_ok=True)
    client = AvtovincodClient(token, os.environ.get("AVC_BASE_URL", "https://api.avtovincod.ru"))
    try:
        for name, spec in METHODS.items():
            param = spec.params[0]
            params = {param: TEST_VALUES[param]}
            if name == "ktg-permit":
                params["permit"] = "TEST"
            try:
                res = await client.call(name, **params)
                body = res.data
                status = "ok" if res.test_mode else "ok (НЕ тестовый режим!)"
            except ProviderError as e:
                body, status = {"error": e.code, "message": e.message}, f"ошибка {e.code}"
            (OUT / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2))
            print(f"{name:15} {params} -> {status}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
