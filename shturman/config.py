from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
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
    brand_name: str = "VinCv"

    # Лимит бесплатных экспресс-проверок в сутки (на пользователя бота / IP лендинга)
    free_checks_per_day: int = 3
    # Защита бюджета: каждая бесплатная проверка — платные запросы к avtovincod.ru
    free_checks_per_ip_per_day: int = 10  # на IP, сколько бы аккаунтов с него ни заходило
    free_checks_global_per_day: int = 300  # всего бесплатных проверок в сутки

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
    max_api_url: str = "https://platform-api.max.ru"

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

    # Вход через Яндекс ID: приложение на oauth.yandex.ru, Redirect URI — <PUBLIC_BASE_URL>/auth/yandex/callback
    yandex_client_id: str = ""
    yandex_client_secret: str = ""

    # Почта (SMTP) — подтверждение адреса и сброс пароля
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True  # 465 — SSL; для 587 поставьте false (STARTTLS)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # например noreply@vincv.ru

    # Реквизиты для оферты, политики и страницы контактов (обязательны для модерации Robokassa)
    legal_name: str = ""  # ФИО самозанятого / ИП или название организации
    legal_status: str = ""  # «Плательщик налога на профессиональный доход», «ИП», «ООО»
    legal_inn: str = ""
    legal_ogrn: str = ""  # ОГРНИП / ОГРН, если есть
    legal_address: str = ""
    contact_email: str = ""
    contact_phone: str = ""

    # Личный кабинет и каталог
    # анонимные экспресс-проверки с сайта хранятся N дней, проверки пользователей — бессрочно
    anon_checks_retention_days: int = 30

    @field_validator("avc_token", "max_bot_token", "telegram_bot_token", "telegram_payment_token",
                     "robokassa_login", "robokassa_password1", "robokassa_password2", "smtp_password",
                     "yandex_client_id", "yandex_client_secret",
                     mode="before")
    @classmethod
    def _clean_secret(cls, v: object, info) -> object:
        """Токены и пароли: убираем пробелы и кавычки; русские буквы — почти всегда незаменённая заготовка."""
        if not isinstance(v, str):
            return v
        v = v.strip().strip('"').strip("'").strip()
        if v and not v.isascii():
            name = info.field_name.upper()
            raise ValueError(f"{name} содержит русские буквы или спецсимволы — похоже, в .env осталась заготовка "
                             f"вместо настоящего значения. Скопируйте {name} заново из личного кабинета.")
        return v

    # Тревоги (сотрудникам в MAX/Telegram и в панели)
    alert_external_spend_rub: float = 50  # баланс у провайдера ушёл больше наших запросов — утечка токена
    alert_spend_hour_rub: float = 500  # наш расход у провайдера за час
    alert_spend_day_rub: float = 3000  # наш расход за сутки
    alert_balance_below_rub: float = 500  # остаток на балансе avtovincod.ru
    alert_errors_per_hour: int = 30  # ошибок источников за час
    alert_repeat_hours: float = 3  # повторять ту же тревогу не чаще
    balance_probe_minutes: int = 30  # узнавать баланс бесплатным тестовым запросом

    # Панель администратора: секретный адрес и сотрудники только из .env (см. shturman/admin/accounts.py)
    admin_path: str = ""  # например /ops-k7f2x9q4m1; пусто — панель выключена
    admin_users: str = ""  # создаётся командой: python -m shturman.admin.cli add <логин>
    admin_allowed_ips: str = ""  # необязательно: IP или подсети через запятую — с остальных панель не откроется
    # Кому слать тревоги: ID в MAX (бот отвечает на /id) и chat_id в Telegram, через запятую
    alert_max_user_ids: str = ""
    alert_tg_chat_ids: str = ""
    backup_dir: str = "./backups"  # в Docker — /app/backups (смонтировано в /opt/shturman/backups)
    staff_mfa_hours: int = 12  # сколько часов действует пройденная 2FA в сессии сотрудника

    @property
    def admin_prefix(self) -> str:
        p = "/" + self.admin_path.strip().strip("/")
        return "" if p == "/" else p

    # Цены, ₽
    price_basic: int = 199
    price_dossier: int = 399
    price_dossier_plus: int = 699
    price_pack_10: int = 850
    price_pack_25: int = 1950
    price_pack_50: int = 3500


@lru_cache
def get_settings() -> Settings:
    return Settings()
