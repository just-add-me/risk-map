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


def main() -> int:
    zones.load_active()
    idx = zones.index()
    if not idx.zones:
        print("Зоны не загружены — дождитесь построения (вкладка «Циркуляры»)")
        return 2
    print(f"Зоны {zones.active_ref()}: {len(idx.zones)} шт.\n")
    failed = 0
    for title, lon, lat, expect in CHECKS:
        inside = [z.id for z in idx.zones if idx.contains(z, lon, lat)]
        ok = (not inside) if expect is None else any(expect in z for z in inside)
        failed += not ok
        print(f"{'✓' if ok else '✗'} {title:<40} → {', '.join(inside) or 'вне зон'}")
    print(f"\nОшибок: {failed} из {len(CHECKS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
