"""Кто какие флоты видит.

У каждого флота есть владелец. Владелец может поделиться флотом с другим пользователем:
«только просмотр» или «может редактировать» (добавлять и убирать суда). Переименовать,
удалить флот и управлять доступом может только владелец.
"""
from __future__ import annotations

from typing import Optional

from . import config, db, users

LEVELS = {"view": 1, "edit": 2, "owner": 3}

# SQL: флоты, доступные пользователю (параметры: username ×3)
VISIBLE_FLEETS_SQL = (
    "SELECT f.*, CASE WHEN f.owner=? THEN 'owner' WHEN s.can_edit=1 THEN 'edit' ELSE 'view' END AS access "
    "FROM fleets f LEFT JOIN fleet_shares s ON s.fleet_id=f.id AND s.username=? "
    "WHERE (f.owner=? OR s.username IS NOT NULL)"
)
# SQL: MMSI судов во флотах пользователя (параметры: username, username)
VISIBLE_MMSI_SQL = (
    "SELECT v.mmsi FROM vessels v JOIN fleets f ON f.id=v.fleet_id "
    "LEFT JOIN fleet_shares s ON s.fleet_id=f.id AND s.username=? WHERE (f.owner=? OR s.username IS NOT NULL)"
)


def fleets(user: str) -> list[dict]:
    return db.query(VISIBLE_FLEETS_SQL + " ORDER BY f.owner<>? , f.name", (user, user, user, user))


def level(fid: int, user: str) -> Optional[str]:
    row = db.query_one(VISIBLE_FLEETS_SQL + " AND f.id=?", (user, user, user, fid))
    return row["access"] if row else None


def require(fid: int, user: str, need: str) -> None:
    lvl = level(fid, user)
    if lvl is None:
        raise ValueError("Флот не найден")
    if LEVELS[lvl] < LEVELS[need]:
        raise ValueError("Только просмотр: менять этот флот может владелец"
                         if need == "edit" else "Это может сделать только владелец флота")


def mmsis(user: str) -> set[str]:
    return {r["mmsi"] for r in db.query(VISIBLE_MMSI_SQL, (user, user))}


def all_usernames() -> list[str]:
    return [config.ADMIN_USER] + [r["username"] for r in db.query("SELECT username FROM users ORDER BY username")
                                  if r["username"] != config.ADMIN_USER]


def is_user(name: str) -> bool:
    return name == config.ADMIN_USER or users.exists(name)


def shares(fid: int) -> list[dict]:
    return db.query("SELECT username, can_edit FROM fleet_shares WHERE fleet_id=? ORDER BY username", (fid,))
