"""SQLite: одна база-файл, без отдельного сервера. Для 100 судов этого с запасом."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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
-- происшествия и опасности из AIS: бедствие, аварийные маяки, «на мели», спасательная авиация
CREATE TABLE IF NOT EXISTS incidents(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,              -- когда впервые получено
    last_seen TEXT NOT NULL,       -- последнее повторение
    kind TEXT NOT NULL,            -- distress | sart | mob | epirb | aground | nuc | sar | safety
    mmsi TEXT NOT NULL,
    name TEXT,
    lat REAL, lon REAL,
    text TEXT NOT NULL DEFAULT '',
    test INTEGER NOT NULL DEFAULT 0,
    source TEXT,                   -- тип сообщения AIS
    extra TEXT,                    -- высота самолёта, адресат сообщения и т. п.
    count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_incidents_seen ON incidents(last_seen);
CREATE INDEX IF NOT EXISTS idx_incidents_mmsi ON incidents(mmsi);
CREATE INDEX IF NOT EXISTS idx_notifications_mmsi ON notifications(mmsi);
-- конструктор отслеживания: правила пользователя, их состояние по судам, свои области на карте
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,            -- zone_enter | zone_exit | zone_near | ais_lost | nav_status | speed | destination | port_arrival | port_departure | incident
    target_type TEXT NOT NULL,     -- all | fleet | vessel
    target TEXT,                   -- id флота или MMSI
    params TEXT NOT NULL,          -- параметры условия (JSON)
    audience TEXT NOT NULL DEFAULT 'me',   -- me | fleet (все, кто видит флот)
    severity TEXT NOT NULL DEFAULT 'warning',
    created_at TEXT NOT NULL,
    last_fired TEXT,
    fired_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rule_state(
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    mmsi TEXT NOT NULL,
    value TEXT,
    since TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(rule_id, mmsi)
);
CREATE TABLE IF NOT EXISTS areas(
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    geometry TEXT NOT NULL,        -- GeoJSON Polygon
    created_at TEXT NOT NULL
);
-- санкционные списки (суда по IMO) и задержания в портах; screening_hits — что уже сообщили по судам флотов
CREATE TABLE IF NOT EXISTS sanctions(
    imo TEXT NOT NULL,
    source TEXT NOT NULL,          -- ofac | uk | eu | un
    name TEXT, program TEXT, listed_on TEXT, details TEXT, url TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    PRIMARY KEY(imo, source)
);
CREATE TABLE IF NOT EXISTS detentions(
    source TEXT NOT NULL,          -- tokyo
    imo TEXT NOT NULL,
    detained_on TEXT NOT NULL,
    name TEXT, flag TEXT, built TEXT, gt TEXT, ship_type TEXT, company TEXT, port TEXT,
    released_on TEXT, reasons TEXT, first_seen TEXT NOT NULL,
    PRIMARY KEY(source, imo, detained_on)
);
CREATE INDEX IF NOT EXISTS idx_detentions_imo ON detentions(imo);
CREATE TABLE IF NOT EXISTS screening_hits(
    mmsi TEXT NOT NULL, imo TEXT NOT NULL, ref TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY(mmsi, imo, ref)
);
CREATE TABLE IF NOT EXISTS news(
    imo TEXT NOT NULL, title_key TEXT NOT NULL, url TEXT, title TEXT, outlet TEXT,
    published TEXT, found_at TEXT NOT NULL, topics TEXT,
    PRIMARY KEY(imo, title_key)
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
            # в режиме WAL NORMAL безопасен (при сбое питания теряются лишь последние транзакции) и заметно быстрее FULL
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _migrate_fleet_owner(_conn)
            _conn.executescript(SCHEMA)
            # миграции: новые колонки в существующей базе
            cols = {r[1] for r in _conn.execute("PRAGMA table_info(ais_state)")}
            for col, typ in (("length", "REAL"), ("beam", "REAL"), ("draught", "REAL"), ("eta", "TEXT"),
                             ("nav", "INTEGER")):
                if col not in cols:
                    _conn.execute(f"ALTER TABLE ais_state ADD COLUMN {col} {typ}")
            # уведомление может быть адресовано одному пользователю (сработало его правило)
            if "username" not in {r[1] for r in _conn.execute("PRAGMA table_info(notifications)")}:
                _conn.execute("ALTER TABLE notifications ADD COLUMN username TEXT")
            # индексы для выборки уведомлений пользователя (колонка username появляется только после миграции)
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_mmsi ON notifications(mmsi)")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(username)")
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


NOTIFICATIONS_KEEP_DAYS = 90


def cleanup_notifications() -> int:
    """Уведомления старше 90 дней и их отметки «прочитано» — удаляем, иначе таблица растёт бесконечно."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NOTIFICATIONS_KEEP_DAYS)).replace(microsecond=0).isoformat()
    n = execute("DELETE FROM notifications WHERE ts < ?", (cutoff,)).rowcount
    if n:
        execute("DELETE FROM notification_reads WHERE notification_id NOT IN (SELECT id FROM notifications)")
    return n


def notify(kind: str, severity: str, title: str, body: str = "",
           mmsi: Optional[str] = None, zone_id: Optional[str] = None, username: Optional[str] = None) -> int:
    """username — адресат; без него уведомление видят все, кому доступно судно (или все, если судна нет)."""
    cur = execute(
        "INSERT INTO notifications(ts, kind, severity, title, body, mmsi, zone_id, username) VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), kind, severity, title, body, mmsi, zone_id, username),
    )
    return int(cur.lastrowid or 0)
