"""Бэкапы базы из панели: создание, список, скачивание, восстановление.

PostgreSQL: `pg_dump --clean --if-exists` → gzip (тот же формат, что у deploy/backup.sh, поэтому
ночные копии из cron видны в панели и восстанавливаются так же). Восстановление — `psql` одной
транзакцией: при ошибке база остаётся как была. SQLite (разработка) — копия файла через backup API.
"""
from __future__ import annotations

import asyncio
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from ..config import Settings
from ..db import Database

NAME_RE = re.compile(r"^shturman-[0-9A-Za-z_-]{1,80}\.(sql|sqlite)\.gz$")
# Строки, которые новый pg_dump пишет, а старый сервер не понимает (pg_dump 17 → сервер 16).
_SKIP = (b"SET transaction_timeout",)


class BackupError(Exception):
    pass


@dataclass
class BackupFile:
    name: str
    size: int
    created: datetime


def backup_dir(settings: Settings) -> Path:
    d = Path(settings.backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(settings: Settings, name: str) -> Path:
    """Путь к файлу бэкапа; имя проверяется, чтобы нельзя было выйти за пределы папки."""
    if not NAME_RE.fullmatch(name or ""):
        raise BackupError("Недопустимое имя файла")
    path = backup_dir(settings) / name
    if not path.is_file():
        raise BackupError("Файл не найден")
    return path


def list_backups(settings: Settings) -> list[BackupFile]:
    out = []
    for p in backup_dir(settings).iterdir():
        if p.is_file() and NAME_RE.fullmatch(p.name):
            st = p.stat()
            out.append(BackupFile(p.name, st.st_size, datetime.fromtimestamp(st.st_mtime)))
    return sorted(out, key=lambda b: b.created, reverse=True)


def disk_free(settings: Settings) -> tuple[int, int]:
    usage = shutil.disk_usage(backup_dir(settings))
    return usage.free, usage.total


def _pg(settings: Settings) -> tuple[str, dict[str, str]]:
    """URL для pg_dump/psql без пароля (пароль — через PGPASSWORD, чтобы не светить в списке процессов)."""
    url = make_url(settings.database_url)
    env = {**os.environ}
    if url.password:
        env["PGPASSWORD"] = url.password
    conninfo = url.set(drivername="postgresql", password=None).render_as_string(hide_password=False)
    return conninfo, env


def _is_sqlite(settings: Settings) -> bool:
    return make_url(settings.database_url).get_backend_name() == "sqlite"


def _sqlite_path(settings: Settings) -> str:
    db = make_url(settings.database_url).database
    if not db or db == ":memory:":
        raise BackupError("База в памяти — бэкап не нужен")
    return db


async def create_backup(settings: Settings, label: str = "manual") -> BackupFile:
    label = re.sub(r"[^0-9A-Za-z_-]", "", label)[:20] or "manual"
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    ext = "sqlite" if _is_sqlite(settings) else "sql"
    target = backup_dir(settings) / f"shturman-{stamp}-{label}.{ext}.gz"
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        if ext == "sqlite":
            await asyncio.to_thread(_sqlite_backup, _sqlite_path(settings), tmp)
        else:
            await _pg_dump(settings, tmp)
        tmp.rename(target)
    finally:
        tmp.unlink(missing_ok=True)
    st = target.stat()
    return BackupFile(target.name, st.st_size, datetime.fromtimestamp(st.st_mtime))


def _sqlite_backup(src: str, dst_gz: Path) -> None:
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "db.sqlite"
        with sqlite3.connect(src) as a, sqlite3.connect(copy) as b:
            a.backup(b)
        with open(copy, "rb") as f, gzip.open(dst_gz, "wb") as g:
            shutil.copyfileobj(f, g)


async def _pg_dump(settings: Settings, dst_gz: Path) -> None:
    conninfo, env = _pg(settings)
    proc = await asyncio.create_subprocess_exec(
        "pg_dump", "--clean", "--if-exists", "--no-owner", "--no-privileges", "-d", conninfo,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    with gzip.open(dst_gz, "wb") as g:
        while chunk := await proc.stdout.read(1 << 16):
            g.write(chunk)
    err = await proc.stderr.read()
    if await proc.wait() != 0:
        raise BackupError("pg_dump: " + err.decode(errors="replace")[-500:])


async def restore_backup(settings: Settings, db: Database, name: str) -> None:
    """Восстанавливает базу из файла. Перед вызовом сделайте свежую копию (это делает панель)."""
    path = path_for(settings, name)
    await db.engine.dispose()  # закрываем соединения пула, чтобы не держать блокировки
    if name.endswith(".sqlite.gz"):
        if not _is_sqlite(settings):
            raise BackupError("Это бэкап SQLite, а сервер работает на PostgreSQL")
        await asyncio.to_thread(_sqlite_restore, path, _sqlite_path(settings))
        return
    if _is_sqlite(settings):
        raise BackupError("Это бэкап PostgreSQL, а база — SQLite")
    conninfo, env = _pg(settings)
    proc = await asyncio.create_subprocess_exec(
        "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-d", conninfo,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env)

    async def feed():
        with gzip.open(path, "rb") as g:
            for line in g:
                if not line.startswith(_SKIP):
                    proc.stdin.write(line)
                    await proc.stdin.drain()
        proc.stdin.close()

    feeder = asyncio.create_task(feed())
    err = await proc.stderr.read()
    await feeder
    if await proc.wait() != 0:
        raise BackupError("psql: " + err.decode(errors="replace")[-500:])


def _sqlite_restore(src_gz: Path, dst: str) -> None:
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "restore.sqlite"
        with gzip.open(src_gz, "rb") as g, open(copy, "wb") as f:
            shutil.copyfileobj(g, f)
        with sqlite3.connect(copy) as a, sqlite3.connect(dst) as b:
            a.backup(b)


def delete_backup(settings: Settings, name: str) -> None:
    path_for(settings, name).unlink()
