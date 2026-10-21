#!/usr/bin/env bash
# Обновление до последней версии из GitHub:  bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
docker compose ps
