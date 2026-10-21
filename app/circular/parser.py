"""Разбор текста циркуляра JWC (JWLA-xxx) в структуру «спецификация зон».

Циркуляр состоит из трёх частей:
  1. Титульная страница: что добавлено / изменено / удалено.
  2. Перечень (Listed Areas): регионы, страны и «waters as defined overleaf».
  3. Defined Waters: текстовое описание границ с координатами.

На выходе — словарь, из которого builder.py строит геометрию. Каждая запись
получает «шаблон» (template) — способ построить полигон. Если что-то в тексте
не удалось однозначно понять, запись помечается needs_review=True, и в панели
появится уведомление «проверьте вручную».
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Optional

from . import coords

REGION_HEADINGS = {
    "africa", "asia", "europe", "middle east", "south america", "north america",
    "central america", "americas", "oceania", "caribbean", "south east asia", "southeast asia",
}

_NOISE = [
    re.compile(r"^JWLA-?\d+$", re.I),
    re.compile(r"^\d{1,2}\s*(st|nd|rd|th)?\s*\^?(st|nd|rd|th)?\s+[A-Z][a-z]+\s+\d{4}$"),
    re.compile(r"^Joint (War )?Committee Circular$", re.I),
    re.compile(r"^A joint LMA/IUA committee$", re.I),
    re.compile(r"^JWC Listed Areas$", re.I),
    re.compile(r"^Hull War, Piracy, Terrorism and Related Perils$", re.I),
    re.compile(r"^Page \d+( of \d+)?$", re.I),
]

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "zone"


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def _clean_lines(text: str) -> list[str]:
    text = text.replace(" ", " ").replace("\r", "")
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]


def _is_noise(line: str) -> bool:
    return not line or any(rx.match(line) for rx in _NOISE)


def parse_ref(text: str) -> tuple[Optional[str], Optional[int]]:
    m = re.search(r"JWLA[-\s]?0*(\d{1,4})", text)
    if not m:
        return None, None
    n = int(m.group(1))
    return f"JWLA-{n:03d}", n


def parse_date(text: str) -> Optional[str]:
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*\^?(?:st|nd|rd|th)?\s+"
                  r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
                  text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
def _classify_country(name: str, qualifier: str) -> dict:
    q = qualifier.lower()
    entry: dict = {"template": "country", "params": {"country": name}, "severity": "full"}
    if "eez" in q and ("only" in q) and ("beyond territorial" in q or "offshore installation" in q):
        entry["template"] = "eez_beyond_tw"
        entry["severity"] = "conditional"
    elif "eez" in q and "including" in q:
        entry["template"] = "country_plus_eez"
    elif "offshore oil terminal" in q:
        entry["params"]["extras"] = "offshore_terminals"
    elif q:
        lat = None
        m = re.search(r"(south|north) of\s+(.+)$", qualifier, re.I)
        if m:
            vals = coords.find_values(m.group(2))
            if vals and vals[0][0] == "lat":
                lat = vals[0][1]
                entry["template"] = "country_clip_lat"
                entry["params"].update({"side": m.group(1).lower(), "lat": lat})
        if lat is None and not q.startswith("including"):
            # Незнакомая оговорка: строим всю страну, но просим человека проверить
            entry["needs_review"] = True
            entry["review_reason"] = f"Неизвестная оговорка: «{qualifier}». Построена вся страна."
    return entry


def _classify_defined(name: str, block: str) -> dict:
    n = name.lower()
    b = block.lower()
    pts = coords.dedupe(coords.find_points(block))
    entry: dict = {"severity": "full", "params": {}}

    if "sea of azov" in b and len(b) < 40 and "black sea" not in b:
        entry["template"] = "azov"
    elif "black sea" in b:
        m = re.search(r"excluding territorial waters of adjoining countries other than (.+)", block, re.I | re.S)
        if m:
            keep = re.split(r",|\band\b", m.group(1).replace("\n", " "))
            entry["template"] = "black_sea_basin"
            entry["params"]["keep_countries"] = [k.strip(" .;") for k in keep if k.strip(" .;")]
        elif len(pts) >= 2:
            entry["template"] = "generic_coords"
            entry["params"]["points"] = pts
            entry["params"]["closure"] = "black_sea"
            entry["needs_review"] = True
            entry["review_reason"] = "Чёрное море задано координатами — проверьте форму полигона."
        else:
            entry["template"] = "black_sea_basin"
            entry["params"]["keep_countries"] = []
            entry["needs_review"] = True
            entry["review_reason"] = "Не удалось разобрать исключения для Чёрного моря."
    elif "inland waters of ukraine" in b:
        entry["template"] = "inland_ukraine"
    elif "inland waters of russia" in b:
        entry["template"] = "inland_russia_don"
        entry["params"]["lon_limit"] = coords.find_longitude_line(block) or 41.0
    elif "belarus" in b:
        entry["template"] = "inland_belarus"
        lat = coords.find_latitude_line(block)
        entry["params"]["lat_limit"] = lat if lat is not None else 52.5
    elif "cabo delgado" in n:
        entry["template"] = "cabo_delgado"
        entry["params"]["points"] = pts
        if len(pts) != 4:
            entry["needs_review"] = True
            entry["review_reason"] = f"Ожидалось 4 точки, найдено {len(pts)}."
    elif "gulf of guinea" in n:
        entry["template"] = "gulf_of_guinea"
        entry["params"]["points"] = pts
        if len(pts) != 3:
            entry["needs_review"] = True
            entry["review_reason"] = f"Ожидалось 3 точки, найдено {len(pts)}."
    elif "indian ocean" in n or "red sea" in n or "gulf of aden" in n:
        entry["template"] = "indian_ocean"
        lat = coords.find_latitude_line(block)
        entry["params"]["red_sea_lat"] = lat if lat is not None else 18.0
        entry["params"]["exclude_egypt_tw"] = "egyptian" in b and "not including" in b
        entry["params"]["points"] = pts
        if len(pts) != 5:
            entry["needs_review"] = True
            entry["review_reason"] = f"Ожидалось 5 точек границы, найдено {len(pts)}."
    elif len(pts) >= 3:
        entry["template"] = "generic_coords"
        entry["params"]["points"] = pts
        entry["needs_review"] = True
        entry["review_reason"] = "Новая зона: построена по координатам напрямую, проверьте форму."
    else:
        entry["template"] = "unknown"
        entry["needs_review"] = True
        entry["review_reason"] = "Не удалось понять границы зоны — нужна ручная разметка."
    return entry


def _split_list_section(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Делит текст на титульную часть, перечень и Defined Waters."""
    def_idx = next((i for i, ln in enumerate(lines) if re.match(r"^Defined Waters\b", ln, re.I)), None)
    end = def_idx if def_idx is not None else len(lines)
    header_idx = [i for i, ln in enumerate(lines[:end]) if re.match(r"^Hull War, Piracy, Terrorism", ln, re.I)]
    list_start = header_idx[-1] + 1 if len(header_idx) >= 2 else (header_idx[0] + 1 if header_idx else 0)
    cover = lines[:list_start]
    listed = lines[list_start:end]
    defined = lines[end + 1:] if def_idx is not None else []
    return cover, listed, defined


def _parse_cover(cover: list[str]) -> dict:
    out = {"added": [], "amended": [], "deleted": []}
    current = None
    for raw in cover:
        ln = raw.strip()
        if not ln or _is_noise(ln):
            continue
        if re.match(r"^The application of this list", ln, re.I):
            break
        m = re.match(r"^(Added|Amended|Deleted)\s*:\s*(.*)$", ln, re.I)
        if m:
            current = m.group(1).lower()
            if m.group(2):
                out[current].append(m.group(2).strip())
            continue
        if current and not re.match(r"^(Committee|Date|Circular reference|Contact)\b", ln):
            out[current].append(ln)
    return out


def parse(text: str) -> dict:
    lines = _clean_lines(text)
    ref, number = parse_ref(text)
    issued = parse_date(text)
    cover, listed, defined_lines = _split_list_section(lines)
    cover_info = _parse_cover(cover)

    # ---- перечень ---------------------------------------------------------
    items: list[dict] = []
    region = None
    listed = [ln for ln in listed if not _is_noise(ln)]
    i = 0
    while i < len(listed):
        ln = listed[i]
        nxt = listed[i + 1] if i + 1 < len(listed) else ""
        if ln.lower() in REGION_HEADINGS:
            region = ln
        elif re.match(r"^waters as defined overleaf$", nxt, re.I):
            items.append({"kind": "defined", "name": ln, "region": region or ln})
            i += 1
        elif re.search(r"as defined overleaf", ln, re.I):
            name = re.split(r",\s*(?:waters\s+)?as defined|\s+as defined", ln, flags=re.I)[0].strip()
            items.append({"kind": "defined", "name": name, "region": region})
        elif ln[:1].islower() and items:
            items[-1]["raw"] = items[-1].get("raw", items[-1]["name"]) + " " + ln
        else:
            items.append({"kind": "country", "name": ln, "raw": ln, "region": region})
        i += 1

    # ---- Defined Waters: блоки по заголовкам ---------------------------------
    defined_lines = [ln for ln in defined_lines if not _is_noise(ln)]
    stop = next((k for k, ln in enumerate(defined_lines) if re.match(r"^Definitions?\s*:", ln, re.I)), None)
    definitions_text = " ".join(defined_lines[stop:]) if stop is not None else ""
    if stop is not None:
        defined_lines = defined_lines[:stop]

    defined_items = [it for it in items if it["kind"] == "defined"]
    headings = {it["name"].lower(): it for it in defined_items}
    for it in defined_items:
        if it.get("region"):
            headings.setdefault(it["region"].lower(), it)

    blocks: dict[str, list[str]] = {}
    current_key = None
    k = 0
    while k < len(defined_lines):
        ln = defined_lines[k]
        low = ln.lower()
        match_key = None
        for hk in headings:
            if low == hk or (len(low) >= 8 and hk.startswith(low) and not low.endswith(":")):
                match_key = hk
                break
        if match_key:
            acc = low
            while k + 1 < len(defined_lines) and acc != match_key and \
                    match_key.startswith((acc + " " + defined_lines[k + 1].lower()).strip()):
                k += 1
                acc = (acc + " " + defined_lines[k].lower()).strip()
            current_key = match_key
            blocks.setdefault(current_key, [])
        elif current_key:
            blocks[current_key].append(ln)
        k += 1

    # ---- собираем записи --------------------------------------------------
    entries: list[dict] = []
    used_blocks = set()
    for it in items:
        if it["kind"] == "country":
            raw = it.get("raw", it["name"])
            name, _, qualifier = raw.partition(",")
            name, qualifier = name.strip(), qualifier.strip()
            e = {"id": slug(name), "name": name, "region": it.get("region"), "kind": "country",
                 "qualifier": qualifier, "text": raw}
            e.update(_classify_country(name, qualifier))
            entries.append(e)
            continue

        key = it["name"].lower()
        if key not in blocks and it.get("region") and it["region"].lower() in blocks:
            key = it["region"].lower()
        block_lines = blocks.get(key, [])
        used_blocks.add(key)
        numbered = _split_numbered(block_lines)
        if numbered:
            # Группа вида «Europe: 1) Sea of Azov 2) Black Sea ...» — каждая позиция отдельной зоной
            for num, item_text in numbered:
                title = _item_title(item_text)
                e = {"id": slug(f"{it.get('region') or it['name']}-{title}"), "name": title,
                     "region": it.get("region") or it["name"], "kind": "defined",
                     "qualifier": "", "text": item_text, "group": it["name"], "item_no": num}
                e.update(_classify_defined(title, item_text))
                entries.append(e)
        else:
            block = "\n".join(block_lines)
            e = {"id": slug(it["name"]), "name": it["name"], "region": it.get("region"), "kind": "defined",
                 "qualifier": "", "text": block}
            e.update(_classify_defined(it["name"], block))
            if not block_lines:
                e["needs_review"] = True
                e["review_reason"] = "Описание границ не найдено в разделе Defined Waters."
            entries.append(e)

    # одинаковые id (на всякий случай) делаем уникальными
    seen: dict[str, int] = {}
    for e in entries:
        if e["id"] in seen:
            seen[e["id"]] += 1
            e["id"] = f"{e['id']}-{seen[e['id']]}"
        else:
            seen[e["id"]] = 1
        e["hash"] = text_hash(e["text"] + "|" + e.get("qualifier", ""))
        e.setdefault("needs_review", False)

    _apply_cover_flags(entries, cover_info)
    return {
        "ref": ref,
        "number": number,
        "issued": issued,
        "cover": cover_info,
        "definitions": definitions_text,
        "entries": entries,
    }


def _split_numbered(lines: list[str]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for ln in lines:
        m = re.match(r"^(\d{1,2})\)\s*(.*)$", ln)
        if m:
            out.append((int(m.group(1)), m.group(2)))
        elif out:
            out[-1] = (out[-1][0], out[-1][1] + "\n" + ln)
    return out


def _item_title(text: str) -> str:
    first = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    for pat, title in [
        (r"^Sea of Azov\b", "Sea of Azov"),
        (r"\bBlack Sea\b", "Black Sea"),
        (r"inland waters of Ukraine", "Inland waters of Ukraine"),
        (r"Inland waters of Russia", "Inland waters of Russia (Don, Donets)"),
        (r"inland waters of Belarus", "Inland waters of Belarus"),
    ]:
        if re.search(pat, first, re.I):
            return title
    return first.split(",")[0][:60]


def _apply_cover_flags(entries: list[dict], cover: dict) -> None:
    """Пометки «added/amended» по титульной странице (используются, если нет предыдущей версии)."""
    def hit(e: dict, lines: list[str]) -> bool:
        name = e["name"].lower()
        if not name:
            return False
        low = [re.sub(r"^\d+\)\s*", "", ln.lower()).strip() for ln in lines]
        if e["kind"] == "country":
            # страна считается упомянутой, только если стоит отдельной строкой
            return any(ln == name or ln.startswith(name + ",") for ln in low)
        return any(ln.startswith(name) or name.startswith(ln) and len(ln) > 8 for ln in low)

    for e in entries:
        if hit(e, cover.get("added", [])):
            e["cover_change"] = "added"
        elif hit(e, cover.get("amended", [])):
            e["cover_change"] = "amended"
        else:
            e["cover_change"] = "unchanged"


def diff(old: Optional[dict], new: dict) -> dict:
    """Сравнение двух спецификаций по id и хэшу текста."""
    if not old:
        return {
            "base": None,
            "added": [e["id"] for e in new["entries"] if e.get("cover_change") == "added"],
            "amended": [e["id"] for e in new["entries"] if e.get("cover_change") == "amended"],
            "removed": [],
            "removed_names": cover_deleted(new),
        }
    old_map = {e["id"]: e for e in old["entries"]}
    new_map = {e["id"]: e for e in new["entries"]}
    return {
        "base": old.get("ref"),
        "added": [i for i in new_map if i not in old_map],
        "amended": [i for i in new_map if i in old_map and new_map[i]["hash"] != old_map[i]["hash"]],
        "removed": [i for i in old_map if i not in new_map],
        "removed_names": [old_map[i]["name"] for i in old_map if i not in new_map],
    }


def cover_deleted(spec: dict) -> list[str]:
    return list(spec.get("cover", {}).get("deleted", []))
