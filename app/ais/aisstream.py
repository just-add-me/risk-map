"""Клиент aisstream.io (бесплатный поток AIS по WebSocket).

Ограничения сервиса: до 50 MMSI в одной подписке, подписку нужно отправить
в течение 3 секунд после подключения. Поэтому флот режется на пачки по 50,
на каждую — своё соединение. При изменении состава флота соединения
пересоздаются. Покрытие в основном береговое: в открытом океане бывают пропуски.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Optional

from .. import config, db
from . import ingest

log = logging.getLogger(__name__)

CHUNK = 50
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                 "ShipStaticData", "StaticDataReport"]

status: dict[str, Any] = {"connected": 0, "last_message": None, "messages": 0, "last_error": None}


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def handle(raw: str | bytes) -> None:
    msg = json.loads(raw)
    if "error" in msg:
        status["last_error"] = msg["error"]
        log.error("aisstream: %s", msg["error"])
        return
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData") or msg.get("Metadata") or {}
    body = (msg.get("Message") or {}).get(mtype) or {}
    mmsi = str(meta.get("MMSI") or body.get("UserID") or "")
    if not mmsi:
        return
    status["messages"] += 1
    status["last_message"] = db.now_iso()
    name = (meta.get("ShipName") or "").strip() or None

    if mtype in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        lat = _f(body.get("Latitude", meta.get("latitude")))
        lon = _f(body.get("Longitude", meta.get("longitude")))
        if lat is None or lon is None:
            return
        sog = _f(body.get("Sog"))
        cog = _f(body.get("Cog"))
        hdg = _f(body.get("TrueHeading"))
        if hdg == 511:
            hdg = None
        if sog is not None and sog >= 102.3:
            sog = None
        ingest.position(mmsi, lat, lon, sog=sog, cog=cog, heading=hdg, source="aisstream", ais_name=name)
    elif mtype == "ShipStaticData":
        imo = body.get("ImoNumber")
        ingest.static_data(mmsi, name=(body.get("Name") or name or "").strip() or None,
                           imo=str(imo) if imo else None,
                           callsign=(body.get("CallSign") or "").strip() or None,
                           ship_type=body.get("Type"),
                           destination=(body.get("Destination") or "").strip() or None)
    elif mtype == "StaticDataReport":
        rep = body.get("ReportA") or {}
        if rep.get("Valid") and rep.get("Name"):
            ingest.static_data(mmsi, name=rep["Name"].strip())


async def _connection(chunk: list[str], stop: asyncio.Event) -> None:
    import websockets  # импорт здесь: в демо-режиме библиотека не нужна

    backoff = 5
    while not stop.is_set():
        try:
            async with websockets.connect(config.AISSTREAM_URL, ping_interval=20, ping_timeout=20,
                                          max_size=2 ** 22) as ws:
                await ws.send(json.dumps({
                    "APIKey": config.AISSTREAM_API_KEY,
                    "BoundingBoxes": [[[-90, -180], [90, 180]]],
                    "FiltersShipMMSI": chunk,
                    "FilterMessageTypes": MESSAGE_TYPES,
                }))
                status["connected"] += 1
                log.info("aisstream: подписка на %d судов", len(chunk))
                backoff = 5
                try:
                    async for raw in ws:
                        if stop.is_set():
                            break
                        try:
                            handle(raw)
                        except Exception:  # noqa: BLE001
                            log.exception("aisstream: не удалось обработать сообщение")
                finally:
                    status["connected"] -= 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            status["last_error"] = str(exc)
            log.warning("aisstream: соединение потеряно (%s), повтор через %d с", exc, backoff)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff + random.random() * 3)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 300)


async def run(stop: asyncio.Event) -> None:
    if not config.AISSTREAM_API_KEY:
        log.error("AIS_MODE=aisstream, но AISSTREAM_API_KEY не задан")
        return
    log.info("AIS: aisstream.io")
    while not stop.is_set():
        mmsis = ingest.tracked_mmsis()
        signature = ",".join(mmsis)
        tasks = [asyncio.create_task(_connection(mmsis[i:i + CHUNK], stop))
                 for i in range(0, len(mmsis), CHUNK)]
        # ждём, пока не изменится состав флота
        while not stop.is_set() and ",".join(ingest.tracked_mmsis()) == signature:
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not stop.is_set():
            log.info("aisstream: состав флота изменился — переподписываюсь")
