"""Удаляет отдельную функцию из кода: её файлы и всё, что помечено её меткой.

    python3 tools/remove_feature.py журнал-заходов
    python3 tools/remove_feature.py подозрительное-поведение

Метка в коде — «[имя-функции]»: строка с меткой удаляется целиком, а строки между
«[имя-функции] начало» и «[имя-функции] конец» — вместе с этими строками. Данные в базе
(таблицы функции) не трогаются — их можно удалить отдельно, см. DROP_SQL ниже.
После удаления: python -m pytest -q tests, коммит, bash deploy/update.sh.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEATURES = {
    "журнал-заходов": {"files": ["app/zone_log.py"],
                       "drop_sql": "DROP TABLE IF EXISTS zone_visits; DELETE FROM rules WHERE kind='visit_undeclared';"},
    "подозрительное-поведение": {"files": ["app/anomalies.py"],
                                 "drop_sql": "DROP TABLE IF EXISTS anomaly_state; "
                                             "DELETE FROM incidents WHERE kind IN ('dark','sts','spoof','identity');"},
}


def strip(text: str, tag: str) -> str:
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if f"{tag} начало" in line:
            skip = True
            continue
        if f"{tag} конец" in line:
            skip = False
            continue
        if skip or tag in line:
            continue
        out.append(line)
    return "".join(out)


def main(name: str) -> None:
    if name not in FEATURES:
        sys.exit(f"Нет такой функции. Есть: {', '.join(FEATURES)}")
    tag = f"[{name}]"
    for f in FEATURES[name]["files"]:
        p = ROOT / f
        if p.exists():
            p.unlink()
            print("удалён файл", f)
    for p in [*ROOT.glob("app/**/*.py"), *ROOT.glob("static/*.*"), *ROOT.glob("tests/*.py")]:
        text = p.read_text(encoding="utf-8")
        if tag in text:
            p.write_text(strip(text, tag), encoding="utf-8")
            print("очищен", p.relative_to(ROOT))
    print(f"Готово. Данные в базе остались; удалить их можно так:\n  {FEATURES[name]['drop_sql']}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
