"""Журнал пребываний судов флотов в зонах — фактический документ для страховщика (доп. премия).

Каждый вход и выход — строка в zone_visits (ведёт alerts.process; старые восстановлены один раз из
истории уведомлений — backfill). Для журнала они склеиваются в «пребывания» (stays): если судно вышло
из зоны и вернулось в неё меньше чем через MERGE_GAP_H часов, это одно пребывание — с началом, концом,
временем фактически в зоне и числом пересечений границы. Отметок «заявлено» нет: система этого не знает.
Функция отдельная: всё, что её подключает, помечено меткой [журнал-заходов] — чтобы удалить функцию,
удалите этот файл и отмеченные строки (поиск: grep -rn "[журнал-заходов]" app static tests), а в базе —
таблицу zone_visits.
"""
from __future__ import annotations

import io
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import access, db, zones

log = logging.getLogger(__name__)

MERGE_GAP_H = 24              # вышло и вернулось быстрее — то же пребывание


def _label(mmsi: str) -> str:
    from .alerts import vessel_label
    return vessel_label(mmsi)


def on_change(mmsi: str, zone_id: str, prev: Optional[str], cur: Optional[str], lat: float, lon: float, ts: str,
              initial: bool, ended: bool) -> None:
    """Вызывается из alerts.process при смене статуса судна в зоне."""
    if cur == "inside":
        opened(mmsi, zone_id, lat, lon, ts, initial=initial)
    elif prev == "inside":
        closed(mmsi, zone_id, lat, lon, ts, note="зона перестала действовать" if ended else "")


def _layer(zone_id: str) -> Optional[str]:
    z = zones.index().get(zone_id)
    return z.props.get("layer") if z else None


def opened(mmsi: str, zone_id: str, lat: Optional[float] = None, lon: Optional[float] = None,
           ts: Optional[str] = None, initial: bool = False) -> None:
    if db.query_one("SELECT 1 FROM zone_visits WHERE mmsi=? AND zone_id=? AND exited_at IS NULL", (mmsi, zone_id)):
        return
    db.execute("INSERT INTO zone_visits(mmsi, zone_id, zone_name, layer, entered_at, entry_lat, entry_lon, initial) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (mmsi, zone_id, zones.zone_name(zone_id), _layer(zone_id), ts or db.now_iso(), lat, lon, int(initial)))


def closed(mmsi: str, zone_id: str, lat: Optional[float] = None, lon: Optional[float] = None,
           ts: Optional[str] = None, note: str = "") -> None:
    db.execute("UPDATE zone_visits SET exited_at=?, exit_lat=?, exit_lon=?, note=COALESCE(NULLIF(?, ''), note) "
               "WHERE mmsi=? AND zone_id=? AND exited_at IS NULL", (ts or db.now_iso(), lat, lon, note, mmsi, zone_id))


def close_removed() -> None:
    """Судно убрали из всех флотов — открытые заходы закрываем, историю оставляем."""
    db.execute("UPDATE zone_visits SET exited_at=?, note='судно убрано из флота' "
               "WHERE exited_at IS NULL AND mmsi NOT IN (SELECT mmsi FROM vessels)", (db.now_iso(),))


def backfill() -> int:
    """Один раз: заходы из истории уведомлений (вход/выход) и текущих статусов «в зоне»."""
    if db.query_one("SELECT 1 FROM kv WHERE key='zone_log:backfilled'"):
        return 0
    n = 0
    for r in db.query("SELECT mmsi, zone_id, kind, ts FROM notifications WHERE kind IN ('zone_enter','zone_exit') "
                      "AND mmsi IS NOT NULL AND zone_id IS NOT NULL ORDER BY ts, id"):
        if r["kind"] == "zone_enter":
            opened(r["mmsi"], r["zone_id"], ts=r["ts"])
            n += 1
        else:
            closed(r["mmsi"], r["zone_id"], ts=r["ts"])
    for r in db.query("SELECT mmsi, zone_id, since FROM zone_state WHERE status='inside'"):
        opened(r["mmsi"], r["zone_id"], ts=r["since"], initial=True)
    # заход, который по истории «открыт», а судно уже не в зоне, — закрываем временем последней позиции
    for r in db.query("SELECT v.id, v.mmsi, v.zone_id FROM zone_visits v WHERE v.exited_at IS NULL AND NOT EXISTS "
                      "(SELECT 1 FROM zone_state s WHERE s.mmsi=v.mmsi AND s.zone_id=v.zone_id AND s.status='inside')"):
        db.execute("UPDATE zone_visits SET exited_at=?, note='выход восстановлен приблизительно' WHERE id=?",
                   (db.now_iso(), r["id"]))
    db.execute("INSERT INTO kv(key, value) VALUES('zone_log:backfilled', ?)", (db.now_iso(),))
    log.info("Журнал заходов: восстановлено из истории %d", n)
    return n


def _hours(a: str, b: Optional[str]) -> float:
    end = datetime.fromisoformat(b) if b else datetime.now(timezone.utc)
    return max(0.0, (end - datetime.fromisoformat(a)).total_seconds() / 3600)


def visits(user: str, days: int = 90, layer: str = "", fleet_id: Optional[int] = None) -> list[dict]:
    """Пребывания судов в зонах за период: входы и выходы одного судна в одной зоне с перерывами
    меньше MERGE_GAP_H склеены. Новые сверху."""
    mine = access.mmsis(user)
    if fleet_id:
        mine &= {r["mmsi"] for r in db.query("SELECT mmsi FROM vessels WHERE fleet_id=?", (fleet_id,))}
    if not mine:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = db.query(f"SELECT * FROM zone_visits WHERE mmsi IN ({','.join('?' * len(mine))}) "
                    "AND (exited_at IS NULL OR exited_at > ?) ORDER BY mmsi, zone_id, entered_at LIMIT 5000",
                    (*mine, since))
    stays: list[dict] = []
    for r in rows:
        if layer and (r["layer"] or "") != layer:
            continue
        last = stays[-1] if stays else None
        if (last and last["mmsi"] == r["mmsi"] and last["zone_id"] == r["zone_id"] and last["exited_at"]
                and _hours(last["exited_at"], r["entered_at"]) < MERGE_GAP_H):
            last["exited_at"] = r["exited_at"]
            last["inside_h"] += _hours(r["entered_at"], r["exited_at"])
            last["crossings"] += 1
            continue
        stays.append({"mmsi": r["mmsi"], "zone_id": r["zone_id"], "zone_name": r["zone_name"], "layer": r["layer"],
                      "entered_at": r["entered_at"], "exited_at": r["exited_at"], "initial": r["initial"],
                      "entry_lat": r["entry_lat"], "entry_lon": r["entry_lon"],
                      "inside_h": _hours(r["entered_at"], r["exited_at"]), "crossings": 1})
    for st in stays:
        span = _hours(st["entered_at"], st["exited_at"])
        st.update(vessel=_label(st["mmsi"]), open=st["exited_at"] is None, span_h=round(span, 1),
                  inside_h=round(st["inside_h"], 1), days=math.ceil(span / 24) if span else 0)
    return sorted(stays, key=lambda x: x["entered_at"], reverse=True)


def xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Пребывания в зонах"
    head = ["Судно", "MMSI", "Зона", "Слой", "Первый вход (UTC)", "Последний выход (UTC)", "Период, ч", "Суток",
            "Фактически в зоне, ч", "Пересечений границы", "Позиция входа", "Примечание"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    fmt = lambda t: t[:16].replace("T", " ") if t else ""  # noqa: E731
    for r in rows:
        ws.append([r["vessel"], r["mmsi"], r["zone_name"], (r["layer"] or "").upper(), fmt(r["entered_at"]),
                   fmt(r["exited_at"]) or "в зоне сейчас", r["span_h"], r["days"], r["inside_h"], r["crossings"],
                   f"{r['entry_lat']:.4f}, {r['entry_lon']:.4f}" if r["entry_lat"] is not None else "",
                   "уже было в зоне в момент начала отслеживания" if r["initial"] else ""])
    for col, w in zip("ABCDEFGHIJKL", (24, 11, 40, 6, 17, 19, 9, 7, 11, 11, 20, 30)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
