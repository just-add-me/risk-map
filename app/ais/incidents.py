"""Происшествия и опасности из потока AIS.

Источники в потоке aisstream (работают — приходят в сообщениях о позиции):
* аварийные маяки — свои номера MMSI: 970… AIS-SART, 972… «человек за бортом», 974… EPIRB;
* навигационный статус судна: 6 «на мели», 2 «не управляется», 14 «аварийный маяк включён».
Обрабатываются, но на 21.10.2026 aisstream их не передаёт (проверено: без фильтра приходят только
позиции и паспортные данные):
* сообщения безопасности (SafetyBroadcastMessage, AddressedSafetyMessage) — текст от судна или станции;
* поисково-спасательные самолёты и вертолёты (StandardSearchAndRescueAircraftReport).
Санкции и задержания (app/screening.py) и новости о судах флота (app/news.py) тоже записываются сюда.

Сообщения редкие, поэтому хранятся в базе (таблица incidents) 14 дней. Повторы одного и того
же события не плодят записи: обновляются время, позиция и счётчик.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import db

log = logging.getLogger(__name__)

KINDS = {
    "distress": {"label": "Сигнал бедствия", "severity": "critical"},
    "sart": {"label": "Аварийный радиомаяк AIS-SART", "severity": "critical"},
    "mob": {"label": "Человек за бортом (MOB)", "severity": "critical"},
    "epirb": {"label": "Аварийный буй EPIRB", "severity": "critical"},
    "aground": {"label": "Судно на мели", "severity": "warning"},
    "nuc": {"label": "Судно не управляется", "severity": "warning"},
    "sar": {"label": "Поисково-спасательный самолёт / вертолёт", "severity": "info"},
    "safety": {"label": "Сообщение о безопасности", "severity": "info"},
    "sanction": {"label": "Санкции: судно вашего флота", "severity": "critical"},
    "sanction_new": {"label": "Новое судно в санкционном списке", "severity": "info"},
    "detention": {"label": "Задержание в порту", "severity": "warning"},
    "news_risk": {"label": "Новости: тревожное", "severity": "warning"},
    "news": {"label": "Новости о судне", "severity": "info"},
    "dark": {"label": "Пропал AIS при хорошем покрытии", "severity": "warning"},      # [подозрительное-поведение]
    "sts": {"label": "Встреча с судном в открытом море", "severity": "warning"},      # [подозрительное-поведение]
    "spoof": {"label": "Скачок позиции", "severity": "warning"},                       # [подозрительное-поведение]
    "identity": {"label": "Смена названия или MMSI", "severity": "warning"},           # [подозрительное-поведение]
}
# Подозрительное поведение (app/anomalies.py) — только суда флотов, правило «рядом с судном» не нужно  [подозрительное-поведение]
ANOMALY_KINDS = {"dark", "sts", "spoof", "identity"}    # [подозрительное-поведение]
# События «про судно», а не «в точке»: на карте показываются там, где судно сейчас
VESSEL_KINDS = {"sanction", "sanction_new", "detention", "news", "news_risk"}
DEVICE_PREFIX = {"970": "sart", "972": "mob", "974": "epirb"}
NAV_KIND = {6: "aground", 2: "nuc", 14: "sart"}
KEEP_DAYS = 14
REFRESH_S = 600            # одно и то же событие обновляем в базе не чаще раза в 10 минут

_lock = threading.Lock()
_recent: dict[tuple[str, str, str], tuple[int, float]] = {}    # (mmsi, kind, текст) → (id, когда писали)


def classify(text: str) -> tuple[str, bool]:
    """Тип по тексту сообщения безопасности и признак «это тест»."""
    t = text.upper()
    test = bool(re.search(r"\bTEST", t))
    if "SART" in t:
        return "sart", test
    if re.search(r"\bMOB\b|MAN OVER ?BOARD", t):
        return "mob", test
    if "EPIRB" in t:
        return "epirb", test
    if re.search(r"MAYDAY|DISTRESS|\bSOS\b", t):
        return "distress", test
    if re.search(r"AGROUND|GROUNDED", t):
        return "aground", test
    return "safety", test


def _ago(**delta) -> str:
    """Момент в прошлом в том же формате, в каком время лежит в базе (db.now_iso)."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(microsecond=0).isoformat()


def _valid(lat: Optional[float], lon: Optional[float]) -> bool:
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)


def record(kind: str, mmsi: str, *, name: Optional[str] = None, lat: Optional[float] = None,
           lon: Optional[float] = None, text: str = "", test: bool = False, source: str = "",
           extra: str = "", tracked: bool = False) -> Optional[int]:
    """Сохраняет событие или обновляет такое же недавнее. Вернёт id новой записи (None — это повтор)."""
    if not _valid(lat, lon):
        lat = lon = None
    key = (mmsi, kind, text)
    now = time.time()
    with _lock:
        known = _recent.get(key)
        if known and now - known[1] < REFRESH_S:
            return None
        row = None if known else db.query_one(
            "SELECT id FROM incidents WHERE mmsi=? AND kind=? AND text=? AND last_seen > ?",
            (mmsi, kind, text, _ago(hours=24)))
        iid = known[0] if known else (row["id"] if row else None)
        if iid:
            db.execute("UPDATE incidents SET last_seen=?, count=count+1, lat=COALESCE(?, lat), lon=COALESCE(?, lon), "
                       "name=COALESCE(?, name) WHERE id=?", (db.now_iso(), lat, lon, name, iid))
            _recent[key] = (iid, now)
            return None
        cur = db.execute(
            "INSERT INTO incidents(ts, last_seen, kind, mmsi, name, lat, lon, text, test, source, extra, count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (db.now_iso(), db.now_iso(), kind, mmsi, name, lat, lon, text, int(test), source, extra))
        iid = int(cur.lastrowid or 0)
        _recent[key] = (iid, now)
    from .. import rules            # правило «происшествие рядом с судном»
    try:
        rules.on_incident({"id": iid, "kind": kind, "mmsi": mmsi, "name": name, "lat": lat, "lon": lon,
                           "text": text, "test": test})
    except Exception:  # noqa: BLE001
        log.exception("Ошибка проверки правил для происшествия %s", iid)
    if tracked and not test and KINDS[kind]["severity"] != "info":
        title = f"{name or 'MMSI ' + mmsi}: {KINDS[kind]['label'].lower()}"
        db.notify("incident", KINDS[kind]["severity"], title, text or "Сообщение получено по AIS.", mmsi)
    return iid


def cleanup() -> None:
    db.execute("DELETE FROM incidents WHERE last_seen < ?", (_ago(days=KEEP_DAYS),))
    cutoff = time.time() - 2 * REFRESH_S
    with _lock:
        for k in [k for k, v in _recent.items() if v[1] < cutoff]:
            del _recent[k]


def recent(days: int = 7) -> list[dict]:
    rows = db.query("SELECT * FROM incidents WHERE last_seen > ? ORDER BY last_seen DESC LIMIT 2000",
                    (_ago(days=max(1, min(days, KEEP_DAYS))),))
    from . import traffic
    for r in rows:
        r["label"] = KINDS.get(r["kind"], {}).get("label", r["kind"])
        r["severity"] = KINDS.get(r["kind"], {}).get("severity", "info")
        if r["kind"] in VESSEL_KINDS:
            r["vessel_bound"] = True
            pos = traffic.get(r["mmsi"]) or db.query_one("SELECT lat, lon FROM ais_state WHERE mmsi=?", (r["mmsi"],))
            if pos and pos.get("lat") is not None:
                r["lat"], r["lon"] = pos["lat"], pos["lon"]
    return rows
