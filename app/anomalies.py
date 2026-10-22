"""Подозрительное поведение судов флотов (признаки теневого флота и обхода санкций).

Без внешних сервисов, по нашим же данным AIS:
* dark — судно молчит больше DARK_HOURS там, где другие суда видны (в радиусе COVER_NM сейчас
  передают хотя бы COVER_MIN судов): значит, не «дыра» в покрытии, а выключенный AIS;
* sts — судно стоит в открытом море (дальше PORT_NM от портов) рядом с другим крупным судном,
  тоже стоящим, дольше STS_MINUTES: так перегружают груз с борта на борт;
* spoof — скачок позиции: чтобы оказаться в новой точке, судну пришлось бы идти быстрее JUMP_KN;
* identity — судно сменило название в AIS, или его IMO теперь передаёт другой MMSI (смена флага).

Всё записывается в происшествия (kinds dark/sts/spoof/identity) с уведомлением.
Функция отдельная: всё, что её подключает, помечено меткой [подозрительное-поведение] — чтобы удалить функцию,
удалите этот файл и отмеченные строки (поиск: grep -rn "[подозрительное-поведение]" app static tests), а в базе —
таблицу anomaly_state и записи incidents с этими видами.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from . import db
from .ais import incidents, shipinfo, traffic

log = logging.getLogger(__name__)

DARK_HOURS = 6
COVER_NM, COVER_MIN = 30, 5
STS_NM, STS_KN, STS_MINUTES, PORT_NM = 0.3, 1.5, 30, 10
JUMP_KN, JUMP_MIN_NM = 50, 20
BIG = {"tanker", "cargo"}
CROWD = 3                    # столько и больше стоящих судов рядом — якорная стоянка или порт, а не встреча двоих
MOORED = 5                   # навигационный статус AIS «ошвартовано»

_sts_seen: dict[tuple[str, str], tuple[float, float]] = {}   # (судно, соседнее) → (впервые, последний раз рядом)


def _nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * 60, (((lon2 - lon1 + 180) % 360) - 180) * 60 * k)


def _once(mmsi: str, kind: str, key: str) -> bool:
    """True — такое ещё не отмечали (и отмечаем)."""
    cur = db.execute("INSERT OR IGNORE INTO anomaly_state(mmsi, kind, key, created_at) VALUES(?,?,?,?)",
                     (mmsi, kind, key, db.now_iso()))
    return bool(cur.rowcount)


def _name(mmsi: str) -> Optional[str]:
    r = db.query_one("SELECT COALESCE(NULLIF(v.name, ''), a.ais_name) AS n FROM vessels v "
                     "LEFT JOIN ais_state a ON a.mmsi=v.mmsi WHERE v.mmsi=?", (mmsi,))
    return r["n"] if r else None


def on_position(mmsi: str, prev: Optional[dict], lat: float, lon: float, ts: str) -> None:
    """Скачок позиции у судна флота (вызывается из ingest.position до записи новой позиции)."""
    if not prev or prev.get("lat") is None or not prev.get("ts"):
        return
    try:
        hours = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev["ts"])).total_seconds() / 3600
    except ValueError:
        return
    d = _nm(prev["lat"], prev["lon"], lat, lon)
    if d < JUMP_MIN_NM or hours <= 0:
        return
    speed = d / max(hours, 1 / 60)
    if speed > JUMP_KN and _once(mmsi, "spoof", ts[:13]):
        incidents.record("spoof", mmsi, name=_name(mmsi), lat=lat, lon=lon, tracked=True, source="AIS",
                         text=f"Скачок на {d:.0f} миль за {hours * 60:.0f} мин (≈{speed:.0f} уз) — "
                              f"из {prev['lat']:.3f}, {prev['lon']:.3f}. Возможна подмена позиции.")


def check_fleet() -> int:
    """Раз в 10 минут: молчание AIS при хорошем покрытии, встречи в море, смена названия и MMSI."""
    from .layers import ports
    # порты: Natural Earth (~1100) + UN/LOCODE (~12 500) — в одном Natural Earth нет даже Генуи
    from .ais import destination
    try:
        destination._load()
    except Exception:  # noqa: BLE001 — справочник не скачался: обойдёмся портами Natural Earth
        log.warning("UN/LOCODE недоступен — порты только из Natural Earth")
    port_pts = [(p["lat"], p["lon"]) for p in ports.load()] + [(p["lat"], p["lon"]) for p in destination._codes.values()]
    now = datetime.now(timezone.utc)
    found = 0
    rows = db.query("SELECT DISTINCT a.*, v.imo AS fleet_imo FROM vessels v JOIN ais_state a ON a.mmsi=v.mmsi "
                    "WHERE a.lat IS NOT NULL")
    for r in rows:
        mmsi, name = r["mmsi"], _name(r["mmsi"])
        # 1. молчание при хорошем покрытии
        try:
            silent_h = (now - datetime.fromisoformat(r["ts"])).total_seconds() / 3600
        except (TypeError, ValueError):
            silent_h = 0
        in_port = any(_nm(r["lat"], r["lon"], a, b) < PORT_NM for a, b in port_pts)
        if silent_h >= DARK_HOURS and not in_port:          # в порту AIS часто выключают законно
            others = [s for s in traffic.near(r["lat"], r["lon"], COVER_NM) if s["mmsi"] != mmsi]
            if len(others) >= COVER_MIN and _once(mmsi, "dark", r["ts"]):
                found += 1
                incidents.record("dark", mmsi, name=name, lat=r["lat"], lon=r["lon"], tracked=True, source="AIS",
                                 text=f"Нет сигнала AIS {silent_h:.0f} ч, хотя рядом ({COVER_NM} миль) сейчас видно "
                                      f"{len(others)} судов. Последняя позиция {r['lat']:.3f}, {r['lon']:.3f}.")
        # 2. встреча с другим судном в открытом море
        elif (silent_h < 1 and (r["sog"] or 0) < STS_KN and not in_port and r["nav"] != MOORED
              and shipinfo.category(r["ship_type"]) in BIG):        # перегрузка с борта на борт — у танкеров и сухогрузов
            around = [s for s in traffic.near(r["lat"], r["lon"], 1.0) if s["mmsi"] != mmsi and (s["sog"] or 0) < STS_KN]
            # скопление — стоянка, а не встреча двоих
            for s in (around if len(around) < CROWD else []):
                if s["distance_nm"] > STS_NM or s["category"] not in BIG or s["nav_status"] == "Ошвартовано":
                    continue
                key, t = (mmsi, s["mmsi"]), now.timestamp()
                first, last = _sts_seen.get(key, (t, t))
                if t - last > 25 * 60:                   # расходились — считаем встречу заново
                    first = t
                _sts_seen[key] = (first, t)
                if t - first >= STS_MINUTES * 60 and _once(mmsi, "sts", f"{s['mmsi']}|{now:%Y-%m-%d}"):
                    found += 1
                    other = s["name"] or f"MMSI {s['mmsi']}"
                    incidents.record("sts", mmsi, name=name, lat=r["lat"], lon=r["lon"], tracked=True, source="AIS",
                                     text=f"Стоит борт к борту с «{other}» ({s['type_name']}, "
                                          f"{shipinfo.flag(s['mmsi']) or 'флаг неизвестен'}) в открытом море, "
                                          f"дальше {PORT_NM} миль от портов — возможна перегрузка с борта на борт.")
        # 3. смена названия или MMSI
        ais_name = (r["ais_name"] or "").strip()
        if ais_name:
            old = db.query_one("SELECT value FROM kv WHERE key=?", (f"ais_name:{mmsi}",))
            if old and old["value"] != ais_name and _once(mmsi, "identity", f"name|{ais_name}"):
                found += 1
                incidents.record("identity", mmsi, name=name, lat=r["lat"], lon=r["lon"], tracked=True, source="AIS",
                                 text=f"Название в AIS сменилось: «{old['value']}» → «{ais_name}».")
            db.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (f"ais_name:{mmsi}", ais_name))
        imo = r["fleet_imo"] or r["imo"]
        if imo:
            other = traffic.find_imo(str(imo))
            o = traffic.get(other) if other and other != mmsi else None
            mine = (traffic.get(mmsi) or {}).get("ts") or 0
            # другой MMSI с этим IMO передаёт позицию сейчас и свежее нашего — похоже на смену флага
            if (o and o.get("ts") and now.timestamp() - o["ts"] <= traffic.POSITION_TTL and o["ts"] > mine + 3600
                    and _once(mmsi, "identity", f"mmsi|{other}")):
                found += 1
                incidents.record("identity", mmsi, name=name, lat=r["lat"], lon=r["lon"], tracked=True, source="AIS",
                                 text=f"IMO {imo} теперь передаёт другой MMSI: {other} (флаг "
                                      f"{shipinfo.flag(other) or 'неизвестен'}, был {shipinfo.flag(mmsi) or 'неизвестен'}). "
                                      f"Возможна смена флага — проверьте и обновите судно во флоте.")
    # пары, которые разошлись, забываем
    for key in [k for k, (_, t) in _sts_seen.items() if now.timestamp() - t > 6 * 3600]:
        del _sts_seen[key]
    return found
