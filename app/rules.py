"""Конструктор отслеживания: правила пользователей.

Правило = что отслеживаем (все мои флоты / флот / судно) + условие (kind и params) +
кому сообщать (только владельцу правила или всем, кто видит флот) + важность.

Правило срабатывает на изменение, а не на состояние: при первом наблюдении судна состояние
только запоминается (иначе новое правило выдало бы лавину уведомлений). Состояние по каждой
паре «правило — судно» хранится в rule_state.

Точки входа:
  on_position(mmsi, …)   — новая позиция судна флота (из ingest.position);
  on_static(mmsi, …)     — сменились назначение или ETA (из ingest.static_data);
  on_incident(row)       — новое происшествие (из ais.incidents.record);
  periodic()             — раз в 10 минут: «нет сигнала AIS».
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from . import access, alerts, db, zones
from .geo.zone_index import ZoneIndex

log = logging.getLogger(__name__)

NAV_NAMES = {0: "идёт под двигателем", 1: "на якоре", 2: "не управляется", 3: "ограничено в манёвре",
             4: "стеснено осадкой", 5: "ошвартовано", 6: "на мели", 7: "ведёт промысел", 8: "идёт под парусом",
             14: "аварийный маяк включён"}
KINDS = {
    "zone_enter": "Вход в зону",
    "zone_exit": "Выход из зоны",
    "zone_near": "Подход к зоне",
    "ais_lost": "Нет сигнала AIS",
    "nav_status": "Смена статуса",
    "speed": "Скорость",
    "destination": "Смена назначения или ETA",
    "port_arrival": "Заход в порт",
    "port_departure": "Выход из порта",
    "incident": "Происшествие рядом",
}
PORT_IN_NM = 3.0       # «в порту»: ближе 3 миль к точке порта и почти без хода
PORT_OUT_NM = 5.0      # «вышел»: дальше 5 миль (запас, чтобы не мигало у границы)

_areas_lock = threading.Lock()
_areas: dict[str, tuple[int, ZoneIndex]] = {}       # владелец → (версия, индекс его областей)
_areas_version = 0


# --------------------------------------------------------------------------- проверка параметров
def validate(kind: str, params: dict, owner: str) -> dict:
    """Приводит параметры к нужному виду; ValueError с понятным текстом — если что-то не так."""
    if kind not in KINDS:
        raise ValueError("Неизвестный тип правила")
    p: dict = {}
    if kind.startswith("zone_"):
        scope = str(params.get("scope") or "")
        if scope.startswith("area:"):
            aid = int(scope[5:] or 0)
            if not db.query_one("SELECT id FROM areas WHERE id=? AND owner=?", (aid, owner)):
                raise ValueError("Область не найдена")
        elif scope.startswith("zone:"):
            if not zones.index().get(scope[5:]):
                raise ValueError("Зона не найдена")
        elif scope not in ("layer:any", "layer:jwc", "layer:ibf", "layer:inl"):
            raise ValueError("Выберите, какие зоны отслеживать")
        p["scope"] = scope
        if kind == "zone_near":
            p["distance_nm"] = _num(params.get("distance_nm"), 1, 500, "Расстояние, мили")
    elif kind == "ais_lost":
        p["hours"] = _num(params.get("hours"), 0.5, 720, "Часов без сигнала")
    elif kind == "nav_status":
        st = [int(x) for x in params.get("statuses") or [] if int(x) in NAV_NAMES]
        if not st:
            raise ValueError("Отметьте хотя бы один статус")
        p["statuses"] = st
    elif kind == "speed":
        p["op"] = "above" if params.get("op") == "above" else "below"
        p["knots"] = _num(params.get("knots"), 0, 60, "Скорость, узлы")
        p["minutes"] = _num(params.get("minutes"), 0, 1440, "Сколько минут подряд")
    elif kind == "destination":
        p["eta"] = bool(params.get("eta"))
    elif kind in ("port_arrival", "port_departure"):
        pid = params.get("port_id")
        p["port_id"] = int(pid) if pid not in (None, "", "any") else None
    elif kind == "incident":
        p["radius_nm"] = _num(params.get("radius_nm"), 1, 300, "Радиус, мили")
        from .ais import incidents
        ks = [k for k in params.get("kinds") or [] if k in incidents.KINDS]
        if not ks:
            raise ValueError("Отметьте хотя бы один тип происшествий")
        p["kinds"] = ks
    return p


def _num(v, lo: float, hi: float, label: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: нужно число") from None
    if not lo <= x <= hi:
        raise ValueError(f"{label}: от {lo:g} до {hi:g}")
    return x


def describe(r: dict) -> str:
    """Человеческое описание условия — для списка правил и текста уведомления."""
    p = r["params"]
    k = r["kind"]
    if k.startswith("zone_"):
        what = scope_name(p["scope"], r["owner"])
        if k == "zone_enter":
            return f"вход в {what}"
        if k == "zone_exit":
            return f"выход из: {what}"
        return f"ближе {p['distance_nm']:g} миль к: {what}"
    if k == "ais_lost":
        return f"нет сигнала AIS дольше {p['hours']:g} ч"
    if k == "nav_status":
        return "статус стал: " + ", ".join(NAV_NAMES[s] for s in p["statuses"])
    if k == "speed":
        return (f"скорость {'ниже' if p['op'] == 'below' else 'выше'} {p['knots']:g} уз"
                + (f" дольше {p['minutes']:g} мин" if p["minutes"] else ""))
    if k == "destination":
        return "сменился порт назначения" + (" или ETA" if p["eta"] else "")
    if k in ("port_arrival", "port_departure"):
        port = _port(p["port_id"])["name"] if p["port_id"] is not None and _port(p["port_id"]) else "любой порт"
        return ("заход в " if k == "port_arrival" else "выход из: ") + port
    if k == "incident":
        from .ais import incidents
        return (f"в {p['radius_nm']:g} милях: "
                + ", ".join(incidents.KINDS[x]["label"].lower() for x in p["kinds"]))
    return KINDS.get(k, k)


def scope_name(scope: str, owner: str) -> str:
    if scope.startswith("layer:"):
        return {"any": "любую зону", "jwc": "зоны JWC", "ibf": "зоны IBF/ITF", "inl": "районы INL"}[scope[6:]]
    if scope.startswith("zone:"):
        return f"зону «{zones.zone_name(scope[5:])}»"
    row = db.query_one("SELECT name FROM areas WHERE id=?", (int(scope[5:]),))
    return f"мою область «{row['name']}»" if row else "удалённую область"


# --------------------------------------------------------------------------- кого касается правило
def _load(r: dict) -> dict:
    return {**r, "params": json.loads(r["params"])}


def _fleets_of(mmsi: str) -> list[int]:
    return [r["fleet_id"] for r in db.query("SELECT fleet_id FROM vessels WHERE mmsi=?", (mmsi,))]


def _applies(r: dict, mmsi: str, fleets: list[int]) -> list[int]:
    """Флоты (из флотов судна), через которые правило касается этого судна. Пусто — не касается."""
    seen = [f for f in fleets if access.level(f, r["owner"])]
    if r["target_type"] == "fleet":
        return [f for f in seen if str(f) == str(r["target"])]
    if r["target_type"] == "vessel":
        return seen if r["target"] == mmsi else []
    return seen


def rules_for(mmsi: str, kinds: tuple[str, ...]) -> list[tuple[dict, list[int]]]:
    fleets = _fleets_of(mmsi)
    if not fleets:
        return []
    marks = ",".join("?" * len(kinds))
    out = []
    for row in db.query(f"SELECT * FROM rules WHERE enabled=1 AND kind IN ({marks})", kinds):
        r = _load(row)
        via = _applies(r, mmsi, fleets)
        if via:
            out.append((r, via))
    return out


def targets_of(r: dict) -> list[tuple[str, list[int]]]:
    """Все суда, которые отслеживает правило (для периодических проверок и происшествий)."""
    if r["target_type"] == "vessel":
        rows = db.query("SELECT DISTINCT mmsi FROM vessels WHERE mmsi=?", (r["target"],))
    elif r["target_type"] == "fleet":
        rows = db.query("SELECT DISTINCT mmsi FROM vessels WHERE fleet_id=?", (int(r["target"]),))
    else:
        rows = db.query(f"SELECT DISTINCT mmsi FROM ({access.VISIBLE_MMSI_SQL})", (r["owner"], r["owner"]))
    out = []
    for row in rows:
        via = _applies(r, row["mmsi"], _fleets_of(row["mmsi"]))
        if via and not row["mmsi"].startswith("IMO:"):
            out.append((row["mmsi"], via))
    return out


# --------------------------------------------------------------------------- состояние и срабатывание
def _state(rid: int, mmsi: str) -> Optional[dict]:
    return db.query_one("SELECT * FROM rule_state WHERE rule_id=? AND mmsi=?", (rid, mmsi))


def _save(rid: int, mmsi: str, value: str, fired: int = 0, keep_since: bool = False) -> None:
    if keep_since:
        db.execute("UPDATE rule_state SET value=?, fired=? WHERE rule_id=? AND mmsi=?", (value, fired, rid, mmsi))
        return
    db.execute("INSERT INTO rule_state(rule_id, mmsi, value, since, fired) VALUES(?,?,?,?,?) "
               "ON CONFLICT(rule_id, mmsi) DO UPDATE SET value=excluded.value, since=excluded.since, "
               "fired=excluded.fired", (rid, mmsi, value, db.now_iso(), fired))


def _recipients(r: dict, via: list[int]) -> set[str]:
    users = {r["owner"]}
    if r["audience"] == "fleet":
        for fid in via:
            f = db.query_one("SELECT owner FROM fleets WHERE id=?", (fid,))
            if f:
                users.add(f["owner"])
            users.update(s["username"] for s in access.shares(fid))
    return users


def fire(r: dict, mmsi: str, via: list[int], detail: str, zone_id: Optional[str] = None) -> None:
    title = f"{alerts.vessel_label(mmsi)}: {r['name']}"
    body = f"{detail}\nПравило: {describe(r)}."
    for user in _recipients(r, via):
        db.notify("rule", r["severity"], title, body, mmsi, zone_id, username=user)
    db.execute("UPDATE rules SET last_fired=?, fired_count=fired_count+1 WHERE id=?", (db.now_iso(), r["id"]))


# --------------------------------------------------------------------------- области пользователя
def areas_changed() -> None:
    global _areas_version
    with _areas_lock:
        _areas_version += 1


def _area_index(owner: str) -> ZoneIndex:
    with _areas_lock:
        cached = _areas.get(owner)
        if cached and cached[0] == _areas_version:
            return cached[1]
        feats = [{"type": "Feature", "properties": {"id": f"area-{a['id']}", "name": a["name"]},
                  "geometry": json.loads(a["geometry"])}
                 for a in db.query("SELECT * FROM areas WHERE owner=?", (owner,))]
        idx = ZoneIndex({"features": feats})
        _areas[owner] = (_areas_version, idx)
        return idx


def _scope_zones(r: dict) -> tuple[ZoneIndex, list]:
    scope = r["params"]["scope"]
    if scope.startswith("area:"):
        idx = _area_index(r["owner"])
        return idx, [z for z in idx.zones if z.id == f"area-{scope[5:]}"]
    idx = zones.index()
    if scope.startswith("zone:"):
        return idx, [z for z in idx.zones if z.id == scope[5:]]
    layer = scope[6:]
    return idx, [z for z in idx.zones if (layer == "any" or z.props.get("layer", "jwc") == layer)
                 and zones.in_season(z.props)]


# --------------------------------------------------------------------------- порты
def _port(pid: Optional[int]) -> Optional[dict]:
    from .layers import ports
    plist = ports.load()
    return plist[pid] if pid is not None and 0 <= pid < len(plist) else None


def _dist_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((((lon2 - lon1) + 180) % 360 - 180) * 60 * k, (lat2 - lat1) * 60)


def _nearest_port(lat: float, lon: float, max_nm: float) -> Optional[tuple[int, dict, float]]:
    from .layers import ports
    best = None
    for i, p in enumerate(ports.load()):
        if abs(p["lat"] - lat) > max_nm / 60 + 0.1:
            continue
        d = _dist_nm(lat, lon, p["lat"], p["lon"])
        if d <= max_nm and (best is None or d < best[2]):
            best = (i, p, d)
    return best


# --------------------------------------------------------------------------- обработчики
def _zone_rule(r: dict, mmsi: str, via: list[int], lat: float, lon: float) -> None:
    idx, zs = _scope_zones(r)
    k = r["kind"]
    if k == "zone_near":
        d_max = r["params"]["distance_nm"]
        cur = {}
        for z in zs:
            if idx.contains(z, lon, lat):
                continue
            d = idx.distance_nm(z, lon, lat, d_max)
            if d is not None:
                cur[z.id] = d
        cur_ids = set(cur)
    else:
        cur_ids = {z.id for z in zs if idx.contains(z, lon, lat)}
    st = _state(r["id"], mmsi)
    _save(r["id"], mmsi, ",".join(sorted(cur_ids)))
    if st is None:                    # первое наблюдение — только запоминаем
        return
    prev = set(filter(None, (st["value"] or "").split(",")))
    names = {z.id: z.props.get("name", z.id) for z in zs}
    if k == "zone_enter":
        for zid in sorted(cur_ids - prev):
            fire(r, mmsi, via, f"Вошло в «{names.get(zid, zid)}». Позиция {lat:.4f}, {lon:.4f}.", zid)
    elif k == "zone_exit":
        for zid in sorted(prev - cur_ids):
            fire(r, mmsi, via, f"Вышло из «{names.get(zid, zid)}». Позиция {lat:.4f}, {lon:.4f}.", zid)
    else:
        for zid in sorted(cur_ids - prev):
            fire(r, mmsi, via, f"До «{names.get(zid, zid)}» ≈ {cur[zid]:.0f} мор. миль.", zid)


def _nav_rule(r: dict, mmsi: str, via: list[int], nav: Optional[int]) -> None:
    if nav is None or nav == 15:
        return
    st = _state(r["id"], mmsi)
    if st is not None and st["value"] == str(nav):
        return
    _save(r["id"], mmsi, str(nav))
    if st is not None and nav in r["params"]["statuses"]:
        before = NAV_NAMES.get(int(st["value"]), "—") if (st["value"] or "").isdigit() else "—"
        fire(r, mmsi, via, f"Статус: {NAV_NAMES.get(nav, nav)} (был: {before}).")


def _speed_rule(r: dict, mmsi: str, via: list[int], sog: Optional[float]) -> None:
    if sog is None:
        return
    p = r["params"]
    hit = sog < p["knots"] if p["op"] == "below" else sog > p["knots"]
    st = _state(r["id"], mmsi)
    if not hit:
        if st is None or st["value"] != "ok":
            _save(r["id"], mmsi, "ok")
        return
    if st is None or st["value"] != "hit":
        if st is None:                 # уже было так, когда правило появилось — ждём смены
            _save(r["id"], mmsi, "hit", fired=1)
            return
        _save(r["id"], mmsi, "hit")
        st = _state(r["id"], mmsi)
    if st["fired"]:
        return
    held = (datetime.now(timezone.utc) - datetime.fromisoformat(st["since"])).total_seconds() / 60
    if held >= p["minutes"]:
        _save(r["id"], mmsi, "hit", fired=1, keep_since=True)
        fire(r, mmsi, via, f"Скорость {sog:.1f} уз" + (f" уже {held:.0f} мин." if p["minutes"] else "."))


def _port_rule(r: dict, mmsi: str, via: list[int], lat: float, lon: float, sog: Optional[float]) -> None:
    st = _state(r["id"], mmsi)
    prev = (st["value"] or "") if st else None
    cur = ""
    if prev:                                         # стояли в порту: «вышло» только дальше PORT_OUT_NM
        p = _port(int(prev))
        if p and _dist_nm(lat, lon, p["lat"], p["lon"]) <= PORT_OUT_NM:
            cur = prev
    if not cur and (sog is None or sog < 1.5):
        near = _nearest_port(lat, lon, PORT_IN_NM)
        cur = str(near[0]) if near else ""
    if cur == prev:
        return
    _save(r["id"], mmsi, cur)
    if prev is None:
        return
    want = r["params"]["port_id"]
    if r["kind"] == "port_arrival" and cur and (want is None or int(cur) == want):
        fire(r, mmsi, via, f"Зашло в порт {_port(int(cur))['name']}.")
    if r["kind"] == "port_departure" and prev and not cur and (want is None or int(prev) == want):
        fire(r, mmsi, via, f"Вышло из порта {_port(int(prev))['name']}.")


def _safe(fn: Callable, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — ошибка в одном правиле не должна ломать приём AIS
        log.exception("Ошибка правила %s", getattr(args[0], "get", lambda k: None)("id"))


def on_position(mmsi: str, lat: float, lon: float, sog: Optional[float], nav: Optional[int]) -> None:
    for r, via in rules_for(mmsi, ("zone_enter", "zone_exit", "zone_near", "nav_status", "speed",
                                   "port_arrival", "port_departure", "ais_lost")):
        k = r["kind"]
        if k.startswith("zone_"):
            _safe(_zone_rule, r, mmsi, via, lat, lon)
        elif k == "nav_status":
            _safe(_nav_rule, r, mmsi, via, nav)
        elif k == "speed":
            _safe(_speed_rule, r, mmsi, via, sog)
        elif k.startswith("port_"):
            _safe(_port_rule, r, mmsi, via, lat, lon, sog)
        elif k == "ais_lost":                        # сигнал есть — сбрасываем «уже сообщили»
            db.execute("UPDATE rule_state SET fired=0 WHERE rule_id=? AND mmsi=?", (r["id"], mmsi))


def on_static(mmsi: str, destination: Optional[str], eta: Optional[str]) -> None:
    for r, via in rules_for(mmsi, ("destination",)):
        value = f"{destination or ''}|{eta or ''}" if r["params"]["eta"] else (destination or "")
        st = _state(r["id"], mmsi)
        if st is not None and st["value"] == value:
            continue
        _save(r["id"], mmsi, value)
        if st is not None:
            old = (st["value"] or "").split("|")
            text = f"Назначение: {destination or '—'}" + (f", ETA {eta or '—'}" if r["params"]["eta"] else "")
            fire(r, mmsi, via, f"{text} (было: {old[0] or '—'}" + (f", ETA {old[1] or '—'}" if len(old) > 1 else "") + ").")


def on_incident(inc: dict) -> None:
    if inc.get("lat") is None or inc.get("test"):
        return
    for row in db.query("SELECT * FROM rules WHERE enabled=1 AND kind='incident'"):
        r = _load(row)
        if inc["kind"] not in r["params"]["kinds"]:
            continue
        for mmsi, via in targets_of(r):
            if mmsi == inc["mmsi"]:
                continue
            pos = db.query_one("SELECT lat, lon FROM ais_state WHERE mmsi=? AND lat IS NOT NULL", (mmsi,))
            if not pos:
                continue
            d = _dist_nm(pos["lat"], pos["lon"], inc["lat"], inc["lon"])
            if d <= r["params"]["radius_nm"]:
                from .ais import incidents
                who = inc.get("name") or f"MMSI {inc['mmsi']}"
                fire(r, mmsi, via, f"{incidents.KINDS[inc['kind']]['label']}: {who}, в {d:.0f} милях от судна."
                     + (f" «{inc['text']}»" if inc.get("text") else ""))


def periodic() -> None:
    """«Нет сигнала AIS»: проверяем раз в 10 минут."""
    now = datetime.now(timezone.utc)
    for row in db.query("SELECT * FROM rules WHERE enabled=1 AND kind='ais_lost'"):
        r = _load(row)
        for mmsi, via in targets_of(r):
            pos = db.query_one("SELECT ts FROM ais_state WHERE mmsi=? AND ts IS NOT NULL", (mmsi,))
            if not pos:
                continue
            hours = (now - datetime.fromisoformat(pos["ts"])).total_seconds() / 3600
            st = _state(r["id"], mmsi)
            if hours < r["params"]["hours"]:
                if st is None:
                    _save(r["id"], mmsi, "ok")
                continue
            if st is None:                 # молчало уже на момент создания правила — сообщим один раз
                _save(r["id"], mmsi, "lost", fired=1)
                fire(r, mmsi, via, f"Последний сигнал AIS {hours:.0f} ч назад.")
            elif not st["fired"]:
                _save(r["id"], mmsi, "lost", fired=1)
                fire(r, mmsi, via, f"Последний сигнал AIS {hours:.0f} ч назад.")
