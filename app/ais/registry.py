"""Справочник всех судов, которые хоть раз попадали в поток AIS, — чтобы находить их поиском и открывать
карточку, даже когда судно ушло из зоны покрытия береговых станций.

В памяти (traffic.py) позиция живёт 2 часа, паспорт — неделю. Сюда раз в 5 минут записывается всё, что
изменилось: паспорт (название, IMO, тип, габариты, назначение) и последняя позиция со временем. Хранится
без срока — ~200 байт на судно, сотни тысяч судов это десятки мегабайт. Отдельный файл data/ships.db,
чтобы запись справочника не мешала основной базе.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Optional

from .. import config
from . import traffic

log = logging.getLogger(__name__)

DB_FILE = config.DATA_DIR / "ships.db"
FIELDS = ("name", "imo", "callsign", "ship_type", "length", "beam", "draught", "destination", "eta", "cls")
POS = ("lat", "lon", "sog", "cog", "heading", "nav")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_last_flush = 0.0


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS ships(
            mmsi TEXT PRIMARY KEY, {', '.join(f'{f}' for f in FIELDS)}, {', '.join(POS)},
            pos_ts REAL, static_ts REAL, first_seen REAL, updated REAL);
        CREATE INDEX IF NOT EXISTS ships_imo ON ships(imo);
        CREATE INDEX IF NOT EXISTS ships_name ON ships(name);
        """)
    return _conn


def flush() -> int:
    """Записать изменившиеся с прошлого раза суда из памяти. Пустые поля старое значение не затирают."""
    global _last_flush
    since, now = _last_flush, time.time()
    rows = [s for s in traffic.changed_since(since)]
    if not rows:
        return 0
    cols = ("mmsi",) + FIELDS + POS + ("pos_ts", "static_ts")
    keep = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in FIELDS)
    # позицию обновляем целиком и только более свежей
    pos = ", ".join(f"{c}=CASE WHEN excluded.pos_ts > COALESCE(pos_ts, 0) THEN excluded.{c} ELSE {c} END"
                    for c in POS + ("pos_ts",))
    sql = (f"INSERT INTO ships({', '.join(cols)}, first_seen, updated) VALUES({', '.join('?' * (len(cols) + 2))}) "
           f"ON CONFLICT(mmsi) DO UPDATE SET {keep}, {pos}, static_ts=COALESCE(excluded.static_ts, static_ts), "
           f"updated=excluded.updated")
    data = [tuple(s.get(c) for c in ("mmsi",) + FIELDS + POS) + (s.get("ts"), s.get("static_ts"), now, now)
            for s in rows]
    with _lock:
        c = _db()
        c.execute("BEGIN")
        c.executemany(sql, data)
        c.execute("COMMIT")
    _last_flush = now
    return len(data)


def _ship(r: sqlite3.Row) -> dict:
    """Запись справочника в виде, как у судна в памяти (traffic.details понимает)."""
    s = {k: r[k] for k in ("mmsi",) + FIELDS + POS if r[k] is not None}
    if r["pos_ts"]:
        s["ts"] = r["pos_ts"]
    return s


def get(mmsi: str) -> Optional[dict]:
    with _lock:
        r = _db().execute("SELECT * FROM ships WHERE mmsi=?", (mmsi,)).fetchone()
    return _ship(r) if r else None


def find_imo(imo: str) -> Optional[str]:
    """MMSI с этим IMO, который передавал позицию последним."""
    with _lock:
        r = _db().execute("SELECT mmsi FROM ships WHERE imo=? ORDER BY COALESCE(pos_ts, 0) DESC LIMIT 1",
                          (imo,)).fetchone()
    return r["mmsi"] if r else None


def search(q: str, limit: int = 12) -> list[dict]:
    """Как traffic.search, но по всем судам, которые когда-либо видели. Свежие позиции выше."""
    q = q.strip().upper()
    if len(q) < 2:
        return []
    if q.isdigit():
        where, rank = "mmsi LIKE ? OR imo LIKE ?", "CASE WHEN mmsi=? OR imo=? THEN 0 ELSE 1 END"
        args, rargs = (f"{q}%", f"{q}%"), (q, q)
    else:
        where = "UPPER(name) LIKE ? OR UPPER(callsign)=?"
        rank = "CASE WHEN UPPER(name)=? OR UPPER(callsign)=? THEN 0 WHEN UPPER(name) LIKE ? THEN 1 ELSE 2 END"
        args, rargs = (f"%{q}%", q), (q, q, f"{q}%")
    with _lock:
        rows = _db().execute(f"SELECT *, {rank} AS rk FROM ships WHERE {where} "
                             f"ORDER BY rk, COALESCE(pos_ts, 0) DESC LIMIT ?", (*rargs, *args, limit)).fetchall()
    return [traffic.details(_ship(r)) for r in rows]


def count() -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) FROM ships").fetchone()[0]
