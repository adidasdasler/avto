"""Общий SSL-контекст для исходящих HTTPS-запросов (MAX, avtovincod.ru, Robokassa).

Доверяем сразу набору certifi и системному хранилищу сертификатов: у части российских сервисов
цепочки сертификатов, которых нет в certifi. Дополнительный корневой сертификат (например,
российский УЦ Минцифры) подключается явно — путём к PEM-файлу в EXTRA_CA_FILE.
"""
from __future__ import annotations

import os
import ssl
from functools import lru_cache

import certifi


@lru_cache
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_default_certs()  # системное хранилище (/etc/ssl/certs)
    extra = os.environ.get("EXTRA_CA_FILE")
    if extra:
        ctx.load_verify_locations(cafile=extra)
    return ctx
