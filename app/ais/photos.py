"""Фото судна по IMO из Wikimedia Commons (свободные лицензии, с указанием автора).

На Commons у многих судов есть категория «IMO 1234567»: в ней лежат файлы или
подкатегория с названием судна. Ответ кэшируется в базе: найденное — на 30 дней,
«фото нет» — на 7 дней, чтобы не спрашивать Commons при каждом клике.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

import requests

from .. import config, db

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia просит указывать в User-Agent, как связаться (без этого ограничивает строже) — адрес сайта, не email
UA = f"WarRiskMonitor/0.1 (https://{config.DOMAIN or 'localhost'}; vessel risk map, photos shown with attribution)"
TTL_FOUND = 30 * 86400
TTL_NONE = 7 * 86400
_lock = threading.Lock()          # Commons отвечает 429 на частые запросы: не чаще раза в секунду
_last_call = 0.0


def _api(**params) -> dict:
    """Запрос к Commons не чаще раза в секунду; на «429 — слишком часто» ждём, сколько просят, и повторяем."""
    global _last_call
    for attempt in range(3):
        wait = 1.0 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = requests.get(API, params={"action": "query", "format": "json", **params},
                         headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 429 and attempt < 2:
            pause = min(float(r.headers.get("Retry-After") or 10), 30)
            log.info("Commons: слишком частые запросы, жду %.0f с", pause)
            time.sleep(pause)
            _last_call = time.time()
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def _first_image(category: str) -> Optional[dict]:
    data = _api(generator="categorymembers", gcmtitle=category, gcmtype="file", gcmlimit=10,
                prop="imageinfo", iiprop="url|extmetadata|mime", iiurlwidth=480,
                iiextmetadatafilter="Artist|LicenseShortName")
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        if not str(info.get("mime", "")).startswith("image/") or not info.get("thumburl"):
            continue
        meta = info.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()
        return {"url": info["thumburl"], "page": info.get("descriptionurl"),
                "author": artist[:80] or None, "license": (meta.get("LicenseShortName") or {}).get("value")}
    return None


def lookup(imo: str) -> Optional[dict]:
    cat = f"Category:IMO {imo}"
    found = _first_image(cat)
    if found:
        return found
    data = _api(list="categorymembers", cmtitle=cat, cmtype="subcat", cmlimit=5)
    for sub in (data.get("query") or {}).get("categorymembers", []):
        found = _first_image(sub["title"])
        if found:
            return found
    return None


def get(imo: str) -> Optional[dict]:
    if not re.fullmatch(r"\d{7}", imo or ""):
        return None
    key = f"photo:{imo}"
    cached = db.kv_get(key)
    if cached and time.time() - cached.get("ts", 0) < (TTL_FOUND if cached.get("photo") else TTL_NONE):
        return cached.get("photo")
    try:
        with _lock:
            photo = lookup(imo)
    except Exception as exc:  # noqa: BLE001 — нет фото не повод ломать карточку
        log.info("Commons: фото для IMO %s не получено: %s", imo, exc)
        return None
    db.kv_set(key, {"ts": time.time(), "photo": photo})
    return photo


def state(imo: str) -> str:
    """found — фото есть, none — точно нет (Commons ответил), unknown — ещё не спрашивали или была ошибка."""
    cached = db.kv_get(f"photo:{imo}")
    if not cached:
        return "unknown"
    return "found" if cached.get("photo") else "none"


def prefetch_fleet(limit: int = 10, pause: float = 3.0) -> int:
    """Фото судов флотов — заранее, понемногу (раз в 10 минут по 10), чтобы карточки открывались сразу с фото."""
    rows = db.query("SELECT DISTINCT COALESCE(NULLIF(v.imo, ''), a.imo) AS imo FROM vessels v "
                    "LEFT JOIN ais_state a ON a.mmsi=v.mmsi")
    done = 0
    for r in rows:
        imo = str(r["imo"] or "")
        if not re.fullmatch(r"\d{7}", imo):
            continue
        cached = db.kv_get(f"photo:{imo}")
        if cached and time.time() - cached.get("ts", 0) < (TTL_FOUND if cached.get("photo") else TTL_NONE):
            continue
        get(imo)
        done += 1
        if done >= limit:
            break
        time.sleep(pause)
    return done
