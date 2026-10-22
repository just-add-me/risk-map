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

import numpy as np

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
# защита памяти: больше судов не держим (вытесняются давно не выходившие на связь)
MAX_SHIPS = 150_000
# одинаковые запросы карты (несколько вкладок, сдвиг + автообновление) отдаём из кэша
QUERY_CACHE_S = 3.0

_CATS = list(shipinfo.CATEGORIES)
_CAT_I = {c: i for i, c in enumerate(_CATS)}


def min_group(zoom: int) -> int:
    """Сколько судов в ячейке нужно для группы: на мелком масштабе — 2, на крупном — больше."""
    return 2 if zoom <= 4 else 3 if zoom <= 6 else 5


_lock = threading.Lock()
_ships: dict[str, dict] = {}
_last_cleanup = 0.0
_qcache: dict[tuple, tuple[float, dict]] = {}
_stats: tuple[float, dict] = (0.0, {})
# массивы координат всех судов для запросов карты: собираются не чаще раза в SNAP_S секунд
SNAP_S = 2.0
_snap_lock = threading.Lock()
_snap: tuple = (0.0, [], None, None, None)


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
        if fields.get("ship_type"):
            s["cat"] = shipinfo.category(fields["ship_type"])     # категорию считаем один раз, а не в каждом запросе
        s["static_ts"] = time.time()


def _maybe_cleanup(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < 300:
        return
    _last_cleanup = now
    with _lock:
        for k in [k for k, s in _ships.items() if _expired(s, now)]:
            del _ships[k]
        extra = len(_ships) - MAX_SHIPS
        if extra > 0:
            oldest = sorted(_ships, key=lambda k: max(_ships[k].get("ts", 0), _ships[k].get("static_ts", 0)))
            for k in oldest[:extra]:
                del _ships[k]
            log.warning("Суда в памяти: достигнут предел %d, вытеснено %d", MAX_SHIPS, extra)


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
            s["cat"] = shipinfo.category(s.get("ship_type"))
            if now - s.get("ts", 0) > POSITION_TTL:       # позиция устарела — оставляем только паспорт
                for k in ("lat", "lon", "sog", "cog", "heading", "ts", "trail", "nav"):
                    s.pop(k, None)
            _ships[s["mmsi"]] = s
        n = len(_ships)
    log.info("Загружен снимок судов: %d (сохранён %d мин назад)", n, int((now - snap.get("saved", now)) / 60))
    return n


def nav_of(mmsi: str) -> Optional[int]:
    """Последний известный навигационный статус (дёшево, без копирования записи)."""
    with _lock:
        return (_ships.get(mmsi) or {}).get("nav")


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

    Сетка — ячейки ~CLUSTER_PX пикселей в проекции Меркатора (как на карте). Ячейка, где судов
    меньше min_group(zoom), отдаётся судами, иначе — группой с числом и рамкой для приближения.
    cats — какие категории показывать (счётчики по категориям считаются по всем).
    """
    key = (round(west, 2), round(south, 2), round(east, 2), round(north, 2), int(zoom),
           tuple(sorted(cats)) if cats is not None else None, limit, tuple(sorted(exclude or ())))
    now = time.time()
    hit = _qcache.get(key)
    if hit and now - hit[0] < QUERY_CACHE_S:
        return hit[1]
    res = _query(west, south, east, north, zoom, cats, limit, exclude or set(), now)
    if len(_qcache) > 64:
        _qcache.clear()
    _qcache[key] = (now, res)
    return res


def reset_caches() -> None:
    """Сбросить кэши запросов карты (для тестов: данные меняются быстрее, чем живут кэши)."""
    global _snap, _stats
    _snap, _stats = (0.0, [], None, None, None), (0.0, {})
    _qcache.clear()


def _arrays(now: float):
    """Живые суда и их координаты/категории массивами numpy; переиспользуются SNAP_S секунд."""
    global _snap
    with _snap_lock:
        if now - _snap[0] < SNAP_S:
            return _snap[1:]
        with _lock:
            live = [s for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL]
        n = len(live)
        lat = np.fromiter((s["lat"] for s in live), float, n)
        lon = np.fromiter((s["lon"] for s in live), float, n)
        cat = np.fromiter((_CAT_I[s.get("cat", "other")] for s in live), np.int64, n)
        _snap = (now, live, lat, lon, cat)
        return _snap[1:]


def _query(west, south, east, north, zoom, cats, limit, exclude, now) -> dict:
    counts = {k: 0 for k in _CATS}
    live, lat, lon, cat = _arrays(now)
    n = len(live)
    if not n:
        return {"ships": [], "clusters": [], "counts": counts, "total": 0}
    # всё дальше — векторами numpy: в 10–20 раз быстрее цикла по десяткам тысяч судов
    inb = (lat >= south) & (lat <= north)
    inb &= ((lon >= west) | (lon <= east)) if west > east else ((lon >= west) & (lon <= east))
    for i, c in enumerate(np.bincount(cat[inb], minlength=len(_CATS))):
        counts[_CATS[i]] = int(c)
    sel = inb
    if cats is not None:
        sel = sel & np.isin(cat, [_CAT_I[c] for c in cats if c in _CAT_I])
    if exclude:
        sel = sel & ~np.fromiter((s["mmsi"] in exclude for s in live), bool, n)
    idx = np.nonzero(sel)[0]
    clusters: list[dict] = []
    singles = idx
    if zoom < CLUSTER_MAX_ZOOM and idx.size:
        singles, clusters = _cluster(idx, lat, lon, cat, zoom)
    if singles.size > limit:                     # свежие позиции важнее
        ts = np.fromiter((live[i]["ts"] for i in singles), float, singles.size)
        singles = singles[np.argsort(-ts, kind="stable")[:limit]]
    rows = []
    for i in singles:
        s = live[i]
        rows.append([s["mmsi"], s["lat"], s["lon"], s.get("cog"), s.get("sog"), _CATS[cat[i]],
                     s.get("name") or "", s.get("length")])
    return {"ships": rows, "clusters": clusters, "counts": counts, "total": int(idx.size)}


def _cluster(idx, lat, lon, cat, zoom):
    """Группировка выбранных судов по сетке; соседние группы сливаются, чтобы кружки не налезали."""
    cell = CLUSTER_PX * 360.0 / (256 * 2 ** max(zoom, 0))
    la, lo, ca = lat[idx], lon[idx], cat[idx]
    my = np.degrees(np.log(np.tan(np.pi / 4 + np.radians(np.clip(la, -85, 85)) / 2)))
    keys = np.floor(lo / cell).astype(np.int64) * 1_000_003 + np.floor(my / cell).astype(np.int64)
    _, inv, cnt = np.unique(keys, return_inverse=True, return_counts=True)
    g, k = cnt.size, len(_CATS)
    # суммы, рамки и счётчики категорий по каждой ячейке
    sum_lo, sum_my, sum_la = (np.bincount(inv, w, g) for w in (lo, my, la))
    lo_min, lo_max, la_min, la_max = np.full(g, np.inf), np.full(g, -np.inf), np.full(g, np.inf), np.full(g, -np.inf)
    np.minimum.at(lo_min, inv, lo)
    np.maximum.at(lo_max, inv, lo)
    np.minimum.at(la_min, inv, la)
    np.maximum.at(la_max, inv, la)
    by_cat = np.bincount(inv * k + ca, minlength=g * k).reshape(g, k)
    need = min_group(zoom)
    big = np.nonzero(cnt >= need)[0]
    merged: list[list] = []                       # [n, Σlon, Σlat, lon_min, lon_max, lat_min, lat_max, категории]
    centers: list[tuple[float, float]] = []
    for gi in big[np.argsort(-cnt[big], kind="stable")]:
        x, y = sum_lo[gi] / cnt[gi], sum_my[gi] / cnt[gi]
        for m, (cx, cy) in zip(merged, centers):
            if abs(cx - x) < cell and abs(cy - y) < cell:
                m[0] += cnt[gi]; m[1] += sum_lo[gi]; m[2] += sum_la[gi]           # noqa: E702
                m[3] = min(m[3], lo_min[gi]); m[4] = max(m[4], lo_max[gi])     # noqa: E702
                m[5] = min(m[5], la_min[gi]); m[6] = max(m[6], la_max[gi])     # noqa: E702
                m[7] = m[7] + by_cat[gi]
                break
        else:
            merged.append([cnt[gi], sum_lo[gi], sum_la[gi], lo_min[gi], lo_max[gi], la_min[gi], la_max[gi],
                           by_cat[gi].copy()])
            centers.append((x, y))
    clusters = [{"lat": round(float(m[2] / m[0]), 4), "lon": round(float(m[1] / m[0]), 4), "n": int(m[0]),
                 "bbox": [float(m[3]), float(m[5]), float(m[4]), float(m[6])],
                 "cats": {_CATS[j]: int(c) for j, c in enumerate(m[7]) if c}} for m in merged]
    singles = idx[cnt[inv] < need]
    return singles, clusters


def find_imo(imo: str) -> Optional[str]:
    """MMSI судна с этим IMO (если оно передавало паспортные данные). Живая позиция — в приоритете."""
    now = time.time()
    best = None
    with _lock:
        for s in _ships.values():
            if str(s.get("imo") or "") == imo:
                live = bool(s.get("ts")) and now - s["ts"] <= POSITION_TTL
                if live:
                    return s["mmsi"]
                best = best or s["mmsi"]
    return best


def search(q: str, limit: int = 12) -> list[dict]:
    """Поиск по названию, MMSI, IMO или позывному. Сначала точные совпадения, потом начало, потом вхождение."""
    q = q.strip().upper()
    if len(q) < 2:
        return []
    now = time.time()
    found = []
    with _lock:
        for s in _ships.values():
            name = (s.get("name") or "").upper()
            fields = (s["mmsi"], str(s.get("imo") or ""), (s.get("callsign") or "").upper())
            if q.isdigit():
                rank = 0 if q in fields[:2] else 1 if any(f.startswith(q) for f in fields[:2]) else None
            elif name == q or fields[2] == q:
                rank = 0
            elif name.startswith(q):
                rank = 1
            elif q in name:
                rank = 2
            else:
                rank = None
            if rank is not None:
                live = bool(s.get("ts")) and now - s["ts"] <= POSITION_TTL
                found.append((rank, not live, name, dict(s)))
    found.sort(key=lambda r: r[:3])
    return [details(r[3]) for r in found[:limit]]


def live_with_imos(imos: set[str]) -> list[dict]:
    """Суда с живой позицией, чей IMO в наборе (санкционные списки)."""
    now = time.time()
    with _lock:
        hits = [dict(s) for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL
                and s.get("lat") is not None and str(s.get("imo") or "") in imos]
    return [details(s) for s in hits]


def near(lat: float, lon: float, nm: float) -> list[dict]:
    """Суда с живой позицией в радиусе nm миль от точки (для карточки порта)."""
    now = time.time()
    dlat = nm / 60.0
    dlon = nm / (60.0 * max(math.cos(math.radians(lat)), 0.05))
    out = []
    with _lock:
        for s in _ships.values():
            if not s.get("ts") or now - s["ts"] > POSITION_TTL:
                continue
            if abs(s["lat"] - lat) <= dlat and abs(((s["lon"] - lon + 180) % 360) - 180) <= dlon:
                k = math.cos(math.radians(lat))
                d = math.hypot((s["lat"] - lat) * 60, (((s["lon"] - lon + 180) % 360) - 180) * 60 * k)
                if d <= nm:
                    out.append({**details(dict(s)), "distance_nm": round(d, 1)})
    return out


def stats() -> dict:
    """Счётчики для шапки; запрашиваются каждые 10 с каждым открытым окном — считаем не чаще раза в 10 с."""
    global _stats
    now = time.time()
    if now - _stats[0] < 10:
        return _stats[1]
    with _lock:
        live = sum(1 for s in _ships.values() if s.get("ts") and now - s["ts"] <= POSITION_TTL)
        _stats = (now, {"ships": live, "known": len(_ships)})
    return _stats[1]
