"""SQLite: одна база-файл, без отдельного сервера. Для 100 судов этого с запасом."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleets(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT NOT NULL,            -- имя пользователя-владельца
    created_at TEXT NOT NULL,
    UNIQUE(owner, name)
);
-- кому владелец открыл флот: только просмотр (can_edit=0) или с правом менять состав судов
CREATE TABLE IF NOT EXISTS fleet_shares(
    fleet_id INTEGER NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    can_edit INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(fleet_id, username)
);
-- у каждого пользователя свои отметки «прочитано»
CREATE TABLE IF NOT EXISTS notification_reads(
    username TEXT NOT NULL,
    notification_id INTEGER NOT NULL,
    PRIMARY KEY(username, notification_id)
);
CREATE TABLE IF NOT EXISTS vessels(
    id INTEGER PRIMARY KEY,
    fleet_id INTEGER NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
    mmsi TEXT NOT NULL,
    imo TEXT,
    name TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(fleet_id, mmsi)
);
-- последнее известное состояние судна по AIS (одно на MMSI, даже если судно в нескольких флотах)
CREATE TABLE IF NOT EXISTS ais_state(
    mmsi TEXT PRIMARY KEY,
    lat REAL, lon REAL, sog REAL, cog REAL, heading REAL,
    ts TEXT,
    ais_name TEXT, imo TEXT, callsign TEXT, ship_type INTEGER, destination TEXT,
    source TEXT,
    stale_notified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions(
    id INTEGER PRIMARY KEY,
    mmsi TEXT NOT NULL,
    ts TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL,
    sog REAL, cog REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_mmsi_ts ON positions(mmsi, ts);
CREATE TABLE IF NOT EXISTS zone_state(
    mmsi TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    status TEXT NOT NULL,          -- inside | near
    since TEXT NOT NULL,
    distance_nm REAL,
    PRIMARY KEY(mmsi, zone_id)
);
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,            -- zone_enter | zone_exit | zone_near | circular | ais_stale | system
    severity TEXT NOT NULL,        -- critical | warning | info
    title TEXT NOT NULL,
    body TEXT,
    mmsi TEXT,
    zone_id TEXT,
    is_read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications(ts);
CREATE TABLE IF NOT EXISTS circulars(
    id INTEGER PRIMARY KEY,
    ref TEXT NOT NULL UNIQUE,      -- JWLA-035
    number INTEGER NOT NULL,
    issued TEXT,                   -- дата циркуляра, ISO
    source_url TEXT,
    file_name TEXT,
    text TEXT,
    spec TEXT,                     -- разобранная структура (JSON)
    diff TEXT,                     -- изменения относительно предыдущего (JSON)
    status TEXT NOT NULL,          -- new | building | active | superseded | error
    build_log TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT
);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,          -- pbkdf2$итерации$соль$хэш, сам пароль не хранится
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _migrate_fleet_owner(_conn)
            _conn.executescript(SCHEMA)
            # миграции: новые колонки в существующей базе
            cols = {r[1] for r in _conn.execute("PRAGMA table_info(ais_state)")}
            for col, typ in (("length", "REAL"), ("beam", "REAL"), ("draught", "REAL"), ("eta", "TEXT")):
                if col not in cols:
                    _conn.execute(f"ALTER TABLE ais_state ADD COLUMN {col} {typ}")
        return _conn


def _migrate_fleet_owner(conn: sqlite3.Connection) -> None:
    """Старая база: флоты без владельца и с глобально уникальным названием.

    Перестраиваем таблицу (так SQLite меняет ограничения), все флоты отдаём администратору,
    его прочитанные уведомления переносим в личные отметки. Перед этим — копия базы.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fleets)")}
    if not cols or "owner" in cols:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = sqlite3.connect(config.DATA_DIR / f"app.db.pre-owners-{stamp}")
    conn.backup(backup)
    backup.close()
    conn.execute("PRAGMA foreign_keys=OFF")       # иначе DROP TABLE удалит суда каскадом
    conn.execute("BEGIN")
    conn.execute("CREATE TABLE fleets_new(id INTEGER PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL, "
                 "created_at TEXT NOT NULL, UNIQUE(owner, name))")
    conn.execute("INSERT INTO fleets_new(id, name, owner, created_at) SELECT id, name, ?, created_at FROM fleets",
                 (config.ADMIN_USER,))
    conn.execute("DROP TABLE fleets")
    conn.execute("ALTER TABLE fleets_new RENAME TO fleets")
    conn.execute("CREATE TABLE IF NOT EXISTS notification_reads(username TEXT NOT NULL, "
                 "notification_id INTEGER NOT NULL, PRIMARY KEY(username, notification_id))")
    conn.execute("INSERT OR IGNORE INTO notification_reads(username, notification_id) "
                 "SELECT ?, id FROM notifications WHERE is_read=1", (config.ADMIN_USER,))
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys=ON")
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise RuntimeError(f"Миграция флотов: нарушены связи {bad[:5]}")


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        return connect().execute(sql, tuple(params))


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def kv_get(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def notify(kind: str, severity: str, title: str, body: str = "",
           mmsi: Optional[str] = None, zone_id: Optional[str] = None) -> int:
    cur = execute(
        "INSERT INTO notifications(ts, kind, severity, title, body, mmsi, zone_id) VALUES(?,?,?,?,?,?,?)",
        (now_iso(), kind, severity, title, body, mmsi, zone_id),
    )
    return int(cur.lastrowid or 0)
