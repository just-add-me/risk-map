"""Общая точка входа для позиций из любого источника (aisstream или демо)."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import alerts, config, db
from . import traffic

log = logging.getLogger(__name__)

# храним точку трека не чаще, чем раз в N минут или при смещении больше M миль
TRAIL_MIN_MINUTES = 5
TRAIL_MIN_NM = 1.0


# Судно, добавленное только по IMO, хранится с временным «MMSI» вида IMO:1234567, пока
# от него не придёт паспортное сообщение AIS с этим IMO — тогда подставляется настоящий MMSI.
PENDING = "IMO:"


def tracked_mmsis() -> list[str]:
    return [r["mmsi"] for r in db.query("SELECT DISTINCT mmsi FROM vessels WHERE mmsi NOT LIKE 'IMO:%' ORDER BY mmsi")]


def pending_imos() -> set[str]:
    return {r["mmsi"][len(PENDING):] for r in db.query("SELECT DISTINCT mmsi FROM vessels WHERE mmsi LIKE 'IMO:%'")}


def resolve_imo(imo: str, mmsi: str) -> int:
    """Судну, ждавшему MMSI, нашёлся номер: подставляем, сразу берём данные из потока, пишем уведомление."""
    rows = db.query("SELECT id, fleet_id, name FROM vessels WHERE mmsi=?", (PENDING + imo,))
    for r in rows:
        if db.query_one("SELECT id FROM vessels WHERE fleet_id=? AND mmsi=?", (r["fleet_id"], mmsi)):
            db.execute("DELETE FROM vessels WHERE id=?", (r["id"],))      # это судно уже есть во флоте по MMSI
        else:
            db.execute("UPDATE vessels SET mmsi=?, imo=? WHERE id=?", (mmsi, imo, r["id"]))
    if rows:
        backfill_from_traffic(mmsi)
        name = rows[0]["name"] or (traffic.get(mmsi) or {}).get("name") or f"IMO {imo}"
        db.notify("system", "info", f"{name}: найден MMSI {mmsi}",
                  f"Судно было добавлено по IMO {imo}. Теперь его позиция отслеживается.", mmsi)
    return len(rows)


def backfill_from_traffic(mmsi: str) -> None:
    """Если судно уже есть в общем потоке AIS — сразу берём его данные, не ждём нового сообщения."""
    s = traffic.get(mmsi)
    if not s:
        return
    static_data(mmsi, name=s.get("name"), imo=s.get("imo"), callsign=s.get("callsign"),
                ship_type=s.get("ship_type"), destination=s.get("destination"), length=s.get("length"),
                beam=s.get("beam"), draught=s.get("draught"), eta=s.get("eta"))
    if s.get("lat") is not None:
        position(mmsi, s["lat"], s["lon"], sog=s.get("sog"), cog=s.get("cog"), heading=s.get("heading"),
                 source="aisstream", ais_name=s.get("name"), nav=s.get("nav"))


def _dist_nm(lat1, lon1, lat2, lon2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * 60 * k, (lat2 - lat1) * 60)


def position(mmsi: str, lat: float, lon: float, sog: Optional[float] = None, cog: Optional[float] = None,
             heading: Optional[float] = None, ts: Optional[str] = None, source: str = "",
             ais_name: Optional[str] = None, nav: Optional[int] = None) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return
    if lat == 91 or lon == 181:   # «нет данных» в AIS
        return
    ts = ts or db.now_iso()
    prev = db.query_one("SELECT lat, lon, ts FROM ais_state WHERE mmsi=?", (mmsi,))
    first = prev is None or prev.get("lat") is None

    db.execute(
        "INSERT INTO ais_state(mmsi, lat, lon, sog, cog, heading, ts, source, ais_name, nav, stale_notified) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(mmsi) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, "
        "sog=excluded.sog, cog=excluded.cog, heading=excluded.heading, ts=excluded.ts, source=excluded.source, "
        "ais_name=COALESCE(excluded.ais_name, ais_state.ais_name), nav=COALESCE(excluded.nav, ais_state.nav), "
        "stale_notified=0",
        (mmsi, lat, lon, sog, cog, heading, ts, source, ais_name, nav),
    )

    store = first
    if not first:
        last = db.query_one("SELECT lat, lon, ts FROM positions WHERE mmsi=? ORDER BY ts DESC LIMIT 1", (mmsi,))
        if not last:
            store = True
        else:
            try:
                age_min = (datetime.fromisoformat(ts) - datetime.fromisoformat(last["ts"])).total_seconds() / 60
            except ValueError:
                age_min = TRAIL_MIN_MINUTES
            store = age_min >= TRAIL_MIN_MINUTES or _dist_nm(last["lat"], last["lon"], lat, lon) >= TRAIL_MIN_NM
    if store:
        db.execute("INSERT INTO positions(mmsi, ts, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)",
                   (mmsi, ts, lat, lon, sog, cog))

    alerts.process(mmsi, lon, lat, sog, initial=first)
    from .. import rules            # правила пользователей (конструктор отслеживания)
    try:
        rules.on_position(mmsi, lat, lon, sog, nav)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка проверки правил для %s", mmsi)


def static_data(mmsi: str, name: Optional[str] = None, imo: Optional[str] = None,
                callsign: Optional[str] = None, ship_type: Optional[int] = None,
                destination: Optional[str] = None, length: Optional[float] = None,
                beam: Optional[float] = None, draught: Optional[float] = None, eta: Optional[str] = None) -> None:
    db.execute(
        "INSERT INTO ais_state(mmsi, ais_name, imo, callsign, ship_type, destination, length, beam, draught, eta) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(mmsi) DO UPDATE SET ais_name=COALESCE(excluded.ais_name, ais_name), "
        "imo=COALESCE(excluded.imo, imo), callsign=COALESCE(excluded.callsign, callsign), "
        "ship_type=COALESCE(excluded.ship_type, ship_type), destination=COALESCE(excluded.destination, destination), "
        "length=COALESCE(excluded.length, length), beam=COALESCE(excluded.beam, beam), "
        "draught=COALESCE(excluded.draught, draught), eta=COALESCE(excluded.eta, eta)",
        (mmsi, name, imo, callsign, ship_type, destination, length, beam, draught, eta),
    )
    if destination is not None or eta is not None:
        from .. import rules
        row = db.query_one("SELECT destination, eta FROM ais_state WHERE mmsi=?", (mmsi,)) or {}
        try:
            rules.on_static(mmsi, row.get("destination"), row.get("eta"))
        except Exception:  # noqa: BLE001
            log.exception("Ошибка проверки правил для %s", mmsi)


def cleanup_trails() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.TRAIL_HOURS)).replace(microsecond=0).isoformat()
    db.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))
    # суда, которых больше нет ни в одном флоте — убираем их состояние
    db.execute("DELETE FROM ais_state WHERE mmsi NOT IN (SELECT mmsi FROM vessels)")
    db.execute("DELETE FROM zone_state WHERE mmsi NOT IN (SELECT mmsi FROM vessels)")
    db.execute("DELETE FROM positions WHERE mmsi NOT IN (SELECT mmsi FROM vessels)")
