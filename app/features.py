"""Переключатели дополнительных функций: включаются и выключаются на сайте (кнопка «⚙ Функции»
в шапке, только администратор). Выключенная функция скрыта в интерфейсе и не шлёт уведомлений;
накопленные данные не удаляются — после включения всё на месте.
"""
from __future__ import annotations

from . import db

FEATURES = {
    "zone_log": {
        "label": "Журнал заходов в зоны",
        "hint": "Вкладка «Журнал»: когда судно вошло в зону и вышло, сколько суток пробыло, заявлен ли "
                "заход страховщику (для дополнительной премии). Выгрузка в Excel, напоминание о незаявленном заходе.",
    },
    "anomalies": {
        "label": "Подозрительное поведение судов",
        "hint": "Пропадание AIS там, где другие суда видны; встречи с другим судном в открытом море; "
                "скачки позиции; смена названия или MMSI. Только для судов флотов.",
    },
}


def enabled(name: str) -> bool:
    row = db.query_one("SELECT value FROM kv WHERE key=?", (f"feature:{name}",))
    return row is None or row["value"] != "0"


def set_enabled(name: str, on: bool) -> None:
    if name not in FEATURES:
        raise ValueError("Нет такой функции")
    db.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (f"feature:{name}", "1" if on else "0"))


def all_states() -> dict:
    return {k: {**v, "on": enabled(k)} for k, v in FEATURES.items()}
