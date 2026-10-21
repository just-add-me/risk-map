"""Строит полигоны зон из разобранного циркуляра (spec) → GeoJSON.

Принципы:
* «Named Countries» = порты и прибрежные воды до 12 морских миль. Строим как
  территорию страны + полоса 12 миль от её берега (туда попадают и порты,
  и стоянки в устьях рек).
* «Defined Waters» — полигон по координатам из циркуляра; со стороны суши
  контур замыкается точками внутри материка, а потом суша вычитается.
* Буфер в милях считается по плиткам 1°×4° в локальной проекции — так
  12 миль остаются 12 милями и у экватора, и в Арктике.

Нужен shapely>=2.0. Модуль запускается редко: при появлении нового циркуляра.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Optional

import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape, LineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from . import countries as ne

log = logging.getLogger(__name__)

TW_NM = 12.0             # территориальные воды
COAST_TOLERANCE_NM = 1.0  # береговая полоса суши, которую не вычитаем (причалы, гавани)
TILE_H, TILE_W = 1.0, 4.0
EMPTY = Polygon()

# --- Контуры, замыкающие морские зоны по суше (суша потом вычитается) ----------
BLACK_SEA_RING = [(27.2, 41.25), (27.2, 47.6), (42.0, 47.6), (42.0, 40.8), (30.0, 40.8), (30.0, 41.25)]
AZOV_BOX = (34.75, 45.25, 39.5, 47.35)
CRIMEA_BOX = (32.4, 44.35, 36.62, 46.25)
DON_DONETS_RING = [(39.0, 46.95), (39.0, 47.35), (39.6, 47.35), (40.2, 47.45), (40.55, 47.75),
                   (40.15, 48.25), (39.75, 48.65), (40.05, 48.75), (40.55, 48.35), (40.95, 47.85),
                   (41.0, 47.75), (41.0, 47.35), (40.4, 47.15), (39.7, 47.05)]
# Приблизительные ИЭЗ (в открытых данных точных границ нет; помечаются как «приблизительно»)
APPROX_EEZ = {
    "GUY": [(-60.2, 8.3), (-59.99, 8.55), (-58.8, 10.6), (-57.0, 11.1), (-55.6, 9.4), (-57.0, 6.0),
            (-58.5, 6.0), (-60.2, 7.5)],
    "VEN": [(-71.33, 11.85), (-70.9, 12.25), (-70.0, 12.1), (-69.0, 11.95), (-68.2, 11.95), (-67.8, 12.6),
            (-67.5, 14.0), (-66.0, 15.0), (-64.5, 16.3), (-63.2, 16.3), (-62.5, 15.0), (-62.3, 12.2),
            (-61.9, 11.0), (-61.5, 9.95), (-59.0, 9.9), (-59.8, 8.6), (-60.5, 8.0), (-66.0, 9.0),
            (-71.0, 10.0), (-72.0, 11.0)],
}
OFFSHORE_TERMINALS = {
    "IRQ": [(48.810, 29.683), (48.800, 29.783)],   # Al Basrah (ABOT), Khor Al Amaya (KAAOT)
}


# ==========================================================================
def _chunk_lines(geom: BaseGeometry, size: int = 120) -> list[LineString]:
    """Разбивает береговые линии на короткие куски — для быстрого поиска по плиткам."""
    out: list[LineString] = []
    for part in shapely.get_parts(geom):
        if part.is_empty or part.geom_type not in ("LineString", "LinearRing"):
            continue
        cs = list(part.coords)
        for i in range(0, len(cs) - 1, size):
            seg = cs[i:i + size + 1]
            if len(seg) >= 2:
                out.append(LineString(seg))
    return out


def strip_nm(lines: BaseGeometry, nm: float) -> BaseGeometry:
    """Полоса шириной nm миль вокруг линий (с учётом сжатия долготы к полюсам)."""
    if lines is None or lines.is_empty:
        return EMPTY
    pieces = _chunk_lines(lines)
    if not pieces:
        return EMPTY
    tree = STRtree(pieces)
    minx, miny, maxx, maxy = (float(v) for v in shapely.total_bounds(pieces))
    out = []
    y = math.floor(miny / TILE_H) * TILE_H
    while y < maxy:
        k = max(math.cos(math.radians(y + TILE_H / 2)), 0.05)
        x = math.floor(minx / TILE_W) * TILE_W
        while x < maxx:
            tile = box(x, y, x + TILE_W, y + TILE_H)
            idx = tree.query(tile)
            if len(idx):
                clip = unary_union([pieces[i] for i in idx]).intersection(tile)
                if not clip.is_empty:
                    scaled = affinity.scale(clip, xfact=k, yfact=1.0, origin=(0, 0))
                    buf = scaled.buffer(nm / 60.0, quad_segs=6)
                    out.append(affinity.scale(buf, xfact=1.0 / k, yfact=1.0, origin=(0, 0)))
            x += TILE_W
        y += TILE_H
    return unary_union(out) if out else EMPTY


def buffer_nm(geom: BaseGeometry, nm: float) -> BaseGeometry:
    if geom.is_empty:
        return EMPTY
    return unary_union([geom, strip_nm(geom.boundary, nm)])


def circle_nm(lon: float, lat: float, nm: float) -> BaseGeometry:
    k = max(math.cos(math.radians(lat)), 0.05)
    c = Point(lon * k, lat).buffer(nm / 60.0, quad_segs=12)
    return affinity.scale(c, xfact=1.0 / k, yfact=1.0, origin=(0, 0))


def polygonal(geom: BaseGeometry) -> BaseGeometry:
    """Оставляет только площадные части и чинит невалидную геометрию."""
    if geom.is_empty:
        return EMPTY
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    polys = [p for p in shapely.get_parts(geom) if p.geom_type in ("Polygon", "MultiPolygon")]
    flat: list[Polygon] = []
    for p in polys:
        flat.extend(shapely.get_parts(p) if p.geom_type == "MultiPolygon" else [p])
    flat = [p for p in flat if p.area > 1e-7]
    if not flat:
        return EMPTY
    return MultiPolygon(flat) if len(flat) > 1 else flat[0]


# ==========================================================================
class World:
    def __init__(self, features: Optional[list[dict]] = None):
        self.features = features if features is not None else ne.load_features()
        self.by_a3: dict[str, BaseGeometry] = {}
        parts: list[BaseGeometry] = []
        for f in self.features:
            if not f["a3"] or not f["geometry"]:
                continue
            g = shape(f["geometry"])
            if not g.is_valid:
                g = shapely.make_valid(g)
            g = g.simplify(0.002, preserve_topology=True)
            prev = self.by_a3.get(f["a3"])
            self.by_a3[f["a3"]] = g if prev is None else unary_union([prev, g])
            parts.extend(p for p in shapely.get_parts(g) if p.geom_type == "Polygon")
        self.land_parts = parts
        self.land_tree = STRtree(parts)
        self._tw: dict[tuple, BaseGeometry] = {}

    # --- базовые слои ---------------------------------------------------------
    def codes(self, name: str) -> list[str]:
        return ne.resolve(name, self.features)

    def country(self, codes: list[str]) -> BaseGeometry:
        geoms = [self.by_a3[c] for c in codes if c in self.by_a3]
        return polygonal(unary_union(geoms)) if geoms else EMPTY

    def tw(self, codes: list[str]) -> BaseGeometry:
        key = tuple(sorted(codes))
        if key not in self._tw:
            self._tw[key] = buffer_nm(self.country(list(key)), TW_NM)
        return self._tw[key]

    def land_in(self, bounds: tuple) -> BaseGeometry:
        b = box(*bounds)
        idx = self.land_tree.query(b)
        if not len(idx):
            return EMPTY
        return polygonal(unary_union([self.land_parts[i] for i in idx]).intersection(b))

    def sea(self, area: BaseGeometry) -> BaseGeometry:
        """Вычитает сушу, оставляя у берега полосу ~1 мили (гавани, причалы)."""
        if area.is_empty:
            return EMPTY
        minx, miny, maxx, maxy = area.bounds
        land = self.land_in((minx - 1, miny - 1, maxx + 1, maxy + 1))
        if land.is_empty:
            return area
        core = land.difference(strip_nm(land.boundary, COAST_TOLERANCE_NM))
        return area.difference(core)


# ==========================================================================
Result = list[tuple[dict, BaseGeometry]]   # (доп. свойства, геометрия)


def _need(w: World, name: str) -> list[str]:
    codes = w.codes(name)
    if not codes:
        raise ValueError(f"Страна «{name}» не найдена в справочнике границ")
    return codes


def t_country(w: World, e: dict) -> Result:
    codes = _need(w, e["params"]["country"])
    g = w.tw(codes)
    if e["params"].get("extras") == "offshore_terminals":
        extra = [circle_nm(lon, lat, 5) for c in codes for lon, lat in OFFSHORE_TERMINALS.get(c, [])]
        if extra:
            g = unary_union([g, *extra])
    return [({}, g)]


def t_country_clip_lat(w: World, e: dict) -> Result:
    codes = _need(w, e["params"]["country"])
    lat = float(e["params"]["lat"])
    clip = box(-180, -90, 180, lat) if e["params"].get("side") == "south" else box(-180, lat, 180, 90)
    return [({}, w.tw(codes).intersection(clip))]


def t_eez_beyond_tw(w: World, e: dict) -> Result:
    codes = _need(w, e["params"]["country"])
    ring = next((APPROX_EEZ[c] for c in codes if c in APPROX_EEZ), None)
    if not ring:
        raise ValueError("Нет контура ИЭЗ для этой страны — нужна ручная разметка")
    g = w.sea(Polygon(ring)).difference(w.tw(codes))
    return [({"approximate": True, "note": "ИЭЗ за пределами 12 миль, контур приблизительный. "
                                           "Зона действует только при заходе на offshore-установки."}, g)]


def t_country_plus_eez(w: World, e: dict) -> Result:
    codes = _need(w, e["params"]["country"])
    out: Result = [({}, w.tw(codes))]
    ring = next((APPROX_EEZ[c] for c in codes if c in APPROX_EEZ), None)
    if ring:
        eez = w.sea(Polygon(ring)).difference(w.tw(codes))
        out.append(({"id_suffix": "-eez", "name_suffix": " — EEZ (offshore installations)",
                     "severity": "conditional", "approximate": True,
                     "note": "Контур ИЭЗ приблизительный. Зона действует только для offshore-установок."}, eez))
    return out


def t_black_sea_basin(w: World, e: dict) -> Result:
    keep = {c for name in e["params"].get("keep_countries", []) for c in w.codes(name)}
    adjoining = ["TUR", "BGR", "ROU", "GEO", "RUS", "UKR"]
    exclude = [c for c in adjoining if c not in keep]
    g = w.sea(Polygon(BLACK_SEA_RING))
    if exclude:
        g = g.difference(unary_union([w.tw([c]) for c in exclude]))
    return [({}, g)]


def t_azov(w: World, e: dict) -> Result:
    return [({}, w.sea(box(*AZOV_BOX)))]


def t_inland_ukraine(w: World, e: dict) -> Result:
    g = unary_union([w.country(["UKR"]), w.country(["RUS"]).intersection(box(*CRIMEA_BOX))])
    return [({"note": "Внутренние воды: показана вся территория, включая Крым."}, g)]


def t_inland_russia_don(w: World, e: dict) -> Result:
    lon_limit = float(e["params"].get("lon_limit", 41.0))
    g = Polygon(DON_DONETS_RING).intersection(w.country(["RUS"])).intersection(box(-180, -90, lon_limit, 90))
    return [({"approximate": True, "note": "Коридор вдоль Дона и Северского Донца, приблизительно."}, g)]


def t_inland_belarus(w: World, e: dict) -> Result:
    lat = float(e["params"].get("lat_limit", 52.5))
    return [({}, w.country(["BLR"]).intersection(box(-180, -90, 180, lat)))]


def t_cabo_delgado(w: World, e: dict) -> Result:
    pts = e["params"]["points"]
    if len(pts) < 4:
        raise ValueError("Недостаточно точек для Кабу-Делгаду")
    n_coast, n_sea, s_coast, s_sea = pts[:4]
    ring = [n_coast, n_sea, (n_sea[0] + 0.5, n_sea[1]), (s_sea[0] + 0.5, s_sea[1]), s_sea, s_coast,
            (s_coast[0] - 1.0, s_coast[1]), (n_coast[0] - 1.0, n_coast[1])]
    area = Polygon(ring).intersection(w.tw(["MOZ", "TZA"]))
    return [({}, w.sea(area))]


def t_gulf_of_guinea(w: World, e: dict) -> Result:
    pts = e["params"]["points"]
    if len(pts) < 3:
        raise ValueError("Недостаточно точек для Гвинейского залива")
    west, south, east = pts[:3]
    ring = [west, south, east, (13.0, east[1]), (13.0, 7.5), (west[0], 7.5)]
    return [({}, w.sea(Polygon(ring)))]


def t_indian_ocean(w: World, e: dict) -> Result:
    pts = e["params"]["points"]
    if len(pts) < 5:
        raise ValueError("Недостаточно точек для зоны Индийского океана")
    lat = float(e["params"].get("red_sea_lat", 18.0))
    p, a, b, c, s = pts[:5]
    ring = [p, a, b, c, s, (38.0, 3.0), (33.0, 15.0), (33.0, lat), (45.0, lat), (45.0, 31.0),
            (48.0, 31.5), (57.0, 29.0), (62.0, 26.5)]
    area = w.sea(Polygon(ring))
    if e["params"].get("exclude_egypt_tw"):
        area = area.difference(w.tw(["EGY"]))
    return [({}, area)]


def t_generic_coords(w: World, e: dict) -> Result:
    pts = list(e["params"].get("points", []))
    if e["params"].get("closure") == "black_sea" and len(pts) >= 2:
        first, last = pts[0], pts[-1]
        pts += [(41.0, last[1]), (41.0, 47.6), (28.6, 47.6), (28.6, first[1])]
    if len(pts) < 3:
        raise ValueError("Меньше трёх точек — полигон не построить")
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = shapely.make_valid(poly)
    return [({"approximate": True}, w.sea(poly))]


TEMPLATES: dict[str, Callable[[World, dict], Result]] = {
    "country": t_country,
    "country_clip_lat": t_country_clip_lat,
    "eez_beyond_tw": t_eez_beyond_tw,
    "country_plus_eez": t_country_plus_eez,
    "black_sea_basin": t_black_sea_basin,
    "azov": t_azov,
    "inland_ukraine": t_inland_ukraine,
    "inland_russia_don": t_inland_russia_don,
    "inland_belarus": t_inland_belarus,
    "cabo_delgado": t_cabo_delgado,
    "gulf_of_guinea": t_gulf_of_guinea,
    "indian_ocean": t_indian_ocean,
    "generic_coords": t_generic_coords,
}


def _feature(e: dict, extra: dict, geom: BaseGeometry) -> Optional[dict]:
    geom = polygonal(geom)
    if geom.is_empty:
        return None
    geom = polygonal(geom.simplify(0.003, preserve_topology=True))
    geom = shapely.set_precision(geom, 0.0001)
    props: dict[str, Any] = {
        "id": e["id"] + extra.get("id_suffix", ""),
        "name": e["name"] + extra.get("name_suffix", ""),
        "region": e.get("region"),
        "kind": e["kind"],
        "template": e["template"],
        "severity": extra.get("severity", e.get("severity", "full")),
        "qualifier": e.get("qualifier", ""),
        "text": (e.get("text") or "")[:1500],
        "approximate": bool(extra.get("approximate", False)),
        "note": extra.get("note", ""),
        "needs_review": bool(e.get("needs_review")),
        "review_reason": e.get("review_reason", ""),
        "source_entry": e["id"],
    }
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def build(spec: dict, world: Optional[World] = None,
          progress: Optional[Callable[[str], None]] = None) -> tuple[dict, list[str]]:
    """Возвращает (FeatureCollection, журнал). Ошибка по одной зоне не валит остальные."""
    w = world or World()
    logs: list[str] = []
    features: list[dict] = []
    for e in spec["entries"]:
        tpl = TEMPLATES.get(e.get("template", ""))
        if progress:
            progress(e["name"])
        if not tpl:
            logs.append(f"⚠ {e['name']}: нет шаблона построения ({e.get('template')}) — нужна ручная разметка")
            e["needs_review"] = True
            continue
        try:
            for extra, geom in tpl(w, e):
                feat = _feature(e, extra, geom)
                if feat:
                    features.append(feat)
                else:
                    logs.append(f"⚠ {e['name']}: получилась пустая геометрия")
                    e["needs_review"] = True
        except Exception as exc:  # noqa: BLE001 — одна зона не должна ломать всё
            log.exception("Ошибка построения зоны %s", e["id"])
            logs.append(f"✖ {e['name']}: {exc}")
            e["needs_review"] = True
    fc = {
        "type": "FeatureCollection",
        "properties": {"ref": spec.get("ref"), "issued": spec.get("issued")},
        "features": features,
    }
    logs.append(f"Построено зон: {len(features)}")
    return fc, logs
