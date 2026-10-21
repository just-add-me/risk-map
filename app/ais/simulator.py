"""Демо-режим: суда «плывут» по заранее заданным маршрутам через зоны.

Нужен, чтобы проверить карту и алерты без ключа AIS. Каждое судно флота
получает маршрут по своему MMSI; время ускорено в DEMO_SPEED раз.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time

from .. import config
from . import ingest

log = logging.getLogger(__name__)

# (lon, lat) — маршруты проходят через несколько зон из JWLA-035
ROUTES = [
    ("Суэц → Красное море → Аден", 14.0, [
        (32.55, 29.85), (32.75, 28.6), (33.9, 27.4), (35.1, 25.9), (36.6, 23.6), (38.3, 20.6),
        (40.4, 16.8), (42.0, 14.4), (43.3, 12.6), (44.9, 12.3), (47.5, 12.6), (51.5, 12.9)]),
    ("Босфор → Одесса", 11.0, [
        (29.07, 41.15), (29.15, 41.33), (29.6, 41.75), (30.3, 42.7), (30.7, 44.0), (30.85, 45.4),
        (30.75, 46.35), (30.76, 46.49)]),
    ("Абиджан → Лагос", 12.0, [
        (-4.0, 5.15), (-2.5, 4.6), (-0.5, 5.1), (1.0, 5.75), (1.25, 5.95), (2.4, 6.15),
        (3.2, 6.25), (3.39, 6.42)]),
    ("Маскат → Ормуз → Дубай", 13.0, [
        (60.5, 22.0), (59.3, 23.3), (58.6, 23.8), (57.5, 24.9), (56.8, 25.9), (56.3, 26.5),
        (55.6, 26.1), (55.1, 25.3)]),
]


def _seg_nm(a, b) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 60 * k, (b[1] - a[1]) * 60)


def _bearing(a, b) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return (math.degrees(math.atan2((b[0] - a[0]) * k, b[1] - a[1])) + 360) % 360


def position_at(route_points, travelled_nm: float):
    """Позиция на маршруте, едущем «туда-обратно»."""
    legs = [_seg_nm(route_points[i], route_points[i + 1]) for i in range(len(route_points) - 1)]
    total = sum(legs)
    cycle = travelled_nm % (2 * total)
    pts = route_points
    if cycle > total:
        pts = list(reversed(route_points))
        legs = list(reversed(legs))
        cycle -= total
    for i, leg in enumerate(legs):
        if cycle <= leg or i == len(legs) - 1:
            f = min(cycle / leg, 1.0) if leg else 0.0
            a, b = pts[i], pts[i + 1]
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, _bearing(a, b))
        cycle -= leg
    return (*pts[-1][:2], 0.0)


def route_for(mmsi: str):
    h = int(hashlib.md5(mmsi.encode()).hexdigest(), 16)
    return ROUTES[h % len(ROUTES)], (h >> 8) % 1000 / 1000.0


async def run(stop: asyncio.Event) -> None:
    log.info("AIS: демо-режим (симулятор), ускорение x%s", config.DEMO_SPEED)
    start = time.monotonic()
    while not stop.is_set():
        elapsed_h = (time.monotonic() - start) / 3600 * config.DEMO_SPEED
        for mmsi in ingest.tracked_mmsis():
            (name, speed, pts), offset = route_for(mmsi)
            total = sum(_seg_nm(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            lon, lat, cog = position_at(pts, offset * total + elapsed_h * speed)
            try:
                ingest.position(mmsi, round(lat, 5), round(lon, 5), sog=speed, cog=round(cog, 1),
                                heading=round(cog), source="demo")
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки демо-позиции")
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.DEMO_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
