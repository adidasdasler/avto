import pytest

from shturman.config import Settings
from shturman.db import Database
from shturman.provider import MockProvider
from shturman.services import CheckService


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db",
                    telegram_bot_username="test_bot", free_checks_per_day=2,
                    bot_consent_required=False)  # отдельные тесты согласия — в test_privacy.py


@pytest.fixture
async def db(settings):
    d = Database(settings.database_url)
    await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
def provider():
    return MockProvider()


@pytest.fixture
def service(db, provider, settings):
    return CheckService(db, provider, settings, accidents_poll_interval=0)
