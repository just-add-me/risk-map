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
import time
from typing import Any, Optional

from .. import config, db
from . import incidents, ingest, shipinfo, traffic

log = logging.getLogger(__name__)

CHUNK = 50
# весь мир шлёт десятки сообщений в секунду: если 2 минуты тишины — соединение «зависло», переподключаемся
STALL_S = 120
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                 "ShipStaticData", "StaticDataReport",
                 # происшествия: текстовые сообщения безопасности и спасательная авиация
                 "SafetyBroadcastMessage", "AddressedSafetyMessage", "StandardSearchAndRescueAircraftReport"]

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
        nav = body.get("NavigationalStatus")
        prev_nav = traffic.nav_of(mmsi)
        traffic.update_position(mmsi, lat, lon, sog, cog, hdg, name,
                                "A" if mtype == "PositionReport" else "B", nav)
        if mmsi[:3] in incidents.DEVICE_PREFIX:          # аварийный маяк: SART, MOB, EPIRB
            incidents.record(incidents.DEVICE_PREFIX[mmsi[:3]], mmsi, name=name, lat=lat, lon=lon,
                             test=nav == 15, source=mtype)
        # «на мели» / «не управляется» — только если судно сменило статус при нас: у барж и понтонов
        # этот статус часто выставлен постоянно, это не происшествие. «Маяк включён» (14) — всегда.
        elif nav in incidents.NAV_KIND and (nav == 14 or (prev_nav is not None and prev_nav != nav)):
            incidents.record(incidents.NAV_KIND[nav], mmsi, name=name or (traffic.get(mmsi) or {}).get("name"),
                             lat=lat, lon=lon, source=mtype, tracked=tracked)
        if tracked:
            ingest.position(mmsi, lat, lon, sog=sog, cog=cog, heading=hdg, source="aisstream", ais_name=name,
                            nav=nav if nav != 15 else None)
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
    elif mtype in ("SafetyBroadcastMessage", "AddressedSafetyMessage"):
        text = (body.get("Text") or "").strip()
        if not text:
            return
        kind, test = incidents.classify(text)
        s = traffic.get(mmsi) or {}
        lat = _f(meta.get("latitude")) or s.get("lat")
        lon = _f(meta.get("longitude")) or s.get("lon")
        dest = body.get("DestinationID")
        incidents.record(kind, mmsi, name=name or s.get("name"), lat=lat, lon=lon, text=text[:300], test=test,
                         source=mtype, extra=f"адресовано MMSI {dest}" if dest else "", tracked=tracked)
    elif mtype == "StandardSearchAndRescueAircraftReport":
        lat, lon = _f(body.get("Latitude")), _f(body.get("Longitude"))
        alt = body.get("Altitude")
        incidents.record("sar", mmsi, name=name, lat=lat, lon=lon, source=mtype,
                         extra=f"высота {alt} м, скорость {body.get('Sog')} уз" if alt is not None and alt < 4095 else "")
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


_bad_logged = 0.0


def _log_bad_message() -> None:
    """Ошибка разбора сообщения — в журнал не чаще раза в минуту (поток идёт десятками в секунду)."""
    global _bad_logged
    now = time.monotonic()
    if now - _bad_logged > 60:
        _bad_logged = now
        log.exception("aisstream: не удалось обработать сообщение (следующие такие ошибки минуту не пишутся)")


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
                    while not stop.is_set():
                        try:
                            # без фильтра по MMSI тишина означает сбой; с фильтром суда могут молчать часами
                            raw = await asyncio.wait_for(ws.recv(), timeout=STALL_S if chunk is None else None)
                        except asyncio.TimeoutError:
                            status["last_error"] = f"нет данных {STALL_S} с — переподключение"
                            log.warning("aisstream: нет сообщений %d с, переподключаюсь", STALL_S)
                            break
                        try:
                            handle(raw)
                        except Exception:  # noqa: BLE001
                            _log_bad_message()
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
