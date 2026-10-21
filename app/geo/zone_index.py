"""Быстрые проверки «судно внутри зоны?» и «как далеко до зоны?».

Работает с готовым GeoJSON зон (его строит builder.py) и не требует shapely:
только numpy. Расстояния считаются в морских милях в локальной
равнопромежуточной проекции вокруг судна — для дистанций до сотни миль
ошибка пренебрежимо мала.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

NM_PER_DEG_LAT = 60.0


@dataclass
class _Polygon:
    bbox: tuple[float, float, float, float]          # minx, miny, maxx, maxy
    rings: list[np.ndarray]                          # [N x 2] (lon, lat); первое кольцо — внешнее


@dataclass
class Zone:
    id: str
    props: dict
    polygons: list[_Polygon] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (180.0, 90.0, -180.0, -90.0)


def _ring_array(coords) -> np.ndarray:
    arr = np.asarray(coords, dtype=float)[:, :2]
    if len(arr) > 1 and np.allclose(arr[0], arr[-1]):
        arr = arr[:-1]
    return arr


def _crossings(x: float, y: float, ring: np.ndarray) -> int:
    xs, ys = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    straddle = (ys > y) != (yj > y)
    if not straddle.any():
        return 0
    xs_, ys_, xj_, yj_ = xs[straddle], ys[straddle], xj[straddle], yj[straddle]
    x_int = (xj_ - xs_) * (y - ys_) / (yj_ - ys_) + xs_
    return int(np.count_nonzero(x < x_int))


def _min_dist_nm(lon: float, lat: float, ring: np.ndarray) -> float:
    """Минимальное расстояние (мили) от точки до рёбер кольца."""
    k = math.cos(math.radians(lat)) * NM_PER_DEG_LAT
    dlon = (ring[:, 0] - lon + 180.0) % 360.0 - 180.0
    px = dlon * k
    py = (ring[:, 1] - lat) * NM_PER_DEG_LAT
    ax, ay = px, py
    bx, by = np.roll(px, -1), np.roll(py, -1)
    ex, ey = bx - ax, by - ay
    seg_len2 = ex * ex + ey * ey
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(seg_len2 > 0, -(ax * ex + ay * ey) / seg_len2, 0.0)
    t = np.clip(t, 0.0, 1.0)
    cx, cy = ax + t * ex, ay + t * ey
    return float(np.sqrt((cx * cx + cy * cy).min()))


class ZoneIndex:
    def __init__(self, feature_collection: Optional[dict] = None):
        self.zones: list[Zone] = []
        self.meta: dict = {}
        if feature_collection:
            self.load(feature_collection)

    def load(self, fc: dict) -> None:
        self.meta = fc.get("properties", {}) or {}
        zones: list[Zone] = []
        for feat in fc.get("features", []):
            props = feat.get("properties", {}) or {}
            geom = feat.get("geometry") or {}
            zone = Zone(id=str(props.get("id")), props=props)
            gtype = geom.get("type")
            if gtype == "Polygon":
                polys = [geom["coordinates"]]
            elif gtype == "MultiPolygon":
                polys = geom["coordinates"]
            else:
                polys = []
            for poly in polys:
                rings = [_ring_array(r) for r in poly if len(r) >= 3]
                if not rings:
                    continue
                ext = rings[0]
                bbox = (float(ext[:, 0].min()), float(ext[:, 1].min()),
                        float(ext[:, 0].max()), float(ext[:, 1].max()))
                zone.polygons.append(_Polygon(bbox=bbox, rings=rings))
            if zone.polygons:
                zone.bbox = (
                    min(p.bbox[0] for p in zone.polygons), min(p.bbox[1] for p in zone.polygons),
                    max(p.bbox[2] for p in zone.polygons), max(p.bbox[3] for p in zone.polygons),
                )
                zones.append(zone)
        self.zones = zones

    def get(self, zone_id: str) -> Optional[Zone]:
        return next((z for z in self.zones if z.id == zone_id), None)

    # --- проверки ----------------------------------------------------------
    @staticmethod
    def _in_bbox(lon, lat, bbox, mlon=0.0, mlat=0.0) -> bool:
        return (bbox[0] - mlon <= lon <= bbox[2] + mlon) and (bbox[1] - mlat <= lat <= bbox[3] + mlat)

    def contains(self, zone: Zone, lon: float, lat: float) -> bool:
        if not self._in_bbox(lon, lat, zone.bbox):
            return False
        for poly in zone.polygons:
            if not self._in_bbox(lon, lat, poly.bbox):
                continue
            # правило чётности по всем кольцам корректно учитывает «дырки»
            if sum(_crossings(lon, lat, r) for r in poly.rings) % 2 == 1:
                return True
        return False

    def distance_nm(self, zone: Zone, lon: float, lat: float, max_nm: float) -> Optional[float]:
        """Расстояние до границы зоны, если оно не больше max_nm; иначе None."""
        mlat = max_nm / NM_PER_DEG_LAT
        mlon = max_nm / (NM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.05))
        if not self._in_bbox(lon, lat, zone.bbox, mlon, mlat):
            return None
        best = None
        for poly in zone.polygons:
            if not self._in_bbox(lon, lat, poly.bbox, mlon, mlat):
                continue
            for ring in poly.rings:
                d = _min_dist_nm(lon, lat, ring)
                if best is None or d < best:
                    best = d
        if best is None or best > max_nm:
            return None
        return best

    def evaluate(self, lon: float, lat: float, warn_nm: float) -> dict[str, dict]:
        """Вернёт {zone_id: {"status": "inside"|"near", "distance_nm": float}} для зон рядом."""
        out: dict[str, dict] = {}
        for zone in self.zones:
            if self.contains(zone, lon, lat):
                out[zone.id] = {"status": "inside", "distance_nm": 0.0}
                continue
            d = self.distance_nm(zone, lon, lat, warn_nm)
            if d is not None:
                out[zone.id] = {"status": "near", "distance_nm": round(d, 1)}
        return out
