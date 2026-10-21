"""Самопроверка построенных зон на известных точках.

Запуск на сервере:  docker compose exec app python -m app.geo.selftest
"""
from __future__ import annotations

import sys

from .. import zones

# (описание, lon, lat, фрагмент id зоны или None — «вне всех зон»)
CHECKS = [
    ("Красное море, центр", 38.5, 20.0, "indian-ocean"),
    ("Аденский залив", 47.0, 12.5, "indian-ocean"),
    ("Ормузский пролив", 56.4, 26.5, "indian-ocean"),
    ("Порт-Саид (Египет не в списке)", 32.3, 31.3, None),
    ("Чёрное море, центр", 34.0, 43.0, "europe-black-sea"),
    ("Босфор, север (терр. воды Турции)", 29.12, 41.26, None),
    ("Констанца (терр. воды Румынии)", 28.70, 44.15, None),
    ("Одесса", 30.75, 46.48, "europe"),
    ("Новороссийск (Россия)", 37.80, 44.70, "russia"),
    ("Владивосток (Россия целиком в списке)", 131.90, 43.10, "russia"),
    ("Рейд Лагоса", 3.35, 6.25, "gulf-of-guinea"),
    ("Абиджан", -4.0, 5.2, None),
    ("Сингапур", 103.8, 1.2, None),
    ("Терминал ABOT (Ирак)", 48.81, 29.68, "iraq"),
    ("Пемба, Кабу-Делгаду", 40.55, -12.95, "cabo-delgado"),
    ("Мумбаи", 72.8, 18.9, None),
    ("Шельф Гайаны (Stabroek)", -57.5, 8.0, "guyana"),
]

# Зоны IBF/ITF и INL: (описание, lon, lat, слой, фрагмент id или None — «вне зон этого слоя»)
LAYER_CHECKS = [
    ("IBF: Аденский залив", 47.0, 12.5, "ibf", "red-sea-aden"),
    ("IBF: юг Красного моря", 42.0, 14.5, "ibf", "red-sea-aden"),
    ("IBF: Красное море севернее 16°24′N", 38.5, 20.0, "ibf", None),
    ("IBF: Азовское море", 37.0, 46.3, "ibf", "azov"),
    ("IBF: рейд Одессы", 30.9, 46.35, "ibf", "northern-black-sea"),
    ("IBF: южнее Крыма (HRA)", 34.0, 44.3, "ibf", "black-sea-hra"),
    ("IBF: Чёрное море у Турции", 34.0, 42.2, "ibf", None),
    ("IBF: Ормузский пролив", 56.4, 26.5, "ibf", "persian-gulf"),
    ("IBF: Оманский залив южнее 24°N", 59.4, 23.4, "ibf", "gulf-of-oman"),
    ("IBF: 7 миль от берега Ливана", 35.2, 33.5, "ibf", "israel-lebanon-12nm"),
    ("IBF: Сингапур", 103.8, 1.2, "ibf", None),
    ("INL: Гренландское море 75°N", 0.0, 75.0, "inl", "inl-1a"),
    ("INL: Ботнический залив, север", 22.5, 64.5, "inl", "inl-3a"),
    ("INL: озеро Верхнее", -87.5, 47.5, "inl", "inl-5d"),
    ("INL: пролив Дрейка (разрешённый треугольник)", -64.0, -55.5, "inl", None),
    ("INL: море Уэдделла", -45.0, -65.0, "inl", "inl-7"),
    ("INL: Северное море", 3.0, 56.0, "inl", None),
]


def main() -> int:
    zones.load_active()
    idx = zones.index()
    if not idx.zones:
        print("Зоны не загружены — дождитесь построения (вкладка «Циркуляры»)")
        return 2
    jwc = [z for z in idx.zones if z.props.get("layer", "jwc") == "jwc"]
    print(f"Зоны JWC {zones.active_ref()}: {len(jwc)} шт.\n")
    failed = 0
    for title, lon, lat, expect in CHECKS:
        inside = [z.id for z in jwc if idx.contains(z, lon, lat)]
        ok = (not inside) if expect is None else any(expect in z for z in inside)
        failed += not ok
        print(f"{'✓' if ok else '✗'} {title:<40} → {', '.join(inside) or 'вне зон'}")
    total = len(CHECKS)
    for layer in ("ibf", "inl"):
        layer_zones = [z for z in idx.zones if z.props.get("layer") == layer]
        print(f"\nСлой {layer.upper()}: {len(layer_zones)} зон")
        if not layer_zones:
            print("  слой ещё не построен — проверки пропущены")
            continue
        for title, lon, lat, lay, expect in LAYER_CHECKS:
            if lay != layer:
                continue
            total += 1
            inside = [z.id for z in layer_zones if idx.contains(z, lon, lat)]
            ok = (not inside) if expect is None else any(expect in z for z in inside)
            failed += not ok
            print(f"{'✓' if ok else '✗'} {title:<40} → {', '.join(inside) or 'вне зон'}")
    print(f"\nОшибок: {failed} из {total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
