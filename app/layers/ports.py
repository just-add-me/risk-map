"""Порты мира (Natural Earth 1:10m, ~1100 крупных портов) с отметкой, в какие зоны они попадают."""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from .. import config, zones

log = logging.getLogger(__name__)

CACHE_FILE = config.GEODATA_DIR / "ports.json"
_lock = threading.Lock()
_ports: Optional[list[dict]] = None
_with_zones: tuple[int, list[dict]] = (-1, [])


def _prepare() -> list[dict]:
    """Порты + страна (по границам Natural Earth). Считается один раз и кладётся в кэш."""
    from shapely.geometry import Point, shape
    from shapely.strtree import STRtree

    from ..geo import countries as ne
    from . import shapes
    raw = shapes.ports_raw()
    with open(ne.ensure_downloaded(), encoding="utf-8") as f:
        fc = json.load(f)
    geoms, names, by_a3 = [], [], {}
    for ft in fc["features"]:
        props = {k.upper(): v for k, v in (ft.get("properties") or {}).items()}
        geoms.append(shape(ft["geometry"]))
        names.append(props.get("NAME") or props.get("ADMIN") or "")
        by_a3[props.get("ADM0_A3")] = names[-1]
    tree = STRtree(geoms)
    out = []
    for p in raw:
        pt = Point(p["lon"], p["lat"])
        i = tree.nearest(pt)
        country = names[int(i)] if i is not None and geoms[int(i)].distance(pt) < 0.1 else ""
        country = by_a3.get(p.get("a3"), country)
        out.append({"name": p["name"], "lon": round(p["lon"], 4), "lat": round(p["lat"], 4), "country": country})
    return out


def load() -> list[dict]:
    global _ports
    with _lock:
        if _ports is None:
            if CACHE_FILE.exists():
                _ports = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                _ports = _prepare()
                CACHE_FILE.write_text(json.dumps(_ports, ensure_ascii=False), encoding="utf-8")
                log.info("Порты: %d", len(_ports))
        return _ports


def with_zones() -> list[dict]:
    """Порты + id зон, в которые они попадают. Пересчитывается при смене любого слоя зон."""
    global _with_zones
    ver = zones.version()
    if _with_zones[0] == ver:
        return _with_zones[1]
    idx = zones.index()
    out = []
    for p in load():
        # точка порта в справочнике стоит у берега, а морские зоны начинаются у воды: допуск 2 мили
        inside = [z.id for z in idx.zones if idx.contains(z, p["lon"], p["lat"])
                  or idx.distance_nm(z, p["lon"], p["lat"], 2.0) is not None]
        out.append({**p, "zones": inside})
    _with_zones = (ver, out)
    return out
