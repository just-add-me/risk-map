"""Активные зоны всех слоёв + индекс для проверок в памяти.

Слои:
  jwc — JWC Listed Areas (страхование военных рисков), файл data/zones/active.geojson;
  ibf — зоны IBF/ITF для экипажа (WOA, HRA, ERZ), data/zones/ibf.geojson;
  inl — International Navigating Limits (бывш. IWL), data/zones/inl.geojson.
Для API и проверок слои склеиваются в один набор; у каждой зоны есть свойство layer.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from . import config
from .geo.zone_index import ZoneIndex

log = logging.getLogger(__name__)

ACTIVE_FILE = config.ZONES_DIR / "active.geojson"
LAYER_FILES = {
    "jwc": ACTIVE_FILE,
    "ibf": config.ZONES_DIR / "ibf.geojson",
    "inl": config.ZONES_DIR / "inl.geojson",
}
DISPLAY_V = 2              # версия контуров без суши: при изменении способа построения — увеличить
_lock = threading.RLock()
_index = ZoneIndex()
_layers: dict[str, dict] = {}
_raw: Optional[dict] = None
_version = 0


def load_active() -> None:
    global _raw, _version
    with _lock:
        _layers.clear()
        for name, path in LAYER_FILES.items():
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    _layers[name] = json.load(f)
        features = []
        for name, fc in _layers.items():
            for feat in fc.get("features", []):
                feat.setdefault("properties", {}).setdefault("layer", name)
                features.append(feat)
        jwc_meta = dict((_layers.get("jwc") or {}).get("properties") or {})
        _raw = {
            "type": "FeatureCollection",
            "properties": {**jwc_meta, "layers": {n: fc.get("properties") or {} for n, fc in _layers.items()}},
            "features": features,
        } if _layers else None
        _index.load(_raw or {"features": []})
        _version += 1
        log.info("Загружены зоны: %s", ", ".join(f"{n} {len(fc.get('features', []))}" for n, fc in _layers.items()))


def set_active(fc: dict) -> None:
    """Активные зоны JWC (вызывается после построения циркуляра)."""
    set_layer("jwc", fc)


def set_layer(name: str, fc: dict) -> None:
    """Атомарно заменяет слой (сначала во временный файл, потом rename)."""
    path = LAYER_FILES[name]
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    load_active()


def for_map() -> Optional[dict]:
    """Зоны для карты: вместо полного контура — контур без суши (feature["display"]), если он есть."""
    if not _raw:
        return None
    feats = [{"type": "Feature", "properties": f.get("properties") or {},
              "geometry": _seamless(f.get("display") or f["geometry"])} for f in _raw["features"]]
    return {**_raw, "features": feats}


SEAM_SNAP = 0.12          # градусов: точки ближе к 180° ставим ровно на 180°


def _seamless(geom: dict) -> dict:
    """Готовит контур к стыку на 180-м меридиане (карта рисует копии мира со сдвигом 360°).

    Части за пределами ±180° переносятся на другую сторону, а точки у самого меридиана ставятся
    ровно на него — тогда край по 180° на карте не обводится и шов не виден.
    """
    from shapely import affinity
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union
    from .geo.builder import polygonal

    def snap(c):
        if isinstance(c[0], (int, float)):
            x = c[0]
            return [180.0 if x > 180 - SEAM_SNAP else -180.0 if x < -180 + SEAM_SNAP else x, *c[1:]]
        return [snap(x) for x in c]

    try:
        g = shape(geom)
        minx, _, maxx, _ = g.bounds
        if minx < -180 or maxx > 180:
            g = polygonal(unary_union([
                g.intersection(box(-180, -90, 180, 90)),
                affinity.translate(g.intersection(box(-540, -90, -180, 90)), xoff=360),
                affinity.translate(g.intersection(box(180, -90, 540, 90)), xoff=-360)]))
            geom = mapping(g)
        if minx > -180 + SEAM_SNAP and maxx < 180 - SEAM_SNAP:
            return geom
        geom = json.loads(json.dumps(geom))          # mapping() даёт кортежи
        return {**geom, "coordinates": snap(geom["coordinates"])}
    except Exception:  # noqa: BLE001 — карта важнее красоты шва
        log.exception("Не удалось подготовить контур к стыку на 180°")
        return geom


def ensure_display() -> None:
    """Один раз дополняет уже построенные слои контуром без суши (новые зоны получают его при построении)."""
    from .geo.builder import World, add_display
    todo = [n for n in ("jwc", "ibf") if (_layers.get(n) or {}).get("features")
            and (_layers[n].get("properties") or {}).get("display_v", 0) < DISPLAY_V]
    if not todo:
        return
    w = World()
    for name in todo:
        fc = json.loads(json.dumps(_layers[name]))
        for feat in fc["features"]:
            add_display(w, feat)
        fc.setdefault("properties", {})["display_v"] = DISPLAY_V
        set_layer(name, fc)
        log.info("Слой %s: добавлены контуры без суши", name)


def index() -> ZoneIndex:
    return _index


def raw() -> Optional[dict]:
    return _raw


def version() -> int:
    return _version


def layer(name: str) -> Optional[dict]:
    return _layers.get(name)


def active_ref() -> Optional[str]:
    return ((_layers.get("jwc") or {}).get("properties") or {}).get("ref")


def zone_name(zone_id: str) -> str:
    z = _index.get(zone_id)
    return z.props.get("name", zone_id) if z else zone_id


def in_season(props: dict, when: Optional[datetime] = None) -> bool:
    """Сезонные ограничения INL: {"from": "12-10", "to": "05-25"} (месяц-день, через Новый год)."""
    season = props.get("season")
    if not season:
        return True
    md = (when or datetime.now(timezone.utc)).strftime("%m-%d")
    start, end = season["from"], season["to"]
    return start <= md <= end if start <= end else (md >= start or md <= end)


def in_effect(zone_id: str, when: Optional[datetime] = None) -> bool:
    z = _index.get(zone_id)
    return True if z is None else in_season(z.props, when)
