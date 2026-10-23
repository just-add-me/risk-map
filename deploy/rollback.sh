#!/usr/bin/env bash
# Откат кода сайта к метке или коммиту:  bash deploy/rollback.sh pre-ux-2026-10-23
#
# Делает новый коммит «Откат к …», который возвращает все файлы проекта к состоянию метки
# (история не теряется — откат тоже можно отменить), отправляет его в GitHub и обновляет сайт.
# Данные (папка data/: база, справочник судов) не трогаются. Копия базы на момент метки, если
# понадобится, лежит рядом: data/app.db.<метка>.
#
# Откатить только одну доработку, а не всё:  git revert <коммит>, затем git push и bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
target="${1:?Укажите метку или коммит, например: bash deploy/rollback.sh pre-ux-2026-10-23}"
git rev-parse --verify --quiet "$target^{commit}" >/dev/null || { echo "Нет такой метки или коммита: $target"; exit 1; }
git pull --ff-only
if git diff --quiet "$target" HEAD; then echo "Код уже совпадает с $target — откатывать нечего"; exit 0; fi
echo "Будут возвращены к состоянию $target файлы:"; git diff --stat "$target" HEAD | tail -n 15
read -r -p "Откатить? [y/N] " ok; [[ "$ok" == [yYдД]* ]] || { echo "Отменено"; exit 1; }
# дерево файлов — ровно как в метке (файлы, появившиеся позже, уйдут), поверх текущей истории
git rm -r -q --cached .
git checkout "$target" -- .
git checkout HEAD -- deploy/rollback.sh          # сам скрипт отката остаётся — чтобы можно было вернуться обратно
git clean -fdq -e data/ -e .env
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-deploy}" -c user.email="${GIT_AUTHOR_EMAIL:-deploy@localhost}" \
  commit -q -m "Откат к $target"
git push -q origin HEAD
docker compose up -d --build
docker compose ps
echo "Готово: сайт работает на коде $target. Вернуть как было: bash deploy/rollback.sh $(git rev-parse --short HEAD~1)"
