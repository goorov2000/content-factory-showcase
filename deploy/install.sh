#!/usr/bin/env bash
# Этап 3 миграции (docs/vps-migration-guide.md): установка CF на VPS.
# Запуск: sudo bash deploy/install.sh
# Ставит Caddy (официальный репозиторий), рендерит Caddyfile с bcrypt-хэшем
# пароля дашборда, устанавливает systemd-юниты. Дашборд включается сразу;
# ТАЙМЕРЫ ТОЛЬКО КОПИРУЮТСЯ и остаются выключенными до этапа 5 —
# боевой сбор продолжает гонять n8n.
set -euo pipefail
REPO=/home/<user>/projects/CF
PASS_FILE=/home/<user>/.cf/secrets/dashboard-password.txt

[ "$(id -u)" = 0 ] || { echo "нужен sudo: sudo bash deploy/install.sh"; exit 1; }
[ -f "$PASS_FILE" ] || { echo "нет $PASS_FILE — сгенерируй пароль дашборда"; exit 1; }

# 1. Caddy
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy
fi

# 2. Caddyfile: bcrypt-хэш пароля (plaintext никуда не копируется)
HASH=$(caddy hash-password --plaintext "$(cat "$PASS_FILE")")
sed "s|__HASH__|$HASH|" "$REPO/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl restart caddy

# 3. systemd: дашборд — enable+start; таймеры — только файлы (этап 5)
cp "$REPO"/deploy/systemd/*.service "$REPO"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cf-dashboard

echo
echo "Готово:"
systemctl --no-pager --lines=0 status cf-dashboard | head -3
echo
# H13 (аудит 2026-07-24): cf-collect-tiktok/instagram НЕ включаются — ежедневный
# сбор уже делает raw-звено cf-cycle; отдельные таймеры удваивали расход Apify.
echo "Таймеры установлены, но ВЫКЛЮЧЕНЫ (включение — этап 5, командой:"
echo "  sudo systemctl enable --now cf-backup.timer cf-archive.timer cf-cycle.timer \\"
echo "    cf-collect-metrika.timer cf-heartbeat.timer \\"
echo "    cf-collect-performance.timer cf-collect-snowball.timer cf-tune-sources.timer )"
# cf-heartbeat.timer обязателен: без него молчание бота ничего не значит —
# «всё хорошо» и «завод умер» выглядят в чате одинаково. cf-alert@.service
# шаблонный, включать его не нужно: его поднимает OnFailure= упавшего юнита.
echo "  (cf-collect-tiktok/instagram.timer не включать: сбор делает raw-звено cf-cycle)"
