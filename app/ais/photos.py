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

from .. import db

log = logging.getLogger(__name__)

API = "https://commons.wikimedia.org/w/api.php"
UA = "WarRiskMonitor/0.1 (internal vessel map; photos shown with attribution)"
TTL_FOUND = 30 * 86400
TTL_NONE = 7 * 86400
_lock = threading.Lock()          # Commons отвечает 429 на частые запросы: не чаще раза в секунду
_last_call = 0.0


def _api(**params) -> dict:
    global _last_call
    wait = 1.0 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    r = requests.get(API, params={"action": "query", "format": "json", **params},
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return r.json()


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
