# RUNBOOK восстановления CF

Одностраничный сценарий на случай «ноутбук утрачен / данные испорчены / нужно поднять
систему с нуля». **Bus-factor = один ноутбук**, а часть кредов существует только внутри
n8n — этот файл фиксирует, где лежат резервные копии всех секретов и как восстановить
работу.

> **УСТАРЕЛО (2026-07-24): прод переехал на VPS vm-mini (<VPS_IP>), Windows-схема
> ниже — история.** Быстрое восстановление на VPS:
> 1. `git clone git@github.com:<owner>/content-factory.git ~/projects/CF`
> 2. `python3 -m venv .venv && .venv/bin/pip install -e .` (+`[dev]` для тестов)
> 3. Секреты в `~/.cf/secrets/`: service-account.json, apify-token.txt,
>    telegram-token.txt, telegram-chat-id.txt, n8n-api-key.txt,
>    webhook-token.txt, dashboard-password.txt (права 600, каталог 700).
> 4. `sudo bash deploy/install.sh` — systemd-юниты + Caddy; таймеры включать
>    по deploy/install.sh (cf-collect-tiktok/instagram НЕ включать — H13).
> 5. `cf install-hooks` (pre-commit guard) и `systemctl start cf-dashboard`.
> Полный VPS-рерайт RUNBOOK — отдельной итерацией (аудит M8).

Опорные значения (не секреты, лежат в git / `cf.config.json`):

- Репозиторий (приватный): `https://github.com/<owner>/content-factory.git`
- Рабочая копия на ноутбуке: `<local-path>` (синкается Яндекс.Диском)
- n8n (self-hosted): `https://n8n.example.com`
- Google-таблица (`spreadsheet_id`): `<SPREADSHEET_ID>`
- Дашборд: `http://127.0.0.1:8787` (`dashboard.port`)

---

## 1. Секреты и их резервные копии

Каждый секрет обязан иметь **резервную копию в менеджере паролей** (не только на ноутбуке
и не только внутри n8n). Пути рабочих копий — из `cf.config.json` (`service_account_file`,
`n8n.api_key_file`, `n8n.webhook_token_file`); каталог `~/.cf/secrets/` = `%USERPROFILE%\.cf\secrets\`,
вне репозитория и вне папки синка (P0.3).

| Секрет | Где используется | Рабочая копия | Резервная копия | Ротация / перевыпуск |
|---|---|---|---|---|
| **Google service account (JSON)** | доступ CLI и n8n к Google Sheets | `~/.cf/secrets/service-account.json` (`service_account_file`) **и** n8n-credential «CF Sheets Service Account» (id `EL6SGqYTslRa3i9v`) | менеджер паролей (JSON вложением) | GCP Console → сервис-аккаунт → новый ключ; заменить файл и n8n-credential; старый ключ отозвать |
| **n8n API key** | `cf push-n8n` / `cf pull-n8n` к n8n | `~/.cf/secrets/n8n-api-key.txt` (`n8n.api_key_file`) | менеджер паролей | n8n UI → Settings → n8n API → создать новый, отозвать старый; обновить файл |
| **Webhook token (X-CF-Token)** | auth на вебхуки дашборд → n8n (P0.2) | `~/.cf/secrets/webhook-token.txt` (`n8n.webhook_token_file`) **и** n8n-credential «CF Webhook Token» (Header Auth, `X-CF-Token`) | менеджер паролей | `python -c "import secrets; print(secrets.token_hex(32))"` → обновить файл и значение credential → рестарт дашборда |
| **Telegram-бот (уведомления)** | `cf.notify` — сводки цикла/сбоев оператору (этап 3) | `~/.cf/secrets/telegram-token.txt` + `telegram-chat-id.txt` (`telegram.*` в конфиге) | менеджер паролей | @BotFather → /revoke → новый токен → обновить файл |
| **Пароль дашборда (basic auth)** | Caddy на cf.example.com (этап 3) | `~/.cf/secrets/dashboard-password.txt` (bcrypt-хэш рендерится в /etc/caddy/Caddyfile) | менеджер паролей | новый пароль в файл → `sudo bash deploy/install.sh` (перерендерит Caddyfile) |
| **GitHub deploy-ключ VPS** | git push решений раннера с сервера | `~/.ssh/id_ed25519` на VPS | менеджер паролей (приватный ключ) | GitHub → Settings → Deploy keys: удалить старый, добавить новый публичный |
| **Apify API token** | auth акторов Apify: n8n-credential (CF 01 / 01b / 04) и `cf collect` (порт сбора) | `~/.cf/secrets/apify-token.txt` (`apify.token_file`, с 2026-07-23) **и** Apify-credential в n8n (до сноса) | менеджер паролей | Apify Console → Settings → API tokens → отозвать/создать; обновить файл и n8n-credential |
| **OAuth-токен Яндекс.Метрики** | `cf collect metrika` — переходы и заказы UTM-контура из Reporting API (§6) | `~/.cf/secrets/metrika-token.txt` (`metrika.token_file`, права 600) | менеджер паролей | oauth.yandex.ru → выпустить новый токен, отозвать старый; заменить файл |

> Ротация скомпрометированных ключей — решение 2026-07-22: **не блокер** (репы приватные,
> продукт внутренний); обязательна перед выходом на b2b, до тех пор — рекомендуемая гигиена.
> Колонка «Ротация / перевыпуск» — процедуры на этот случай.

**Доступы, без которых восстановление не пройдёт** (хранить логины/2FA в менеджере паролей;
где именно лежат сейчас — **уточнить у владельца**):

- Аккаунт **GitHub** (клон приватного репозитория; логин + PAT/passkey) — уточнить у владельца.
- Логин в **n8n UI** (админ инстанса `n8n.example.com`) — уточнить у владельца.
- Доступ к **серверу/хостингу n8n** (self-hosted, env-переменные инстанса) — уточнить у владельца.
- **Google-аккаунт** — владелец таблицы `14F5S-…` и GCP-проекта сервис-аккаунта — уточнить у владельца.
- Аккаунт **Apify** (консоль, для ротации токена) — уточнить у владельца.
- Аккаунт **Яндекс.Диска** (синк ноутбука: рабочая копия репо + `agent-runtime/backups`) — уточнить у владельца.

> Сверка (приёмка P5.15): три `*_file`-ключа `cf.config.json` и четыре n8n-credential
> («CF Sheets Service Account», «CF Webhook Token», Apify-credential, n8n API key) — все в
> таблице выше. `spreadsheet_id` / `n8n.base_url` / `dashboard.port` — не секреты, лежат в git.
> `dashboard.notify_url` сейчас пуст; если задать — может содержать секретный webhook (Telegram),
> тогда добавить строкой в таблицу и в менеджер паролей.

---

## 2. Чек-лист восстановления (пустая машина → рабочая система)

Требования: Windows, Python 3.12+, git, `claude` CLI в PATH (для фан-аута).

> Клонируешь в синкуемую папку Яндекс.Диска (`<local-path>`) — **сразу выполни исключения
> `agent-runtime/` и `.git` из синхронизации** (§5, «Безопасность и секреты»), иначе синк-демон
> начнёт портить `.git`.

```powershell
# 1. Клон репозитория
git clone https://github.com/<owner>/content-factory.git CF
cd CF

# 2. Виртуальное окружение + пакет
python -m venv .venv
.venv\Scripts\pip install -e .[dev]

# 3. Секреты из менеджера паролей → ~/.cf/secrets/ (пути из cf.config.json)
#    %USERPROFILE%\.cf\secrets\service-account.json
#    %USERPROFILE%\.cf\secrets\n8n-api-key.txt
#    %USERPROFILE%\.cf\secrets\webhook-token.txt
mkdir -Force "$env:USERPROFILE\.cf\secrets"   # затем положить три файла из менеджера паролей

# 4. Санити-проверки
.venv\Scripts\python -m cf install-hooks   # pre-commit guard на секреты
.venv\Scripts\python -m pytest -q          # окружение живое (весь сьют зелёный)
.venv\Scripts\python -m cf status          # связь с Google-таблицей есть

# 5. Дашборд
.venv\Scripts\python -m cf dashboard        # http://127.0.0.1:8787
```

**Восстановление данных Google Sheets** (если вкладку испортили/затёрли). Бэкапы — датированные
JSONL в `agent-runtime/backups/` (создаёт `cf backup`, ретенция 30 дней; синкаются Яндекс.Диском):

```powershell
.venv\Scripts\python -m cf backup                                   # снять свежий снимок ПЕРЕД правками
.venv\Scripts\python -m cf restore --tab briefs --file agent-runtime\backups\2026-07-21-briefs.jsonl --yes
```

`cf restore` **перезаписывает вкладку целиком** — только точечный откат одной вкладки из её снимка,
пустой файл откат отменяет. Если бэкапов на новой машине нет (Яндекс.Диск ещё не докачал) — данные
живут в самой Google-таблице; `cf backup` снимет первый снимок.

**n8n (после восстановления кредов из §1):**

```powershell
.venv\Scripts\python -m cf push-n8n n8n\cf01-tiktok      # (и cf01-instagram, cf01b-snowball)
```

В n8n-инстансе восстановить/проверить три credential из §1 (Apify, «CF Sheets Service Account»,
«CF Webhook Token»), затем `cf push-n8n` каждого воркфлоу. Детали разводки — `docs/n8n-integration.md`.

---

## 3. Автозадания (Планировщик Windows)

Восстановить после переустановки. Дашборд должен быть **запущен** (положить в автозагрузку
`shell:startup` ярлык `python -m cf dashboard` с **рабочей папкой `<local-path>`**
— поле «Рабочая папка» ярлыка, или .cmd с `cd /d <local-path>`; иначе `load_config`
не найдёт `cf.config.json`), иначе POST `/cycle/run` некуда слать.

Команды PowerShell (каждая — одной строкой; копировать целиком). Локальный `curl` не шлёт ни
Origin, ни Referer, поэтому CSRF-middleware дашборда (P0.5) пропускает POST `/cycle/run`; если
задание всё же добавит Origin — он обязан быть `http://127.0.0.1:8787`, иначе POST режется.

```powershell
# Утренний цикл 07:30 → POST /cycle/run (P5.3)
schtasks /Create /TN "CF Morning Cycle" /SC DAILY /ST 07:30 /TR "curl -X POST http://127.0.0.1:8787/cycle/run"

# Ежедневный бэкап Google Sheets (P1.1). cd /d в корень репо ОБЯЗАТЕЛЕН: без Start-in задание
# стартует в System32, а load_config и --out-dir относительные — иначе задание тихо падает.
schtasks /Create /TN "CF Backup" /SC DAILY /ST 07:00 /TR "cmd /c cd /d <local-path> && .venv\Scripts\python.exe -m cf backup"

# Еженедельная ротация старых raw/run_log строк, ПОСЛЕ бэкапа (P3.9)
schtasks /Create /TN "CF Archive" /SC WEEKLY /D SUN /ST 07:10 /TR "cmd /c cd /d <local-path> && .venv\Scripts\python.exe -m cf archive --older-than 45d --yes"
```

Утренний цикл `/cycle/run` прогоняет фан-аут, а внутри него — `formula-guard` (авто-пауза формул,
в т.ч. performance-правило P5.10). **Пока задание не заведено, гвард и perf-проверка идут только при
ручном запуске цикла с дашборда.**

---

## 4. Полугодовой drill восстановления

Раз в **6 месяцев** прогнать восстановление «вживую» и замерить время — иначе RUNBOOK тихо протухает
(сменился путь, ключ, credential-id), и это вскроется только в реальной аварии.

**Сценарий (замерять время от старта до финиша):**

1. Взять чистую машину/VM или отдельную папку **без доступа к рабочим `~/.cf/secrets`**.
2. Пройти чек-лист §2, **опираясь только на менеджер паролей** (не заглядывая в «живой» ноутбук).
   Секундомер: от «пустая машина» до трёх зелёных — `cf status` отвечает, дашборд открылся,
   roundtrip прошёл: `cf backup`, затем `cf restore` только что снятого снимка маленькой вкладки
   (например `seeds`) с `--yes` — вкладка вернулась без потерь.
3. Проверить, что **каждый** секрет из таблицы §1 достался из менеджера паролей (включая Apify-token,
   который вне n8n нигде не лежит).
4. Записать результат в журнал ниже: дата, кто, затраченное время, что не хватило/устарело.
5. Обновить этот RUNBOOK по найденным пробелам и запланировать следующий drill (+6 мес) в календаре.

Напоминание о drill стоит статической строкой в карточке **«Ритуалы недели»** на `/lab` (ссылается
сюда); полугодовой ритм в еженедельную карточку кодом не заводится — она про недельные ритуалы.

**Журнал drill** (замер времени восстановления):

| Дата | Кто проводил | Время восстановления | Пробелы / что обновлено |
|---|---|---|---|
| _(первый drill — запланировать)_ | — | — | — |

---

## 5. Операторские действия после аудита 2026-07

Разовые действия ветки `audit-fixes-2026-07`, которые оператор доводит на «живых» системах
(git/Sheets/n8n офлайн-правками не закрываются).

**Безопасность и секреты**

- **Ротация скомпрометированных ключей** (Apify token, Google service-account, n8n API key —
  побывали в git/облаке): решение 2026-07-22 — **не блокер** (репы приватные, продукт
  внутренний); обязательна перед выходом на b2b, до тех пор — рекомендуемая гигиена.
  Процедуры — колонка «Ротация» в §1.
- В n8n создать Header Auth credential **«CF Webhook Token»** и включить на **4 вебхуках**
  (`cf-raw-tiktok`, `cf-raw-instagram`, `cf-snowball-tiktok`, `cf-stats`) (P0.2).
- Секреты перенесены в `~/.cf/secrets/` — **новые ключи класть туда** (P0.3; фактические пути —
  `cf.config.json`: `service_account_file`, `n8n.api_key_file`, `n8n.webhook_token_file`).
- В клиенте Яндекс.Диска **исключить `agent-runtime/` и `.git` из синхронизации** (синк-демон
  портит `.git`, тянет мусор в облако).
- **Замкнуть секреты в менеджере паролей** (bus-factor = 1: наследник спросить не сможет).
  Занести **6 доступов** из §1 — GitHub, n8n UI, хостинг n8n, Google-аккаунт, Apify-консоль,
  Яндекс.Диск — **и копию Apify-токена** (сейчас живёт только внутри n8n), после чего **снять
  пометки «уточнить у владельца» из §1**.

**n8n (на живом инстансе)**

- Довести **Coalesce-ноду** (P1.16) в **CF 01 TikTok** и **CF 01b Snowball** — по пошаговой
  инструкции `docs/n8n-integration.md` (раздел P1.16), затем `cf pull-n8n` каждого.
- Версионировать **CF 04 Performance** (P5.1) — ✅ выполнено (40bf69b, `n8n/cf04-performance/`);
  осталось убедиться, что performance-строки несут `brief_id` и `measured_at`.
- **P5.14**: `cf push-n8n` трёх воркфлоу (`cf01-tiktok`, `cf01-instagram`, `cf01b-snowball`);
  приёмочный прогон **с искусственно битым батчем** (hashtag-стадия и reel-стадия). **Ручной импорт
  `workflow.json` в UI запрещён — только `cf push-n8n`** (inline-код в JSON дрейфует, истина в
  `code/*.js`). Апгрейд конфига акторов до n8n Variables (`$vars.apify_actor_*`) — по желанию.

**Google Sheets (колонки в живых листах)**

- Добавить колонку **`cta`** в лист **CF Creative Briefs** и колонку **`prompt_version`** в лист
  **CF Published Reels** (без них бриф не проходит ревью-схему / eval теряет привязку к версии промпта).
- Добавить колонку **`reviewed_at`** в лист **CF Creative Briefs** — **строго последним столбцом**,
  пустое значение по умолчанию. Без неё `cf auto-approve` считает недельный кап формулы по дате
  генерации брифа (фикс M31 не работает): бэклог старых pending-брифов обходит кап, а свежие
  брифы упираются в него раньше срока. Пока колонки нет, дашборд показывает предупреждение
  «Схема таблиц», `cf status` печатает ВНИМАНИЕ, а `cf auto-approve` помечает это в Run Log.
  Реестр ожидаемых колонок — `cf.sheets.EXPECTED_COLUMNS` (один источник правды для обоих сигналов).
- Добавить колонки **`creator_slot`** и **`assigned_at`** в лист **CF Creative Briefs** —
  последними столбцами, пустые по умолчанию. Без них кнопка «Отдал в работу» на «Очереди съёмки»
  отвечает ошибкой «Назначение не сохранено» (намеренно громко: молча потерять назначение нельзя),
  плитка «Ждут назначения исполнителя» считает все одобренные сценарии, а среднее полукольцо
  «Темпа недели» стоит на нуле. Список слотов — `cf.config.json` → `dashboard.creator_slots`;
  при найме слот переименовывается там же, одной правкой.

**Автозадания**

- Завести `schtasks 07:30 → POST /cycle/run` (§3, P5.3) — **пока не заведён**; до этого гвард и
  perf-проверка (P5.10) гоняются только при ручном запуске цикла. Дашборд — в автозагрузку.

**Знать оператору (поведение системы)**

- **Пере-апрув формулы против ежедневного гварда**: ре-апрувнутая формула будет снова **запаузена**,
  пока 28-дневное окно замеров не очистится. Override/snooze нет — это ожидаемо.

**CF DS (соседний репозиторий)**

- Пересоздать **symlink `canvas-design`** по инструкции `<CF DS>/SETUP.md` (абсолютный путь
  машинно-зависим, в git его нет).

**Документация (при коммите README — WIP владельца, здесь не трогается)**

- `README.md` содержал протухшие строки 605/685/737/751 — ✅ поправлены 2026-07-22
  (candidate/ab-plan вместо before/after, порог «<5 замеренных reels на версию»,
  rejection-history по enum-коду причины, `log-prompt-version --candidate`).
- Ручная проверка `/cf-generate-briefs` по новосозданному скаффолд-промпту ниши (P5.2).

---

## 6. UTM-контур — приёмка владельцем

Разовые шаги, чтобы контур «ролик → переход → заказ» ожил
(спека: `docs/plans/2026-07-30-utm-contour/spec.md`). Здесь только ИМЕНА
файлов и ключей — значения владелец подставляет свои. Пока шаги 1–2 не
сделаны, `cf collect metrika` честно отвечает `insufficient_data` с перечнем
недостающего — ничего не падает.

1. **OAuth-токен Метрики** → файл `~/.cf/secrets/metrika-token.txt`
   (права 600; путь читается из `cf.config.json` → `metrika.token_file`).
   Копию токена — в менеджер паролей (строка в таблице §1).
2. **Номер счётчика** → `cf.config.json` → `metrika.counter_id`.
3. **В настройках Яндекс.КИТа проверить**: счётчик Метрики подключён к сайту,
   e-commerce события включены (галка в настройках, не код) — без них
   CF Orders останется пустым.
4. **Заполнить `accounts`** в `cf.config.json`: слаги/хэндлы своих аккаунтов,
   рабочим выставить `active: true` (сейчас там PLACEHOLDER-хэндлы).
5. **Добавить колонку `account`** в живой лист CF Published Reels — последним
   столбцом, пустую по умолчанию (дрейф схемы виден в `cf status`).
6. **Включить таймер сборщика**:
   `sudo systemctl enable --now cf-collect-metrika.timer`
   (ежедневно 08:20 — между raw-сбором 08:00 и performance 08:40).
7. **Вкладки CF UTM Traffic и CF Orders руками не создавать** — их создаст
   первый прогон сборщика.
