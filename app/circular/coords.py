"""Разбор координат в формате циркуляров JWC.

Примеры, которые встречаются в документах:
    25°19’15”N, 65°E      10°48’N      6° 06′ 45″ N      10° 10.3' S      45° 10.858'N
"""
from __future__ import annotations

import re

_NUM = r"(\d+(?:\.\d+)?)"
_MIN_MARK = r"['’′`´]"
_SEC_MARK = r"(?:[\"”″]|'')"

COORD_RE = re.compile(
    _NUM + r"\s*°\s*"
    r"(?:" + _NUM + r"\s*" + _MIN_MARK + r"\s*)?"
    r"(?:" + _NUM + r"\s*" + _SEC_MARK + r"\s*)?"
    r"([NSEW])\b"
)

LATITUDE_LINE_RE = re.compile(r"(?:latitude|horizontal line at)\s*" + COORD_RE.pattern, re.I)
LONGITUDE_LINE_RE = re.compile(r"(?:longitude|vertical line at)\s*" + COORD_RE.pattern, re.I)


def normalize(text: str) -> str:
    """Убирает переносы строк внутри координат (PDF часто рвёт «10° 10.3'\\nS»)."""
    t = text.replace(" ", " ")
    t = re.sub(r"([°'’′\"”″])\s*\n\s*([NSEW]\b)", r"\1 \2", t)
    return t


def _to_decimal(deg: str, minutes: str | None, seconds: str | None, hemi: str) -> float:
    value = float(deg) + float(minutes or 0) / 60.0 + float(seconds or 0) / 3600.0
    return -value if hemi in "SW" else value


def find_values(text: str) -> list[tuple[str, float]]:
    """Все значения в порядке появления: [('lat', 25.32), ('lon', 65.0), ...]."""
    out = []
    for m in COORD_RE.finditer(normalize(text)):
        deg, mn, sec, hemi = m.groups()
        axis = "lat" if hemi in "NS" else "lon"
        out.append((axis, _to_decimal(deg, mn, sec, hemi)))
    return out


def find_points(text: str) -> list[tuple[float, float]]:
    """Пары координат как (lon, lat) — порядок GeoJSON.

    Одиночные «линии» вида «south of Latitude 25.5°N» пропускаются: они
    разбираются отдельно через find_latitude_line().
    """
    t = normalize(text)
    skip = set()
    for rx in (LATITUDE_LINE_RE, LONGITUDE_LINE_RE):
        for m in rx.finditer(t):
            skip.add(m.end())
    points: list[tuple[float, float]] = []
    pending: tuple[str, float] | None = None
    for m in COORD_RE.finditer(t):
        if m.end() in skip:
            pending = None
            continue
        deg, mn, sec, hemi = m.groups()
        axis = "lat" if hemi in "NS" else "lon"
        val = _to_decimal(deg, mn, sec, hemi)
        if pending and pending[0] != axis:
            lat = pending[1] if pending[0] == "lat" else val
            lon = val if pending[0] == "lat" else pending[1]
            points.append((round(lon, 6), round(lat, 6)))
            pending = None
        else:
            pending = (axis, val)
    return points


def find_latitude_line(text: str) -> float | None:
    m = LATITUDE_LINE_RE.search(normalize(text))
    if not m:
        return None
    deg, mn, sec, hemi = m.groups()
    return _to_decimal(deg, mn, sec, hemi)


def find_longitude_line(text: str) -> float | None:
    m = LONGITUDE_LINE_RE.search(normalize(text))
    if not m:
        return None
    deg, mn, sec, hemi = m.groups()
    return _to_decimal(deg, mn, sec, hemi)


def dedupe(points: list[tuple[float, float]], tol: float = 1e-4) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in points:
        if not any(abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol for q in out):
            out.append(p)
    return out
