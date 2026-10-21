"""Все суда из потока AIS (не только флоты) — в памяти, без записи в базу.

Поток aisstream по всему миру — ~70–150 сообщений в секунду, за час набирается
20–40 тыс. судов. Позиции старше TTL забываются, паспортные данные (тип,
габариты, IMO) живут неделю: суда передают их редко, а меняются они почти никогда.

Чтобы после перезапуска не ждать, пока слой наберётся заново, всё сохраняется
на диск (data/traffic.json.gz) раз в 5 минут и при остановке, а при старте читается.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import threading
import time
from typing import Optional

from .. import config
from . import shipinfo

log = logging.getLogger(__name__)
SNAPSHOT_FILE = config.DATA_DIR / "traffic.json.gz"

POSITION_TTL = 2 * 3600
STATIC_TTL = 7 * 24 * 3600
# короткий трек в памяти: точка не чаще раза в 10 минут, до 3 часов
TRAIL_STEP = 600
TRAIL_POINTS = 18
# группировка на карте: ячейка ~36 пикселей; группа — только если судов в ячейке столько,
# что значки налезают друг на друга. С CLUSTER_MAX_ZOOM — все суда по отдельности
CLUSTER_PX = 36
CLUSTER_MAX_ZOOM = 10


def min_group(zoom: int) -> int:
    """Сколько судов в ячейке нужно для группы: на мелком масштабе — 2, на крупном — больше."""
    return 2 if zoom <= 4 else 3 if zoom <= 6 else 5

_lock = threading.Lock()
_ships: dict[str, dict] = {}
_last_cleanup = 0.0


def update_position(mmsi: str, lat: float, lon: float, sog: Optional[float], cog: Optional[float],
                    heading: Optional[float], name: Optional[str], ais_class: str,
                    nav: Optional[int] = None) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return
    now = time.time()
    with _lock:
        s = _ships.setdefault(mmsi, {"mmsi": mmsi})
        trail = s.setdefault("trail", [])
        if not trail or now - trail[-1][2] >= TRAIL_STEP:
            trail.append((round(lat, 4), round(lon, 4), now))
            del trail[:-TRAIL_POINTS]
        s.update(lat=round(lat, 5), lon=round(lon, 5), sog=sog, cog=cog, heading=heading, ts=now,
                 cls=ais_class)
        if nav is not None and nav != 15:
            s["nav"] = nav
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
        for k in [k for k, s in _ships.items() if _expired(s, now)]:
            del _ships[k]


def _expired(s: dict, now: float) -> bool:
    return now - s.get("ts", 0) > POSITION_TTL and now - s.get("static_ts", 0) > STATIC_TTL


def save() -> int:
    """Снимок на диск: сначала во временный файл, потом rename — файл никогда не бывает оборванным."""
    now = time.time()
    with _lock:
        data = [dict(s) for s in _ships.values() if not _expired(s, now)]
    tmp = SNAPSHOT_FILE.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
        json.dump({"saved": now, "ships": data}, f, separators=(",", ":"))
    tmp.replace(SNAPSHOT_FILE)
    return len(data)


def load() -> int:
    if not SNAPSHOT_FILE.exists():
        return 0
    try:
        with gzip.open(SNAPSHOT_FILE, "rt", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("Снимок судов не прочитан: %s", exc)
        return 0
    now = time.time()
    with _lock:
        for s in snap.get("ships", []):
            if _expired(s, now) or s["mmsi"] in _ships:
                continue
            s["trail"] = [tuple(p) for p in s.get("trail", [])]
            if now - s.get("ts", 0) > POSITION_TTL:       # позиция устарела — оставляем только паспорт
                for k in ("lat", "lon", "sog", "cog", "heading", "ts", "trail", "nav"):
                    s.pop(k, None)
            _ships[s["mmsi"]] = s
        n = len(_ships)
    log.info("Загружен снимок судов: %d (сохранён %d мин назад)", n, int((now - snap.get("saved", now)) / 60))
    return n


def get(mmsi: str) -> Optional[dict]:
    with _lock:
        s = _ships.get(mmsi)
        return dict(s) if s else None


def trail(mmsi: str) -> list[dict]:
    with _lock:
        s = _ships.get(mmsi) or {}
        pts = list(s.get("trail", []))
        if s.get("lat") is not None:
            pts.append((s["lat"], s["lon"], s["ts"]))
    return [{"lat": a, "lon": b, "age_s": int(time.time() - t)} for a, b, t in pts]


def details(s: dict) -> dict:
    """Карточка судна для интерфейса: всё, что удалось расшифровать."""
    return {
        "mmsi": s["mmsi"], "name": s.get("name"), "imo": s.get("imo"), "callsign": s.get("callsign"),
        "flag": shipinfo.flag(s["mmsi"]), "flag_iso": shipinfo.flag_iso(s["mmsi"]),
        "nav_status": shipinfo.nav_status(s.get("nav")), "ship_type": s.get("ship_type"),
        "type_name": shipinfo.type_name(s.get("ship_type")), "category": shipinfo.category(s.get("ship_type")),
        "length": s.get("length"), "beam": s.get("beam"), "draught": s.get("draught"),
        "destination": s.get("destination"), "eta": s.get("eta"), "ais_class": s.get("cls"),
        "lat": s.get("lat"), "lon": s.get("lon"), "sog": s.get("sog"), "cog": s.get("cog"),
        "heading": s.get("heading"), "age_s": int(time.time() - s["ts"]) if s.get("ts") else None,
    }


def query(west: float, south: float, east: float, north: float, zoom: int, cats: Optional[set[str]] = None,
          limit: int = 5000, exclude: Optional[set[str]] = None) -> dict:
    """Суда в прямоугольнике карты; на мелком масштабе близкие суда объединяются в группы.

    Сетка — ячейки ~CLUSTER_PX пикселей в проекции Меркатора (как на карте). Ячейка с одним
    судном отдаётся как судно, с несколькими — как группа с числом и рамкой для приближения.
    cats — какие категории показывать (счётчики по категориям считаются по всем).
    """
    now = time.time()
    wrap = west > east          # прямоугольник через линию перемены дат
    counts = {k: 0 for k in shipinfo.CATEGORIES}
    with _lock:
        ships = [s for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL]
    picked = []
    for s in ships:
        lat, lon = s["lat"], s["lon"]
        if not (south <= lat <= north and ((lon >= west or lon <= east) if wrap else west <= lon <= east)):
            continue
        cat = shipinfo.category(s.get("ship_type"))
        counts[cat] += 1
        if (cats is None or cat in cats) and not (exclude and s["mmsi"] in exclude):
            picked.append((s, cat))
    total = len(picked)
    cell = None if zoom >= CLUSTER_MAX_ZOOM else CLUSTER_PX * 360.0 / (256 * 2 ** max(zoom, 0))
    groups: dict[tuple[int, int], list] = {}
    for s, cat in picked:
        if cell is None:
            key = (id(s), 0)
        else:
            y = math.degrees(math.log(math.tan(math.pi / 4 + math.radians(max(min(s["lat"], 85), -85)) / 2)))
            key = (math.floor(s["lon"] / cell), math.floor(y / cell))
        groups.setdefault(key, []).append((s, cat))
    singles, clusters = [], []
    need = min_group(zoom)
    for members in groups.values():
        if len(members) < need:
            for s, cat in members:
                singles.append((s["ts"], [s["mmsi"], s["lat"], s["lon"], s.get("cog"), s.get("sog"), cat,
                                          s.get("name") or "", s.get("length")]))
            continue
        lats = [m[0]["lat"] for m in members]
        lons = [m[0]["lon"] for m in members]
        by_cat: dict[str, int] = {}
        for _, cat in members:
            by_cat[cat] = by_cat.get(cat, 0) + 1
        clusters.append({"lat": round(sum(lats) / len(lats), 4), "lon": round(sum(lons) / len(lons), 4),
                         "n": len(members), "bbox": [min(lons), min(lats), max(lons), max(lats)], "cats": by_cat})
    if len(singles) > limit:
        singles.sort(key=lambda r: -r[0])      # свежие позиции важнее
        singles = singles[:limit]
    return {"ships": [r[1] for r in singles], "clusters": clusters, "counts": counts, "total": total}


def stats() -> dict:
    now = time.time()
    with _lock:
        live = sum(1 for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL)
        return {"ships": live, "known": len(_ships)}
