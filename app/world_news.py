"""Важные мировые морские новости в Ленте («в мире») — отдельная функция [мировые-новости].

Как устроено:
1. Сайт раз в 30 минут читает ленты трёх морских изданий (collect) и складывает новые статьи
   в таблицу world_news со статусом «ждёт оценки».
2. Оценивает ИИ (Claude) вне сайта: скрипт deploy/news_judge.sh на сервере раз в час (cron) берёт
   очередь (python -m app.world_news pending), спрашивает Claude «что из этого важно для страховщика
   военных рисков» и отдаёт ответ обратно (python -m app.world_news apply). Ключ API сайту не нужен:
   Claude Code на сервере работает по подписке пользователя.
3. Важные статьи становятся записями в incidents (world_critical / world_news): Лента «в мире»,
   метка на карте, если ИИ назвал место; «прочитано», «важное», корзина — как у остальных событий.

Если оценка не прошла (кончился лимит подписки, нет входа) — статьи ждут и оцениваются позже;
старше MAX_WAIT_H часов — уже не новости, их просто пропускаем.

Удалить функцию целиком: python3 tools/remove_feature.py мировые-новости
"""
from __future__ import annotations

import email.utils
import json
import logging
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from . import config, db
from .ais import incidents
from .news import _clean, parse_rss

log = logging.getLogger(__name__)

FEEDS = {
    "gCaptain": "https://gcaptain.com/feed/",
    "The Maritime Executive": "https://maritime-executive.com/articles.rss",
    "Splash247": "https://splash247.com/feed/",
}
INTERVAL_S = 30 * 60
FIRST_RUN_H = 24          # при первом запуске берём статьи только за последние сутки
MAX_WAIT_H = 72           # сколько статья может ждать оценки
BATCH = 60                # статей за один вызов ИИ
KEEP_DAYS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS world_news(
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT,
    outlet TEXT,
    published TEXT,
    found_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | important | skip | expired
    judged_at TEXT,
    importance TEXT,                          -- critical | high
    summary_ru TEXT,
    place TEXT,
    incident_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_world_news_status ON world_news(status, found_at);
"""

PROMPT = """Ты — дежурный аналитик страховщика военных рисков на море (зоны JWC Listed Areas, пиратство, санкции).
Ниже свежие морские новости. Отбери ТОЛЬКО важные для такого страховщика:
атаки на суда, захваты, задержания, абордажи, взрывы, ракеты и дроны, минная опасность, перекрытие проливов
и портов, новые санкции против судов и судовладельцев, крупные аварии в зонах риска.
НЕ важно: бизнес, верфи и заказы судов, назначения, конференции, рынки фрахта и цены, экология,
подъём старых боеприпасов, учения без происшествий, общая политика без последствий для судов.
Ответь только JSON-массивом без пояснений, по одному объекту на важную новость:
[{"n": номер, "importance": "critical" или "high", "place": "место по-русски или null",
"lat": широта места или null, "lon": долгота места или null, "vessel": "название судна латиницей или null",
"summary_ru": "1–2 предложения по-русски: что случилось и где"}]
Координаты — только если место конкретное (пролив, порт, район моря), иначе null. Если важного нет — [].
"""

state: dict = {"last_collect": None, "last_error": None}
_lock = threading.Lock()
_ready = False


def ensure() -> None:
    global _ready
    if not _ready:
        db.connect().executescript(SCHEMA)
        _ready = True


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def parse_feed(text: str, outlet: str) -> list[dict]:
    """RSS (gCaptain, Splash247) и Atom (Maritime Executive) → {title, url, published, summary, outlet}."""
    if "<rss" in text[:500]:
        items = parse_rss(text, "feed")
        for it in items:
            it["outlet"] = outlet
        return items
    import xml.etree.ElementTree as ET
    out = []
    for e in ET.fromstring(text).iter():
        if not e.tag.endswith("}entry"):
            continue
        get = {c.tag.split("}")[-1]: c for c in e}
        link = get.get("link")
        url = (link.get("href") if link is not None else "") or ""
        when = (get.get("published") if get.get("published") is not None else get.get("updated"))
        try:
            pub = datetime.fromisoformat((when.text or "").replace("Z", "+00:00")) if when is not None else None
        except ValueError:
            pub = None
        title = _clean(get["title"].text if get.get("title") is not None else "")
        summary = _clean(get["summary"].text if get.get("summary") is not None else "")[:400]
        if title and url.startswith("http"):
            out.append({"title": title, "url": url, "published": pub, "summary": summary, "outlet": outlet})
    return out


def collect() -> int:
    """Новые статьи из лент → очередь на оценку. Вернёт число добавленных."""
    ensure()
    first = db.query_one("SELECT 1 FROM world_news LIMIT 1") is None
    since = datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_H if first else MAX_WAIT_H)
    added = 0
    for outlet, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": config.HTTP_USER_AGENT})
            r.raise_for_status()
            items = parse_feed(r.text, outlet)
        except Exception as exc:  # noqa: BLE001 — одно издание не должно ломать остальные
            log.warning("Мировые новости: %s не ответил: %s", outlet, exc)
            state["last_error"] = f"{outlet}: {exc}"
            continue
        for it in items:
            if it["published"] and it["published"] < since:
                continue
            cur = db.execute("INSERT OR IGNORE INTO world_news(url, title, summary, outlet, published, found_at) "
                             "VALUES(?,?,?,?,?,?)",
                             (it["url"], it["title"], it["summary"], outlet,
                              it["published"].replace(microsecond=0).isoformat() if it["published"] else None,
                              db.now_iso()))
            added += cur.rowcount or 0
    # не дождались оценки — уже не новости
    db.execute("UPDATE world_news SET status='expired' WHERE status='pending' AND found_at < ?", (_ago(MAX_WAIT_H),))
    db.execute("DELETE FROM world_news WHERE found_at < ?", (_ago(KEEP_DAYS * 24),))
    state["last_collect"] = db.now_iso()
    return added


def pending() -> dict:
    """Очередь для ИИ: номера статей и готовый текст запроса."""
    ensure()
    rows = db.query("SELECT id, title, summary, outlet FROM world_news WHERE status='pending' "
                    "ORDER BY found_at LIMIT ?", (BATCH,))
    lines = [f"[{r['id']}] {r['outlet']} | {r['title']} — {(r['summary'] or '')[:220]}" for r in rows]
    return {"ids": [r["id"] for r in rows], "prompt": PROMPT + "\n" + "\n".join(lines) if rows else ""}


def parse_answer(text: str) -> list[dict]:
    """Ответ ИИ → список важных. Модель иногда оборачивает JSON в ```json … ``` или пишет фразу до него."""
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        raise ValueError("в ответе нет JSON-массива")
    data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise ValueError("ответ — не список")
    return [x for x in data if isinstance(x, dict) and isinstance(x.get("n"), int)]


def _num(v, lo: float, hi: float) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def _vessel_mmsi(name: str) -> str:
    """MMSI судна из справочника, только если такое название там ровно одно (тёзок не угадываем)."""
    from .ais import registry
    try:
        exact = [s for s in registry.search(name, limit=5) if (s.get("name") or "").upper() == name.upper()]
    except Exception:  # noqa: BLE001
        return ""
    return exact[0]["mmsi"] if len(exact) == 1 else ""


def apply(ids: list[int], answer: str) -> dict:
    """Результат оценки: важные → в Ленту, остальные из этой пачки — «не важно»."""
    ensure()
    picked = {x["n"]: x for x in parse_answer(answer)}
    rows = {r["id"]: r for r in db.query(
        "SELECT * FROM world_news WHERE status='pending' AND id IN (%s)" % ",".join("?" * len(ids)), tuple(ids))} if ids else {}
    important = 0
    with _lock:
        for nid, row in rows.items():
            x = picked.get(nid)
            if not x:
                db.execute("UPDATE world_news SET status='skip', judged_at=? WHERE id=?", (db.now_iso(), nid))
                continue
            level = "critical" if x.get("importance") == "critical" else "high"
            summary = _clean(str(x.get("summary_ru") or "")) or row["title"]
            place = _clean(str(x.get("place") or "")) or None
            lat, lon = _num(x.get("lat"), -90, 90), _num(x.get("lon"), -180, 180)
            if lat is None or lon is None:
                lat = lon = None
            vessel = _clean(str(x.get("vessel") or "")) if x.get("vessel") not in (None, "null") else ""
            text = f"{place}: {summary}" if place and place.lower() not in summary.lower() else summary
            iid = incidents.record("world_critical" if level == "critical" else "world_news",
                                   _vessel_mmsi(vessel) if vessel else "", name=vessel or None, lat=lat, lon=lon,
                                   text=text, source=row["outlet"] or "", extra=row["url"])
            db.execute("UPDATE world_news SET status='important', judged_at=?, importance=?, summary_ru=?, place=?, "
                       "incident_id=? WHERE id=?", (db.now_iso(), level, summary, place, iid, nid))
            important += 1
    db.kv_set("world_news_judged", db.now_iso())
    return {"judged": len(rows), "important": important}


def status() -> dict:
    ensure()
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM world_news GROUP BY status")}
    return {"counts": counts, "last_collect": state["last_collect"], "last_judged": db.kv_get("world_news_judged"),
            "last_error": state["last_error"]}


def main(argv: list[str]) -> None:
    """Команды для deploy/news_judge.sh (запускаются внутри контейнера):
    pending              — очередь на оценку (JSON: ids, prompt)
    apply                — на вход JSON {"ids": [...], "answer": "ответ ИИ"}
    collect | status     — собрать ленты сейчас / что в очереди"""
    db.connect()
    cmd = argv[0] if argv else ""
    if cmd == "pending":
        print(json.dumps(pending(), ensure_ascii=False))
    elif cmd == "apply":
        data = json.load(sys.stdin)
        print(json.dumps(apply([int(i) for i in data["ids"]], data["answer"]), ensure_ascii=False))
    elif cmd == "collect":
        print("добавлено статей:", collect())
    elif cmd == "status":
        print(json.dumps(status(), ensure_ascii=False))
    else:
        sys.exit(main.__doc__)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main(sys.argv[1:])
