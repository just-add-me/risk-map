"""Дополнительные пользователи панели (кроме администратора из .env).

Пароли хранятся только в виде хэша PBKDF2-SHA256 с солью, сами пароли нигде не сохраняются.
Управление — командой на сервере:

    docker compose exec app python -m app.users list
    docker compose exec app python -m app.users add ivan            # пароль сгенерируется и покажется один раз
    docker compose exec app python -m app.users passwd ivan         # новый пароль
    docker compose exec app python -m app.users delete ivan         # удалённый пользователь сразу теряет доступ
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys

from . import config, db

ITERATIONS = 200_000
NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${digest.hex()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    except ValueError:
        return False
    return hmac.compare_digest(calc.hex(), digest)


def verify(username: str, password: str) -> bool:
    row = db.query_one("SELECT pw_hash FROM users WHERE username=?", (username,))
    if not row:
        _check(password, _hash("x"))     # одинаковое время ответа, есть пользователь или нет
        return False
    return _check(password, row["pw_hash"])


def exists(username: str) -> bool:
    return db.query_one("SELECT 1 AS x FROM users WHERE username=?", (username,)) is not None


def new_password() -> str:
    return secrets.token_urlsafe(12)


def add(username: str, password: str | None = None) -> str:
    if not NAME_RE.match(username):
        raise ValueError("Имя: 3–32 символа, латиница, цифры, точка, дефис, подчёркивание")
    if username == config.ADMIN_USER or exists(username):
        raise ValueError(f"Пользователь {username} уже есть")
    password = password or new_password()
    db.execute("INSERT INTO users(username, pw_hash, created_at) VALUES(?,?,?)",
               (username, _hash(password), db.now_iso()))
    return password


def set_password(username: str, password: str | None = None) -> str:
    if not exists(username):
        raise ValueError(f"Пользователя {username} нет")
    password = password or new_password()
    db.execute("UPDATE users SET pw_hash=? WHERE username=?", (_hash(password), username))
    return password


def delete(username: str) -> None:
    if not exists(username):
        raise ValueError(f"Пользователя {username} нет")
    db.execute("DELETE FROM users WHERE username=?", (username,))


def main(argv: list[str]) -> int:
    db.connect()
    cmd, *args = argv or ["list"]
    try:
        if cmd == "list":
            print(f"{config.ADMIN_USER}  (администратор из .env)")
            for r in db.query("SELECT username, created_at FROM users ORDER BY username"):
                print(f"{r['username']}  (создан {r['created_at']})")
        elif cmd == "add" and args:
            pwd = add(args[0])
            print(f"Создан пользователь {args[0]}\nПароль (показывается один раз): {pwd}")
        elif cmd == "passwd" and args:
            print(f"Новый пароль для {args[0]}: {set_password(args[0])}")
        elif cmd == "delete" and args:
            delete(args[0])
            print(f"Пользователь {args[0]} удалён")
        else:
            print(__doc__)
            return 2
    except ValueError as exc:
        print(f"Ошибка: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
