#!/usr/bin/env bash
# Оценка мировых морских новостей ИИ [мировые-новости] — запускается по расписанию (cron) раз в час:
#   17 * * * * bash /home/deploy/risk-map/deploy/news_judge.sh >> /home/deploy/risk-map/data/news_judge.log 2>&1
#
# Берёт у сайта очередь статей, спрашивает Claude (Claude Code по подписке, самая экономная модель),
# отдаёт ответ сайту. Если Claude недоступен (кончился лимит, нет входа) — статьи ждут следующего запуска.
# Вручную: bash deploy/news_judge.sh        Что в очереди: docker compose exec app python -m app.world_news status
set -euo pipefail
cd "$(dirname "$0")/.."
CLAUDE="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
MODEL="${NEWS_MODEL:-haiku}"
exec 9>/tmp/news_judge.lock
flock -n 9 || { echo "$(date -u +%FT%TZ) уже идёт"; exit 0; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
docker compose exec -T app python -m app.world_news pending > "$work/pending.json"
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); open(sys.argv[2],"w").write(d["prompt"]); print(len(d["ids"]))' \
  "$work/pending.json" "$work/prompt.txt" > "$work/n"
n=$(cat "$work/n")
if [ "$n" = "0" ]; then echo "$(date -u +%FT%TZ) очередь пуста"; exit 0; fi

# чистая сессия: без инструментов, без настроек проекта и истории — только текст запроса
if ! (cd "$work" && "$CLAUDE" -p --model "$MODEL" --tools "" --strict-mcp-config --setting-sources "" \
      --disable-slash-commands --no-session-persistence --output-format json \
      --system-prompt "Ты отвечаешь строго в заданном формате." < prompt.txt > answer.json 2> err.txt); then
  echo "$(date -u +%FT%TZ) Claude недоступен, статей ждут: $n — $(head -c 300 "$work/err.txt" "$work/answer.json" 2>/dev/null | tr '\n' ' ')"
  exit 1
fi
python3 -I -c '
import json, sys
a = json.load(open(sys.argv[1])); p = json.load(open(sys.argv[2]))
if a.get("is_error"):
    sys.exit("Claude ответил ошибкой: " + str(a.get("result"))[:300])
json.dump({"ids": p["ids"], "answer": a.get("result") or ""}, open(sys.argv[3], "w"), ensure_ascii=False)
u = a.get("usage") or {}
print("токенов: вход", (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
      + (u.get("cache_creation_input_tokens") or 0), "ответ", u.get("output_tokens"), end=" | ")
' "$work/answer.json" "$work/pending.json" "$work/apply.json" > "$work/usage" || {
  echo "$(date -u +%FT%TZ) $(cat "$work/usage" 2>/dev/null)"; exit 1; }
res=$(docker compose exec -T app python -m app.world_news apply < "$work/apply.json")
echo "$(date -u +%FT%TZ) статей $n | $(cat "$work/usage")$res"
# журнал не разрастается: последние 2000 строк
log=data/news_judge.log
if [ -f "$log" ] && [ "$(wc -l < "$log")" -gt 2000 ]; then tail -n 1000 "$log" > "$log.tmp" && mv "$log.tmp" "$log"; fi
