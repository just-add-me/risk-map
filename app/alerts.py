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

log = logging.getLogger(__name__)


def vessel_label(mmsi: str) -> str:
    row = db.query_one(
        "SELECT v.name AS name, a.ais_name AS ais_name FROM vessels v "
        "LEFT JOIN ais_state a ON a.mmsi = v.mmsi WHERE v.mmsi=? ORDER BY v.id LIMIT 1", (mmsi,))
    name = (row or {}).get("name") or (row or {}).get("ais_name")
    return f"{name} (MMSI {mmsi})" if name else f"MMSI {mmsi}"


def _fmt_duration(since_iso: str) -> str:
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


def _eta_text(distance_nm: float, sog: Optional[float]) -> str:
    if not sog or sog < 0.5:
        return ""
    hours = distance_nm / sog
    return f", при {sog:.0f} уз. ≈ {hours:.1f} ч" if hours < 72 else f", при {sog:.0f} уз. ≈ {hours / 24:.1f} сут"


def _zone_suffix(zone_id: str) -> str:
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


def _enter_severity(zone_id: str) -> str:
    """Вход в зону JWC, WOA и HRA — критично; ERZ и INL — предупреждение."""
    z = zones.index().get(zone_id)
    p = z.props if z else {}
    if p.get("layer") == "inl" or (p.get("layer") == "ibf" and p.get("category") in ("erz", "erz2")):
        return "warning"
    return "critical"


def process(mmsi: str, lon: float, lat: float, sog: Optional[float] = None,
            initial: bool = False, reason: str = "") -> list[int]:
    """Пересчитывает статусы судна. Возвращает id созданных уведомлений."""
    idx = zones.index()
    result = idx.evaluate(lon, lat, config.WARN_DISTANCE_NM)
    # сезонные ограничения INL вне сезона не действуют
    off_season = {zid for zid in result if not zones.in_effect(zid)}
    for zid in off_season:
        del result[zid]
    old = {r["zone_id"]: r for r in db.query("SELECT * FROM zone_state WHERE mmsi=?", (mmsi,))}
    label = vessel_label(mmsi)
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
        zname = zones.zone_name(zid)
        suffix = _zone_suffix(zid)

        if cur == prev:
            if cur == "near":
                db.execute("UPDATE zone_state SET distance_nm=? WHERE mmsi=? AND zone_id=?", (dist, mmsi, zid))
            continue

        if cur == "inside":
            if initial:
                title = f"{label}: находится в зоне «{zname}»"
                body = "Судно уже было в зоне на момент начала отслеживания." + suffix
            elif reason:
                title = f"{label}: теперь в зоне «{zname}»"
                body = reason + suffix
            else:
                title = f"{label}: ВХОД в зону «{zname}»"
                body = f"Позиция {lat:.4f}, {lon:.4f}." + suffix
            created.append(db.notify("zone_enter", _enter_severity(zid), title, body, mmsi, zid))
        elif prev == "inside":
            dur = _fmt_duration(old[zid]["since"])
            if not zones.in_effect(zid):
                title = f"{label}: сезонное ограничение «{zname}» закончилось"
                body = "Судно по-прежнему в этом районе, но до следующего сезона ограничение не действует."
            elif reason:
                title = f"{label}: зона «{zname}» больше не действует для судна"
                body = reason
            else:
                title = f"{label}: ВЫХОД из зоны «{zname}»"
                body = f"Позиция {lat:.4f}, {lon:.4f}. Время в зоне: {dur}." if dur else ""
            created.append(db.notify("zone_exit", "info", title, body, mmsi, zid))
        elif cur == "near" and prev is None:
            title = f"{label}: приближается к зоне «{zname}»"
            body = f"До границы ≈ {dist:.0f} мор. миль{_eta_text(dist or 0, sog)}." + suffix
            created.append(db.notify("zone_near", "warning", title, body, mmsi, zid))

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
    """После смены циркуляра пересчитываем все суда; уведомления — с пояснением причины."""
    rows = db.query("SELECT mmsi, lat, lon, sog FROM ais_state WHERE lat IS NOT NULL")
    count = 0
    for r in rows:
        count += len(process(r["mmsi"], r["lon"], r["lat"], r["sog"], reason=reason))
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


def check_stale() -> int:
    """Уведомление, если от судна давно нет AIS (бесплатный AIS бывает с дырами в покрытии)."""
    limit_h = config.STALE_AIS_HOURS
    n = 0
    rows = db.query("SELECT DISTINCT a.mmsi, a.ts, a.stale_notified FROM ais_state a "
                    "JOIN vessels v ON v.mmsi=a.mmsi WHERE a.ts IS NOT NULL")
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            age_h = (now - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        except ValueError:
            continue
        if age_h > limit_h and not r["stale_notified"]:
            db.notify("ais_stale", "warning", f"{vessel_label(r['mmsi'])}: нет данных AIS {age_h:.0f} ч",
                      "Судно вне зоны покрытия береговых станций или выключило AIS. "
                      "Статус по зонам показан на момент последней позиции.", r["mmsi"])
            db.execute("UPDATE ais_state SET stale_notified=1 WHERE mmsi=?", (r["mmsi"],))
            n += 1
    return n
