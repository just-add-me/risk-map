"""Границы стран (Natural Earth 1:10m) — скачиваются один раз в data/geodata."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from .. import config

log = logging.getLogger(__name__)

COUNTRIES_FILE = config.GEODATA_DIR / "ne_10m_admin_0_countries.geojson"

# Название в циркуляре → коды ADM0_A3 из Natural Earth (иногда страна = несколько полигонов)
NAME_TO_A3: dict[str, list[str]] = {
    "benin": ["BEN"], "djibouti": ["DJI"], "eritrea": ["ERI"], "libya": ["LBY"],
    "nigeria": ["NGA"], "somalia": ["SOM", "SOL"], "sudan": ["SDN"], "togo": ["TGO"],
    "bahrain": ["BHR"], "iran": ["IRN"], "iraq": ["IRQ"], "israel": ["ISR"], "kuwait": ["KWT"],
    "lebanon": ["LBN"], "oman": ["OMN"], "qatar": ["QAT"], "saudi arabia": ["SAU"],
    "syria": ["SYR"], "united arab emirates": ["ARE"], "uae": ["ARE"], "yemen": ["YEM"],
    "russia": ["RUS"], "russian federation": ["RUS"], "guyana": ["GUY"], "venezuela": ["VEN"],
    "ukraine": ["UKR"], "belarus": ["BLR"], "turkey": ["TUR"], "türkiye": ["TUR"], "turkiye": ["TUR"],
    "bulgaria": ["BGR"], "romania": ["ROU"], "georgia": ["GEO"], "egypt": ["EGY"],
    "mozambique": ["MOZ"], "tanzania": ["TZA"], "pakistan": ["PAK"], "cameroon": ["CMR"],
    "ethiopia": ["ETH"], "kenya": ["KEN"], "mali": ["MLI"], "myanmar": ["MMR"],
    "afghanistan": ["AFG"], "chad": ["TCD"], "palestine": ["PSX"], "gaza": ["PSX"],
    "central african republic": ["CAF"], "democratic republic of congo": ["COD"],
    "democratic republic of the congo": ["COD"], "south sudan": ["SSD"], "haiti": ["HTI"],
    "colombia": ["COL"], "mexico": ["MEX"], "jordan": ["JOR"], "algeria": ["DZA"],
    "philippines": ["PHL"], "indonesia": ["IDN"], "malaysia": ["MYS"], "taiwan": ["TWN"],
}


def download_once(url: str, path: Path, min_size: int) -> Path:
    """Скачивает файл Natural Earth, если его ещё нет (или он оборван)."""
    if path.exists() and path.stat().st_size > min_size:
        return path
    log.info("Скачиваю %s", url)
    tmp = path.with_suffix(".part")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": config.HTTP_USER_AGENT}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(path)
    return path


def ensure_downloaded() -> Path:
    return download_once(config.NE_COUNTRIES_URL, COUNTRIES_FILE, 1_000_000)


def load_features() -> list[dict]:
    path = ensure_downloaded()
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    out = []
    for feat in fc["features"]:
        props = {k.upper(): v for k, v in (feat.get("properties") or {}).items()}
        out.append({"a3": props.get("ADM0_A3") or props.get("ISO_A3"),
                    "names": {str(props.get(k, "")).lower() for k in
                              ("NAME", "NAME_LONG", "ADMIN", "FORMAL_EN", "NAME_EN", "SOVEREIGNT")
                              if props.get(k)},
                    "geometry": feat["geometry"]})
    return out


def resolve(name: str, features: list[dict]) -> list[str]:
    """Название страны из циркуляра → список кодов. Пустой список — не нашли."""
    key = name.strip().lower()
    if key in NAME_TO_A3:
        return NAME_TO_A3[key]
    for f in features:
        if key in f["names"]:
            return [f["a3"]]
    return []
