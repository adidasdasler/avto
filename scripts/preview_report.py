"""Собирает примеры отчётов по тестовому VIN в reports_preview/ (экспресс и «Досье+»).

    python scripts/preview_report.py            # офлайн-образцы
    (PROVIDER_MODE=live и AVC_TOKEN в .env)  — тестовый режим avtovincod.ru
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shturman.config import get_settings  # noqa: E402
from shturman.db import Database  # noqa: E402
from shturman.provider import make_provider  # noqa: E402
from shturman.provider.methods import TEST_VALUES  # noqa: E402
from shturman.services import CheckService  # noqa: E402


async def _pdf(svc: CheckService, check) -> bytes | None:
    try:
        return await svc.pdf(check)
    except (OSError, ImportError) as e:  # на Windows WeasyPrint нужен GTK/Pango
        print(f"PDF не собран ({e.__class__.__name__}): нет системных библиотек WeasyPrint. "
              "HTML-отчёты сохранены — откройте их в браузере (Ctrl+P → «Сохранить как PDF»).")
        return None


async def main() -> None:
    settings = get_settings().model_copy(update={"database_url": "sqlite+aiosqlite:///:memory:"})
    out = ROOT / "reports_preview"
    out.mkdir(exist_ok=True)
    db = Database(settings.database_url)
    await db.create_all()
    provider = make_provider(settings)
    svc = CheckService(db, provider, settings, accidents_poll_interval=1)
    try:
        check = await svc.express(TEST_VALUES["vin"], client_ip="preview")
        (out / "express.html").write_text(svc.html(check), encoding="utf-8")
        express_pdf = await _pdf(svc, check)
        check = await svc.upgrade(check.id, "dossier_plus")
        (out / "dossier_plus.html").write_text(svc.html(check), encoding="utf-8")
        full_pdf = await _pdf(svc, check)
        if express_pdf and full_pdf:
            (out / "express.pdf").write_bytes(express_pdf)
            (out / "dossier_plus.pdf").write_bytes(full_pdf)
        print(f"Готово: {out} (провайдер: {settings.provider_mode})")
    finally:
        await provider.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
