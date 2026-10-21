"""Общие кирпичики геометрии для слоёв IBF и INL (поверх builder.World)."""
from __future__ import annotations

import json
from typing import Any, Optional

import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .. import config
from ..geo import countries as ne
from ..geo.builder import EMPTY, World, circle_nm, polygonal

LAKES_FILE = config.GEODATA_DIR / "ne_10m_lakes.geojson"
PORTS_FILE = config.GEODATA_DIR / "ne_10m_ports.geojson"


def water(w: World, area: BaseGeometry) -> BaseGeometry:
    """Площадь минус суша, без береговой полосы — для больших районов (Арктика, Балтика)."""
    if area.is_empty:
        return EMPTY
    minx, miny, maxx, maxy = area.bounds
    land = w.land_in((minx - 0.5, miny - 0.5, maxx + 0.5, maxy + 0.5))
    return polygonal(area.difference(land)) if not land.is_empty else area


def part_with(geom: BaseGeometry, lon: float, lat: float) -> BaseGeometry:
    """Из кусков водной площади оставляет тот, где лежит точка (отделяет одно море от соседнего)."""
    p = Point(lon, lat)
    for part in shapely.get_parts(geom):
        if part.contains(p) or part.distance(p) < 0.05:
            return part
    return EMPTY


def coastal_band(w: World, bounds: tuple, nm: float) -> BaseGeometry:
    """Воды в пределах nm миль от любой суши внутри прямоугольника (острова, архипелаги)."""
    from ..geo.builder import buffer_nm
    land = w.land_in(bounds)
    if land.is_empty:
        return EMPTY
    return polygonal(buffer_nm(land, nm).difference(land).intersection(box(*bounds)))


def lakes(names: list[str]) -> BaseGeometry:
    path = ne.download_once(config.NE_LAKES_URL, LAKES_FILE, 1_000_000)
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    want = {n.lower() for n in names}
    geoms = [shape(ft["geometry"]) for ft in fc["features"]
             if str((ft.get("properties") or {}).get("name") or "").lower() in want]
    return polygonal(unary_union(geoms)) if geoms else EMPTY


# Крупные порты, которых нет в Natural Earth, но которые важны для зон IBF (lon, lat)
EXTRA_PORTS: dict[str, list[tuple[str, float, float]]] = {
    "UKR": [("Izmail", 28.85, 45.34), ("Reni", 28.29, 45.45), ("Kiliya", 29.26, 45.45),
            ("Bilhorod-Dnistrovskyi", 30.35, 46.19), ("Chornomorsk", 30.65, 46.30), ("Pivdennyi", 31.02, 46.62),
            ("Mykolaiv", 31.98, 46.95), ("Olvia", 31.90, 46.88), ("Skadovsk", 32.91, 46.11),
            ("Mariupol", 37.55, 47.08), ("Sevastopol", 33.53, 44.62), ("Yevpatoria", 33.37, 45.19),
            ("Yalta", 34.17, 44.50), ("Feodosia", 35.40, 45.03), ("Kerch", 36.48, 45.35)],
    "ISR": [("Ashdod", 34.64, 31.82), ("Hadera", 34.86, 32.47)],
    "LBN": [("Sidon", 35.37, 33.56), ("Tyre", 35.19, 33.27), ("Jounieh", 35.61, 33.98)],
}


def ports_raw() -> list[dict]:
    path = ne.download_once(config.NE_PORTS_URL, PORTS_FILE, 100_000)
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    out = []
    for ft in fc["features"]:
        lon, lat = ft["geometry"]["coordinates"][:2]
        out.append({"name": (ft.get("properties") or {}).get("name") or "", "lon": float(lon), "lat": float(lat)})
    for code, items in EXTRA_PORTS.items():
        for name, lon, lat in items:
            same = next((p for p in out if abs(p["lon"] - lon) < 0.03 and abs(p["lat"] - lat) < 0.03), None)
            if same:
                same["a3"] = code        # порт уже есть в Natural Earth — уточняем только страну
            else:
                out.append({"name": name, "lon": lon, "lat": lat, "a3": code})
    return out


def ports_of(w: World, codes: list[str]) -> list[dict]:
    """Порты страны: точка внутри границ или в 3 милях от них (порты лежат у самой воды)."""
    g = w.country(codes)
    if g.is_empty:
        return []
    return [p for p in ports_raw()
            if p.get("a3") in codes or (not p.get("a3") and g.distance(Point(p["lon"], p["lat"])) < 0.05)]


def port_circles(w: World, codes: list[str], nm: float) -> BaseGeometry:
    return unary_union([circle_nm(p["lon"], p["lat"], nm) for p in ports_of(w, codes)] or [EMPTY])


def split_antimeridian(geom: BaseGeometry) -> BaseGeometry:
    """Полигон с долготами > 180 разрезается на две половины по ±180."""
    if geom.is_empty or geom.bounds[2] <= 180:
        return geom
    west = geom.intersection(box(-180, -90, 180, 90))
    east = affinity.translate(geom.intersection(box(180, -90, 540, 90)), xoff=-360)
    return polygonal(unary_union([west, east]))


def feature(props: dict[str, Any], geom: BaseGeometry, tolerance: float = 0.003) -> Optional[dict]:
    geom = polygonal(geom)
    if geom.is_empty:
        return None
    geom = polygonal(geom.simplify(tolerance, preserve_topology=True))
    if geom.is_empty:
        return None
    geom = shapely.set_precision(geom, 0.0001)
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


__all__ = ["EMPTY", "MultiPolygon", "Polygon", "World", "box", "circle_nm", "coastal_band", "feature",
           "lakes", "part_with", "polygonal", "port_circles", "ports_of", "ports_raw", "split_antimeridian",
           "unary_union", "water"]
