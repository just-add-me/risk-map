"""International Navigating Limits (INL 01/11/03, бывш. Institute Warranty Limits, IWL).

Районы, куда страховка корпуса не пускает судно без согласования со страховщиком
и доплаты премии: полярные воды и районы со льдом, часть из них — только в холодный сезон.
Редакция INL 01/11/03 действует с 2003 года и меняется крайне редко, поэтому слой
встроен в код и строится один раз; при изменении описаний ниже поднимите VERSION.

Границы — по тексту INL (вторичный источник: Gard / Maritime Optima). Районы, заданные
названием моря без координат, нарисованы приблизительно и помечены «≈».
"""
from __future__ import annotations

import logging

from .. import db, zones
from . import shapes as s

log = logging.getLogger(__name__)

VERSION = "INL-2003-v1"

# Линия, отделяющая Охотское море от Тихого океана: Курилы и Камчатка (lon, lat)
KURIL_KAMCHATKA = [(150.3, 46.0), (152.0, 47.3), (154.5, 49.0), (156.4, 50.7), (156.7, 51.0), (156.5, 52.5),
                   (156.8, 55.0), (157.8, 57.0), (159.5, 58.5), (162.8, 60.0), (166.0, 61.5), (166.5, 63.0)]
WEST_OF_KAMCHATKA = s.Polygon([(130.0, 46.0), *KURIL_KAMCHATKA, (130.0, 63.0)])


def _arctic(w):
    return s.water(w, s.box(-180, 70, 180, 90))


def _barents(w):
    return s.water(w, s.box(25.8, 67.5, 58.5, 70)).difference(s.box(31.5, 63.5, 44.5, 66.9))


def _white_sea(w):
    return s.part_with(s.water(w, s.box(31.5, 63.5, 44.5, 66.9)), 37.5, 65.5)


def _chukchi(w):
    return s.water(w, s.box(-180, 65.75, -156, 70))


def _bothnia_north(w):
    return s.water(w, s.Polygon([(16, 63.83), (20.26, 63.83), (21.6, 63.1), (26.5, 63.1), (26.5, 66.5), (16, 66.5)]))


def _finland_east(w):
    return s.part_with(s.water(w, s.box(28.75, 59.3, 31.0, 61.0)), 29.3, 60.05)


def _north_of_5924(w):
    return s.part_with(s.water(w, s.box(16.0, 59.4, 31.0, 66.5)), 20.0, 61.0)


def _riga(w):
    return s.part_with(s.water(w, s.box(22.0, 56.8, 25.0, 59.0)), 23.5, 57.6)


def _greenland(w):
    return w.sea(w.tw(["GRL"]))


def _na_north(w):
    return s.water(w, s.box(-100, 52.1667, -50, 70))


def _st_lawrence_gulf(w):
    ring = [(-69.42, 46.5), (-69.42, 50.5), (-55.37, 52.2), (-55.37, 51.3), (-59.30, 47.62), (-60.40, 47.04),
            (-61.42, 45.64), (-66.5, 45.6)]
    return s.water(w, s.Polygon(ring))


def _st_lawrence_river(w):
    ring = [(-69.42, 48.6), (-69.42, 47.6), (-71.2, 46.7), (-73.6, 45.4), (-74.4, 45.1), (-74.4, 45.7),
            (-71.5, 47.1), (-70.0, 48.6)]
    return s.water(w, s.Polygon(ring))


def _great_lakes(w):
    return s.lakes(["Lake Superior", "Lake Michigan", "Lake Huron", "Lake Erie", "Lake Ontario", "Lake Saint Clair"])


def _na_west(w):
    return s.water(w, s.box(-170, 54.5, -100, 70))


def _islands_bc_aleutians(w):
    return s.unary_union([
        s.coastal_band(w, (-133.3, 51.8, -130.9, 54.3), 12),     # Queen Charlotte (Haida Gwaii)
        s.coastal_band(w, (172.0, 51.0, 180.0, 53.5), 12),       # Алеутские, западная часть
        s.coastal_band(w, (-180.0, 51.0, -164.3, 55.0), 12),     # Алеутские, восточная часть
    ])


def _southern_ocean(w):
    triangle = s.Polygon([(-50, -50), (-67.5, -57), (-160, -50)])
    return s.water(w, s.box(-180, -90, 180, -50).difference(triangle))


def _kerguelen_crozet(w):
    return s.unary_union([s.coastal_band(w, (68.4, -50.1, 70.7, -48.4), 12),
                          s.coastal_band(w, (50.0, -46.6, 52.6, -45.8), 12)])


def _okhotsk_ne(w):
    return s.water(w, s.box(140, 55, 170, 63)).intersection(WEST_OF_KAMCHATKA)


def _okhotsk_nw(w):
    return s.water(w, s.box(130, 53, 140, 60))


def _east_asia_46(w):
    return s.water(w, s.box(130, 46, 170, 63)).intersection(WEST_OF_KAMCHATKA)


def _bering(w):
    # долготы > 180 — продолжение за линией перемены дат; режем на две половины
    ring = [(162.5, 56.0), (166.5, 55.3), (172.5, 53.0), (180.0, 51.6), (184.0, 51.6), (190.0, 52.6),
            (194.0, 53.7), (195.6, 54.5), (197.0, 55.0), (200.0, 55.5), (203.0, 57.0), (203.0, 65.75),
            (162.5, 65.75)]
    halves = s.split_antimeridian(s.Polygon(ring))
    return s.unary_union([s.water(w, g) for g in getattr(halves, "geoms", [halves])])


# (id, название, функция, сезон или None, приблизительно?, пояснение)
AREAS = [
    ("1a", "Арктика: севернее 70°N", _arctic, None, False, ""),
    ("1b", "Баренцево море", _barents, None, True,
     "Разрешены заходы в Кольский залив, Мурманск и порты Норвегии, если судно не идёт севернее 72°30′N "
     "и восточнее 35°E."),
    ("2a", "Белое море", _white_sea, None, True, ""),
    ("2b", "Чукотское море", _chukchi, None, True, ""),
    ("3a", "Ботнический залив севернее линии Умео–Вааса", _bothnia_north, ("12-10", "05-25"), False, ""),
    ("3b", "Финский залив восточнее 28°45′E", _finland_east, ("12-15", "05-15"), False,
     "Сезонно — для судов до 90 000 т дедвейта; суда крупнее 90 000 т — круглый год."),
    ("3c", "Балтика севернее 59°24′N", _north_of_5924, ("01-08", "05-05"), False,
     "Ботнический и Финский заливы и прилегающие воды. Заходы в Стокгольм, Таллин и Хельсинки разрешены."),
    ("3d", "Рижский залив (восточнее 22°E, южнее 59°N)", _riga, ("12-28", "05-05"), True, ""),
    ("4", "Территориальные воды Гренландии", _greenland, None, False, ""),
    ("5a", "Северная Америка: севернее 52°10′N, между 50°W и 100°W", _na_north, None, False, ""),
    ("5b", "Залив Св. Лаврентия, проливы Белл-Айл, Кабота и Кансо", _st_lawrence_gulf, ("12-21", "04-30"), True,
     "Залив и река Св. Лаврентия восточнее Ле-Эскумен, пролив Белл-Айл западнее о. Белл-Айл, "
     "пролив Кабота западнее линии мыс Рей — мыс Норт, пролив Кансо севернее дамбы."),
    ("5c", "Река Св. Лаврентия западнее Ле-Эскумен", _st_lawrence_river, ("12-01", "04-30"), True, ""),
    ("5d", "Великие озёра и морской путь Св. Лаврентия", _great_lakes, None, True,
     "Исключены круглый год. Сам морской путь (Seaway) на карте не показан, только озёра."),
    ("6a", "Северная Америка: севернее 54°30′N, между 100°W и 170°W", _na_west, None, False, ""),
    ("6b", "Острова Королевы Шарлотты и Алеутские острова", _islands_bc_aleutians, None, True,
     "Любые порты и места на островах; на карте — 12 миль от берега."),
    ("7", "Южный океан: южнее 50°S", _southern_ocean, None, False,
     "Кроме треугольника 50°S 50°W — 57°S 67°30′W — 50°S 160°W."),
    ("8", "Территориальные воды Кергелен и Крозе", _kerguelen_crozet, None, True, ""),
    ("9a", "Охотское море: севернее 55°N, восточнее 140°E", _okhotsk_ne, ("11-01", "06-01"), True, ""),
    ("9b", "Охотское море: севернее 53°N, западнее 140°E", _okhotsk_nw, ("11-01", "06-01"), True, ""),
    ("9c", "Восточная Азия: севернее 46°N, западнее Курил и Камчатки", _east_asia_46, ("12-01", "05-01"), True, ""),
    ("10", "Берингово море", _bering, None, True,
     "Разрешён только транзит: не севернее 54°30′N, вход и выход западнее о. Булдырь или через проливы "
     "Амчитка, Амукта, Унимак; два радара, GPS, GMDSS, гирокомпас, актуальные карты и лоции."),
]

MONTHS_RU = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def season_text(season: tuple[str, str]) -> str:
    def f(md: str) -> str:
        m, d = md.split("-")
        return f"{int(d)} {MONTHS_RU[int(m) - 1]}"
    return f"с {f(season[0])} по {f(season[1])}"


def build(world=None) -> tuple[dict, list[str]]:
    from ..geo.builder import World
    w = world or World()
    features, logs = [], []
    for code, title, fn, season, approx, note in AREAS:
        props = {
            "id": f"inl-{code}", "name": f"INL {code}: {title}", "layer": "inl",
            "region": "INL (IWL) — навигационные ограничения", "kind": "inl",
            "category": "seasonal" if season else "permanent", "severity": "full",
            "season": {"from": season[0], "to": season[1], "text": season_text(season)} if season else None,
            "approximate": approx, "note": note, "needs_review": False, "review_reason": "",
            "change": "unchanged", "text": "",
        }
        try:
            feat = s.feature(props, fn(w), tolerance=0.01)
            if feat:
                features.append(feat)
                logs.append(f"✓ {props['name']}")
            else:
                logs.append(f"⚠ {props['name']}: пустая геометрия")
        except Exception as exc:  # noqa: BLE001
            log.exception("INL: ошибка построения %s", code)
            logs.append(f"✖ {props['name']}: {exc}")
    fc = {"type": "FeatureCollection", "features": features,
          "properties": {"version": VERSION, "built_at": db.now_iso()}}
    return fc, logs


def ensure_built() -> None:
    """Строит слой при первом запуске или после изменения описаний (VERSION)."""
    current = ((zones.layer("inl") or {}).get("properties") or {}).get("version")
    if current == VERSION:
        return
    log.info("Строю слой INL (%s)", VERSION)
    fc, logs = build()
    zones.set_layer("inl", fc)
    db.kv_set("inl_log", logs)
    log.info("INL: %d районов", len(fc["features"]))
