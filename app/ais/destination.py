"""Пункт назначения из AIS → порт с координатами, зоны в порту и на прямой до него.

Судно передаёт пункт назначения свободным текстом: код UN/LOCODE («ZADUR», «GR PIR»), название
(«ROTTERDAM», «GENOA _»), маршрут («EEVAN<>FIHEL»), «FOR ORDERS» или мусор. Разбираем по
справочнику UN/LOCODE (открытая копия datasets/un-locode на GitHub; сайт UNECE с сервера
недоступен): сначала код, потом название (с вариантами в скобках, без слов PORT/ANCH/OFF),
потом близкое по написанию название. Распознаётся примерно половина непустых пунктов.

Путь до порта строится по сети морских путей (searoute): через каналы и проливы, в обход суши.
Это типовой маршрут, а не план рейса, поэтому зоны по пути — оценка, а зоны в самом порту —
надёжный признак: по ним уведомляем (check_fleet).
"""
from __future__ import annotations

import collections
import csv
import difflib
import functools
import logging
import math
import re
import threading
from typing import Optional

from .. import config, db, zones
from ..geo import countries as ne

log = logging.getLogger(__name__)

LOCODE_URL = "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
LOCODE_FILE = config.GEODATA_DIR / "un-locode.csv"
PORT_TOLERANCE_NM = 3.0      # координаты UN/LOCODE — с точностью до минуты, обычно центр города
ROUTE_STEP_NM = 25.0

JUNK = re.compile(r"^(FOR ORDERS?|ORDERS?|TBA|TBN|N/?A|NONE|UNKNOWN|[-.=_ ]+|FISHING|SEA|AT SEA|HOME|ANCHORAGE|OPL|"
                  r"DRIFTING|TRIALS?|SEA TRIALS?)$")
NOISE = re.compile(r"\b(PORT|PT|ANCH(?:ORAGE)?|OFF ?SHORE|OFF|ROADS?|PILOTS?|STATION|STN|OPL|TERMINAL|TERM|INNER|"
                   r"OUTER|HARBOU?R|HBR)\b")

# Крупные порты, у которых в UN/LOCODE нет координат (самые частые в AIS на 22.10.2026), — по картам
EXTRA_COORDS = {
    "CAVAN": (49.29, -123.11), "MAPTM": (35.89, -5.50), "JPKZU": (35.38, 139.91), "AUHPT": (-21.28, 149.30),
    "SIKOP": (45.55, 13.73), "NLEEM": (53.45, 6.83), "JPCHB": (35.58, 140.09), "GBTIL": (51.45, 0.36),
    "COCTG": (10.40, -75.53), "ARSLO": (-32.75, -60.73), "AEJEA": (25.01, 55.06), "TRALI": (38.80, 26.95),
    "GBFAW": (50.83, -1.33), "GBFXT": (51.95, 1.32), "JPNGO": (35.05, 136.88), "USLCH": (30.22, -93.25),
    "DEBRB": (53.89, 9.14), "IDPLM": (-2.98, 104.76), "JPMIZ": (34.50, 133.73), "IDMUB": (-0.27, 117.60),
    "ITSAL": (40.67, 14.75), "GBPME": (50.80, -1.10), "ESSVQ": (37.36, -5.99), "DKODE": (55.42, 10.39),
    "DKNBG": (55.31, 10.80), "BSFPO": (26.52, -78.77), "USVAN": (45.63, -122.68), "TTCHA": (10.68, -61.64),
    "RUPRI": (60.35, 28.66), "TRNEM": (38.77, 26.92),
}
NEAR_3_NM = 1500             # трёхбуквенный код без страны («JEA») — ближайший к судну порт с таким кодом
NOT_CODES = {"OFF", "SEA", "TBC", "TBA", "UAE", "ARA", "NIL", "EOS", "FOR"}

_lock = threading.Lock()
_codes: dict[str, dict] = {}                     # «ZADUR» → {code, name, country, lat, lon}
_names: dict[str, list[str]] = {}                # «DURBAN» → [«ZADUR»]
_name_list: list[str] = []


def _nk(s: str) -> str:
    return re.sub(r"[^A-Z]", "", s.upper())


def _coord(c: str) -> Optional[tuple[float, float]]:
    m = re.match(r"(\d{2})(\d{2})([NS]) (\d{3})(\d{2})([EW])", c or "")
    if not m:
        return None
    lat = (int(m[1]) + int(m[2]) / 60) * (1 if m[3] == "N" else -1)
    lon = (int(m[4]) + int(m[5]) / 60) * (1 if m[6] == "E" else -1)
    return lat, lon


def _load() -> None:
    """Справочник портов UN/LOCODE (функция 1 — морской порт). У кода без координат берём
    координаты той же точки из другой строки справочника или порт Natural Earth с тем же названием."""
    global _name_list
    if _codes:
        return
    with _lock:
        if _codes:
            return
        path = ne.download_once(LOCODE_URL, LOCODE_FILE, 3_000_000)
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        from ..layers import shapes
        ne_ports = collections.defaultdict(list)
        for p in shapes.ports_raw():
            ne_ports[_nk(p["name"])].append((p["lat"], p["lon"]))
        major = set(ne_ports)
        any_coord: dict[tuple[str, str], tuple[float, float]] = {}
        for r in rows:
            c = _coord(r["Coordinates"])
            if c:
                any_coord.setdefault((r["Country"], _nk(r["NameWoDiacritics"].split("(")[0])), c)
        names = collections.defaultdict(list)
        for r in rows:
            if r["Function"][:1] != "1":
                continue
            full = r["NameWoDiacritics"]
            base = _nk(full.split("(")[0])
            variants = {base, _nk(full)} | {_nk(x) for x in re.findall(r"\(([^)]+)\)", full)}
            c = (_coord(r["Coordinates"]) or EXTRA_COORDS.get(r["Country"] + r["Location"])
                 or any_coord.get((r["Country"], base)))
            if not c:
                c = next((ne_ports[v][0] for v in variants if len(ne_ports.get(v, [])) == 1), None)
            if not c:
                continue
            code = r["Country"] + r["Location"]
            _codes[code] = {"code": code, "name": full, "country": r["Country"], "lat": round(c[0], 4),
                            "lon": round(c[1], 4), "major": base in major}
            for v in variants:
                if len(v) >= 3:
                    names[v].append(code)
        _names.update(names)
        _name_list = list(names)
        log.info("UN/LOCODE: портов с координатами %d", len(_codes))


def _pick(cands: list[str]) -> str:
    """Одинаковое название у нескольких портов: крупный порт (есть в Natural Earth) важнее."""
    return next((c for c in cands if _codes[c]["major"]), cands[0])


@functools.lru_cache(maxsize=20000)
def resolve(dest: str) -> Optional[dict]:
    """Текст пункта назначения → порт (или None). Поле how: code / name / fuzzy."""
    _load()
    parts = [p for p in re.split(r">+|<>|=>|->|/", (dest or "").upper()) if p.strip()]
    if not parts:
        return None
    d = re.sub(r"\s+", " ", parts[-1]).strip(" .,-_\\^")
    if not d or JUNK.match(d) or "ORDER" in d:
        return None
    k = re.sub(r"[^A-Z0-9]", "", d)
    m = re.match(r"^([A-Z]{2})[ \-_.]?([A-Z0-9]{3})\b", d)
    for code in ([k] if len(k) == 5 else []) + ([m[1] + m[2]] if m else []):
        if code in _codes:
            return {**_codes[code], "how": "code"}
    for cand in (d, re.split(r"[,.]", d)[0], NOISE.sub(" ", d)):
        n = _nk(cand)
        if n in _names:
            return {**_codes[_pick(_names[n])], "how": "name"}
    n = _nk(NOISE.sub(" ", re.split(r"[,.]", d)[0]))
    if len(n) >= 7:
        close = difflib.get_close_matches(n, _name_list, n=1, cutoff=0.88)
        if close:
            return {**_codes[_pick(_names[close[0]])], "how": "fuzzy"}
    return None


def resolve_near(dest: str, lat: Optional[float], lon: Optional[float]) -> Optional[dict]:
    """Как resolve, плюс трёхбуквенный код без страны («JEA» → Jebel Ali): ближайший к судну порт."""
    port = resolve(dest or "")
    if port or lat is None or lon is None:
        return port
    parts = [p for p in re.split(r">+|<>|=>|->|/", (dest or "").upper()) if p.strip()]
    k = re.sub(r"[^A-Z0-9]", "", parts[-1]) if parts else ""
    if len(k) != 3 or k in NOT_CODES:
        return None
    near = sorted((distance_nm(lat, lon, p["lat"], p["lon"]), c) for c, p in _codes.items() if c[2:] == k)
    if near and near[0][0] <= NEAR_3_NM:
        return {**_codes[near[0][1]], "how": "near"}
    return None


def _zones_at(lon: float, lat: float, tolerance_nm: float = 0.0) -> list[str]:
    idx = zones.index()
    return [z.id for z in idx.zones
            if zones.in_effect(z.id) and (idx.contains(z, lon, lat)
                                          or (tolerance_nm and idx.distance_nm(z, lon, lat, tolerance_nm) is not None))]


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _sea_path(lat: float, lon: float, port: dict) -> tuple[list[list[float]], float, str]:
    """Путь по сети морских путей (searoute, Apache-2.0); если не вышло — прямая линия."""
    try:
        import searoute
        r = searoute.searoute([lon, lat], [port["lon"], port["lat"]], units="naut")
        pts = [[c[1], c[0]] for c in r.geometry["coordinates"]]
        if len(pts) >= 2:
            return pts, float(r.properties["length"]), "sea"
    except Exception:  # noqa: BLE001 — нет пути (озеро, река) или ошибка библиотеки
        log.debug("searoute: нет пути до %s", port.get("code"), exc_info=True)
    return [[lat, lon], [port["lat"], port["lon"]]], distance_nm(lat, lon, port["lat"], port["lon"]), "straight"


def _unwrap(pts: list[list[float]]) -> list[list[float]]:
    """Долготы без скачков через 180°: линия на карте идёт коротким путём."""
    out = [list(pts[0])]
    for la, lo in pts[1:]:
        prev = out[-1][1]
        lo += 360 * round((prev - lo) / 360)
        out.append([la, lo])
    return out


@functools.lru_cache(maxsize=2000)
def _route_cached(lat: float, lon: float, code: str) -> dict:
    port = _codes[code] if code in _codes else None
    if not port:
        return {}
    pts, length, how = _sea_path(lat, lon, port)
    pts = _unwrap(pts)
    on_way: list[str] = []
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        steps = max(1, int(distance_nm(la1, lo1, la2, lo2) / ROUTE_STEP_NM))
        for i in range(steps + 1):
            t = i / steps
            x = (lo1 + (lo2 - lo1) * t + 180) % 360 - 180
            for zid in _zones_at(x, la1 + (la2 - la1) * t):
                if zid not in on_way:
                    on_way.append(zid)
    return {"distance_nm": round(length), "path": how, "line": [[round(a, 4), round(b, 4)] for a, b in pts],
            "on_way": on_way}


def route(lat: float, lon: float, port: dict) -> dict:
    """Путь до порта (по морю, если получилось), зоны в порту и по пути (кроме той, где судно сейчас)."""
    code = port["code"]
    if code not in _codes:                       # порт не из справочника (тесты, ручной ввод)
        _codes[code] = {**port}
    r = _route_cached(round(lat, 2), round(lon, 2), code)
    at_port = _zones_at(port["lon"], port["lat"], PORT_TOLERANCE_NM)
    here = set(_zones_at(lon, lat))
    return {"distance_nm": r.get("distance_nm"), "path": r.get("path"), "line": r.get("line"),
            "port_zones": at_port, "route_zones": [z for z in r.get("on_way", []) if z not in at_port and z not in here]}


def for_vessel(mmsi: str, lat: Optional[float], lon: Optional[float], dest: Optional[str]) -> Optional[dict]:
    """Всё для карточки: порт, расстояние, зоны (с названиями)."""
    port = resolve_near(dest or "", lat, lon)
    if not port:
        return None
    out = {"port": port["name"], "code": port["code"], "country": port["country"], "lat": port["lat"],
           "lon": port["lon"], "how": port["how"], "port_zones": _zones_at(port["lon"], port["lat"], PORT_TOLERANCE_NM)}
    if lat is not None and lon is not None:
        out.update(route(lat, lon, port))
    for key in ("port_zones", "route_zones"):
        out[key] = [{"id": z, "name": zones.zone_name(z),
                     "layer": (zones.index().get(z).props.get("layer") if zones.index().get(z) else None)}
                    for z in out.get(key, [])]
    return out


def check_fleet() -> int:
    """Уведомление один раз на рейс: судно флота идёт в порт, который лежит в зоне риска."""
    sent = 0
    for r in db.query("SELECT DISTINCT a.mmsi, a.destination, a.eta, a.lat, a.lon, "
                      "COALESCE(NULLIF(v.name, ''), a.ais_name) AS name "
                      "FROM vessels v JOIN ais_state a ON a.mmsi=v.mmsi WHERE a.destination IS NOT NULL"):
        port = resolve_near(r["destination"], r["lat"], r["lon"])
        if not port:
            continue
        zs = _zones_at(port["lon"], port["lat"], PORT_TOLERANCE_NM)
        if not zs:
            continue
        key = f"{port['code']}|{r['destination']}"
        cur = db.execute("INSERT OR IGNORE INTO dest_alerts(mmsi, dest_key, created_at) VALUES(?,?,?)",
                         (r["mmsi"], key, db.now_iso()))
        if not cur.rowcount:
            continue
        names = ", ".join(zones.zone_name(z) for z in zs)
        db.notify("dest_zone", "warning", f"{r['name'] or 'MMSI ' + r['mmsi']}: идёт в порт в зоне риска",
                  f"Пункт назначения «{r['destination']}» — {port['name']} ({port['country']})"
                  f"{', ETA ' + r['eta'] if r['eta'] else ''}. Порт в зоне: {names}.", r["mmsi"])
        sent += 1
    return sent
