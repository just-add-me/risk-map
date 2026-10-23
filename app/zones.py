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
    feats = []
    for f in _raw["features"]:
        geom = _seamless(f.get("display") or f["geometry"])
        feats.append({"type": "Feature", "properties": {**(f.get("properties") or {}), **_label(geom)}, "geometry": geom})
    outlines = _outlines(feats)
    for f in feats:
        f["geometry"] = _slim(f["geometry"], f["properties"].get("layer") or "jwc")
    for o in outlines:
        o["geometry"] = _slim(o["geometry"], o["properties"]["layer"])
    return {**_raw, "features": feats, "outlines": outlines}


# Упрощение контуров ТОЛЬКО для карты (проверка «судно в зоне» идёт по точным контурам в индексе):
# слой → (допуск упрощения, °; мельче какой площади, кв. °, убираем островки и дырки).
# JWC/IBF: ~0,6 мили — 12-мильные полосы выглядят так же; INL — огромные ледовые районы: ~1,8 мили и без
# островков меньше ~50 км². Вместе с округлением координат до ~100 м — вдвое меньше точек и данных.
SLIM = {"jwc": (0.01, 0.001), "ibf": (0.01, 0.001), "inl": (0.03, 0.005)}


def _slim(geom: dict, layer: str) -> dict:
    from shapely.geometry import MultiPolygon, Polygon, mapping, shape
    from .geo.builder import polygonal
    tol, min_area = SLIM.get(layer, SLIM["jwc"])
    try:
        g = shape(geom)
        parts = []
        for p in getattr(g, "geoms", [g]):
            if p.area < min_area:
                continue
            parts.append(Polygon(p.exterior, [h for h in p.interiors if Polygon(h).area >= min_area]))
        if not parts:
            return geom
        g = polygonal(MultiPolygon(parts) if len(parts) > 1 else parts[0])
        g = polygonal(g.simplify(tol, preserve_topology=True))
        if g.is_empty:
            return geom
        out = json.loads(json.dumps(mapping(g)))

        def rnd(c):
            return [round(c[0], 3), round(c[1], 3)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]
        return {**out, "coordinates": rnd(out["coordinates"])}
    except Exception:  # noqa: BLE001 — лучше показать точный контур, чем ничего
        log.exception("Не удалось упростить контур для карты")
        return geom


# Порядок видов внутри слоя: более важный рисуется целиком, из следующих вырезается то, что он уже покрыл
OUTLINE_ORDER = {"jwc": ["country", "sea", "conditional"], "ibf": ["woa", "hra", "erz", "erz2"]}


def zone_type(p: dict) -> Optional[str]:
    """Вид зоны — как в интерфейсе (static/app.js zoneType)."""
    layer = p.get("layer") or "jwc"
    if layer == "ibf":
        return p.get("category") if p.get("category") in OUTLINE_ORDER["ibf"] else "woa"
    if layer == "inl":
        return "inl_season" if p.get("season") else "inl_perm"
    if p.get("severity") == "conditional":
        return "conditional"
    return "sea" if p.get("kind") == "defined" else "country"


def is_inland(p: dict) -> bool:
    """«Внутренние воды» (реки, лиманы, речные порты): контур — вся территория страны, иначе речной порт
    выпал бы из зоны. На карте такие зоны — пунктир без заливки, чтобы не выглядели «зоной вся страна»."""
    return str(p.get("template") or "").startswith("inland_")


def _outlines(feats: list[dict]) -> list[dict]:
    """«Территории» для карты: зоны одного вида склеены в один контур (граница только по внешнему краю),
    между видами — без наложений. Так у Бахрейна, Катара и ОАЭ не получается клубка пересекающихся линий.
    Внутренние воды — отдельным контуром (inland) после всех остальных: побережье остаётся за морскими зонами."""
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    from .geo.builder import polygonal
    out = []
    for layer, order in OUTLINE_ORDER.items():
        taken = None
        inland = []
        for t in order:
            inland += [(t, shape(f["geometry"]).buffer(0)) for f in feats
                       if (f["properties"].get("layer") or "jwc") == layer and zone_type(f["properties"]) == t
                       and is_inland(f["properties"])]
            gs = [shape(f["geometry"]).buffer(0) for f in feats
                  if (f["properties"].get("layer") or "jwc") == layer and zone_type(f["properties"]) == t
                  and not is_inland(f["properties"])]
            if not gs:
                continue
            try:
                u = unary_union(gs)
                part = polygonal(u.difference(taken) if taken is not None else u)
                taken = u if taken is None else unary_union([taken, u])
                part = polygonal(part.simplify(0.002, preserve_topology=True))
            except Exception:  # noqa: BLE001 — без контура карта покажет зоны по отдельности
                log.exception("Не удалось склеить зоны %s/%s", layer, t)
                continue
            if not part.is_empty:
                out.append({"type": "Feature", "properties": {"layer": layer, "type": t},
                            "geometry": _seamless(mapping(part))})
        for t in dict.fromkeys(t for t, _ in inland):
            try:
                u = unary_union([g for tt, g in inland if tt == t])
                part = polygonal(u.difference(taken) if taken is not None else u)
                part = polygonal(part.simplify(0.002, preserve_topology=True))
            except Exception:  # noqa: BLE001
                log.exception("Не удалось склеить внутренние воды %s/%s", layer, t)
                continue
            if not part.is_empty:
                out.append({"type": "Feature", "properties": {"layer": layer, "type": t, "inland": True},
                            "geometry": _seamless(mapping(part))})
    return out


def _label(geom: dict) -> dict:
    """Точка для значка зоны — «визуальный центр» самой большой части (внутри контура, а не центр
    рамки, который у изогнутой зоны попадает на сушу), и рамка зоны — чтобы прятать значок у мелких зон."""
    from shapely.geometry import shape
    from shapely.ops import polylabel
    try:
        g = shape(geom)
        if g.is_empty:
            return {}
        big = max(getattr(g, "geoms", [g]), key=lambda p: p.area)
        pt = polylabel(big, tolerance=0.05)
        return {"label_pt": [round(pt.y, 3), round(pt.x, 3)], "bbox": [round(v, 3) for v in big.bounds]}
    except Exception:  # noqa: BLE001 — без значка карта всё равно работает
        return {}


SEAM_SNAP = 0.12          # градусов: точки ближе к 180° ставим ровно на 180°


def _seamless(geom: dict) -> dict:
    """Готовит контур к стыку на 180-м меридиане (карта рисует копии мира со сдвигом 360°).

    Части за пределами ±180° обрезаются, а точки у самого меридиана ставятся ровно на него —
    тогда край по 180° на карте не обводится и шов не виден.
    """
    from shapely.geometry import box, mapping, shape
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
            # за ±180° выходит только 12-мильная полоса, построенная по суше с этой стороны; у другой
            # стороны своя полоса, а перенос лёг бы на чужую сушу — поэтому просто обрезаем
            geom = mapping(polygonal(g.intersection(box(-180, -90, 180, 90))))
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
