"""Зоны IBF/ITF для экипажа: WOA (военные действия), HRA (высокий риск), ERZ (расширенный риск).

Источник — PDF «IBF List of designated risk areas», который выкладывают на сайте IMEC
(config.IBF_PAGE_URL). Список пересматривают часто, обычно на 2–4 недели вперёд.

Что делает автоматически (вместе с проверкой циркуляров JWC, раз в CIRCULAR_CHECK_HOURS):
  1. Открывает страницу IMEC и берёт ссылку на самый свежий PDF списка.
  2. Если ссылка новая — скачивает, разбирает разделы «DESIGNATION N» (название, тип, сроки).
  3. Строит контуры по встроенным шаблонам (их подбираем по названию зоны).
     Если описание границ в документе изменилось или зона незнакомая — помечаем
     «требует проверки» и пишем уведомление.
  4. Пересчитывает все суда.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timezone
from typing import Callable, Optional
from urllib.parse import urljoin

from .. import alerts, config, db, zones

log = logging.getLogger(__name__)

state: dict = {"running": False, "step": "", "last_error": None}

CATEGORIES = {
    "woa": {"short": "WOA", "label": "Зона военных действий (WOA)",
            "benefits": "Бонус 100% базовой зарплаты (минимум 5 дней), двойные компенсации при гибели и травме, "
                        "право экипажа отказаться от рейса, охрана уровня ISPS 3."},
    "hra": {"short": "HRA", "label": "Зона высокого риска (HRA)",
            "benefits": "Бонус 100% базовой зарплаты за каждый день в зоне, двойные компенсации, "
                        "право экипажа отказаться от рейса, охрана уровня ISPS 3."},
    "erz": {"short": "ERZ", "label": "Зона расширенного риска (ERZ)",
            "benefits": "Бонус 100% и двойные компенсации только в день нападения на судно. "
                        "Права отказа от рейса нет. Усиленные меры сверх BMP."},
    "erz2": {"short": "ERZ-2", "label": "Зона расширенного риска, уровень 2",
             "benefits": "Бонус 100% базовой зарплаты за каждый день у причала и на лоцманской проводке, "
                         "двойные компенсации."},
}

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?(?:of)?(" + "|".join(MONTHS) + r")(\d{4})", re.I)


# --------------------------------------------------------------------------- разбор документа
def find_dates(text: str) -> list[str]:
    """Даты вида «1st of October 2026» → ISO. Пробелы убираем: PDF рвёт слова («1 3th of M arch»)."""
    compact = re.sub(r"\s+", "", text)
    out = []
    for d, m, y in _DATE_RE.findall(compact):
        try:
            out.append(date(int(y), MONTHS.index(m.lower()) + 1, int(d)).isoformat())
        except ValueError:
            continue
    return out


def category_of(title: str) -> Optional[str]:
    t = title.lower()
    if "warlike" in t:
        return "woa"
    if "high risk" in t:
        return "hra"
    if "extended risk" in t:
        return "erz2" if "level 2" in t else "erz"
    return None


def area_text(block: str) -> str:
    """Только описание границ — без абзацев о выплатах и сроках (они меняются в каждой версии)."""
    stop = re.search(r"During (?:a vessel|the period)|The entitlements|This designation|Protection of seafarers",
                     block)
    part = block[:stop.start()] if stop else block
    part = re.sub(r"^\s*\d+\s*$", " ", part, flags=re.M)        # номера страниц
    return re.sub(r"\s+", " ", part).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha1(re.sub(r"[^a-z0-9]", "", text.lower()).encode()).hexdigest()[:12]


def parse(text: str) -> dict:
    head = text.split("DESIGNATION", 1)[0]
    head_dates = find_dates(head[:800])
    list_date = head_dates[0] if head_dates else None
    list_until = head_dates[-1] if len(head_dates) > 1 and re.search(r"extended|until", head[:800], re.I) else None
    designations = []
    for m in re.finditer(r"DESIGNATION\s*(\d+)\s*[:.]?\s*\n(.*?)(?=DESIGNATION\s*\d+\s*[:.]?\s*\n|\Z)", text, re.S):
        number, block = int(m.group(1)), m.group(2)
        # название — первая строка раздела; дальше идёт описание границ
        first, _, rest = block.strip().partition("\n")
        title = re.sub(r"\s+", " ", first).strip()
        effect = re.search(r"(?:in effect|in force)(.*?)(?:with further|\Z)", block, re.S | re.I)
        dates = find_dates(effect.group(1)) if effect else []
        area = area_text(rest)
        tpl = template_for(title)
        designations.append({
            "number": number,
            "title": title,
            "category": category_of(title),
            "template": tpl,
            "valid_from": dates[0] if dates else None,
            "valid_to": dates[-1] if len(dates) > 1 else None,
            "area_text": area[:1500],
            "fingerprint": fingerprint(area),
        })
    return {"list_date": list_date, "list_until": list_until, "designations": designations}


def find_latest_pdf(html: str, base_url: str) -> Optional[str]:
    """Ссылка на самый свежий PDF списка IBF на странице IMEC."""
    best: tuple[str, str] | None = None
    for m in re.finditer(r"""href\s*=\s*["']([^"']+\.pdf)(?:\?[^"']*)?["']""", html, re.I):
        href = m.group(1)
        name = href.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
        if not re.search(r"IBF", name, re.I) or not re.search(r"list|risk", name, re.I):
            continue
        dates = find_dates(name)
        key = dates[-1] if dates else ""
        url = urljoin(base_url, href)
        if best is None or key > best[0]:
            best = (key, url)
    return best[1] if best else None


# --------------------------------------------------------------------------- шаблоны контуров
# Опорные точки по тексту списка IBF от 01.10.2026 (lon, lat)
RAS_AL_HADD = (59.80, 22.53)
GUARDAFUI = (51.28, 11.825)
P_BLACK_SEA = [(39.9167, 43.4169), (31.40, 44.0667), (31.1833, 44.7333), (29.6833, 45.20)]   # точки 1–4 HRA
NBS_LINE = [(29.6833, 45.20), (31.00, 45.1667), (32.50, 45.35)]                              # граница WOA
AZOV_LAT = 45.05


def _shapes():
    from . import shapes
    return shapes


def t_yemen(w, d):
    s = _shapes()
    from ..geo.builder import buffer_nm
    yem = w.country(["YEM"])
    mainland = max(s.polygonal(yem).geoms, key=lambda g: g.area) if yem.geom_type == "MultiPolygon" else yem
    return w.sea(buffer_nm(mainland, 12)), {
        "approximate": True,
        "note": "12 миль от материкового побережья Йемена. Исключение коридора MSTC на карте не показано."}


def t_red_sea_aden(w, d):
    s = _shapes()
    ring = [(38.0, 16.402), (42.78, 16.402), (53.11, 16.65), (53.0, 13.4167), GUARDAFUI, (43.0, 9.5), (38.0, 13.0)]
    return w.sea(s.Polygon(ring)), {"note": "От 16°24′N (граница Саудовской Аравии) до линии Йемен/Оман → "
                                            "13°25′N 53°E → маяк Гвардафуй. Воды Джибути и Сомали включены."}


def t_azov(w, d):
    s = _shapes()
    return s.water(w, s.box(34.2, AZOV_LAT, 39.6, 47.4)), {"note": "Азовское море и Керченский пролив севернее 45°03′N."}


def _nbs_poly():
    s = _shapes()
    return s.Polygon([*NBS_LINE, (33.7, 46.15), (34.5, 47.2), (29.5, 46.9), (28.8, 45.3)])


def t_northern_black_sea(w, d):
    s = _shapes()
    return s.water(w, _nbs_poly()), {"note": "Северо-запад Чёрного моря: от границы Украины и Румынии "
                                            "до 45°10′N 31°E и 45°21′N 32°30′E."}


def t_ukraine_ports(w, d):
    s = _shapes()
    ports = s.ports_of(w, ["UKR"])
    g = s.port_circles(w, ["UKR"], 3)
    return g, {"approximate": True,
               "note": "Круги 3 мили вокруг портов Украины из справочника Natural Earth: "
                       + ", ".join(sorted(p["name"] for p in ports))}


def t_black_sea_hra(w, d):
    s = _shapes()
    from ..geo.builder import BLACK_SEA_RING
    basin = s.water(w, s.Polygon(BLACK_SEA_RING))
    north = s.Polygon([*P_BLACK_SEA, (27.0, 45.2), (27.0, 47.6), (42.5, 47.6), (42.5, 43.0)])
    g = basin.intersection(north).difference(_nbs_poly()).difference(s.box(34.2, AZOV_LAT, 39.6, 47.6))
    return g, {"note": "Между линией WOA на северо-западе, Крымом и Азовом на севере и линией "
                       "43°25′N 39°55′E → 44°04′N 31°24′E → 44°44′N 31°11′E → 45°12′N 29°41′E на юге."}


def t_persian_gulf(w, d):
    s = _shapes()
    ring = [(57.1, 24.0), (60.9667, 24.0), (61.65, 24.875), (61.65, 25.175), (61.65, 31.5), (47.0, 31.5),
            (47.0, 22.5), (56.0, 22.5)]
    return w.sea(s.Polygon(ring)), {"note": "Все воды западнее линии 24°N (57°06′E → 60°58′E) → пеленг 035° "
                                            "от маяка Рас-эль-Хадд → граница Пакистана. Включая порты и рейды."}


def t_gulf_of_oman(w, d):
    s = _shapes()
    ring = [(57.1, 24.0), (60.9667, 24.0), RAS_AL_HADD, (57.1, 22.53)]
    return w.sea(s.Polygon(ring)), {"note": "Южнее 24°N, до пеленга 035° от маяка Рас-эль-Хадд."}


def _il_ports(w):
    s = _shapes()
    il = [p for p in s.ports_of(w, ["ISR"]) if p["lat"] > 31.0 and p["lon"] < 35.2]
    lb = s.ports_of(w, ["LBN"])
    return il + lb


def t_israel_lebanon_ports(w, d):
    s = _shapes()
    ports = _il_ports(w)
    g = s.unary_union([s.circle_nm(p["lon"], p["lat"], 3) for p in ports] or [s.EMPTY])
    return g, {"approximate": True,
               "note": "Круги 3 мили (причал и лоцманская проводка) вокруг портов: "
                       + ", ".join(sorted(p["name"] for p in ports))}


def t_israel_lebanon_12nm(w, d):
    s = _shapes()
    clip = s.Polygon([(33.5, 32.118), (34.491, 31.595), (36.5, 31.595), (36.5, 34.636), (33.5, 34.636)])
    g = w.sea(s.unary_union([w.tw(["ISR"]), w.tw(["LBN"])])).intersection(clip)
    ports = t_israel_lebanon_ports(w, d)[0]
    return g.difference(ports), {"note": "12 миль от берега Израиля и Ливана, от 34°38′N до границы с Газой; "
                                         "районы портов (ERZ уровня 2) вынесены в отдельную зону."}


def t_gulf_of_guinea(w, d):
    s = _shapes()
    ring = [(-7.53, 4.36), (5.0, 0.0), (11.75, -17.26), (14.5, -17.26), (14.5, 6.5), (-7.53, 7.5)]
    return s.water(w, s.Polygon(ring)), {"approximate": True,
                                         "note": "От границы Либерии и Кот-д'Ивуара до 0°N 5°E и далее до "
                                                 "границы Анголы и Намибии."}


# (ключевые слова в названии зоны, шаблон). Порядок важен: более узкие правила раньше.
TEMPLATE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("yemen",), "yemen"),
    (("red sea",), "red_sea_aden"),
    (("gulf of aden",), "red_sea_aden"),
    (("azov",), "azov"),
    (("kerch",), "azov"),
    (("northern black sea",), "northern_black_sea"),
    (("ports in ukraine",), "ukraine_ports"),
    (("high risk", "black sea"), "black_sea_hra"),
    (("persian",), "persian_gulf"),
    (("hormuz",), "persian_gulf"),
    (("gulf of oman",), "gulf_of_oman"),
    (("level 2", "israel"), "israel_lebanon_ports"),
    (("level 2", "leban"), "israel_lebanon_ports"),
    (("israel",), "israel_lebanon_12nm"),
    (("lebanon",), "israel_lebanon_12nm"),
    (("gulf of guinea",), "gulf_of_guinea"),
]
TEMPLATES: dict[str, Callable] = {
    "yemen": t_yemen, "red_sea_aden": t_red_sea_aden, "azov": t_azov,
    "northern_black_sea": t_northern_black_sea, "ukraine_ports": t_ukraine_ports,
    "black_sea_hra": t_black_sea_hra, "persian_gulf": t_persian_gulf, "gulf_of_oman": t_gulf_of_oman,
    "israel_lebanon_ports": t_israel_lebanon_ports, "israel_lebanon_12nm": t_israel_lebanon_12nm,
    "gulf_of_guinea": t_gulf_of_guinea,
}
# Отпечатки описаний границ, по которым сделаны шаблоны (список от 01.10.2026).
# Если IBF перепишет границы — отпечаток не совпадёт, и зона получит пометку «проверить».
KNOWN_FINGERPRINTS: dict[str, set[str]] = {
    "yemen": {"e80ed1b2eeb6"}, "red_sea_aden": {"4542d35547e8"}, "azov": {"3a8ebf6a478a"},
    "northern_black_sea": {"0d038ef3037a"}, "ukraine_ports": {"1acb455c6cf6"}, "black_sea_hra": {"bd133c2d398a"},
    "persian_gulf": {"99d6cd79b476"}, "gulf_of_oman": {"c67818b1d3cc"}, "israel_lebanon_12nm": {"a340f1be7cb5"},
    "israel_lebanon_ports": {"2d5c820c4218"},
}


def template_for(title: str) -> Optional[str]:
    t = title.lower()
    for words, tpl in TEMPLATE_RULES:
        if all(wd in t for wd in words):
            return tpl
    return None


def short_title(title: str) -> str:
    """«IBF Warlike Operations Area – Sea of Azov …» → «Sea of Azov …»."""
    parts = re.split(r"\s+[–-]\s+", title)
    rest = [p for p in parts[1:] if not re.fullmatch(r"level\s*2", p.strip(), re.I)]
    return " – ".join(rest) if rest else title


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# --------------------------------------------------------------------------- построение слоя
def build(spec: dict, world=None, prev: Optional[dict] = None) -> tuple[dict, list[str]]:
    from ..geo.builder import World, add_display
    s = _shapes()
    w = world or World()
    prev_by_tpl = {d["template"]: d for d in (prev or {}).get("designations", []) if d.get("template")}
    features, logs = [], []
    for d in spec["designations"]:
        cat = d["category"]
        name = f"IBF {CATEGORIES[cat]['short'] if cat else '?'}: {short_title(d['title'])}"
        review = []
        if not cat:
            review.append("не удалось определить тип зоны")
        tpl = TEMPLATES.get(d["template"] or "")
        if not tpl:
            review.append("незнакомая зона — для неё нет шаблона границ, нужна ручная разметка")
        known = KNOWN_FINGERPRINTS.get(d["template"] or "")
        if tpl and known and d["fingerprint"] not in known:
            review.append("описание границ в документе изменилось — контур построен по прежнему описанию")
        p = prev_by_tpl.get(d["template"])
        change = "added" if prev is not None and not p else (
            "amended" if p and p.get("fingerprint") != d["fingerprint"] else "unchanged")
        props = {
            "id": f"ibf-{slug(d['template'] or short_title(d['title']))}",
            "name": name, "layer": "ibf", "region": "IBF/ITF — экипаж", "kind": "ibf",
            "category": cat, "severity": "full", "designation": d["number"], "title": d["title"],
            "valid_from": d["valid_from"], "valid_to": d["valid_to"],
            "list_date": spec.get("list_date"), "list_until": spec.get("list_until"),
            "benefits": CATEGORIES[cat]["benefits"] if cat else "", "text": d["area_text"],
            "approximate": False, "note": "", "needs_review": bool(review), "review_reason": "; ".join(review),
            "change": change,
        }
        if not tpl:
            logs.append(f"⚠ {name}: {props['review_reason']}")
            continue
        try:
            geom, extra = tpl(w, d)
            props.update({k: v for k, v in extra.items() if k in ("approximate", "note")})
            feat = s.feature(props, geom)
            if feat:
                add_display(w, feat)
                features.append(feat)
                logs.append(f"✓ {name}")
            else:
                logs.append(f"⚠ {name}: получилась пустая геометрия")
        except Exception as exc:  # noqa: BLE001 — одна зона не должна ломать остальные
            log.exception("IBF: ошибка построения %s", name)
            logs.append(f"✖ {name}: {exc}")
    fc = {"type": "FeatureCollection", "features": features,
          "properties": {"list_date": spec.get("list_date"), "list_until": spec.get("list_until"),
                         "source_url": spec.get("source_url"), "built_at": db.now_iso(), "display_v": 2}}
    return fc, logs


def diff(prev: Optional[dict], spec: dict) -> list[str]:
    if not prev:
        return []
    old = {d["template"] or d["title"]: d for d in prev.get("designations", [])}
    new = {d["template"] or d["title"]: d for d in spec.get("designations", [])}
    lines = []
    for k in new.keys() - old.keys():
        lines.append(f"Добавлена: {new[k]['title']}")
    for k in old.keys() - new.keys():
        lines.append(f"Исключена: {old[k]['title']}")
    for k in new.keys() & old.keys():
        a, b = old[k], new[k]
        if a["category"] != b["category"]:
            lines.append(f"Изменён тип: {b['title']}")
        elif a["fingerprint"] != b["fingerprint"]:
            lines.append(f"Изменены границы: {b['title']}")
        if a.get("valid_to") != b.get("valid_to") and b.get("valid_to"):
            lines.append(f"Срок продлён до {b['valid_to']}: {short_title(b['title'])}")
    return lines


# --------------------------------------------------------------------------- автообновление
def _http_get(url: str):
    from ..circular.pipeline import _http_get as get
    return get(url)


async def activate(data: bytes, url: str) -> dict:
    from ..circular.pipeline import extract_text
    text = await asyncio.to_thread(extract_text, data, "ibf.pdf")
    spec = parse(text)
    if not spec["designations"]:
        raise ValueError("В документе не найдено ни одного раздела «DESIGNATION» — формат списка IBF изменился")
    spec["source_url"] = url
    stamp = spec.get("list_date") or date.today().isoformat()
    (config.IBF_DIR / f"IBF-{stamp}.pdf").write_bytes(data)
    (config.IBF_DIR / f"IBF-{stamp}.txt").write_text(text, encoding="utf-8")
    prev = db.kv_get("ibf_spec")
    state["step"] = "Строю зоны IBF"
    fc, logs = await asyncio.to_thread(build, spec, None, prev)
    zones.set_layer("ibf", fc)
    changes = diff(prev, spec)
    db.kv_set("ibf_spec", spec)
    db.kv_set("ibf_active", {"url": url, "list_date": spec["list_date"], "list_until": spec["list_until"],
                             "count": len(fc["features"]), "log": logs, "activated_at": db.now_iso()})
    db.kv_set("ibf_expired_notified", None)
    review = [ln for ln in logs if ln.startswith(("⚠", "✖"))]
    body = [f"Список от {spec['list_date'] or '—'}, действует до {spec['list_until'] or '—'}. "
            f"Зон на карте: {len(fc['features'])}."]
    body += changes
    if review:
        body.append("Требует проверки: " + "; ".join(r.lstrip("⚠✖ ") for r in review))
    db.notify("circular", "warning" if (changes or review) else "info",
              "Обновлён список зон IBF/ITF" if prev else "Загружен список зон IBF/ITF", "\n".join(body))
    n = await asyncio.to_thread(alerts.reevaluate_all, f"Причина: обновлён список зон IBF от {spec['list_date']}.")
    log.info("IBF: активирован список %s, зон %d, уведомлений по судам %d", spec["list_date"], len(fc["features"]), n)
    return {"ok": True, "message": f"Список IBF от {spec['list_date']}: зон {len(fc['features'])}"}


async def check_now(manual: bool = False) -> dict:
    if state["running"]:
        return {"ok": False, "message": "Проверка IBF уже идёт"}
    state.update(running=True, step="Открываю сайт IMEC", last_error=None)
    db.kv_set("ibf_last_check", db.now_iso())
    try:
        html = (await asyncio.to_thread(_http_get, config.IBF_PAGE_URL)).text
        url = find_latest_pdf(html, config.IBF_PAGE_URL)
        if not url:
            raise RuntimeError("На странице IMEC не найдено ссылки на список IBF")
        db.kv_set("ibf_fail_notified", False)
        active = db.kv_get("ibf_active") or {}
        if active.get("url") == url and zones.layer("ibf"):
            _check_expiry(active)
            return {"ok": True, "message": f"Новой версии нет (действует список от {active.get('list_date')})"}
        state["step"] = "Скачиваю список IBF"
        data = (await asyncio.to_thread(_http_get, url)).content
        return await activate(data, url)
    except Exception as exc:  # noqa: BLE001
        from ..circular.pipeline import _human_error
        msg = _human_error(exc).replace("сайт LMA", "сайт IMEC").replace("lmalloyds.com", "imec.org.uk")
        state["last_error"] = msg
        log.warning("Проверка IBF не удалась: %s", exc)
        if manual or not db.kv_get("ibf_fail_notified"):
            db.notify("system", "warning", "Не удалось проверить список IBF", f"{msg}. Повторю автоматически.")
            db.kv_set("ibf_fail_notified", True)
        return {"ok": False, "message": msg}
    finally:
        state.update(running=False, step="")


def _check_expiry(active: dict) -> None:
    until = active.get("list_until")
    if not until or until >= datetime.now(timezone.utc).date().isoformat():
        return
    if db.kv_get("ibf_expired_notified") == until:
        return
    db.notify("circular", "warning", "Срок списка IBF истёк, новой версии пока нет",
              f"Действующий список от {active.get('list_date')} был продлён до {until}. "
              "Зоны остаются на карте; проверка новой версии идёт автоматически.")
    db.kv_set("ibf_expired_notified", until)


def status() -> dict:
    active = db.kv_get("ibf_active") or {}
    return {**{k: active.get(k) for k in ("url", "list_date", "list_until", "count", "activated_at", "log")},
            "last_check": db.kv_get("ibf_last_check"), "page_url": config.IBF_PAGE_URL,
            "running": state["running"], "step": state["step"], "last_error": state["last_error"]}
