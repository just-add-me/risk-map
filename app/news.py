"""Новости о судах флота из открытых источников (без языковой модели).

Раз в 6 часов для каждого судна флота ищем статьи в RSS-поиске Google News и Bing News:
по номеру IMO и по названию судна вместе с морскими словами. Статья засчитывается, только если
в заголовке или описании есть IMO или название судна. Тревожные слова (захват, задержание,
атака, пожар, санкции…) делают находку «тревожной»: она попадает в происшествия как news_risk
и даёт уведомление. Остальные — news, видны в «Событиях» и в карточке судна без уведомления.

Найденные ссылки хранятся в таблице news, чтобы одна и та же статья не появлялась повторно.
"""
from __future__ import annotations

import email.utils
import html
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from . import config, db, fleet_import
from .ais import incidents

log = logging.getLogger(__name__)

INTERVAL_S = 6 * 3600
MAX_AGE_DAYS = 30            # старше — не показываем
PAUSE_S = 2.0                # пауза между запросами к поисковикам
KEEP_DAYS = 180              # сколько помнить уже показанные ссылки

MARITIME = r"ship|vessel|tanker|carrier|cargo|bulk|container|freighter|imo|port|crew|seafarer|maritime|sailor|судн|танкер"
RISK_WORDS = {
    "захват / задержание": r"seiz|detain|detention|arrest|impound|hijack|boarded|captur",
    "нападение": r"attack|missile|drone|strike|pirat|gunfire|shot at|explosi",
    "авария": r"fire|blaze|collision|collid|aground|grounding|sank|sink|capsiz|disabled|engine failure|leak|spill",
    "санкции": r"sanction|blacklist|shadow fleet|dark fleet|ofac|embargo|spoofing",
    "экипаж": r"abandon|stranded|unpaid|crew (?:missing|killed|injured|dead)|kidnap",
    "расследование": r"charged|charges against|charges (?:[\w\-]+ ){0,5}(?:captain|crew|master|officers|owner|operator)|indict|investigat|probe|fined|penalt",
}

state: dict = {"running": False, "last_run": None, "last_error": None, "found": 0}
_lock = threading.Lock()


def _get(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": config.HTTP_USER_AGENT})
    r.raise_for_status()
    return r.text


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _date(v: str) -> Optional[datetime]:
    try:
        d = email.utils.parsedate_to_datetime(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_rss(text: str, source: str) -> list[dict]:
    """Элементы RSS → {title, url, published, outlet, summary}."""
    out = []
    root = ET.fromstring(text)
    for it in root.iter("item"):
        title = _clean(it.findtext("title") or "")
        url = (it.findtext("link") or "").strip()
        outlet = _clean(it.findtext("source") or "")
        for child in it:                     # у Bing источник — <News:Source> с пространством имён
            if child.tag.endswith("}Source") and not outlet:
                outlet = _clean(child.text or "")
        if source == "bing" and "apiclick" in url:          # переходник Bing → прямая ссылка
            url = (parse_qs(urlparse(url).query).get("url") or [url])[0]
        if source == "google" and outlet and title.endswith(" - " + outlet):
            title = title[: -len(outlet) - 3]
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "published": _date(it.findtext("pubDate") or ""),
                        "outlet": outlet, "summary": _clean(it.findtext("description") or "")[:400],
                        "via": source})
    return out


def _google(q: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"


def _queries(name: str, imo: str) -> list[tuple[str, str]]:
    when = f" when:{MAX_AGE_DAYS}d"
    q = [("google", _google(f'"IMO {imo}" OR "IMO: {imo}"' + when))]
    if name and len(name) >= 5:
        words = " (tanker OR vessel OR ship OR carrier OR cargo)"
        q.append(("google", _google(f'"{name}"' + words + when)))
        q.append(("bing", "https://www.bing.com/news/search?q=" + quote_plus(f'"{name}"' + words) + "&format=rss"))
    return q


def relevant(item: dict, name: str, imo: str) -> bool:
    """Статья про это судно: в тексте IMO, либо название целиком и морские слова."""
    blob = f"{item['title']} {item['summary']}"
    if imo in blob:
        return True
    if not name or len(name) < 5:
        return False
    pat = r"(?<![\w])" + r"[\s\-]*".join(map(re.escape, re.split(r"[\s\-]+", name))) + r"(?![\w])"
    return bool(re.search(pat, blob, re.I)) and bool(re.search(rf"\b(?:{MARITIME})", blob, re.I))


def risk_topics(item: dict) -> list[str]:
    blob = f"{item['title']} {item['summary']}"
    # с начала слова: иначе «ceasefire» — пожар, а «bleak» — течь
    return [label for label, pat in RISK_WORDS.items() if re.search(rf"\b(?:{pat})", blob, re.I)]


def _fleet() -> list[dict]:
    """Суда флотов с IMO: название (из флота или AIS), IMO, MMSI."""
    seen, out = set(), []
    for r in db.query("SELECT v.mmsi, COALESCE(v.imo, a.imo) AS imo, COALESCE(NULLIF(v.name, ''), a.ais_name) AS name "
                      "FROM vessels v LEFT JOIN ais_state a ON a.mmsi=v.mmsi"):
        imo = str(r["imo"] or "")
        if fleet_import.imo_valid(imo) and imo not in seen:
            seen.add(imo)
            out.append({"imo": imo, "mmsi": r["mmsi"], "name": (r["name"] or "").strip()})
    return out


def check_vessel(v: dict, pause: float = PAUSE_S) -> int:
    """Ищет новости об одном судне; возвращает число новых статей."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    found: dict[str, dict] = {}
    for source, url in _queries(v["name"], v["imo"]):
        try:
            items = parse_rss(_get(url), source)
        except Exception as exc:  # noqa: BLE001 — один поисковик не должен ломать остальные
            log.warning("Новости: %s не ответил для IMO %s: %s", source, v["imo"], exc)
            state["last_error"] = f"{source}: {exc}"
            items = []
        for it in items:
            if it["published"] and it["published"] < cutoff:
                continue
            if relevant(it, v["name"], v["imo"]):
                key = re.sub(r"\W+", " ", it["title"].lower()).strip()
                found.setdefault(key, it)
        time.sleep(pause)
    # первая проверка судна — без уведомлений: иначе разом придут статьи за прошлый месяц
    first = not db.query_one("SELECT 1 FROM news WHERE imo=? LIMIT 1", (v["imo"],))
    new = 0
    for it in found.values():
        cur = db.execute("INSERT OR IGNORE INTO news(imo, title_key, url, title, outlet, published, found_at, topics) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (v["imo"], re.sub(r"\W+", " ", it["title"].lower()).strip()[:300], it["url"], it["title"],
                          it["outlet"], it["published"].replace(microsecond=0).isoformat() if it["published"] else None,
                          db.now_iso(), ", ".join(risk_topics(it))))
        if not cur.rowcount:
            continue
        new += 1
        topics = risk_topics(it)
        lat, lon = _position(v["mmsi"])
        incidents.record("news_risk" if topics else "news", v["mmsi"], name=v["name"] or None, lat=lat, lon=lon,
                         text=it["title"], source=it["outlet"] or it["via"], extra=it["url"], tracked=not first)
    return new


def _position(mmsi: str) -> tuple[Optional[float], Optional[float]]:
    row = db.query_one("SELECT lat, lon FROM ais_state WHERE mmsi=?", (mmsi,))
    return (row["lat"], row["lon"]) if row else (None, None)


def run() -> str:
    if not _lock.acquire(blocking=False):
        return "уже идёт"
    state["running"] = True
    state["last_error"] = None
    try:
        fleet = _fleet()
        total = sum(check_vessel(v) for v in fleet)
        db.execute("DELETE FROM news WHERE found_at < ?",
                   ((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).replace(microsecond=0).isoformat(),))
        state.update(last_run=db.now_iso(), found=total)
        return f"судов {len(fleet)}, новых статей {total}"
    finally:
        state["running"] = False
        _lock.release()
