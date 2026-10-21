"""Клиент aisstream.io (бесплатный поток AIS по WebSocket).

Два режима:
* AIS_TRAFFIC=1 (по умолчанию) — одно соединение на весь мир: все суда попадают в
  слой «Все суда» (traffic.py, только в памяти), а суда из флотов дополнительно
  идут в базу и алерты.
* AIS_TRAFFIC=0 — только суда флотов: до 50 MMSI в одной подписке, флот режется на
  пачки по 50, на каждую — своё соединение.
Подписку нужно отправить в течение 3 секунд после подключения. Покрытие в основном
береговое: в открытом океане бывают пропуски.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Optional

from .. import config, db
from . import ingest, shipinfo, traffic

log = logging.getLogger(__name__)

CHUNK = 50
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                 "ShipStaticData", "StaticDataReport"]

status: dict[str, Any] = {"connected": 0, "last_message": None, "messages": 0, "last_error": None}
# MMSI судов из флотов; None — фильтр на стороне aisstream (режим без общего слоя)
_tracked: Optional[set[str]] = None
# IMO судов, добавленных без MMSI: ждём от них паспортное сообщение
_pending: set[str] = set()


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
    tracked = _tracked is None or mmsi in _tracked

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
        if cog is not None and cog >= 360:
            cog = None
        traffic.update_position(mmsi, lat, lon, sog, cog, hdg, name,
                                "A" if mtype == "PositionReport" else "B", body.get("NavigationalStatus"))
        if tracked:
            ingest.position(mmsi, lat, lon, sog=sog, cog=cog, heading=hdg, source="aisstream", ais_name=name)
    elif mtype == "ShipStaticData":
        imo = body.get("ImoNumber")
        length, beam = shipinfo.dimensions(body.get("Dimension"))
        draught = _f(body.get("MaximumStaticDraught")) or None
        info = dict(name=(body.get("Name") or name or "").strip() or None,
                    imo=str(imo) if imo else None,
                    callsign=(body.get("CallSign") or "").strip() or None,
                    ship_type=body.get("Type") or None,
                    destination=(body.get("Destination") or "").strip() or None,
                    length=length, beam=beam, draught=draught, eta=shipinfo.eta_text(body.get("Eta")))
        traffic.update_static(mmsi, **info)
        if info["imo"] and info["imo"] in _pending:
            _pending.discard(info["imo"])
            ingest.resolve_imo(info["imo"], mmsi)
            if _tracked is not None:
                _tracked.add(mmsi)
        elif tracked:
            ingest.static_data(mmsi, **info)
    elif mtype == "StaticDataReport":
        rep_a = body.get("ReportA") or {}
        rep_b = body.get("ReportB") or {}
        info: dict[str, Any] = {}
        if rep_a.get("Valid") and rep_a.get("Name"):
            info["name"] = rep_a["Name"].strip()
        if rep_b.get("Valid"):
            length, beam = shipinfo.dimensions(rep_b.get("Dimension"))
            info.update(ship_type=rep_b.get("ShipType") or None,
                        callsign=(rep_b.get("CallSign") or "").strip() or None, length=length, beam=beam)
        if info:
            traffic.update_static(mmsi, **info)
            if tracked:
                ingest.static_data(mmsi, **info)


async def _connection(chunk: Optional[list[str]], stop: asyncio.Event) -> None:
    """chunk — MMSI для фильтра; None — весь мир без фильтра."""
    import websockets  # импорт здесь: в демо-режиме библиотека не нужна

    backoff = 5
    while not stop.is_set():
        try:
            async with websockets.connect(config.AISSTREAM_URL, ping_interval=20, ping_timeout=20,
                                          max_size=2 ** 22) as ws:
                sub = {"APIKey": config.AISSTREAM_API_KEY, "BoundingBoxes": [[[-90, -180], [90, 180]]],
                       "FilterMessageTypes": MESSAGE_TYPES}
                if chunk is not None:
                    sub["FiltersShipMMSI"] = chunk
                await ws.send(json.dumps(sub))
                status["connected"] += 1
                log.info("aisstream: подписка на %s", "все суда мира" if chunk is None else f"{len(chunk)} судов")
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
    if config.AIS_TRAFFIC:
        await _run_world(stop)
        return
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


async def _run_world(stop: asyncio.Event) -> None:
    """Одно соединение на весь мир; список судов флотов обновляем раз в 15 секунд."""
    global _tracked, _pending
    _tracked = set(ingest.tracked_mmsis())
    _pending = ingest.pending_imos()
    task = asyncio.create_task(_connection(None, stop))
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
            _tracked = set(ingest.tracked_mmsis())
            _pending = ingest.pending_imos()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
