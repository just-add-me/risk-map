#!/usr/bin/env bash
# Первичная установка на чистый Ubuntu 22.04/24.04.
# Запуск из папки проекта:  sudo bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

echo "==> Файрвол (SSH, HTTP, HTTPS)"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi

if [ ! -f .env ]; then
  echo "==> Создаю .env"
  IP="$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)"
  DOMAIN="${IP//./-}.sslip.io"
  PASS="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)"
  SECRET="$(openssl rand -hex 32)"
  sed -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
      -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${PASS}|" \
      -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" \
      .env.example > .env
  chmod 600 .env
  CREATED=1
fi

mkdir -p data
echo "==> Сборка и запуск (первый раз 2–4 минуты)"
docker compose up -d --build

DOMAIN="$(grep -E '^DOMAIN=' .env | cut -d= -f2)"
echo
echo "Готово. Откройте: https://${DOMAIN}"
if [ "${CREATED:-0}" = "1" ]; then
  echo "Логин: admin"
  echo "Пароль: ${PASS}   (сохраните его; он также лежит в файле .env)"
fi
echo "Первое построение зон по JWLA-035 займёт 1–3 минуты — прогресс виден на карте."
echo "Логи: docker compose logs -f app"
