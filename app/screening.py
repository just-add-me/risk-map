"""Проверка судов по санкционным спискам и спискам задержаний в портах.

Источники (официальные, бесплатные, по IMO):
  санкции  — OFAC SDN (США), UK Sanctions List (FCDO), список судов ЕС по ст. 3s Регламента 833/2014
             (выгрузка Датского морского ведомства), сводный список Совбеза ООН;
  задержания — Tokyo MoU (APCIS, открытый список задержаний за месяц).

Раз в сутки списки скачиваются заново. Что даёт:
  * карточка любого судна с IMO показывает, в каких списках оно есть и было ли задержано;
  * судно из флота нашлось в списке → происшествие «Санкции» / «Задержание» и уведомление;
  * новые суда в санкционных списках и свежие задержания попадают во вкладку «Происшествия».
"""
from __future__ import annotations

import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Optional

import requests

from . import config, db, fleet_import

log = logging.getLogger(__name__)

SOURCES = {
    "ofac": {"label": "OFAC (США)", "url": "https://www.treasury.gov/ofac/downloads/sdn.csv",
             "page": "https://sanctionssearch.ofac.treas.gov/"},
    "uk": {"label": "Великобритания", "url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv",
           "page": "https://www.gov.uk/government/publications/the-uk-sanctions-list"},
    "eu": {"label": "ЕС (ст. 3s Рег. 833/2014)",
           "page": "https://www.dma.dk/growth-and-framework-conditions/maritime-sanctions/sanctions-against-russia-and-belarus/eu-vessel-designations"},
    "un": {"label": "ООН", "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
           "page": "https://main.un.org/securitycouncil/content/un-sc-consolidated-list"},
}
TOKYO_URL = "https://apcis.tmou.org/isss/public_apcis.php"
TOKYO_PAGE = "https://www.tokyo-mou.org/inspections-detentions/detention-list/"

state: dict = {"running": False, "last_error": None}


def _get(url: str, **kw) -> requests.Response:
    r = requests.request(kw.pop("method", "GET"), url, timeout=120,
                         headers={"User-Agent": config.HTTP_USER_AGENT}, **kw)
    r.raise_for_status()
    return r


def _imos(text: str) -> list[str]:
    """IMO из свободного текста; номера без верной контрольной цифры отбрасываем."""
    return sorted({m for m in re.findall(r"IMO[^0-9]{0,20}(\d{7})", text) if fleet_import.imo_valid(m)})


# --------------------------------------------------------------------------- разбор источников
def parse_ofac(text: str) -> list[dict]:
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 12 or r[2].strip().lower() != "vessel":
            continue
        clean = lambda v: "" if v.strip() in ("-0-", "") else v.strip()  # noqa: E731
        for imo in _imos(r[11]):
            out.append({"imo": imo, "name": clean(r[1]), "program": clean(r[3]), "listed_on": None,
                        "details": "; ".join(x for x in (clean(r[6]), f"флаг {clean(r[9])}" if clean(r[9]) else "",
                                                         f"владелец {clean(r[10])}" if clean(r[10]) else "") if x),
                        "url": f"https://sanctionssearch.ofac.treas.gov/Details.aspx?id={r[0].strip()}"})
    return out


def parse_uk(text: str) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    hdr_i = next(i for i, r in enumerate(rows[:5]) if "IMO number" in r)
    h = {name: i for i, name in enumerate(rows[hdr_i])}
    by_imo: dict[str, dict] = {}
    for r in rows[hdr_i + 1:]:
        if len(r) < len(h) or r[h["Designation Type"]].strip().lower() != "ship":
            continue
        imo = re.sub(r"\D", "", r[h["IMO number"]])
        if not fleet_import.imo_valid(imo):
            continue
        entry = by_imo.setdefault(imo, {"imo": imo, "name": "", "program": r[h["Regime Name"]].strip(),
                                        "listed_on": _date(r[h["Date Designated"]]), "details": "",
                                        "url": SOURCES["uk"]["page"]})
        if r[h["Name type"]].strip() == "Primary Name" or not entry["name"]:
            entry["name"] = r[h["Name 6"]].strip()
        if "Type of ship" in h and r[h["Type of ship"]].strip():
            entry["details"] = r[h["Type of ship"]].strip()
    return list(by_imo.values())


def parse_eu(text: str) -> list[dict]:
    out = []
    for r in csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"):
        if len(r) < 4:
            continue
        imo = re.sub(r"\D", "", r[1])
        if fleet_import.imo_valid(imo):
            out.append({"imo": imo, "name": r[0].strip(), "program": r[3].strip(), "listed_on": _date(r[2]),
                        "details": "", "url": SOURCES["eu"]["page"]})
    return out


def parse_un(text: str) -> list[dict]:
    out = []
    root = ET.fromstring(text)
    for ent in root.iter("ENTITY"):
        blob = ET.tostring(ent, encoding="unicode")
        for imo in _imos(blob):
            out.append({"imo": imo, "name": (ent.findtext("FIRST_NAME") or "").strip(),
                        "program": (ent.findtext("UN_LIST_TYPE") or "").strip(),
                        "listed_on": (ent.findtext("LISTED_ON") or "")[:10] or None,
                        "details": (ent.findtext("REFERENCE_NUMBER") or "").strip(), "url": SOURCES["un"]["page"]})
    return out


def _date(v: str) -> Optional[str]:
    v = (v or "").strip()
    for fmt in ("%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _eu_csv_url() -> str:
    html = _get(SOURCES["eu"]["page"]).text
    m = re.search(r'href="([^"]*ImportversionListOfEUDesignatedVessels[^"]*\.csv)"', html, re.I)
    if not m:
        raise RuntimeError("На странице Датского морского ведомства не найдена выгрузка списка ЕС")
    return requests.compat.urljoin(SOURCES["eu"]["page"], m.group(1))


def parse_tokyo(html: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&nbsp;", " ").strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) < 14 or not fleet_import.imo_valid(cells[1]):
            continue
        out.append({"imo": cells[1], "name": cells[2], "flag": cells[3], "built": cells[4][:4], "gt": cells[5],
                    "ship_type": cells[6], "company": cells[9], "port": cells[10],
                    "detained_on": _date(cells[11]), "released_on": _date(cells[12]), "reasons": cells[13][:400]})
    return out


# --------------------------------------------------------------------------- обновление
def refresh_sanctions() -> dict:
    """Скачивает все санкционные списки, обновляет таблицу и возвращает новые записи по источникам."""
    added: dict[str, list[dict]] = {}
    errors = []
    for src in SOURCES:
        try:
            if src == "eu":
                text = _get(_eu_csv_url()).content.decode("utf-8-sig", errors="replace")
            else:
                raw = _get(SOURCES[src]["url"]).content
                text = raw.decode("latin-1" if src == "ofac" else "utf-8", errors="replace")
            items = {"ofac": parse_ofac, "uk": parse_uk, "eu": parse_eu, "un": parse_un}[src](text)
        except Exception as exc:  # noqa: BLE001 — один недоступный источник не отменяет остальные
            log.warning("Санкции %s: %s", src, exc)
            errors.append(f"{SOURCES[src]['label']}: {exc}")
            continue
        if not items:
            errors.append(f"{SOURCES[src]['label']}: список пуст — формат изменился?")
            continue
        first_load = not db.query_one("SELECT 1 AS x FROM sanctions WHERE source=?", (src,))
        known = {r["imo"] for r in db.query("SELECT imo FROM sanctions WHERE source=?", (src,))}
        now = db.now_iso()
        seen = set()
        for it in items:
            if it["imo"] in seen:
                continue
            seen.add(it["imo"])
            db.execute("INSERT INTO sanctions(imo, source, name, program, listed_on, details, url, first_seen, last_seen) "
                       "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(imo, source) DO UPDATE SET name=excluded.name, "
                       "program=excluded.program, listed_on=COALESCE(excluded.listed_on, listed_on), "
                       "details=excluded.details, url=excluded.url, last_seen=excluded.last_seen",
                       (it["imo"], src, it["name"], it["program"], it["listed_on"], it["details"], it["url"], now, now))
        # кого исключили из списка — убираем (судно больше не под санкциями этого источника)
        for imo in known - seen:
            db.execute("DELETE FROM sanctions WHERE imo=? AND source=?", (imo, src))
        if not first_load:          # при первой загрузке весь список «новый» — это не событие
            added[src] = [it for it in items if it["imo"] in seen - known]
        log.info("Санкции %s: %d судов, новых %d, исключено %d", src, len(seen), len(seen - known), len(known - seen))
    db.kv_set("sanctions_status", {"updated": db.now_iso(), "errors": errors,
                                   "counts": {s: db.query_one("SELECT COUNT(*) AS n FROM sanctions WHERE source=?",
                                                              (s,))["n"] for s in SOURCES}})
    return added


def refresh_detentions() -> list[dict]:
    """Списки задержаний Tokyo MoU за текущий и прошлый месяц. Вернёт задержания, которых ещё не было."""
    today = date.today()
    months = [(today.year, today.month), (today.year - (today.month == 1), (today.month - 2) % 12 + 1)]
    new = []
    for year, month in months:
        html = _get(TOKYO_URL, method="POST", data={
            "Mode": "DetList", "MOU": "TMOU", "Auth": "", "Src": "online", "Type": "Auth",
            "Month": f"{month:02d}", "Year": str(year), "SaveFile": ""}).text
        for d in parse_tokyo(html):
            if not d["detained_on"]:
                continue
            cur = db.execute("INSERT OR IGNORE INTO detentions(source, imo, detained_on, name, flag, built, gt, ship_type, "
                             "company, port, released_on, reasons, first_seen) VALUES('tokyo',?,?,?,?,?,?,?,?,?,?,?,?)",
                             (d["imo"], d["detained_on"], d["name"], d["flag"], d["built"], d["gt"], d["ship_type"],
                              d["company"], d["port"], d["released_on"], d["reasons"], db.now_iso()))
            if cur.rowcount:
                new.append(d)
            else:
                db.execute("UPDATE detentions SET released_on=COALESCE(?, released_on) "
                           "WHERE source='tokyo' AND imo=? AND detained_on=?", (d["released_on"], d["imo"], d["detained_on"]))
    db.kv_set("detentions_status", {"updated": db.now_iso(),
                                    "count": db.query_one("SELECT COUNT(*) AS n FROM detentions")["n"]})
    return new


# --------------------------------------------------------------------------- проверка и события
def lookup(imo: Optional[str]) -> dict:
    if not imo or not fleet_import.imo_valid(str(imo)):
        return {"sanctions": [], "detentions": []}
    sanc = db.query("SELECT source, name, program, listed_on, url FROM sanctions WHERE imo=? ORDER BY source", (imo,))
    for s in sanc:
        s["label"] = SOURCES[s["source"]]["label"]
    det = db.query("SELECT port, detained_on, released_on, reasons, flag, built, gt, company FROM detentions "
                   "WHERE imo=? ORDER BY detained_on DESC LIMIT 5", (imo,))
    return {"sanctions": sanc, "detentions": det}


def _fleet_vessels() -> dict[str, list[str]]:
    """IMO → MMSI судов во флотах (IMO из флота или из паспортных данных AIS)."""
    out: dict[str, list[str]] = {}
    for r in db.query("SELECT v.mmsi, COALESCE(v.imo, a.imo) AS imo FROM vessels v "
                      "LEFT JOIN ais_state a ON a.mmsi=v.mmsi"):
        if r["imo"] and fleet_import.imo_valid(str(r["imo"])):
            out.setdefault(str(r["imo"]), []).append(r["mmsi"])
    return out


def _position(imo: str, mmsi: Optional[str]) -> tuple[Optional[float], Optional[float], Optional[str]]:
    from .ais import traffic
    m = mmsi if mmsi and not mmsi.startswith("IMO:") else traffic.find_imo(imo)
    s = traffic.get(m) if m else None
    if (not s or s.get("lat") is None) and m:       # судно флота — последняя позиция есть и в базе
        s = db.query_one("SELECT lat, lon FROM ais_state WHERE mmsi=? AND lat IS NOT NULL", (m,))
    return (s or {}).get("lat"), (s or {}).get("lon"), m


def screen_fleets() -> int:
    """Суда флотов, впервые найденные в санкционных списках или задержаниях: происшествие + уведомление."""
    from .ais import incidents
    from . import alerts
    hits = 0
    for imo, mmsis in _fleet_vessels().items():
        found = lookup(imo)
        for mmsi in mmsis:
            # все новые для этого судна списки — одним происшествием и одним уведомлением
            fresh = [s for s in found["sanctions"] if not db.query_one(
                "SELECT 1 AS x FROM screening_hits WHERE mmsi=? AND imo=? AND ref=?", (mmsi, imo, f"sanction:{s['source']}"))]
            if not fresh:
                continue
            for s in fresh:
                db.execute("INSERT INTO screening_hits(mmsi, imo, ref, first_seen) VALUES(?,?,?,?)",
                           (mmsi, imo, f"sanction:{s['source']}", db.now_iso()))
            lat, lon, _ = _position(imo, mmsi)
            lines = [f"{s['label']}: {s['program']}" + (f", с {s['listed_on']}" if s["listed_on"] else "") for s in fresh]
            incidents.record("sanction", mmsi, name=fresh[0]["name"], lat=lat, lon=lon, text="; ".join(lines)[:300],
                             source=", ".join(s["label"] for s in fresh), extra=f"IMO {imo}")
            db.notify("incident", "critical", f"{alerts.vessel_label(mmsi)}: в санкционных списках ({len(fresh)})",
                      "\n".join(lines), mmsi)
            hits += 1
        for d in found["detentions"]:
            key = f"detention:{d['detained_on']}"
            for mmsi in mmsis:
                if db.query_one("SELECT 1 AS x FROM screening_hits WHERE mmsi=? AND imo=? AND ref=?", (mmsi, imo, key)):
                    continue
                db.execute("INSERT INTO screening_hits(mmsi, imo, ref, first_seen) VALUES(?,?,?,?)",
                           (mmsi, imo, key, db.now_iso()))
                text = _detention_text(d)
                lat, lon = _port_position(d["port"])
                incidents.record("detention", mmsi, lat=lat, lon=lon, text=text, source="Tokyo MoU", extra=f"IMO {imo}")
                db.notify("incident", "warning", f"{alerts.vessel_label(mmsi)}: задержание в порту", text, mmsi)
                hits += 1
    return hits


def _detention_text(d: dict) -> str:
    rel = f", освобождено {d['released_on']}" if d.get("released_on") else ", ещё задержано"
    return f"{d['port']}: задержано {d['detained_on']}{rel}. {d['reasons']}"[:300]


def _port_position(port: str) -> tuple[Optional[float], Optional[float]]:
    """«Vancouver, Canada» → координаты порта из справочника, если название совпало."""
    from .layers import ports
    name = (port or "").split(",")[0].strip().lower()
    p = next((p for p in ports.load() if p["name"].lower() == name), None)
    return (p["lat"], p["lon"]) if p else (None, None)


def publish_events(added: dict[str, list[dict]], detentions: list[dict]) -> None:
    """Новые суда в санкционных списках и свежие задержания — во вкладку «Происшествия» (без уведомлений)."""
    from .ais import incidents
    for src, items in added.items():
        for it in items[:300]:
            lat, lon, mmsi = _position(it["imo"], None)
            incidents.record("sanction_new", mmsi or f"IMO:{it['imo']}", name=it["name"], lat=lat, lon=lon,
                             text=f"{SOURCES[src]['label']}: {it['program']}", source=SOURCES[src]["label"],
                             extra=f"IMO {it['imo']}")
    for d in detentions:
        lat, lon = _port_position(d["port"])
        _, _, mmsi = _position(d["imo"], None)
        incidents.record("detention", mmsi or f"IMO:{d['imo']}", name=d["name"], lat=lat, lon=lon,
                         text=_detention_text(d), source="Tokyo MoU", extra=f"IMO {d['imo']} · флаг {d['flag']}")


def run_daily() -> str:
    """Полное обновление: списки → события → проверка флотов. Вызывается раз в сутки и кнопкой."""
    state.update(running=True, last_error=None)
    try:
        added = refresh_sanctions()
        try:
            dets = refresh_detentions()
        except Exception as exc:  # noqa: BLE001
            log.warning("Задержания Tokyo MoU: %s", exc)
            dets = []
            state["last_error"] = f"Tokyo MoU: {exc}"
        if not db.kv_get("screening_done"):      # первая загрузка: из архива за 2 месяца — только последние 14 дней
            cutoff = date.fromordinal(date.today().toordinal() - 14).isoformat()
            dets = [d for d in dets if d["detained_on"] >= cutoff]
        publish_events(added, dets)
        hits = screen_fleets()
        db.kv_set("screening_done", db.now_iso())
        return f"Списки обновлены; совпадений по флотам: {hits}"
    finally:
        state["running"] = False


def status() -> dict:
    return {"sanctions": db.kv_get("sanctions_status"), "detentions": db.kv_get("detentions_status"),
            "last_run": db.kv_get("screening_done"), "running": state["running"], "last_error": state["last_error"],
            "sources": {k: {"label": v["label"], "page": v["page"]} for k, v in SOURCES.items()},
            "tokyo_page": TOKYO_PAGE}


def due() -> bool:
    last = db.kv_get("screening_done")
    if not last:
        return True
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > 20 * 3600

