"""Логика алертов: сравниваем новое положение судна с зонами и пишем уведомления.

Статусы судна относительно каждой зоны: inside (внутри), near (ближе
WARN_DISTANCE_NM), нет записи — далеко. Уведомления создаются только при
смене статуса, поэтому дубликатов нет.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from . import config, db, zones
from . import zone_log    # [журнал-заходов]

log = logging.getLogger(__name__)


def vessel_label(mmsi: str) -> str:
    row = db.query_one(
        "SELECT v.name AS name, a.ais_name AS ais_name FROM vessels v "
        "LEFT JOIN ais_state a ON a.mmsi = v.mmsi WHERE v.mmsi=? ORDER BY v.id LIMIT 1", (mmsi,))
    name = (row or {}).get("name") or (row or {}).get("ais_name")
    return f"{name} (MMSI {mmsi})" if name else f"MMSI {mmsi}"


def fmt_duration(since_iso: str) -> str:
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(since_iso)
    except ValueError:
        return ""
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} мин"
    if hours < 48:
        return f"{hours:.1f} ч"
    return f"{hours / 24:.1f} сут"


def eta_text(distance_nm: float, sog: Optional[float]) -> str:
    if not sog or sog < 0.5:
        return ""
    hours = distance_nm / sog
    return f", при {sog:.0f} уз. ≈ {hours:.1f} ч" if hours < 72 else f", при {sog:.0f} уз. ≈ {hours / 24:.1f} сут"


def zone_note(zone_id: str) -> str:
    z = zones.index().get(zone_id)
    if not z:
        return ""
    p = z.props
    if p.get("layer") == "ibf":
        return f"\nЭкипаж (IBF/ITF): {p.get('benefits', '')}"
    if p.get("layer") == "inl":
        return ("\nНавигационные ограничения INL: нужен заранее согласованный со страховщиком корпуса заход "
                "и доплата премии, иначе страховка корпуса может не действовать.")
    if p.get("severity") == "conditional":
        return " (условная зона: только при заходе на offshore-установки)"
    return ""


def process(mmsi: str, lon: float, lat: float, sog: Optional[float] = None,
            initial: bool = False, reason: str = "") -> list[int]:
    """Пересчитывает статусы судна в зонах (для карты, списка флота и журнала заходов).
    Возвращает id зон, где статус изменился. Уведомления об этом шлют правила (rules.py)."""
    idx = zones.index()
    result = idx.evaluate(lon, lat, config.WARN_DISTANCE_NM)
    # сезонные ограничения INL вне сезона не действуют
    off_season = {zid for zid in result if not zones.in_effect(zid)}
    for zid in off_season:
        del result[zid]
    old = {r["zone_id"]: r for r in db.query("SELECT * FROM zone_state WHERE mmsi=?", (mmsi,))}
    created: list[int] = []
    now = db.now_iso()

    # гистерезис: не выпускаем из зоны, пока судно не отошло от границы хотя бы на 0.3 мили
    for zid, st in old.items():
        if not zones.in_effect(zid):
            continue
        if st["status"] == "inside" and result.get(zid, {}).get("status") != "inside":
            z = idx.get(zid)
            if z is not None:
                d = idx.distance_nm(z, lon, lat, config.EXIT_HYSTERESIS_NM)
                if d is not None:
                    result[zid] = {"status": "inside", "distance_nm": 0.0}

    for zid in set(old) | set(result):
        prev = old.get(zid, {}).get("status")
        cur = result.get(zid, {}).get("status")
        dist = result.get(zid, {}).get("distance_nm")

        if cur == prev:
            if cur == "near":
                db.execute("UPDATE zone_state SET distance_nm=? WHERE mmsi=? AND zone_id=?", (dist, mmsi, zid))
            continue

        created.append(zid)          # уведомления о входе/выходе/подходе шлют правила (rules.py)

        zone_log.on_change(mmsi, zid, prev, cur, lat, lon, now, initial, bool(reason) or not zones.in_effect(zid))  # [журнал-заходов]

        if cur is None:
            db.execute("DELETE FROM zone_state WHERE mmsi=? AND zone_id=?", (mmsi, zid))
        else:
            db.execute(
                "INSERT INTO zone_state(mmsi, zone_id, status, since, distance_nm) VALUES(?,?,?,?,?) "
                "ON CONFLICT(mmsi, zone_id) DO UPDATE SET status=excluded.status, since=excluded.since, "
                "distance_nm=excluded.distance_nm",
                (mmsi, zid, cur, now, dist),
            )
    return created


def reevaluate_all(reason: str) -> int:
    """После смены циркуляра пересчитываем статусы всех судов; о новых зонах сообщат правила."""
    rows = db.query("SELECT mmsi, lat, lon, sog, nav FROM ais_state WHERE lat IS NOT NULL")
    count = 0
    from . import rules
    for r in rows:
        count += len(process(r["mmsi"], r["lon"], r["lat"], r["sog"], reason=reason))
        rules.on_position(r["mmsi"], r["lat"], r["lon"], r["sog"], r.get("nav"))   # правила сообщат о новых зонах
    # зоны, которых больше нет в списке, удаляем из состояния
    known = {z.id for z in zones.index().zones}
    for r in db.query("SELECT DISTINCT zone_id FROM zone_state"):
        if r["zone_id"] not in known:
            db.execute("DELETE FROM zone_state WHERE zone_id=?", (r["zone_id"],))
    return count


def vessel_status(mmsi: str) -> dict:
    rows = db.query("SELECT zone_id, status, since, distance_nm FROM zone_state WHERE mmsi=?", (mmsi,))
    inside = [r for r in rows if r["status"] == "inside"]
    near = sorted([r for r in rows if r["status"] == "near"], key=lambda r: r["distance_nm"] or 0)
    level = "inside" if inside else ("near" if near else "safe")
    return {
        "level": level,
        "inside": [{"zone_id": r["zone_id"], "name": zones.zone_name(r["zone_id"]), "since": r["since"]} for r in inside],
        "near": [{"zone_id": r["zone_id"], "name": zones.zone_name(r["zone_id"]), "distance_nm": r["distance_nm"]}
                 for r in near],
    }


