# Гайд: переезд CF на VPS (Beget) — по шагам

Дата: 2026-07-23. Основа: спека
[2026-07-22-vps-autonomy-design.md](superpowers/specs/2026-07-22-vps-autonomy-design.md)
(порядок миграции — её §6) + практики из урока «VPS: агентная система в облаке»
(SSH-ключи, Remote-SSH, tmux, claude CLI).

**Главное правило: n8n живёт до конца приёмки.** Он — текущий прод и эталон
для сравнения. CF разворачивается на том же VPS *рядом* с ним. Снос n8n —
последний шаг, после недели наблюдения.

Роли в каждом шаге помечены: **[оператор]** — делаешь руками ты,
**[Claude]** — делаю я (код, конфиги, тесты), **[вместе]** — я готовлю
команды/файлы, ты выполняешь на сервере.

---

## Картина целиком

```
Этап 0. Доступ к VPS: SSH-ключ, Remote-SSH, проверка ресурсов   [оператор]
Этап 1. Apify-токен в ~/.cf/secrets/                            [оператор]  ← единственный блокер сейчас
Этап 2. Порт сбора в Python на ноутбуке, pytest зелёный         [Claude]
Этап 3. Деплой на VPS рядом с живым n8n (таймеры выключены)     [вместе]
Этап 4. Приёмка: параллельный прогон, diff с n8n                [вместе]
Этап 5. Переключение: таймеры CF вкл, расписания n8n выкл,
        неделя наблюдения (n8n выключен, но не снесён)          [вместе]
Этап 6. Снос n8n + чистка репо и ноутбука                       [вместе]
```

Отличие от урока: там агентка живёт в tmux-сессии. У нас рантайм CF —
**systemd** (сервис дашборда + таймеры): переживает ребут сервера, догоняет
пропущенные запуски (`Persistent=true`). tmux используем только для ручных
сессий claude на сервере.

---

## Этап 0. Доступ к VPS — [оператор]

Сервер уже есть (тот, где n8n — `n8n.example.com`). Нужно: вход по ключу,
удобное подключение из VS Code, проверка ресурсов.

### 0.1 Первый вход по паролю

```powershell
ssh root@<VPS_HOST>
```

Пароль — из письма Beget (или уже сохранённый).

### 0.2 SSH-ключ вместо пароля

На ноутбуке (Windows, PowerShell; ssh-keygen встроен в Windows 10+):

```powershell
ssh-keygen -t ed25519 -C "operator@laptop"
# на вопросы — Enter (путь по умолчанию ~\.ssh\id_ed25519)
```

`ssh-copy-id` на Windows нет — копируем ключ вручную (пароль спросит в
последний раз):

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@<VPS_HOST> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Проверка: `ssh root@<VPS_HOST>` — должен пустить без пароля.

### 0.3 Запись в ~/.ssh/config

Файл `C:\Users\<user>\.ssh\config` (создать, если нет):

```
Host cf-vps
    HostName <VPS_HOST>
    User root
    IdentityFile ~/.ssh/id_ed25519
```

Дальше везде просто `ssh cf-vps`. (`ControlMaster` из урока — фича
Linux/macOS-клиента, Windows OpenSSH её не умеет; пропускаем, Remote-SSH
работает и без неё.)

### 0.4 VS Code Remote-SSH

1. Расширение **Remote-SSH** (Microsoft).
2. `F1` → *Remote-SSH: Connect to Host* → `cf-vps`.
3. Открыть папку сервера — редактор и терминал работают прямо на VPS.

### 0.5 Отключить вход по паролю

Когда ключ проверен (из урока — закрывает подбор пароля ботами). На сервере
в `/etc/ssh/sshd_config`:

```
PasswordAuthentication no
```

затем `systemctl restart ssh`. **Сначала убедись, что вход по ключу работает
из второго окна терминала**, иначе можно запереть себя снаружи.

### 0.6 Проверка ресурсов

claude CLI требует минимум 4 ГБ RAM (метрика из урока), а рядом ещё n8n и
дашборд:

```bash
free -h && df -h / && nproc
```

Если меньше 4 ГБ — поднять тариф в панели Beget до этапа 3 (по уроку
комфорт — 4 ядра / 8 ГБ; память и ядра на Beget меняются без пересоздания).

---

## Этап 1. Apify-токен — [оператор] ← делается прямо сейчас

Единственный оставшийся блокер этапа «снять с живого n8n» (API n8n секреты
credential'ов не отдаёт — проверено):

1. [console.apify.com](https://console.apify.com) → Settings → API tokens →
   скопировать (или выпустить новый).
2. Сохранить в менеджер паролей.
3. Положить на сервер:

```bash
mkdir -p ~/.cf/secrets && chmod 700 ~/.cf/secrets
# вставить токен:
cat > ~/.cf/secrets/apify-token.txt
# (вставить, Enter, Ctrl+D)
chmod 600 ~/.cf/secrets/apify-token.txt
```

Ротация ключей — не блокер (решение 2026-07-22: репы приватные, продукт
внутренний). Сюда же позже лягут `service-account.json` и Telegram-токен
(этап 3).

**Как только токен на месте — скажи мне, и я стартую этап 2.**

---

## Этап 2. Порт сбора в Python — [Claude], на ноутбуке

Спека §2. Делается через writing-plans → план реализации → выполнение.
Коротко, что появится:

- пакет `src/cf/collect`: `tiktok.py`, `instagram.py`, `snowball.py`,
  `performance.py` + общие `gate.py`, `normalize.py`, `coalesce.py`,
  `subtitles.py`, `apify.py`; CLI `cf collect <стадия>`;
- логика 1:1 с `n8n/*/code/*.js`; ~1400 строк JS-тестов переезжают в pytest —
  зелёный pytest = критерий готовности порта;
- Apify-клиент start + poll (вместо run-sync): снимает лимит 300 с, батчи
  параллельно; ретраи поверх `retry.py`;
- sheets-upsert по `raw_id` + **coalesce P1.16 сразу** (обязателен до первого
  боевого snowball);
- реестр `sources/` + exploration-квота (спека §2.1);
- дашборд: звено raw запускает `python -m cf collect ...` subprocess'ом,
  контур вебхуков/X-CF-Token удаляется.

Твоё участие: ревью плана реализации и финальное ревью спеки (она ждёт
approve). Смоук в конце: `cf collect tiktok --dry-run` (1 батч, без записи
в боевой лист).

---

## Этап 3. Деплой на VPS рядом с n8n — [вместе]

Всё ставится, но **таймеры остаются выключенными** — боевой сбор продолжает
гонять n8n.

### 3.1 Репо и окружение

Deploy-ключ GitHub (серверу — право push: коммиты раннера и кнопок решений
идут с сервера):

```bash
ssh-keygen -t ed25519 -C "cf-vps-deploy" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub
# → GitHub: repo content-factory → Settings → Deploy keys → Add (галка Allow write access)
```

```bash
git clone git@github.com:<owner>/content-factory.git /srv/cf
cd /srv/cf && python3 -m venv .venv && .venv/bin/pip install -e .
```

### 3.2 Секреты

В `~/.cf/secrets/`: `apify-token.txt` (уже есть с этапа 1),
`service-account.json` (гугл-таблицы — скопировать с ноутбука через scp),
`telegram-token.txt` + chat_id (бот создаётся у @BotFather — [оператор]).

### 3.3 systemd-сервис дашборда

`/etc/systemd/system/cf-dashboard.service` (файл подготовлю я):

```ini
[Unit]
Description=CF Dashboard
After=network-online.target

[Service]
WorkingDirectory=/srv/cf
ExecStart=/srv/cf/.venv/bin/uvicorn cf.dashboard.app:app --host 127.0.0.1 --port 8787
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now cf-dashboard
```

Наружу дашборд не торчит — только localhost, снаружи его отдаёт Caddy.

### 3.4 Домен и Caddy

1. [оператор] DNS: A-запись `cf.example.com` → IP сервера (там же, где
   заведена `n8n.example.com`).
2. Caddy (reverse proxy, HTTPS от Let's Encrypt автоматом, вход по паролю):

```bash
apt install caddy
caddy hash-password   # ввести пароль дашборда → получить хэш
```

`/etc/caddy/Caddyfile`:

```
cf.example.com {
    basic_auth {
        operator <хэш>
    }
    reverse_proxy 127.0.0.1:8787
}
```

`systemctl reload caddy` → дашборд доступен с любого устройства по
`https://cf.example.com` с паролем.

### 3.5 claude CLI на сервере

Как в уроке:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
claude   # первый запуск: покажет ссылку + код
```

Ссылку открыть в браузере на ноутбуке, войти в аккаунт подписки, код вставить
обратно в терминал. Логин долгоживущий (хранится в `~/.claude`); ротация —
операторская процедура, попадёт в RUNBOOK. Для ручных сессий агента на
сервере — tmux:

```bash
tmux new -s cf     # работать; отцепиться: Ctrl+b, затем d
tmux attach -t cf  # вернуться (хоть с другого устройства)
```

### 3.6 Таймеры и deploy.sh

Я готовлю systemd-таймеры по расписанию спеки §3 (бэкап 07:00, цикл 07:30,
сбор 08:00/08:20/08:40, вс: архив, snowball, source-tuner; все
`Persistent=true`) — ставим их, но **не включаем**. Плюс `deploy.sh`:
`git pull` + restart сервиса — весь деплой одной командой.

---

## Этап 4. Приёмка — [вместе], n8n ещё живой

Спека §6 п.4 — ради этого n8n и не сносим раньше времени:

1. **Параллельный прогон**: Python (`cf collect ...` руками) и n8n собирают
   одни и те же источники → diff строк листа. Расхождения разбираем до нуля
   объяснимых.
2. **Битый батч**: искусственно ломаем один батч (несуществующий хэштег /
   таймаут) → остальные батчи доезжают, потери корректно видны в CF Run Log.
3. **Особое внимание Instagram**: контракт reel-scraper кладёт reel-URL в
   поле `username`; формы ошибок акторов известны только по живым ранам —
   сверяем именно на живых.

Инструментарий (готов 2026-07-24): `cf collect <stage> --dry-run --batches 0
--no-explore` — полный прогон без записи в Sheets и без exploration-батча
(состав источников 1:1 с n8n), `cf collect-diff <stage>` — дифф dry-run
JSONL против строк n8n за день (человекочитаемая сводка + JSON-отчёт в
`agent-runtime/collect/`). Боевые времена n8n по штампам листа: tiktok
~04:03 UTC, instagram ~04:22 UTC (его «08:00» — таймзона UTC+4).

Критерии зелёные → этап 5.

---

## Этап 5. Переключение — [вместе]

1. Включаем systemd-таймеры CF.
2. Выключаем расписания в n8n (воркфлоу деактивировать, **не удалять**).
3. **Неделя наблюдения**: n8n стоит выключенный как фолбэк, дашборд на
   ноутбуке тоже пока работает как фолбэк. Смотрим Telegram-уведомления,
   CF Run Log, heartbeat таймеров.
4. Инцидент → откат за минуты: таймеры выкл, воркфлоу n8n вкл.

---

## Этап 6. Снос n8n и чистка — [вместе], только после недели без инцидентов

На сервере: остановить/удалить n8n, убрать его домен из прокси. В репо
(спека §6 п.6): `n8napi.py`, `n8nsync.py`, `cmd_pull_n8n`/`cmd_push_n8n`,
`check_n8n` в health (заменить чеком Apify), блоки `n8n` и
`dashboard.workflows` в `cf.config.json`, каталог `n8n/` (после порта тестов),
`test_n8napi.py`/`test_n8nsync.py`; RUNBOOK правится,
`docs/n8n-integration.md` — в архив. На ноутбуке: упразднить schtasks-задания
CF Morning Cycle/Backup/Archive. Устаревают секреты n8n-api-key и
webhook-token.

---

## Чек-лист оператора (минимум ручных действий)

- [x] 0: доступ к VPS есть (2026-07-23; примечание: VPS взят другой, не Beget —
      <VPS_IP>, репо живёт на нём в ~/projects/CF, n8n остался удалённым)
- [x] 1: Apify-токен в `~/.cf/secrets/apify-token.txt` (2026-07-23)
- [x] 2: approve спеки и плана реализации порта (2026-07-23, план docs/plans/2026-07-23-collect-port.md)
- [x] 3: DNS-запись `cf.example.com` → <VPS_IP>; Telegram-бот <BOT_USERNAME>
      (e2e через cf.notify подтверждён); deploy-ключ в GitHub (origin на ssh,
      master запушен); claude CLI на VPS работает; `deploy/install.sh` выполнен —
      дашборд на https://cf.example.com за basic auth, таймеры выключены
      (2026-07-24)
- [x] 4: приёмка выполнена 2026-07-24, оператор принял (акт
  docs/stage4-acceptance-2026-07-24.md: точные поля 100%, битый батч
  деградирует как n8n, IG-транскрипты — ASR-недетерминизм актора)
- [x] 5: переключение 2026-07-24 ~03:00 EEST — 8 таймеров CF включены
  (tiktok 08:00, IG 08:20, performance 08:40, вс: архив 07:10, снежок 09:00,
  tune-sources 10:00), расписания n8n деактивированы (CF 04 оставлен
  фолбэком дашборда). Неделя наблюдения — до ~2026-07-31; откат:
  таймеры disable + N8nApi.set_active(id, True)
- [ ] 6: команда на снос n8n
