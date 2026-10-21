"""Активный набор зон: файл data/zones/active.geojson + индекс для проверок в памяти."""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from . import config
from .geo.zone_index import ZoneIndex

log = logging.getLogger(__name__)

ACTIVE_FILE = config.ZONES_DIR / "active.geojson"
_lock = threading.RLock()
_index = ZoneIndex()
_raw: Optional[dict] = None


def load_active() -> None:
    global _raw
    with _lock:
        if ACTIVE_FILE.exists():
            with open(ACTIVE_FILE, encoding="utf-8") as f:
                _raw = json.load(f)
            _index.load(_raw or {})
            log.info("Загружены зоны %s: %d шт.", (_raw.get("properties") or {}).get("ref"), len(_index.zones))
        else:
            _raw = None
            _index.load({"features": []})


def set_active(fc: dict) -> None:
    """Атомарно заменяет активные зоны (сначала во временный файл, потом rename)."""
    tmp = ACTIVE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(ACTIVE_FILE)
    load_active()


def index() -> ZoneIndex:
    return _index


def raw() -> Optional[dict]:
    return _raw


def active_ref() -> Optional[str]:
    return ((_raw or {}).get("properties") or {}).get("ref")


def zone_name(zone_id: str) -> str:
    z = _index.get(zone_id)
    return z.props.get("name", zone_id) if z else zone_id
