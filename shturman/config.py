from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Провайдер avtovincod.ru
    avc_token: str = ""
    avc_base_url: str = "https://api.avtovincod.ru"
    # mock — офлайн-образцы; live — настоящий API (с тестовыми значениями тоже бесплатно)
    provider_mode: Literal["mock", "live"] = "mock"
    avc_rate_per_minute: int = 60

    database_url: str = "sqlite+aiosqlite:///./shturman.db"
    public_base_url: str = "http://localhost:8000"
    brand_name: str = "Штурман Авто"

    # Лимит бесплатных экспресс-проверок в сутки (на пользователя бота / IP лендинга)
    free_checks_per_day: int = 3

    # Telegram
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    # provider_token ЮКассы из @BotFather → Payments
    telegram_payment_token: str = ""
    # Прокси для api.telegram.org, если он недоступен напрямую (http://, socks5:// — нужен aiohttp-socks)
    telegram_proxy: str = ""
    # Только для разработки: «оплата» без платёжного провайдера
    dev_fake_payments: bool = False

    # MAX
    max_bot_token: str = ""
    # Ссылка на бота, например https://max.ru/shturman_bot (для кнопок на сайте)
    max_bot_url: str = ""
    max_api_url: str = "https://platform-api2.max.ru"

    # Robokassa
    robokassa_login: str = ""
    robokassa_password1: str = ""
    robokassa_password2: str = ""
    robokassa_test: bool = True
    robokassa_hash: str = "md5"  # как выбрано в ЛК: md5 / sha256 / sha512 ...
    # Чек по 54-ФЗ (для самозанятых — «Робочеки»): передавать Receipt в ссылке
    robokassa_receipt: bool = True
    robokassa_sno: str = ""  # система налогообложения; пусто — как в ЛК
    robokassa_tax: str = "none"
    # Проверять статус счёта запросом OpStateExt (работает без публичного сервера для ResultURL)
    robokassa_poll_status: bool = True
    # За обратным прокси (nginx) брать IP клиента из X-Forwarded-For
    trust_proxy_headers: bool = False

    # Цены, ₽
    price_basic: int = 199
    price_dossier: int = 399
    price_dossier_plus: int = 699
    price_pack_10: int = 850
    price_pack_25: int = 1950
    price_pack_50: int = 3500
    price_unlimited_day: int = 1490


@lru_cache
def get_settings() -> Settings:
    return Settings()
