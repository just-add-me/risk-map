"""Новости о судах флота из открытых источников (без языковой модели).

Раз в 6 часов для каждого судна флота ищем статьи в RSS-поиске Google News и Bing News:
по номеру IMO и по названию судна вместе с морскими словами. Упоминание судна в тексте статьи
засчитывается (см. relevant). Одинаковые новости разных изданий схлопываются в один сюжет
(same_story); показывается статья самого авторитетного издания, рядом — число источников.
Тревожные слова (захват, задержание, атака, пожар, санкции…) делают сюжет «тревожным»: он
попадает в происшествия как news_risk и даёт уведомление. Остальные — news, без уведомления.

Найденные статьи хранятся в таблице news, чтобы одна и та же статья не появлялась повторно.
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
    "нападение": r"attack|missile|drone|(?<!counter[- ])strike|pirat|gunfire|shot at|explosi",
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
    imo_q = f'"IMO {imo}" OR "IMO: {imo}"'
    q = [("google", _google(imo_q + when)),
         ("bing", "https://www.bing.com/news/search?q=" + quote_plus(imo_q) + "&format=rss")]
    if name and len(re.sub(r"\W", "", name)) >= 4:
        words = " (tanker OR vessel OR ship OR carrier OR cargo OR yacht OR superyacht)"
        q.append(("google", _google(f'"{name}"' + words + when)))
        q.append(("bing", "https://www.bing.com/news/search?q=" + quote_plus(f'"{name}"' + words) + "&format=rss"))
    return q


def _name_re(name: str) -> re.Pattern:
    """Название целиком, с любыми пробелами и дефисами между словами («SANAR-14» = «Sanar 14»)."""
    return re.compile(r"(?<![\w])" + r"[\s\-]*".join(map(re.escape, re.split(r"[\s\-]+", name))) + r"(?![\w'’])",
                      re.I)


VESSEL_WORDS = (r"superyacht|yacht|vessel|ship|tanker|carrier|freighter|bulker|boat|ferry|trawler|"
                r"m/?v|m/?t|m/?y|s/?y|судн\w*|танкер\w*|яхт\w*")


def mentions(text: str, name: str, imo: str) -> bool:
    """IMO в тексте, или название судна рядом со «судовым» словом (до 4 слов от него) или в кавычках.
    Просто «название + где-то морское слово» мало: у «ENERGY», «DREAM» находились статьи про энергетику."""
    if imo in text:
        return True
    if not name or len(re.sub(r"\W", "", name)) < 4:
        return False
    nm = _name_re(name).pattern
    single = len(re.split(r"[\s\-]+", name.strip())) == 1
    gap = 1 if single else 4          # однословное («Energy», «Koru») — почти вплотную к «судовому» слову
    vw = rf"(?<!\bto )(?:{VESSEL_WORDS})\b"            # «to ship oil» — глагол, а не судно
    near = rf"\b{vw}\W+(?:[\w’'\-]+\W+){{0,{gap}}}?({nm})|({nm})\W+(?:[\w’'\-]+\W+){{0,{gap}}}?{vw}"
    quoted = rf"[\"“”«'‘]({nm})[\"“”»'’]"
    for pat in (quoted, near):
        for m in re.finditer(pat, text, re.I):
            g = next(g for g in range(1, (m.re.groups or 0) + 1) if m.group(g))
            found, start = m.group(g), m.start(g)
            # однословное название — имя собственное: с заглавной («ship energy-saving» — не судно Energy)
            if single and not found[:1].isupper():
                continue
            # …и не хвост чужого названия («Century Dream»)
            prev = re.search(r"([A-Za-zА-Яа-яЁё]+)\W{1,2}$", text[max(0, start - 30):start])
            if single and prev and prev.group(1)[0].isupper() and not re.fullmatch(VESSEL_WORDS, prev.group(1), re.I):
                continue
            return True
    return False


def _lengths_m(text: str) -> list[float]:
    """Длины судов в тексте: «134m», «80-metre», «305-foot», «236 ft»."""
    out = [float(x) for x in re.findall(r"\b(\d{2,3}(?:[.,]\d)?)\s?-?(?:m|metres?|meters?)\b", text, re.I)]
    out += [float(x) * 0.3048 for x in re.findall(r"\b(\d{2,3})\s?-?(?:foot|feet|ft)\b", text, re.I)]
    return out


def length_conflict(text: str, length: Optional[float]) -> bool:
    """В заголовке указана длина, и ни одна не похожа на нашу (±15 %) — это судно-тёзка («50m Koru» ≠ 125 м)."""
    ls = _lengths_m(text)
    return bool(length and ls and not any(abs(x - length) <= 0.15 * length for x in ls))


def relevant(item: dict, name: str, imo: str, length: Optional[float] = None) -> bool:
    """Статья про это судно: IMO или название в заголовке/описании, а иначе — в тексте самой статьи.

    Текст открываем только по прямым ссылкам Bing. Ссылки Google ведут на их окно согласия на cookies,
    а сам поиск Google по точной фразе неточен (на «EAGLE S» находит «Harry S. Truman») — поэтому
    его находки без названия в заголовке или описании не засчитываем.
    """
    blob = f"{item['title']} {item['summary']}"
    if imo not in blob and length_conflict(item["title"], length):
        return False
    if mentions(blob, name, imo):
        return True
    if item.get("via") != "bing":
        return False
    text = article_text(item["url"])
    return bool(text) and mentions(text, name, imo)


def article_text(url: str) -> Optional[str]:
    """Текст статьи по прямой ссылке (до 1,5 МБ). None — открыть не удалось."""
    try:
        with requests.get(url, timeout=15, stream=True, headers={"User-Agent": config.HTTP_USER_AGENT}) as r:
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return None
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > 1_500_000:
                    break
        page = body.decode(r.encoding or "utf-8", errors="replace")
        page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
        return _clean(page)
    except requests.RequestException:
        return None


def risk_topics(item: dict) -> list[str]:
    blob = f"{item['title']} {item['summary']}"
    # с начала слова: иначе «ceasefire» — пожар, а «bleak» — течь
    return [label for label, pat in RISK_WORDS.items() if re.search(rf"\b(?:{pat})", blob, re.I)]


# --- одинаковые новости разных изданий -> один сюжет -----------------------------------
# Данных о просмотрах у RSS нет, поэтому сюжет показываем статьёй самого авторитетного издания.
OUTLET_RANK = [
    (10, "reuters", "bloomberg", "associated press", "ap news", "financial times", "wall street journal"),
    (9, "lloyd's list", "lloyds list", "tradewinds", "bbc", "new york times", "the guardian", "s&p global"),
    (8, "maritime executive", "gcaptain", "splash247", "splash 247", "seatrade", "cnn", "al jazeera", "politico"),
    (7, "safety4sea", "marine insight", "hellenic shipping", "offshore energy", "maritime bulletin", "argus",
     "kpler", "windward", "the economist", "kyiv independent"),
]
STOP = set("the a an of to in on for and or with from by at as is are was were be has have after over into new "
           "its his her their this that ship vessel tanker cargo carrier".split())


def outlet_rank(outlet: str) -> int:
    o = (outlet or "").lower()
    return next((rank for rank, *names in OUTLET_RANK if any(n in o for n in names)), 3)


def _tokens(title: str, name: str) -> set[str]:
    skip = {w.lower() for w in re.split(r"[\s\-]+", name or "")}
    return {w for w in re.findall(r"[\w']{3,}", title.lower()) if w not in STOP and w not in skip}


def same_story(a: str, b: str, name: str) -> bool:
    """Заголовки об одном событии: большая доля общих значимых слов."""
    ta, tb = _tokens(a, name), _tokens(b, name)
    if not ta or not tb:
        return False
    common = len(ta & tb)
    return common / min(len(ta), len(tb)) >= 0.6 and common >= 2 or common / len(ta | tb) >= 0.45


def _key(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()[:300]


def _fleet() -> list[dict]:
    """Суда флотов с IMO: название (из флота или AIS), IMO, MMSI."""
    seen, out = set(), []
    for r in db.query("SELECT v.mmsi, COALESCE(v.imo, a.imo) AS imo, COALESCE(NULLIF(v.name, ''), a.ais_name) AS name, "
                      "a.length FROM vessels v LEFT JOIN ais_state a ON a.mmsi=v.mmsi"):
        imo = str(r["imo"] or "")
        if fleet_import.imo_valid(imo) and imo not in seen:
            seen.add(imo)
            out.append({"imo": imo, "mmsi": r["mmsi"], "name": (r["name"] or "").strip(), "length": r["length"]})
    return out


def check_vessel(v: dict, pause: float = PAUSE_S) -> int:
    """Ищет новости об одном судне; возвращает число новых статей."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    known = {r["title_key"] for r in db.query("SELECT title_key FROM news WHERE imo=?", (v["imo"],))}
    known_urls = {r["url"] for r in db.query("SELECT url FROM news WHERE imo=?", (v["imo"],))}
    found: dict[str, dict] = {}
    for i, (source, url) in enumerate(_queries(v["name"], v["imo"])):
        try:
            items = parse_rss(_get(url), source)
        except Exception as exc:  # noqa: BLE001 — один поисковик не должен ломать остальные
            log.warning("Новости: %s не ответил для IMO %s: %s", source, v["imo"], exc)
            state["last_error"] = f"{source}: {exc}"
            items = []
        for it in items:
            it["query"] = "imo" if i < 2 else "name"
            key = _key(it["title"])
            if key in known or key in found or it["url"] in known_urls:
                continue
            if it["published"] and it["published"] < cutoff:
                continue
            if relevant(it, v["name"], v["imo"], v.get("length")):
                found[key] = it
        time.sleep(pause)
    if not found:
        return 0
    # первая проверка судна — без уведомлений: иначе разом придут статьи за прошлый месяц
    first = not known
    # сюжеты за последние 2 недели: новые статьи присоединяем к ним, если это то же событие
    stories: dict[str, list[dict]] = {}
    for r in db.query("SELECT * FROM news WHERE imo=? AND found_at > ?", (v["imo"], _ago(14))):
        stories.setdefault(r["story"] or r["title_key"], []).append(r)
    touched: set[str] = set()
    for key, it in sorted(found.items(), key=lambda kv: -outlet_rank(kv[1]["outlet"])):
        story = next((sk for sk, rows in stories.items()
                      if any(same_story(it["title"], r["title"], v["name"]) for r in rows)), key)
        row = {"imo": v["imo"], "title_key": key, "url": it["url"], "title": it["title"], "outlet": it["outlet"],
               "published": it["published"].replace(microsecond=0).isoformat() if it["published"] else None,
               "found_at": db.now_iso(), "topics": ", ".join(risk_topics(it)), "story": story, "incident_id": None}
        db.execute("INSERT OR IGNORE INTO news(imo, title_key, url, title, outlet, published, found_at, topics, story) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", tuple(row[k] for k in ("imo", "title_key", "url", "title", "outlet",
                                                                         "published", "found_at", "topics", "story")))
        stories.setdefault(story, []).append(row)
        touched.add(story)
    for story in touched:
        _sync_story(v, stories[story], notify=not first)
    return len(found)


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _sync_story(v: dict, rows: list[dict], notify: bool) -> None:
    """Одна запись в происшествиях на сюжет: статья самого авторитетного издания, счётчик — число
    изданий. Если к спокойному сюжету добавилась тревожная статья — сюжет становится тревожным."""
    best = max(rows, key=lambda r: (outlet_rank(r["outlet"]), -(datetime.fromisoformat(r["published"]).timestamp()
                                                                 if r["published"] else 0)))
    risky = any(r["topics"] for r in rows)
    kind = "news_risk" if risky else "news"
    iid = next((r["incident_id"] for r in rows if r.get("incident_id")), None)
    old = db.query_one("SELECT kind FROM incidents WHERE id=?", (iid,)) if iid else None
    if old:
        db.execute("UPDATE incidents SET kind=?, text=?, source=?, extra=?, count=?, last_seen=? WHERE id=?",
                   (kind, best["title"], best["outlet"] or "", best["url"], len(rows), db.now_iso(), iid))
        if notify and kind == "news_risk" and old["kind"] != "news_risk":
            from . import rules
            rules.on_vessel_event(v["mmsi"], "news_risk", "warning", "новости: тревожное", best["title"])
    else:
        lat, lon = _position(v["mmsi"])
        iid = incidents.record(kind, v["mmsi"], name=v["name"] or None, lat=lat, lon=lon, text=best["title"],
                               source=best["outlet"] or "", extra=best["url"], tracked=notify)
        if iid and len(rows) > 1:
            db.execute("UPDATE incidents SET count=? WHERE id=?", (len(rows), iid))
    if iid:
        db.execute("UPDATE news SET incident_id=? WHERE imo=? AND title_key IN (%s)" % ",".join("?" * len(rows)),
                   (iid, v["imo"], *[r["title_key"] for r in rows]))


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
