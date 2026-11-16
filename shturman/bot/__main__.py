import asyncio
import logging

from ..config import get_settings
from ..db import Database
from ..provider import make_provider
from ..services import CheckService
from .telegram import run_polling


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    db = Database(settings.database_url)
    await db.create_all()
    provider = make_provider(settings)
    try:
        await run_polling(CheckService(db, provider, settings), settings)
    finally:
        await provider.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
