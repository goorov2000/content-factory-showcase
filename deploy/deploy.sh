#!/usr/bin/env bash
# Обновление CF на VPS одной командой (спека §1): git pull + зависимости +
# рестарт дашборда. Запуск: bash deploy/deploy.sh (sudo спросит на рестарте).
set -euo pipefail
cd /home/<user>/projects/CF

# M12 (аудит 2026-07-24): рестарт убивает всю cgroup дашборда вместе с идущим
# циклом/фан-аутом и его claude-агентами — перед рестартом проверяем, что
# конвейер свободен (те же stage-локи, что у раннера). Обход: --force.
check_pipeline_idle() {
  [ "${1:-}" = "--force" ] && return 0
  for f in agent-runtime/locks/stage-*.lock; do
    [ -e "$f" ] || continue
    if ! flock -n "$f" true; then
      echo "конвейер занят ($f) — деплой отменён; дождитесь конца звена" >&2
      echo "или осознанно: bash deploy/deploy.sh --force" >&2
      return 1
    fi
  done
}

check_pipeline_idle "${1:-}" || exit 1

git pull --ff-only
.venv/bin/pip install -q -e .
.venv/bin/python -m pytest -q tests/test_collect_parity.py >/dev/null \
  && echo "parity ok"
# повторная проверка: pull+pip+pytest занимают минуты, за это время мог
# стартовать цикл (check-then-act окно из ревью аудита)
check_pipeline_idle "${1:-}" || exit 1
sudo systemctl restart cf-dashboard
echo "деплой завершён: $(git log --oneline -1)"
