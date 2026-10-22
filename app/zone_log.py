"""Журнал заходов судов флотов в зоны — для заявок страховщику на дополнительную премию (AP).

Каждый заход — строка в zone_visits: когда судно вошло в зону и вышло, где, сколько пробыло,
заявлен ли заход (отмечают пользователи с правом редактировать флот). Записи ведёт
alerts.process при входе и выходе. Старые заходы восстанавливаются один раз из истории
уведомлений (backfill). Заявлен ли заход, система не знает: по умолчанию «нет отметки» (см. UNMARKED).
Напоминание о заходе без отметки — правило «Заход без отметки о заявке» (rules.py).
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

REMIND_HOURS = 6


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


# Отметка о заявке (колонка declared). Заявлен ли заход, система знать не может — по умолчанию «нет отметки»,
# пока пользователь не отметит. Для судна можно один раз указать «заявки не нужны» (годовое покрытие,
# не страхуется от военных рисков) — тогда его заходы сразу получают NOT_REQUIRED и напоминаний нет.
UNMARKED, DECLARED, NOT_REQUIRED = 0, 1, 2
STATUS = {"unmarked": UNMARKED, "declared": DECLARED, "not_required": NOT_REQUIRED}
STATUS_LABEL = {UNMARKED: "нет отметки", DECLARED: "заявлено", NOT_REQUIRED: "не требуется"}


def skip_vessels() -> set[str]:
    """Суда, для которых заявки на заходы не нужны."""
    return {r["key"][len("ap_skip:"):] for r in db.query("SELECT key FROM kv WHERE key LIKE 'ap_skip:%'")}


def opened(mmsi: str, zone_id: str, lat: Optional[float] = None, lon: Optional[float] = None,
           ts: Optional[str] = None, initial: bool = False) -> None:
    if db.query_one("SELECT 1 FROM zone_visits WHERE mmsi=? AND zone_id=? AND exited_at IS NULL", (mmsi, zone_id)):
        return
    status = NOT_REQUIRED if mmsi in skip_vessels() else UNMARKED
    db.execute("INSERT INTO zone_visits(mmsi, zone_id, zone_name, layer, entered_at, entry_lat, entry_lon, initial, declared) "
               "VALUES(?,?,?,?,?,?,?,?,?)",
               (mmsi, zone_id, zones.zone_name(zone_id), _layer(zone_id), ts or db.now_iso(), lat, lon, int(initial), status))


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
    mine = access.mmsis(user)
    if fleet_id:
        mine &= {r["mmsi"] for r in db.query("SELECT mmsi FROM vessels WHERE fleet_id=?", (fleet_id,))}
    if not mine:
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = db.query(f"SELECT * FROM zone_visits WHERE mmsi IN ({','.join('?' * len(mine))}) "
                    "AND (exited_at IS NULL OR exited_at > ?) ORDER BY entered_at DESC LIMIT 2000", (*mine, since))
    editable = editable_mmsis(user)
    for r in rows:
        if layer and (r["layer"] or "") != layer:
            continue
        h = _hours(r["entered_at"], r["exited_at"])
        r.update(vessel=_label(r["mmsi"]), hours=round(h, 1), days=math.ceil(h / 24) if h else 0,
                 open=r["exited_at"] is None, can_edit=r["mmsi"] in editable,
                 status=next(k for k, v in STATUS.items() if v == (r["declared"] or 0)))
    return [r for r in rows if not layer or (r["layer"] or "") == layer]


def editable_mmsis(user: str) -> set[str]:
    return {r["mmsi"] for r in db.query(
        "SELECT v.mmsi FROM vessels v JOIN fleets f ON f.id=v.fleet_id "
        "LEFT JOIN fleet_shares s ON s.fleet_id=f.id AND s.username=? WHERE f.owner=? OR s.can_edit=1", (user, user))}


def mark(user: str, vid: int, status: str, note: Optional[str] = None) -> None:
    """Отметка у захода: unmarked / declared / not_required."""
    if status not in STATUS:
        raise ValueError("Неизвестная отметка")
    row = db.query_one("SELECT mmsi FROM zone_visits WHERE id=?", (vid,))
    if not row or row["mmsi"] not in editable_mmsis(user):
        raise ValueError("Отмечать заявку может тот, кто может редактировать флот судна")
    v = STATUS[status]
    db.execute("UPDATE zone_visits SET declared=?, declared_at=?, declared_by=?, note=COALESCE(?, note) WHERE id=?",
               (v, db.now_iso() if v else None, user if v else None, note.strip()[:300] if note is not None else None, vid))


def set_vessel_skip(user: str, mmsi: str, skip: bool) -> int:
    """«Заявки не нужны» для всех заходов судна (или отмена). Возвращает, сколько заходов без отметки обновлено."""
    if mmsi not in editable_mmsis(user):
        raise ValueError("Менять это может тот, кто может редактировать флот судна")
    if skip:
        db.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (f"ap_skip:{mmsi}", user))
        cur = db.execute("UPDATE zone_visits SET declared=?, declared_at=?, declared_by=? WHERE mmsi=? AND declared=?",
                         (NOT_REQUIRED, db.now_iso(), user, mmsi, UNMARKED))
        return cur.rowcount
    db.execute("DELETE FROM kv WHERE key=?", (f"ap_skip:{mmsi}",))
    return 0


def xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Заходы в зоны"
    head = ["Судно", "MMSI", "Зона", "Слой", "Вход (UTC)", "Выход (UTC)", "Часов", "Суток", "Заявка",
            "Когда отмечено", "Кто", "Позиция входа", "Примечание"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    fmt = lambda t: t[:16].replace("T", " ") if t else ""  # noqa: E731
    for r in rows:
        ws.append([r["vessel"], r["mmsi"], r["zone_name"], (r["layer"] or "").upper(), fmt(r["entered_at"]),
                   fmt(r["exited_at"]) or "в зоне сейчас", r["hours"], r["days"], STATUS_LABEL.get(r["declared"] or 0, "нет отметки"),
                   fmt(r["declared_at"]), r["declared_by"] or "",
                   f"{r['entry_lat']:.4f}, {r['entry_lon']:.4f}" if r["entry_lat"] is not None else "",
                   ("вошло до начала отслеживания; " if r["initial"] else "") + (r["note"] or "")])
    for col, w in zip("ABCDEFGHIJKLM", (24, 11, 40, 6, 17, 17, 8, 7, 9, 17, 10, 20, 40)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
