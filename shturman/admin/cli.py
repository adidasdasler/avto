"""Сотрудники панели — управление из терминала сервера (из браузера их не добавить).

На сервере (из /opt/shturman):
    ./deploy/admin.sh init                       # секретный адрес панели (ADMIN_PATH)
    ./deploy/admin.sh add ivan --role admin      # спросит пароль, покажет QR для приложения 2FA
    ./deploy/admin.sh add olga --role support
    ./deploy/admin.sh passwd ivan                # новый пароль
    ./deploy/admin.sh reset-2fa ivan             # новый QR (потерян телефон)
    ./deploy/admin.sh remove olga
    ./deploy/admin.sh list
После изменений: docker compose up -d api

Пароль в .env не попадает — только его хеш (scrypt). Секрет 2FA хранится в .env: файл должен быть
доступен только root (chmod 600 .env).
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import string
import sys
from pathlib import Path

import qrcode

from .. import totp
from .accounts import LOGIN_RE, ROLES, StaffAccount, hash_password, parse

ENV = Path(os.environ.get("ENV_FILE", ".env"))


# ---------------------------------------------------------------- .env
def read_env() -> list[str]:
    return ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []


def get_var(lines: list[str], key: str) -> str:
    for line in lines:
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def set_var(key: str, value: str) -> None:
    lines, done = read_env(), False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i], done = f"{key}={value}", True
    if not done:
        lines.append(f"{key}={value}")
    # пишем в тот же файл (он может быть смонтирован в контейнер по отдельности — rename не подходит)
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass


def load() -> dict[str, StaffAccount]:
    return parse(get_var(read_env(), "ADMIN_USERS"))


def save(accounts: dict[str, StaffAccount]) -> None:
    set_var("ADMIN_USERS", ";".join(a.entry() for a in accounts.values()))


# ---------------------------------------------------------------- вывод
def show_qr(account: StaffAccount, brand: str) -> None:
    uri = totp.provisioning_uri(account.totp_secret, account.login, f"{brand} admin")
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(uri)
    qr.make(fit=True)
    print("\nОтсканируйте QR-код приложением (Яндекс Ключ, Google Authenticator, Aegis, 1Password…):\n")
    try:
        qr.print_ascii(tty=sys.stdout.isatty(), invert=not sys.stdout.isatty())
    except OSError:
        qr.print_ascii(invert=True)
    print(f"\nЕсли QR не читается — введите ключ вручную: {account.totp_secret}")
    print("Очистите экран после настройки: clear\n")


def ask_password() -> str:
    while True:
        p1 = getpass.getpass("Пароль сотрудника (не короче 12 символов, ввод не отображается): ")
        if len(p1) < 12:
            print("Слишком короткий.")
            continue
        if getpass.getpass("Повторите пароль: ") != p1:
            print("Не совпадает, ещё раз.")
            continue
        return p1


def confirm_code(account: StaffAccount) -> None:
    """Проверяем, что приложение настроено правильно, — иначе сотрудник не войдёт."""
    for _ in range(3):
        code = input("Введите 6-значный код из приложения для проверки: ").strip()
        if totp.verify(account.totp_secret, code) is not None:
            print("Код верный.")
            return
        print("Неверно. Проверьте время на телефоне и попробуйте ещё раз.")
    print("Код так и не подошёл — запись всё равно сохранена; при необходимости выполните reset-2fa.")


def brand() -> str:
    return get_var(read_env(), "BRAND_NAME") or "VinCv"


def panel_url() -> str:
    lines = read_env()
    base = (get_var(lines, "PUBLIC_BASE_URL") or "https://" + (get_var(lines, "DOMAIN") or "vincv.ru")).rstrip("/")
    path = get_var(lines, "ADMIN_PATH").strip("/")
    return f"{base}/{path}/login" if path else "(ADMIN_PATH не задан — выполните init)"


def restart_hint() -> None:
    print(f"\nПрименить: docker compose up -d api\nВход: {panel_url()}")
    https_hint()


def https_hint() -> None:
    if panel_url().startswith("http://"):
        print("\n⚠ В .env PUBLIC_BASE_URL начинается с http:// — замените на https://vincv.ru (без / в конце),\n"
              "  иначе cookie панели и кабинета уходят без флага Secure, а в боте нет кнопки «Открыть отчёт».\n"
              "  Затем: docker compose up -d")


# ---------------------------------------------------------------- команды
def cmd_init(args) -> None:
    current = get_var(read_env(), "ADMIN_PATH")
    if current and not args.force:
        print(f"ADMIN_PATH уже задан. Сменить адрес: init --force\nВход: {panel_url()}")
        return
    alphabet = string.ascii_lowercase + string.digits
    set_var("ADMIN_PATH", "/ops-" + "".join(secrets.choice(alphabet) for _ in range(16)))
    print(f"Секретный адрес панели: {panel_url()}\nНикому его не пересылайте в открытых чатах.")
    https_hint()
    if not load():
        print("Теперь добавьте сотрудника: ./deploy/admin.sh add <логин> --role admin")
    restart_hint()


def cmd_add(args) -> None:
    login = args.login.strip().lower()
    if not LOGIN_RE.fullmatch(login):
        sys.exit("Логин: 3–32 символа, латиница в нижнем регистре, цифры, _ и -")
    accounts = load()
    if login in accounts:
        sys.exit(f"Сотрудник {login} уже есть. Сменить пароль: passwd {login}, новый QR: reset-2fa {login}")
    acc = StaffAccount(login, args.role, hash_password(ask_password()), totp.new_secret())
    show_qr(acc, brand())
    confirm_code(acc)
    accounts[login] = acc
    save(accounts)
    print(f"\nСотрудник {login} ({ROLES[args.role]}) добавлен.")
    if not get_var(read_env(), "ADMIN_PATH"):
        cmd_init(argparse.Namespace(force=False))
    else:
        restart_hint()


def _existing(login: str) -> tuple[dict[str, StaffAccount], StaffAccount]:
    accounts = load()
    acc = accounts.get(login.strip().lower())
    if acc is None:
        sys.exit(f"Сотрудника {login} нет. Список: list")
    return accounts, acc


def cmd_passwd(args) -> None:
    accounts, acc = _existing(args.login)
    accounts[acc.login] = StaffAccount(acc.login, acc.role, hash_password(ask_password()), acc.totp_secret)
    save(accounts)
    print("Пароль изменён.")
    restart_hint()


def cmd_reset_2fa(args) -> None:
    accounts, acc = _existing(args.login)
    acc = StaffAccount(acc.login, acc.role, acc.password_hash, totp.new_secret())
    show_qr(acc, brand())
    confirm_code(acc)
    accounts[acc.login] = acc
    save(accounts)
    print("Старый код больше не действует.")
    restart_hint()


def cmd_role(args) -> None:
    accounts, acc = _existing(args.login)
    accounts[acc.login] = StaffAccount(acc.login, args.role, acc.password_hash, acc.totp_secret)
    save(accounts)
    print(f"{acc.login}: {ROLES[args.role]}")
    restart_hint()


def cmd_remove(args) -> None:
    accounts, acc = _existing(args.login)
    del accounts[acc.login]
    save(accounts)
    print(f"{acc.login} удалён. Его открытые сессии перестанут действовать после перезапуска api.")
    restart_hint()


def cmd_list(args) -> None:
    accounts = load()
    print(f"Адрес панели: {panel_url()}")
    https_hint()
    ips = get_var(read_env(), "ADMIN_ALLOWED_IPS")
    print(f"Разрешённые IP: {ips or 'любые (ADMIN_ALLOWED_IPS не задан)'}")
    if not accounts:
        print("Сотрудников нет.")
    for a in accounts.values():
        print(f"  {a.login:<20} {ROLES[a.role]}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="admin", description="Сотрудники панели администратора")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="сгенерировать секретный адрес панели")
    p.add_argument("--force", action="store_true", help="сменить существующий адрес")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("add", help="добавить сотрудника (пароль + QR для 2FA)")
    p.add_argument("login")
    p.add_argument("--role", choices=list(ROLES), default="support")
    p.set_defaults(fn=cmd_add)
    for name, fn, hint in [("passwd", cmd_passwd, "сменить пароль"), ("reset-2fa", cmd_reset_2fa, "новый QR-код"),
                           ("remove", cmd_remove, "удалить сотрудника")]:
        p = sub.add_parser(name, help=hint)
        p.add_argument("login")
        p.set_defaults(fn=fn)
    p = sub.add_parser("role", help="сменить роль")
    p.add_argument("login")
    p.add_argument("role", choices=list(ROLES))
    p.set_defaults(fn=cmd_role)
    sub.add_parser("list", help="список сотрудников").set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nОтменено, ничего не изменено.")


if __name__ == "__main__":
    main()
