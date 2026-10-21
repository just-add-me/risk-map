"""Все суда из потока AIS (не только флоты) — в памяти, без записи в базу.

Поток aisstream по всему миру — ~70–150 сообщений в секунду, за час набирается
20–40 тыс. судов. Позиции старше TTL забываются, паспортные данные (тип,
габариты, IMO) живут сутки: суда передают их раз в несколько минут.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from . import shipinfo

POSITION_TTL = 2 * 3600
STATIC_TTL = 24 * 3600

_lock = threading.Lock()
_ships: dict[str, dict] = {}
_last_cleanup = 0.0


def update_position(mmsi: str, lat: float, lon: float, sog: Optional[float], cog: Optional[float],
                    heading: Optional[float], name: Optional[str], ais_class: str) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return
    now = time.time()
    with _lock:
        s = _ships.setdefault(mmsi, {"mmsi": mmsi})
        s.update(lat=round(lat, 5), lon=round(lon, 5), sog=sog, cog=cog, heading=heading, ts=now,
                 cls=ais_class)
        if name and not s.get("name"):
            s["name"] = name
    _maybe_cleanup(now)


def update_static(mmsi: str, **fields) -> None:
    with _lock:
        s = _ships.setdefault(mmsi, {"mmsi": mmsi})
        s.update({k: v for k, v in fields.items() if v not in (None, "")})
        s["static_ts"] = time.time()


def _maybe_cleanup(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < 300:
        return
    _last_cleanup = now
    with _lock:
        for k in [k for k, s in _ships.items()
                  if now - s.get("ts", 0) > POSITION_TTL and now - s.get("static_ts", 0) > STATIC_TTL]:
            del _ships[k]


def get(mmsi: str) -> Optional[dict]:
    with _lock:
        s = _ships.get(mmsi)
        return dict(s) if s else None


def details(s: dict) -> dict:
    """Карточка судна для интерфейса: всё, что удалось расшифровать."""
    return {
        "mmsi": s["mmsi"], "name": s.get("name"), "imo": s.get("imo"), "callsign": s.get("callsign"),
        "flag": shipinfo.flag(s["mmsi"]), "ship_type": s.get("ship_type"),
        "type_name": shipinfo.type_name(s.get("ship_type")), "category": shipinfo.category(s.get("ship_type")),
        "length": s.get("length"), "beam": s.get("beam"), "draught": s.get("draught"),
        "destination": s.get("destination"), "eta": s.get("eta"), "ais_class": s.get("cls"),
        "lat": s.get("lat"), "lon": s.get("lon"), "sog": s.get("sog"), "cog": s.get("cog"),
        "heading": s.get("heading"), "age_s": int(time.time() - s["ts"]) if s.get("ts") else None,
    }


def in_bbox(west: float, south: float, east: float, north: float, limit: int) -> tuple[list[list], dict, int]:
    """Суда в прямоугольнике карты: компактные строки, счётчики по категориям, сколько всего."""
    now = time.time()
    wrap = west > east          # прямоугольник через линию перемены дат
    rows, counts = [], {k: 0 for k in shipinfo.CATEGORIES}
    with _lock:
        ships = [s for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL]
    for s in ships:
        lat, lon = s["lat"], s["lon"]
        if not (south <= lat <= north and ((lon >= west or lon <= east) if wrap else west <= lon <= east)):
            continue
        cat = shipinfo.category(s.get("ship_type"))
        counts[cat] += 1
        rows.append((s["ts"], [s["mmsi"], lat, lon, s.get("cog"), s.get("sog"), cat, s.get("name") or "",
                               s.get("length")]))
    total = len(rows)
    if total > limit:
        rows.sort(key=lambda r: -r[0])         # свежие позиции важнее
        rows = rows[:limit]
    return [r[1] for r in rows], counts, total


def stats() -> dict:
    now = time.time()
    with _lock:
        live = sum(1 for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL)
        return {"ships": live, "known": len(_ships)}
