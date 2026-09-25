# CF — план исправлений по аудиту 2026-07

> **Статус: выполнено 74/74 и влито в master** (2026-07-21, merge 02f1e55;
> 777 pytest + 145 node зелёные на момент влития).

> **For agentic workers:** REQUIRED SUB-SKILL: используй superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans — выполнение по задаче с ревью между задачами. Чекбоксы `- [ ]` — для трекинга.

**Goal:** Закрыть 108 подтверждённых находок `docs/AUDIT-2026-07.md` шестью фазами: P0 безопасность → P1 данные/n8n → P2 дашборд → P3 оптимизация → P4 мёртвый груз → P5 бизнес.

**Architecture:** Правки локализованы в существующих модулях (`src/cf/sheets.py` — единственная точка доступа к Sheets, `dashboard/runner|decisions|data|sections` — пульт, `n8n/*/code/*.js` — Code-ноды). Новое: `src/cf/lock.py` (межпроцессные локи), команды `cf backup / restore / archive / mark-published / formula-perf`. Каждая задача — самостоятельный коммит с тестом.

**Tech Stack:** Python 3 (FastAPI, gspread 6.2, pytest), htmx 1.9 + Jinja2, n8n (синк через `cf push-n8n`/`pull-n8n`), Google Sheets, Claude CLI (headless-агенты).

**Правила выполнения**

- Ground truth: `pytest -q` — на старте **328 passed**; после каждой задачи прогон зелёный, счётчик тестов растёт.
- Каждая задача выполняется через TDD (superpowers:test-driven-development): сначала падающий тест из критерия приёмки, потом фикс.
- Порядок фаз обязателен; внутри фазы задачи независимы, если не указана зависимость.
- 🔴 Критические: **P0.1** (утёкший токен) и **P2.1** (кнопка ▶). P2.1 разрешено вытащить вперёд и сделать сразу после P0.
- n8n-правки: код в `n8n/*/code/`, синк `python -m cf push-n8n`, затем ручной тестовый прогон вебхука.
- Задачи P4.5–P4.7 выполняются в соседнем репозитории **CF DS** (`<local-path-ds>`).
- Effort: **S** — часы, **M** — день-два, **L** — неделя+ (шкала аудита).
- Коммиты: `fix(P1.3): ...` — ID задачи в сообщении.

Сводка: 74 задачи — P0: 6 (4S/2M) · P1: 21 (13S/8M) · P2: 16 (13S/3M) · P3: 9 (6S/3M) · P4: 7 (7S) · P5: 15 (5S/8M/2L).

---

## P0 · Безопасность (6 задач)

Закрывает: C2, guard.py, n8n-вебхуки, CSRF, allowlist, secrets в облаке. Всё остальное ждёт, пока не отозваны утёкшие ключи.

- [x] **P0.1 🔴 Ротация и вычистка ключа внешнего API (C2)** — `<файл в витринной копии не указывается>`
  **Суть:** в git закоммичен живой ключ стороннего сервиса. Порядок: (1) ротировать ключ в консоли сервиса; (2) обновить креденшел в n8n-инстансе; (3) в файле заменить значение на `<REDACTED>` либо удалить файл из репо; (4) историю считать скомпрометированной — ротация обязательна независимо от чистки (filter-repo опционален, репо приватный).
  **Приёмка:** запрос к API сервиса со старым ключом → 401; `git grep -I` по префиксу ключа в рабочей копии пуст; сбор через n8n работает с новым ключом (тестовый прогон).
  **Effort:** S

- [x] **P0.2 · Auth на n8n-вебхуки** — `cf.config.json:12`, `n8n/cf01-tiktok/workflow.json:528-531`, аналогичные webhook-ноды в `n8n/cf01-instagram/workflow.json` и `n8n/cf01b-snowball/workflow.json`, `src/cf/dashboard/runner.py:45-46`
  **Суть:** webhook-ноды подняты с `options:{}` без authentication, URL закоммичены — любой запускает платный сбор. Включить Header Auth (`X-CF-Token`) на всех webhook-нодах; токен — в `secrets/webhook-token.txt` (после P0.3 — вне синк-папки); `_default_post` читает путь к токену из конфига и шлёт заголовок.
  **Приёмка:** POST без заголовка на каждый из вебхуков (raw-tiktok, raw-instagram, snowball-tiktok, stats) → 401/403; запуск звена raw с дашборда проходит; тест: `_default_post` добавляет заголовок.
  **Effort:** S

- [x] **P0.3 · Secrets и ключи — вне Яндекс.Диска, ротация** — `cf.config.json:3` (`service_account_file`), n8n-ключ (конфиг, строка ~6), каталог `secrets/`, `.gitignore:2`
  **Суть:** `secrets/service-account.json` и `n8n-api-key.txt` лежат в папке облачной синхронизации — реплицируются в облако Яндекса. Перенести в `%USERPROFILE%\.cf\secrets\`, поправить пути в `cf.config.json`; после переноса ротировать оба ключа (старые побывали в облаке); `agent-runtime/` и `.git` исключить из синка (настройка клиента Я.Диска) — см. также P1.9 про порчу `.git` синк-демоном.
  **Приёмка:** в `<local-path>\secrets\` нет файлов ключей; `cf status` работает с новыми путями; старый service-account отключён в GCP, старый n8n-key отозван.
  **Effort:** S

- [x] **P0.4 · Pre-commit guard ловит API-токены** — `src/cf/guard.py:3`
  **Суть:** SECRET_MARKERS содержит лишь два маркера приватного ключа (PEM-заголовок и JSON-поле сервис-аккаунта) — ключ из C2 прошёл мимо. Добавить regex-паттерны: `apify_api_\w+`, `sk-[A-Za-z0-9]{20,}`, `ghp_\w+|github_pat_\w+`, `eyJ[A-Za-z0-9_-]{20,}\.` (JWT), `AKIA[0-9A-Z]{16}`, `X-N8N-API-KEY`. Проверка и по маркерам-строкам, и по регекспам.
  **Приёмка:** юнит-тесты: staged-контент с каждым паттерном → COMMIT BLOCKED; легитимный код (например `skill`, `eyJhb` в комментарии-примере короче порога) не блокируется; pytest зелёный.
  **Effort:** S

- [x] **P0.5 · CSRF-защита POST-эндпоинтов дашборда** — `src/cf/dashboard/app.py:234` (и все POST: `/cycle/run`, `/stages/{stage}/run`, `/stages/{stage}/reply`, `/briefs/{id}/review`, `/formulas/decision`, `/niches/decision`)
  **Суть:** cross-site форма запускает конвейер/одобряет брифы. Рекомендация: FastAPI-middleware, сверяющий `Origin`/`Referer` с `http://127.0.0.1:8787` для всех POST; запрос без обоих заголовков — 403 (htmx и формы same-origin их шлют).
  **Приёмка:** тест: POST c `Origin: http://evil.example` → 403 и действие не выполнено; POST с корректным Origin и без Origin+Referer соответственно проходит/режется; все существующие тесты дашборда зелёные (фикстурам добавить заголовок).
  **Effort:** M

- [x] **P0.6 · Сузить allowlist агентских команд** — `.claude/settings.json:11`
  **Суть:** `Bash(python -m cf:*)` (и `cf:*`, и PowerShell-варианты) авто-разрешает state-changing команды — ломает железное правило №3. Заменить wildcard на явный список read/рабочих подкоманд (`read`, `status`, `profile`, `analyze-batch`, `cluster-other`, `validate`, `log-run`, `add-brief`, `trace`, `rejection-history`, `eval-prep`); `log-prompt-version`, `set-review`, `formula-status`, `approve-formula`, `push-n8n`, `apply-sources`, `apply-niches` — только через permission-prompt.
  **Приёмка:** headless `claude -p` внутри фан-аута выполняет `cf profile` без вопроса, а `cf set-review --status approved` требует подтверждения/падает по разрешениям; полный цикл фан-аута проходит end-to-end.
  **Effort:** M

---

## P1 · Данные и n8n (21 задача)

Целостность того, что пишется и читается: слой Sheets, индекс формул, локи, CLI-гигиена, Code-ноды n8n. Первая задача — бэкап, страховка под все последующие правки записи.

- [x] **P1.1 · `cf backup` / `cf restore`** — новые команды в `src/cf/cli.py` поверх `src/cf/sheets.py` (из аудита, Линза 3)
  **Суть:** операционная БД без единой резервной копии, при том что `set_column_by_key` умеет перезаписать колонку целиком. `cf backup`: read_rows по всем вкладкам → датированные JSONL в `agent-runtime/backups/` (ретенция 30 дней); `cf restore --tab <tab> --file <jsonl>` — точечный откат вкладки. Запуск раз в сутки — Планировщик Windows (документируется в P5.15 RUNBOOK).
  **Приёмка:** `cf backup` создаёт файл на каждую вкладку из конфига; roundtrip-тест backup→restore на FakeSheets восстанавливает данные в точности; задача идёт первой в фазе.
  **Effort:** M

- [x] **P1.2 · Идемпотентный `ensure_tab` + первые тесты** — `src/cf/sheets.py:126-133`, `tests/fakes.py`
  **Суть:** сбой между `add_worksheet` и записью заголовков → ретрай видит вкладку и возвращает False, вкладка навсегда без заголовков, все аппенды молча пишут пустые строки. Фикс: существующая вкладка с пустым `row_values(1)` → дописать заголовки. `ensure_tab` сейчас вообще без тестов — FakeClient научить `worksheets()`/`add_worksheet`.
  **Приёмка:** тест «add_worksheet прошёл, append заголовков упал, повторный вызов дописывает заголовки»; тест обычной идемпотентности (вкладка с заголовками → False, ничего не пишется).
  **Effort:** S

- [x] **P1.3 · Честный результат `update_row_fields`/`update_rows_where`** — `src/cf/sheets.py:95,116`
  **Суть:** поле, отсутствующее в headers, молча пропускается, метод возвращает True — решение оператора теряется без сигнала. Возвращать успех только если записаны все поля; незаписанные — в лог warning и в результат (например, кортеж/исключение — выбрать одно и применить в обоих методах); вызывающие (`actions.review_brief`) пробрасывают ошибку в UI.
  **Приёмка:** тест: поле вне headers → результат сигнализирует о пропуске; дашборд-ревью с «переименованной» колонкой показывает ошибку, а не «сохранено».
  **Effort:** S

- [x] **P1.4 · Алиасы в `set_column_by_key`** — `src/cf/sheets.py:153-166`
  **Суть:** единственный метод без `column_aliases`: канонический `key_column` даёт updated=0 и колонку, перезаписанную старыми значениями; алиасный `field` создаёт дублирующую колонку. Прогнать оба через `_actual_name`/`_to_actual`, как в остальных методах.
  **Приёмка:** тест: `cf apply-niches --key source_url` при фактическом заголовке `url` обновляет строки; `field=review_status` при фактическом `human_status` не создаёт новую колонку.
  **Effort:** S

- [x] **P1.5 · Аппенд под конкуренцией без дублей** — `src/cf/sheets.py:78` (`_last_row_equals`), `src/cf/cli.py:370` (`cmd_add_brief`)
  **Суть:** ретрай проверяет только последнюю строку — параллельный аппендер (фан-аут workers=2 пишет run_log) вставляет свою, и повтор даёт дубль; add-brief check-then-append не атомарен. Фикс: при ретрае сканировать хвост листа (последние ~10 строк) на свою строку; в add-brief после аппенда перечитывать хвост по brief_id и трактовать дубль как успех первого.
  **Приёмка:** тест на FakeWorksheet с интерливингом (чужая строка появляется между попыткой и ретраем) → в листе ровно одна своя строка; конкурентный двойной add-brief одного brief_id → одна строка.
  **Effort:** M

- [x] **P1.6 · UNFORMATTED-чтение в update/set_column** — `src/cf/sheets.py:92,113,156,164`
  **Суть:** `get_all_records()` без `value_render_option=unformatted` в update_row_fields/update_rows_where/set_column_by_key, тогда как read_rows читает unformatted намеренно (ru_RU-локаль, комментарий `sheets.py:58`): числовые ключи не матчатся, roundtrip в set_column_by_key искажает числа.
  **Приёмка:** тесты по образцу `test_sheets.py:156`: числовой ключ (views=1000, локально «1 000») матчится в update_rows_where; set_column_by_key не переписывает нетронутые числовые ячейки форматированным текстом.
  **Effort:** S

- [x] **P1.7 · Достоверные фейки Sheets** — `tests/fakes.py:28,45,99`
  **Суть:** FakeSheets принимает любые поля (реальный пишет только колонки из headers) — тесты не ловят молчаливую потерю; нет `fail_updates` — ветки ретраев записи не тестируемы; `get_all_records` через `dict(zip(...))` не добивает короткие строки и не numericise'ит. Выровнять поведение фейка с gspread по всем трём пунктам.
  **Приёмка:** запись поля вне headers на фейке ведёт себя как реальный Sheets (пропуск/сигнал — согласно P1.3); появился счётчик fail_updates и тест частично применённой записи (`update_rows_where`: первый update прошёл, второй упал, ретрай); существующий сьют после ужесточения зелёный (найденные им реальные баги чинятся здесь же).
  **Effort:** M

- [x] **P1.8 · Канонический относительный path в индексе approved-формул** — `src/cf/cli.py:183-214` (set_formula_status), `src/cf/dashboard/decisions.py:110-122` (decide_formula), данные: `formulas/_approved/index.json:4,13`
  **Суть:** дашборд кладёт в индекс абсолютный машинный путь, CLI — относительный: дедуп/удаление по строке `e["path"] != path_str` не срабатывают → пауза не снимает формулу, абсолютные пути коммитятся, n8n-потребители индекса читают бессмыслицу. Фикс: единая функция канонизации (POSIX-относительный путь от корня репо) внутри `set_formula_status` — любые входы (`.\`, абсолютный, относительный) приводятся к одному виду; `decisions.decide_formula` передаёт относительный путь; статус в файл формулы писать только ПОСЛЕ успешного построения entry (сейчас KeyError на неполной формуле оставляет файл мутированным — `cli.py:194`, `decisions.py:122`); при перезаписи файла агентом со сменой статуса вниз (approved→proposed) снимать запись из индекса. Данные: удалить абсолютную запись `index.json:13`; разрешить конфликт `index.json:4` — approved v2 указывает на файл с v3 proposed (снять запись из индекса до ре-утверждения v3 оператором).
  **Приёмка:** тесты: approve относительным путём + пауза через `.\formulas\x.json` и через абсолютный → запись удалена из индекса; approve формулы без name/niche/version → файл не изменён, внятная ошибка; в `index.json` после фикса данных нет абсолютных путей и нет approved-записей, чей файл имеет status≠approved; auto-approve не одобряет брифы формулы v3-proposed.
  **Effort:** M

- [x] **P1.9 · Межпроцессные локи: индекс, git, звенья** — новый `src/cf/lock.py`; применение: `src/cf/cli.py:199`, `src/cf/dashboard/runner.py:139,371-393`, `src/cf/dashboard/decisions.py:24-50`
  **Суть:** read-modify-write `index.json` без лока (гвард из фан-аута × кнопка дашборда × CLI = lost update); два git-коммиттера конкурируют за `.git/index.lock`, сбой глотается warning'ом; `_claim` звена — только threading.Lock внутри процесса (второй дашборд/slash-команда идут параллельно). Фикс: `cf/lock.py` — lock-файл + `msvcrt.locking` с ретраем/таймаутом; обернуть (а) мутации index.json в set_formula_status, (б) обе точки git-операций, (в) захват звена — lock-файл на stage (второй процесс получает отказ). Сбой git поднять из warning в problems звена (механизм в run_fanout_sync уже есть).
  **Приёмка:** стресс-тест: N потоков × set_formula_status с файловым локом → все записи в индексе; тест: захваченное звено не отдаётся второму «процессу» (эмуляция вторым инстансом StageRunner на том же lock-каталоге); сбой git виден в problems, не только в логе.
  **Effort:** M

- [x] **P1.10 · Нормализация review_status в CLI** — `src/cf/cli.py:262,287,329,396`
  **Суть:** сравнения строго в нижнем регистре, а дашборд нормализует (`data.py:43`) — «Approved» руками в таблице ломает кап, formula-guard, auto-approve и rejection-history. Единый хелпер `norm_status(s) = str(s).strip().lower()` во всех четырёх местах.
  **Приёмка:** тесты: 'Approved ', 'REJECTED', 'Pending' учитываются капом/гвардом/автоапрувом идентично дашборду.
  **Effort:** S

- [x] **P1.11 · Кап auto-approve при мусорной дате** — `src/cf/cli.py:282-295`
  **Суть:** approved-бриф с нечитаемой `generated_at` не считается в 7-дневный кап → безлимитное авто-одобрение; семантика противоположна formula-guard (`cli.py:320`), где unparseable попадает в окно. Считать unparseable-даты в кап (консервативно) + warning в вывод.
  **Приёмка:** тест: у формулы 5 approved-брифов с generated_at «15.07.2026»/«» → кап исчерпан, шестой бриф не одобряется.
  **Effort:** S

- [x] **P1.12 · Полнота CF Run Log** — `src/cf/cli.py:302` (auto-approve), `src/cf/cli.py:337` (formula-guard), `prompts/agents/niche-classifier.md:26-32` (+ `src/cf/cli.py:638` cmd_apply_niches)
  **Суть:** auto-approve логирует только успешное одобрение (6 веток отказа — молча), formula-guard — только паузу, ре-аудит классификатора массово перетирает niche вообще без log-run (нарушение правила №6). Логировать все исходы (success/insufficient_data/failed) в обеих командах; в промпт ре-аудита добавить обязательный шаг `cf log-run`; надёжнее — `cmd_apply_niches` логирует сам.
  **Приёмка:** тесты: каждая ветка выхода auto-approve и formula-guard оставляет строку в run_log фейка; в niche-classifier.md шаги ре-аудита (стр. 26-32) содержат log-run; apply-niches пишет run_log.
  **Effort:** S

- [x] **P1.13 · CLI-мелочи целостности** — `src/cf/cli.py:52,77,94,150,433`
  **Суть:** пять локальных багов: (52) `--niche-empty` применяется до dedupe_rows — устаревший дубль без ниши проходит (переставить после дедупа); (77) `--limit 0` выводит все запуски (`runs[-0:]` — guard на 0); (94) подсчёт proposals по подстроке `status: X` по всему файлу (парсить frontmatter, как validate_proposal_text); (150) cluster-other без дедупа по source_url (добавить, как в analyze_rows); (433) log-prompt-version деактивирует только `active='TRUE'`, но активной считается и '1'/'YES' (нормализовать через _prompt_is_active).
  **Приёмка:** по юнит-тесту на каждый из пяти случаев.
  **Effort:** S

- [x] **P1.14 · Контракты промптов и схем** — `.claude/commands/cf-review-brief.md:11`, `prompts/agents/source-tuner.md:15`, `schemas/patterns.schema.json:31`
  **Суть:** (а) ревью-инструкция собирает JSON из колонок и валидирует по brief.schema.json, но add-brief не пишет обязательную `cta` → каждый бриф уходил бы в reject; references склеены через `«; »`, а инструкция велит делить по запятой. Рекомендация: добавить колонку cta в `cmd_add_brief` (`cli.py:374-388`, данные есть в payload_json) и указать в инструкции разделитель `; `. (б) source-tuner предписывает статус `insufficient_evidence` — не входит в `runlog.VALID_STATUSES`, log-run упадёт: заменить на `insufficient_data`. (в) `patterns.schema.json` допускает evidence.source_urls minItems:1 при правиле «меньше 3 URL → паттерн не пишется»: поднять minItems до 3.
  **Приёмка:** сборка JSON по инструкции из реальных колонок add-brief проходит `cf validate brief`; `grep -r insufficient_evidence prompts/` пуст; `cf validate patterns` отклоняет паттерн с 2 URL.
  **Effort:** S

- [x] **P1.15 · Библиотеки: proposals, n8nsync, n8napi, io** — `src/cf/proposals.py:17`, `src/cf/n8nsync.py:7`, `src/cf/n8napi.py:23`, `src/cf/io.py:14`
  **Суть:** (proposals) frontmatter без закрывающего `---` охватывает весь файл — требовать закрытие, иначе invalid; (n8nsync) `code_filename` схлопывает разные имена узлов в один файл — при коллизии добавлять суффикс-хэш имени узла; (n8napi) гвард `body.get("settings", {})` мёртв — comprehension строкой выше кидает KeyError: сначала подставить дефолты, потом собирать body; (io) `write_json_atomic`: ретрай `os.replace` на PermissionError (файл открыт читателем/синк-демоном, WinError 5/32) с коротким бэкоффом.
  **Приёмка:** юнит-тесты: незакрытый frontmatter → invalid; два узла с коллизией имён → два разных файла и push в правильные узлы; workflow без settings → push не падает; os.replace, падающий 2 раза → запись успешна с третьей.
  **Effort:** M

- [x] **P1.16 · TikTok: повторный сбор не затирает транскрипт** — `n8n/cf01-tiktok/workflow.json:144` (+ той же логики нода в `n8n/cf01b-snowball/workflow.json`)
  **Суть:** appendOrUpdate по raw_id слепо перезаписывает все колонки: повторный сбор без субтитров затирает готовый transcript_text пустотой, snowball перетирает source_query (ломает атрибуцию tune-sources). Фикс в воркфлоу: перед записью читать существующие raw_id (lookup) и для существующих строк исключать из update поля transcript_text (при пустом новом значении), source_query и collected_at; либо разделить на append новых / update только метрик (views, likes, …) у существующих.
  **Приёмка:** тестовый прогон: собрать видео с транскриптом → повторный сбор с недоступными субтитрами → transcript_text и source_query в CF Raw TikTok не изменились, метрики обновились; snowball не меняет source_query существующей строки.
  **Effort:** M

- [x] **P1.17 · Транскрипты: мусор не проходит как транскрипт** — `n8n/cf01-tiktok/code/Attach-TikTok-Transcript.js:30-32,51`, идентичная копия `n8n/cf01b-snowball/code/Attach-TikTok-Transcript.js:32`, `n8n/cf01-tiktok/code/Normalize-TikTok-Raw-Rows.js:63`
  **Суть:** (32) тело HTTP-ошибки (JSON `{"status_code":10204,...}` или HTML) проходит текстовый fallback и уходит в Sheets как `raw_saved_actor_transcript`; (30) валидный JSON с нераспознанными ключами (JSON3: `events[].segs[].utf8`) пишется сырым JSON целиком; (63) transcript-не-строка от актора truthy → мусор и заблокированный fallback. Фикс: детект ошибки (HTML-маркеры, `status_code`/`status_msg`, пустой collectText) → transcript='' и `raw_saved_no_actor_transcript`; collectText научить `segs[].utf8`; гард `typeof transcript === 'string'` в нормализаторе. Копию в snowball синхронизировать.
  **Приёмка:** прогон Code-ноды на фикстурах (403-JSON, HTML-страница, JSON3, объект/массив вместо строки): transcript_text пуст или корректен, статус честный; оба воркфлоу содержат одинаковый код.
  **Effort:** S

- [x] **P1.18 · TikTok-гейты: epoch-строки и слитные хэштеги** — `n8n/cf01-tiktok/code/Normalize-TikTok-Raw-Rows.js:20`, `n8n/cf01-tiktok/code/Ingestion-Gate.js:25`
  **Суть:** (20) `toIso` не парсит epoch, пришедший строкой (`new Date('1721030000')` невалиден) → created_at='' → обход фильтров too_old/stale: числовую строку приводить Number'ом; (25) `/#\S+/g` считает `#a#b#c` одним тегом — гейт hashtag_stuffing обходится: матчить `/#[^\s#]+/g`.
  **Приёмка:** фикстуры: `'1721030000'` → валидный ISO 2024 года; `'#a#b#c'` → 3 тега и гейт срабатывает.
  **Effort:** S

- [x] **P1.19 · Instagram-пайплайн: error-items, гейты, хэштеги** — `n8n/cf01-instagram/code/Normalize-Instagram-Raw-Rows.js:55`, `n8n/cf01-instagram/code/Ingestion-Gate-IG.js:16,31`, `n8n/cf01-instagram/code/Normalize-Instagram-Hashtags.js:11,13`
  **Суть:** (Normalize:55) error-item от HTTP-ноды с continueRegularOutput проходит фильтр → мусорная строка `instagram_0`, потеря дневного батча молча: детектить `raw.error`/пустой videoId → не писать строку и сигналить (failed-ветка/лог, чтобы прогон не выглядел успешным); (Gate:16) гейт разворачивает только `item.json`-массив — поддержать формы `{items:[...]}`/`{data:[...]}`; (Gate:31) `views &&` освобождает 0-просмотровые от stale_low_views — сравнивать числом с явной обработкой отсутствия; (Hashtags:11) `'men'` матчится подстрокой в `'women'` — матч по границам слова/точному токену; (Hashtags:13) дедуп тегов без lower() — нормализовать регистр (IG-теги регистронезависимы, двойная оплата скрейпа).
  **Приёмка:** фикстуры на все пять случаев; батч из одного error-item не пишет строк и оставляет видимый след сбоя.
  **Effort:** M

- [x] **P1.20 · Snowball: дедуп seed_url** — `n8n/cf01b-snowball/code/Build-Snowball-Input.js:6`
  **Суть:** один ролик с разными query-параметрами порождает несколько платных Apify-запусков. Нормализация URL (origin+path, без query/fragment) + Set перед сборкой входа актора.
  **Приёмка:** фикстура с `...video/1?is_from_webapp=1` и `...video/1` → один item на входе актора.
  **Effort:** S

- [x] **P1.21 · Классифицировать обе raw-вкладки** — `src/cf/dashboard/runner.py:401`, `.claude/commands/cf-classify-niche.md:6`
  **Суть:** фан-аут зовёт `/cf-classify-niche` без аргументов → по умолчанию только raw_tiktok; Instagram-строки вечно с пустой niche и не попадают в очередь ниш (`runner.py:311`). Запускать классификацию для обеих вкладок (последовательно две команды либо параметр «обе» в команде — синхронизировать с cf-classify-niche.md).
  **Приёмка:** тест раннера: среди вызовов run_command есть классификация raw_tiktok и raw_instagram; после цикла IG-строки получают niche (ручная проверка на живых данных).
  **Effort:** S

---

## P2 · Дашборд (16 задач)

Пульт продюсера: критическая кнопка ▶, потерянные действия, битые метрики, устойчивость к грязным данным, фоновые потоки.

- [x] **P2.1 🔴 Кнопка ▶ звена работает (C1)** — `src/cf/dashboard/templates/partials/stages.html:17`
  **Суть:** кнопка hx-post вложена в `<a class="stage" href=...>` — htmx 1.9.12 не гасит клик у кнопки вне формы, всплытие уводит браузер по href и обрывает POST. Рекомендация: структурно вынести кнопку из якоря (плитка — grid, ссылка и кнопка — соседи, кнопка позиционируется поверх); альтернатива — `hx-on:click="event.preventDefault(); event.stopPropagation()"`.
  **Приёмка:** ручная проверка: клик ▶ на «Raw-ролики» запускает звено, страница остаётся на месте, `#stages` свапается; клик по самой плитке навигирует как раньше; тест шаблона: кнопка не является потомком `<a>`.
  **Effort:** S

- [x] **P2.2 · Ответы и отказы не теряются молча** — `src/cf/dashboard/app.py:231` (reply), `src/cf/dashboard/app.py:237` (start_cycle)
  **Суть:** результат `runner.reply()` игнорируется — набранный продюсером ответ исчезает, если звено успело стать занятым; отказ `start_cycle()` — тихий no-op. При False показывать сообщение (cycle_note/flash) с текстом причины, для reply — возвращать введённый текст в форму.
  **Приёмка:** тесты: reply при занятом звене → ответ страницы содержит пояснение и исходный текст; start_cycle при занятом звене → note на overview.
  **Effort:** S

- [x] **P2.3 · generated_at и niche вместо несуществующих полей** — `src/cf/dashboard/data.py:59-62`, `src/cf/dashboard/templates/briefs.html:29,32,40`, фикстуры `tests/test_dashboard_data.py:72-74`
  **Суть:** пайплайн пишет `generated_at` (cli.py:376), колонки `niche` у брифов нет — «Дата» всегда пуста, briefs_created=0, oldest_pending_days=None, «Ниша» — «—». Рекомендация: алиас `created_at → generated_at` для briefs в `cf.config.json` (минимум правок кода) либо чтение generated_at напрямую — выбрать одно; нишу вытаскивать из payload_json брифа или через формулу (formula→niche), отобразить в списке. Фикстуры перевести на реальные поля таблицы.
  **Приёмка:** тест overview_metrics на фикстурах с generated_at и без created_at → briefs_created>0, oldest_pending_days считается; «Дата» непуста; тесты не используют поле created_at.
  **Effort:** S

- [x] **P2.4 · /lab не падает и не ломается от грязных JSON** — `src/cf/dashboard/templates/lab.html:40,46`, `src/cf/dashboard/sections.py:239-240,284,351,145`, `src/cf/dashboard/app.py:151`
  **Суть:** строковые `avg_views`/`avg_er` из агентского JSON роняют формат (500 на весь /lab — единственный маршрут без try/except); строковый `cluster_size` роняет сортировку очереди ниш; строковый `source_urls` итерируется посимвольно. Фикс: `_as_float`/isinstance-гарды как в защищённых местах (sections.py:347-348, 238); сортировка с приведением; маршрут /lab обернуть try/except по образцу остальных; ссылки рендерить только для http(s) (как в briefs.html).
  **Приёмка:** тест: формула с avg_views="105830", ниша с cluster_size="7", evidence.source_urls строкой → /lab отвечает 200, значения отображаются разумно; `javascript:`-URL не становится ссылкой.
  **Effort:** M

- [x] **P2.5 · Единая шкала ER** — `src/cf/dashboard/data.py:77,95`, `src/cf/dashboard/templates/briefs.html:62`, `src/cf/dashboard/templates/lab.html:40`
  **Суть:** ER во всей системе — доля (0.05–0.15), а дашборд: `round(er, 1)` схлопывает всё в 0.0/0.1 («СРЕДНИЙ ER 0.1%»), карточка брифа показывает долю «0.11», lab — процент «10.5%». Хранить долю без округления до формата, всюду форматировать `{:.1%}`; er_series отдавать сырой долей.
  **Приёмка:** тест: engagement_rate=0.083 → overview «8.3%»; карточка брифа и lab показывают одну шкалу; график ER различает 0.05 и 0.12.
  **Effort:** S

- [x] **P2.6 · Контракт NICHE_RESULT зафиксирован** — `src/cf/dashboard/runner.py:343` (+ `_parse_niche_result`), `.claude/commands/cf-niche-run.md:32`
  **Суть:** раннер понимает только точное `ok`, промпт не велит печатать «ok» (шаг 5 подсказывает «success»); битый JSON при exit 0 превращается в status='ok' с formulas=0. Фикс: в cf-niche-run.md прописать точный формат `NICHE_RESULT {"status":"ok"|"insufficient_data"|"error", ...}`; раннер принимает `success` как синоним `ok`; битый/отсутствующий JSON при exit 0 → status='error' с пометкой unparseable.
  **Приёмка:** тесты: вывод со `"status":"success"` → ниша успешна; битый JSON → error (не ok); ветка `_parse_niche_result → None` покрыта.
  **Effort:** S

- [x] **P2.7 · Живучесть фонового цикла** — `src/cf/dashboard/runner.py:397` (_fanout_params вне try), `:470` (зазор между звеньями), `:178` (TOCTOU raw/factory)
  **Суть:** исключение в `_fanout_params()` убивает поток → звено factory вечно running; между звеньями цикла есть момент any_running()==False → HTMX-опрос, гейтящийся на нём, замирает навсегда; проверка «factory не стартует при raw» вне лока и не в обе стороны. Фикс: _fanout_params внутрь try с деградацией в error; флаг cycle_active, учитываемый опросом и any_running-гейтом; обе взаимные проверки raw/factory внутри `self._lock`.
  **Приёмка:** тесты: исключение из _fanout_params → звено error, поток жив; между звеньями индикатор активности цикла истинный; конкурентный старт raw при running factory (и наоборот) → отказ.
  **Effort:** M

- [x] **P2.8 · Порог очереди = порог профиля** — `src/cf/dashboard/runner.py:31` (min_rows=12), `src/cf/cli.py:682` (default --min-rows 20)
  **Суть:** ниша с 12–19 строками зачисляется в очередь, гарантированно валится на профиле (insufficient_data) и повторяется каждый цикл — платный вызов claude вхолостую. Единая константа/конфиг порога (20), очередь фильтрует по ней после дедупа.
  **Приёмка:** тест: ниша с 15 дедуплицированными строками не попадает в очередь; с 20 — попадает; значение задаётся в одном месте.
  **Effort:** S

- [x] **P2.9 · Валидация имени ниши + тест исключения воркера** — `src/cf/dashboard/runner.py:333` (подстановка в команду), `:334-338` (except без теста)
  **Суть:** имя ниши из Sheets подставляется в `claude -p "/cf-niche-run ..."` без экранирования — пробел/перевод строки ломает разбор аргументов; ветка except, спасающая фан-аут от падения одного воркера, не покрыта. Фикс: валидация имени (буквы/цифры/дефис/подчёркивание; иначе ниша → error без запуска); тест, где run_command бросает TimeoutExpired.
  **Приёмка:** тесты: ниша `'обувь x; y'` отклонена со статусом error и без вызова claude; исключение run_command → error одной ниши, run_fanout_sync доводит остальные.
  **Effort:** S

- [x] **P2.10 · Кэш и границы периодов** — `src/cf/dashboard/data.py:28` (stale общий), `:48` (since с временем суток), `:95` (метка недели без года, серия без фильтра периода)
  **Суть:** stale — один флаг на все вкладки (успех любой вкладки скрывает сбой другой) → сделать per-tab; `since = today - timedelta(days)` сохраняет текущее время — строки ровно N дней назад выпадают → нормализовать к полуночи; метка `W{week:02d}` без года при ключе (year, week) + серия не режется периодом → метка `{year}-W{week:02d}`, фильтр по days.
  **Приёмка:** юнит-тесты: сбой вкладки A + успех B → stale только у A; запись ровно days дней назад входит в окно; серии двух лет не сливаются.
  **Effort:** S

- [x] **P2.11 · HealthMonitor: потокобезопасность и остановка** — `src/cf/dashboard/health.py:43`, `tests/test_dashboard_health.py:38`
  **Суть:** health-поток и threadpool обработчиков делят один gspread-клиент на requests.Session (не потокобезопасна); у HealthMonitor нет stop() — тест навсегда оставляет daemon-поток. Фикс: отдельный экземпляр Sheets для health (или лок вокруг клиента); механизм stop (Event) + teardown в тесте.
  **Приёмка:** тест: monitor.start() → stop() завершает поток (сравнение threading.enumerate до/после); health-чек работает на отдельном клиенте.
  **Effort:** M

- [x] **P2.12 · Шаблоны overview: отчёты и даты** — `src/cf/dashboard/templates/overview.html:48,94`
  **Суть:** секция «Отчёты звеньев» (с hx-poll) не рендерится, если отчётов нет при загрузке — появившиеся отчёты и вопросы агента не видны без F5 → рендерить контейнер с hx-poll всегда (пустое состояние — заглушка); `run.completed_at[:16]` без гварда роняет /overview при отсутствии ключа → `(run.completed_at or "")[:16]`.
  **Приёмка:** тесты: overview без отчётов содержит hx-poll-контейнер; строка run_log без completed_at → 200.
  **Effort:** S

- [x] **P2.13 · Выбор брифа и видимость revised** — `src/cf/dashboard/app.py:85-89,104-105`, `src/cf/dashboard/templates/briefs.html:9,30`
  **Суть:** пустой `?id=` выбирает строку с пустым brief_id вместо первого pending (гард на пустой id); статус `revised` (валидный для cf set-review и предписанный cf-review-brief.md) на дашборде не существует — брифы «на доработку» исчезают из поля зрения: добавить чип «Доработка», счётчик и попадание в очередь внимания (полный UX-поток ревью — P5.5).
  **Приёмка:** тесты: `?id=` пустой → первый pending; бриф revised виден в списке с чипом и посчитан в счётчике.
  **Effort:** S

- [x] **P2.14 · Сбой Run Log виден оператору** — `src/cf/dashboard/actions.py:28`
  **Суть:** review_brief глотает исключение log_run (warning в серверный лог) и возвращает True — след решения теряется молча, в отличие от runner._finish. Возвращать составной результат (решение применено / лог не записан) и показывать предупреждение в UI.
  **Приёмка:** тест: log_run бросает → решение в Sheets применено, ответ страницы содержит предупреждение о Run Log.
  **Effort:** S

- [x] **P2.15 · Стили: pre-wrap и палитра** — `src/cf/dashboard/static/style.css:164` (.script-box), `:204` (.pill.auto)
  **Суть:** у .script-box нет `white-space: pre-wrap` — многострочный скрипт брифа схлопывается в строку; бейдж «авто» залит rgba(90,140,220,.15) — голубой вне закрытой палитры DS. Добавить pre-wrap; цвет заменить токеном из DESIGN.md (CF DS).
  **Приёмка:** скрипт с переносами рендерится многострочно (тест шаблона/ручная проверка); в style.css нет цветов вне палитры DS (сверка с DESIGN.md).
  **Effort:** S

- [x] **P2.16 · meta.source_tab: закрыть контракт с patterns** — `src/cf/dashboard/sections.py:151,338`, `schemas/patterns.schema.json`, `prompts/agents/pattern-analyzer.md`, `.claude/commands/cf-niche-run.md`
  **Суть:** дашборд читает `meta.source_tab`, которого нет ни в схеме, ни в инструкциях агентам (tab кладётся только в имя файла) — поле никогда не пишется. Рекомендация: добавить meta.source_tab в схему и промпты (analyzer пишет его) + fallback-парсинг из имени файла в sections для старых файлов.
  **Приёмка:** тест: sections корректно определяет вкладку и для файла с meta.source_tab, и для старого (из имени); схема описывает поле; промпт велит его писать.
  **Effort:** S

---

## P3 · Оптимизация (9 задач)

Дефицитный ресурс — квота Google Sheets API (60 read/min). Порядок внутри фазы: сначала кэш хэндлов (P3.1) — он умножает эффект остальных.

- [x] **P3.1 · Кэш worksheet-хэндлов** — `src/cf/sheets.py:46` (_ws)
  **Суть:** каждый вызов любой операции заново делает `open_by_key().worksheet()` → полный fetch_sheet_metadata (~половина всех HTTP-вызовов системы). Кэш dict tab_key→Worksheet на инстансе; инвалидация записи кэша при APIError с пересозданием хэндла.
  **Приёмка:** тест: два read_rows подряд → одно обращение open_by_key/worksheet у фейка; ретрай после APIError пересоздаёт хэндл; сьют зелёный.
  **Effort:** S

- [x] **P3.2 · Батч-запись вместо update_cell-циклов** — `src/cf/sheets.py:96,117`
  **Суть:** update_row_fields: get_all_records всей вкладки + update_cell на каждое поле (6+ HTTP на решение по брифу; сессия ревью из 20 брифов ≈ 180 HTTP → 429). Писать одну строку одним values-update (batch_update диапазона строки); update_rows_where — аккумулировать и слать одним batch_update.
  **Приёмка:** тест: запись 3 полей одной строки = 1 update-вызов фейка; поведение P1.3/P1.6 (честный результат, unformatted) сохранено.
  **Effort:** M

- [x] **P3.3 · Батч-экспорт seeds** — `src/cf/cli.py:546`
  **Суть:** append_row в цикле — 3 HTTP на каждый seed (30 URL → 90+ вызовов, 429). Новый `Sheets.append_rows(tab, rows)` (один ws.append_rows + одно чтение заголовков), cmd_export_seeds копит строки и шлёт разом.
  **Приёмка:** тест: 30 seeds → 1 append-вызов и ≤2 чтения; дедуп existing сохранён.
  **Effort:** S

- [x] **P3.4 · Дешёвый поллинг и health-чек** — `src/cf/dashboard/runner.py:152` (_raw_counts), `src/cf/dashboard/health.py:44` (check_sheets)
  **Суть:** _raw_counts каждые 30 с скачивает обе raw-вкладки целиком (мегабайты raw_json) ради len(rows); health каждые 60 с читает весь run_log ради «связь есть». Фикс: подсчёт строк через col_values одной колонки (raw_id); health — row_values(1) run_log.
  **Приёмка:** тесты: _wait_for_raw и check_sheets не вызывают read_rows/get_all_records (только лёгкие методы); функциональность (детект прироста, статус связи) сохранена.
  **Effort:** S

- [x] **P3.5 · Точечная инвалидация кеша дашборда** — `src/cf/dashboard/app.py:122` (post_review), `src/cf/dashboard/data.py` (DataCache)
  **Суть:** post_review делает refresh ДО записи (бесполезно) и оба refresh стирают все вкладки → следующий опрос перекачивает 5+ вкладок. Добавить `DataCache.invalidate(tab)`; после review_brief инвалидировать только briefs и run_log; refresh до записи убрать.
  **Приёмка:** тест: решение по брифу не выбрасывает raw_tiktok/performance из кеша; briefs перечитывается после записи.
  **Effort:** S

- [x] **P3.6 · /briefs и /lab без лишних чтений диска** — `src/cf/dashboard/sections.py:333` (_origin_patterns), `:117` (lab_context)
  **Суть:** каждый GET /briefs парсит все файлы agent-runtime/patterns даже когда pattern_ids уже найдены (нет break); lab_context читает JSON формул, которые _formula_queues затем читает с диска второй раз. Фикс: обход файлов от свежих с ранним выходом при len(found)==len(pattern_ids); единое чтение формул на запрос (передавать распарсенное).
  **Приёмка:** тест со счётчиком открытий: id найдены в первом файле → остальные не читаются; формулы читаются один раз на запрос /lab.
  **Effort:** S

- [x] **P3.7 · read_rows без raw_json + TTL по вкладкам** — `src/cf/sheets.py:60`, `src/cf/dashboard/data.py:8` (из аудита, Линза 3)
  **Суть:** get_all_records тянет все колонки, включая raw_json до 45 КБ/ячейка (~90% объёма вкладки), всеми потребителями. Фикс: read_rows читает через values_get диапазон колонок без тяжёлых (по заголовкам, список исключений в конфиге: raw_json); параметр include_heavy=True для агентов, которым raw_json нужен; TTL кеша не-briefs вкладок поднять до 120–300 с.
  **Приёмка:** тест: read_rows("raw_tiktok") по умолчанию без raw_json, с include_heavy — с ним; вызовы, которым raw_json нужен (grep по потребителям), переведены явно; объём фейкового ответа фиксируется в тесте.
  **Effort:** M

- [x] **P3.8 · Косинус без повторных норм** — `src/cf/cluster.py:29` (+ построение векторов `:37`)
  **Суть:** _cosine пересчитывает нормы обоих векторов на каждой из O(n²) пар (секунды CPU на 700 строк). Предвычислить нормы один раз на вектор (или нормировать векторы при построении) — пара становится O(|пересечение|).
  **Приёмка:** тест равенства кластеров до/после на фикстуре; нормы считаются по одному разу на вектор (проверка структуры кода/счётчик).
  **Effort:** S

- [x] **P3.9 · `cf archive` — ротация raw-вкладок и run_log** — новая команда в `src/cf/cli.py` (+ batch-удаление строк в `src/cf/sheets.py`); мотив: appendOrUpdate-рост (`n8n/cf01-tiktok/workflow.json:144`), run_log (`src/cf/runlog.py:28`), maxAgeDays=30 (`n8n/cf01-tiktok/code/Ingestion-Gate.js:4`)
  **Суть:** вкладки только растут, строки старше 30 дней в сборе уже не участвуют, но читаются каждым get_all_records. `cf archive --older-than 45d`: срез старых строк raw-вкладок и run_log в JSONL (`agent-runtime/archive/`, формат бэкапа P1.1) + batch-удаление из Sheets; запуск еженедельно после бэкапа. Зависимость: P1.1.
  **Приёмка:** тест отбора строк к архивации (граница 45d) и roundtrip сериализации; прогон на копии таблицы: строки удалены, JSONL полон.
  **Effort:** M

---

## P4 · Мёртвый груз (7 задач)

Мёртвые органы управления, мусор, дрейф документации, хвосты CF DS. Задачи P4.5–P4.7 — в репозитории CF DS.

- [x] **P4.1 · ▶ только у настроенных звеньев** — `src/cf/dashboard/runner.py:23` (STAGES), `src/cf/dashboard/templates/partials/stages.html:8`, `src/cf/dashboard/app.py:188-194` (stages_context)
  **Суть:** publish объявлен runnable, вебхука нет — кнопка гарантированно даёт error-плитку каждый день. Пробросить в stages_context множество configured = ключи dashboard.workflows + 'factory'; ▶ рендерить при `runnable and key in configured`; у publish вместо ▶ — ссылка «→ очередь съёмки» (появится в P5.4; до того — без действия).
  **Приёмка:** тест: publish без вебхука в конфиге → нет кнопки ▶ и клик невозможен; добавление вебхука в конфиг возвращает кнопку без правки кода.
  **Effort:** S

- [x] **P4.2 · Мёртвые ключи конфига** — `cf.config.json:9` (dashboard.port), `:64-77` (niches)
  **Суть:** dashboard.port не читается (порт захардкожен в argparse, `src/cf/cli.py:772`) — научить cmd_dashboard брать дефолт порта из конфига; legacy-ключ niches (дубль таксономии, расходится при decide_niche) — удалить, поправив упоминание в `CLAUDE.md:31` (единственный источник — prompts/agents/niche-taxonomy.json).
  **Приёмка:** тест: порт из конфига используется при отсутствии --port; `"niches"` в cf.config.json отсутствует; grep по src/ не находит чтения удалённого ключа.
  **Effort:** S

- [x] **P4.3 · Чистка agent-runtime** — `agent-runtime/tmp_inspect.py:1` и перечень аудита
  **Суть:** удалить: tmp_inspect.py, tmp_extract_refs.py, _extract_refs.py (оставить один reviews/extract-refs.py), tmp-ig.json, tmp-tt.json, tmp-probe.json, tmp-probe-ig.json, niches/_count_empty.py, niches/_filter_empty.py, analysis/2026-07-15-tiktok-quartiles.py, batches/2026-07-14-briefs-debug.json, .superpowers/brainstorm/* (в обоих репо). Файлы gitignored, но синкаются Я.Диском и путают глоббящих агентов.
  **Приёмка:** глоб `agent-runtime/**/tmp*` и перечисленные файлы — пусто; `cf status` и дашборд работают; конвейерные глобы («свежайший файл») не задевают удалённое.
  **Effort:** S

- [x] **P4.4 · Актуализировать справочники** — `CLAUDE.md:21-28` (нет dashboard), `:39-41` (нет /cf-niche-run), `:53-54` (нет CF Seeds), `README.md:854`
  **Суть:** CLAUDE.md отстал: добавить подкоманду dashboard, slash-команду /cf-niche-run (ядро фан-аута), вкладку CF Seeds; README: DESIGN-sentry.md описан как «вахтенный файл ревизий» — исправить на «архив чужой (Sentry) дизайн-системы, источник анти-паттернов».
  **Приёмка:** перечисленные разделы содержат недостающие пункты; grep «вахтенный» по README.md пуст; сверка списков CLAUDE.md с `cf --help` и `.claude/commands/` не находит других пропусков.
  **Effort:** S

- [x] **P4.5 · [CF DS] Убрать trial-шрифты** — `DS/.agents/skills/canvas-design/canvas-fonts/Aeroport-bold-trial.otf` и остальные 8 `*-trial.otf`
  **Суть:** 9 trial-версий Aeroport закоммичены рядом с полным лицензионным набором .ttf — лицензионный риск (canvas-design может выбрать trial по имени) + мёртвые мегабайты. Удалить из git и рабочей копии.
  **Приёмка:** `git ls-files '*-trial.otf'` в CF DS пуст; тестовый рендер canvas-design использует лицензионный Aeroport (в логе/выводе — имя .ttf).
  **Effort:** S

- [x] **P4.6 · [CF DS] Дубль скилла canvas-design** — `DS/.agents/skills/canvas-design/`, `DS/.claude/skills/canvas-design` (symlink на абсолютный путь в рабочей копии, полная копия в git)
  **Суть:** git хранит две полные копии (~138 файлов шрифтов каждая). Оставить источником `.agents/skills/`; `.claude/skills/`-копию убрать из git; механизм подключения — относительный symlink/junction или документированный шаг настройки (абсолютный путь машинно-зависим).
  **Приёмка:** в git одна копия скилла; скилл canvas-design работает при вызове из CF DS (проверка Skill-вызовом); в репо нет абсолютных путей владельца.
  **Effort:** S

- [x] **P4.7 · [CF DS] Починить линтеры дизайн-системы** — `DS/tools/check-residue.sh:6,7`, `DS/tools/check_design.py:66,97`
  **Суть:** четыре дефекта: (sh:7) `grep -v 'DESIGN-sentry\.md'` вырезает всю строку — residue на той же строке невидим (фильтровать точнее: сначала вырезать из строки само упоминание архива, потом искать паттерны); (sh:6) `grep -i` не фолдит кириллицу в C.UTF-8 — «Маскот» проходит (в паттернах — оба регистра явно); (py:66) мёртвое условие `not DOTTED.match(...)` — убрать, а опечатки вида `{color.paper}`/`{Colors.paper}` ловить отдельным регекспом `\{[A-Za-z][A-Za-z0-9-]*\.[A-Za-z0-9.-]+\}` с проверкой группы против известных; (py:97) нечисловой fontWeight («bold») / fontSize не в px («1.5rem») → warning вместо необработанного ValueError.
  **Приёмка:** тест-файлы аудита проходят: строка «Rubik — см. DESIGN-sentry.md» детектится; «Маскот» детектится; `{color.paper}`, `{colours.ink}`, `{Colors.paper}` детектятся; «bold»/«1.5rem» дают warning и ненулевой выход линтера, не traceback.
  **Effort:** S

---

## P5 · Бизнес (15 задач)

Замыкание контура публикация → performance → eval → промпты и снятие ручного труда. Порядок: P5.1 (контур данных) раньше P5.8–P5.11 (eval-стек питается его данными).

- [x] **P5.1 · Замкнуть контур публикации** — форма на `src/cf/dashboard/templates/briefs.html:99-108`, новый POST `/briefs/{id}/published` в `src/cf/dashboard/app.py`, новая CLI `cf mark-published <brief_id> <url>` в `src/cf/cli.py`, версионирование CF 04 в `n8n/cf04-performance/` (`cf pull-n8n`), контракт в `docs/n8n-integration.md:39,62`
  **Суть:** (объединяет «Кнопка Опубликован» и «publish-flow + CF 04» из аудита) цикл ни разу не замкнулся: CF Published Reels никто не пишет, eval-стек мёртв. Форма «Опубликован → URL рилса» на карточке approved-брифа: строка в CF Published Reels (reel_id из URL, brief_id, post_url, published_at с временем, prompt_version из брифа, production_notes) через sheets.append_row + log_run; зеркальная CLI-команда; третье состояние «опубликован» в списке брифов (отличать «одобрен, ждёт съёмки» от «вышел»). CF 04 выгрузить в репо и зафиксировать контракт: performance-строка обязана нести brief_id и measured_at.
  **Приёмка:** e2e-тест на фейках: approve → mark-published → строка в reels со всеми полями, `cf trace` находит цепочку brief→reel; в списке брифов бриф после публикации в состоянии «опубликован»; `n8n/cf04-performance/workflow.json` в git; docs/n8n-integration.md описывает обязательные поля.
  **Effort:** L

- [x] **P5.2 · Автоскаффолд brief-промпта для новой ниши** — `src/cf/dashboard/decisions.py:99-142` (decide_formula), `:145-191` (decide_niche), бейдж в `src/cf/dashboard/templates/lab.html`, шаблон из `prompts/briefs/мужские-образы/reel.md` + `_shared`
  **Суть:** формулы лежат в 7 нишевых папках, промпт есть только у одной ниши — главный тормоз к 70 брифам/нед. При approve формулы/принятии ниши проверять наличие `prompts/briefs/{niche}/reel.md`: нет → красный бейдж «ниша без brief-промпта» в /lab у формулы + кнопка «Создать промпт из шаблона»: минимальный вариант — копия шаблона с подстановкой ниши и пометкой «требует правки» + `cf log-prompt-version brief-{niche}-reel --active`; расширение — headless `claude -p` адаптирует шаблон. Коммит через decision_git.
  **Приёмка:** тест: approve формулы ниши без промпта → бейдж в lab_context; кнопка создаёт файл промпта и активную запись в CF Prompt Versions; `/cf-generate-briefs` для новой ниши генерирует брифы (ручная проверка одного прогона).
  **Effort:** M

- [x] **P5.3 · Автозапуск цикла + уведомление о готовности** — `src/cf/dashboard/runner.py:395-448` (run_fanout_sync), `:232-237` (_finish), новый ключ `dashboard.notify_url` в `cf.config.json`
  **Суть:** цикл стартует только кнопкой, завершается молча — оператор теряет 30–60 мин/день на запуск и ожидание. (1) Планировщик Windows/n8n-cron дёргает POST `/cycle/run` в 07:30 (эндпоинт есть; дашборд — в автозагрузку, шаг в RUNBOOK P5.15); (2) в конце run_fanout_sync и _finish — best-effort POST на notify_url («формул в очереди N, брифов pending M») → n8n шлёт Telegram.
  **Приёмка:** тест: завершение фан-аута делает POST на notify_url с итогами (фейковый httpx); отказ notify не роняет цикл; инструкция по schtasks в RUNBOOK.
  **Effort:** S

- [x] **P5.4 · Очередь съёмки одним листом** — новый роут `/briefs/shooting-list` (данные уже в `src/cf/dashboard/app.py:80-101`), новый шаблон
  **Суть:** для съёмки 10 рилсов/день скрипты нужны пачкой, сейчас — по одному брифу с ручным копированием. Печатная страница: все approved-брифы без опубликованного рилса (hook, script, референсы ссылками, ниша, формула), группировка по нишам, кнопка «Скачать .md». Фильтр «approved и нет в reels» — join через brief_id (P5.1 даёт состояние «опубликован»).
  **Приёмка:** тест: approved без reel — в списке, опубликованные и pending — нет; экспорт .md содержит все карточки; ссылка со звена publish (P4.1) ведёт сюда.
  **Effort:** S

- [x] **P5.5 · Быстрое ревью: htmx-инлайн, hotkeys, revised-поток** — `src/cf/dashboard/app.py:118-126`, `src/cf/dashboard/templates/briefs.html`, DataCache (P3.5)
  **Суть:** каждое решение — полный POST+redirect с двойным сбросом всего кеша; hotkeys нет; revised-брифы невидимы (базовая видимость — P2.13). (1) Форма ревью → hx-post со свапом карточки и строки списка, инвалидация только briefs (P3.5); (2) клавиши A/R/→ (одобрить/отклонить/следующий pending) inline-скриптом; (3) revised — в очередь внимания overview_metrics.
  **Приёмка:** решение по брифу не перезагружает страницу (htmx-своп, тест ответа-партиала); следующий pending доступен с клавиатуры (ручная проверка); revised учтён в очереди внимания (тест метрик). Ориентир аудита: ревью 15 брифов ~3 минуты.
  **Effort:** M

- [x] **P5.6 · Плитка темпа «X/70», воронка и aging** — `src/cf/dashboard/data.py:46-110` (overview_metrics), `src/cf/dashboard/templates/overview.html`, ключ `dashboard.weekly_target` в `cf.config.json` (дефолт 70)
  **Суть:** (объединяет «Плитка X/70» и «Воронка бриф→съёмка→публикация» из аудита; зависит от P2.3 — generated_at) главный KPI не виден. Добавить: briefs_approved_week против target с прогресс-баром; строка воронки «ниш с промптом N/M · формул approved K · брифов/нед X · опубликовано Y» (данные: кеш + formulas/_approved/index.json + список папок prompts/briefs/); aging: approved-брифы без reel старше N дней и конверсия brief→reel за неделю (join по brief_id, как в trace.py); включить aging в weekly-eval как health-показатель.
  **Приёмка:** тест overview_metrics на фикстурах: плитка, воронка, aging считаются верно; при пустом target — дефолт 70; overview рендерит блок.
  **Effort:** M

- [x] **P5.7 · Карточка «Ритуалы недели»** — `/lab` (`src/cf/dashboard/sections.py:174-186`, `templates/lab.html:166-179`), запуск через механизм runner (`_fanout_claude`)
  **Суть:** /cf-eval и /cf-tune-sources живут в терминале по памяти, просроченный eval ничто не показывает, sources-proposal легко забыть применить. Карточка: дата последнего weekly-eval (из имён файлов agent-runtime/evals) и последнего source-stats, бейдж «просрочено» при >7 дней, кнопки ▶ через runner (`claude -p /cf-eval`, отчёт — в «Отчёты звеньев»); для sources-proposal со status=pending — строка «ждёт apply-sources».
  **Приёмка:** тест: eval-файл 8-дневной давности → бейдж «просрочено»; кнопка вызывает runner с нужной командой (фейк); pending-proposal отображается с напоминанием.
  **Effort:** M

- [x] **P5.8 · Eval-prep: честный датасет** — `src/cf/evalprep.py:11-29` (build_eval_dataset), `:37` (строгие сравнения), `src/cf/cli.py:413-428` (cmd_eval_prep)
  **Суть:** (объединяет «дедуп замеров», «guard качества join» и minor evalprep.py:37) ежедневные замеры одного ролика входят в среднее по разу за день; unknown-join молча образует датасет; статусы сравниваются без strip/lower. Фикс: один замер на reel_id — ближайший к 7 дням после published_at (fallback — последний по measured_at); median_views рядом с avg (тяжёлые хвосты); join_health = {unknown_version_share, briefs_not_found}, при unknown >20% — warning и датасет insufficient_data (правило №2); нормализация review_status/active как в дашборде.
  **Приёмка:** тесты: 10 замеров одного reel → 1 строка в датасете (ближайшая к 7 дням); median в агрегатах; unknown 30% → insufficient_data; 'Approved ' учитывается.
  **Effort:** M

- [x] **P5.9 · Пороги eval per-version и валидация proposals** — `prompts/agents/eval-agent.md:21,39`
  **Суть:** (21) критерий proposals_evidence_complete требует `cf validate proposal` от ВСЕХ файлов в proposals/, но валидатор жёстко требует «status: proposed» — любой approved/rejected proposal валит критерий: валидировать только proposed (или согласовать validate_proposal_text с любым валидным статусом — выбрать с оглядкой на P1.13(94)); (39) порог insufficient_data «<5 строк всего» → «<5 замеренных reels на КАЖДУЮ сравниваемую версию».
  **Приёмка:** прогон eval-агента (или сухая проверка промпта): approved-proposal не валит критерий; сравнение 2 vs 3 роликов → insufficient_data.
  **Effort:** S

- [x] **P5.10 · Performance-правило в formula-guard** — `src/cf/cli.py:308-346` (cmd_formula_guard), механика паузы `:183-214`
  **Суть:** формула умирает только от 3 reject или ручной паузы после weekly-eval — 1–2 недели съёмки по мёртвой формуле. Второе правило гварда: у формулы ≥3 замеренных reels за 28 дней и median views < 0.5 × медианы всех замеренных reels периода → авто-пауза с reason и цифрами. Запускать гвард ежедневно (добавить в утренний цикл/задание планировщика из P5.3).
  **Приёмка:** тесты: формула с 3 reels и медианой 0.3× → paused с цифрами в reason; 2 reels — не трогается; здоровая — не трогается; запуск логируется (P1.12).
  **Effort:** M

- [x] **P5.11 · own_performance в карточке формулы** — новая `cf formula-perf` в `src/cf/cli.py`, данные в `formulas/_approved/index.json`, вывод в `src/cf/dashboard/sections.py:114-126` и lab.html
  **Суть:** формула показывает только чужой evidence — собственные результаты не аккумулируются. По briefs→performance собрать на каждую approved-формулу {reels, median_views, avg_er, last_measured_at} → записать в индекс (через set_formula_status-механику с локом P1.9); Лаборатория показывает own_performance рядом с evidence; verdict из weekly-eval дублировать в status_reason формулы.
  **Приёмка:** тест formula-perf на фикстурах briefs/performance → корректные агрегаты в индексе; /lab рендерит блок own_performance; формула без замеров показывает «нет данных».
  **Effort:** M

- [x] **P5.12 · Словарь кодов причин reject** — `src/cf/cli.py:393-410` (rejection-history), `:744` (set-review), `prompts/agents/brief-reviewer.md:40-41`, форма reject на дашборде
  **Суть:** группировка причин по точной строке — повторы не накапливаются никогда, evidence-петля оптимизатора мертва по построению. Enum кодов (reference_mismatch, female_reference, prohibition_violation, weak_hook, not_producible, other) + `--reason-code` у set-review (свободный текст — в notes); дашборд-кнопка reject — выпадающий список кодов; rejection-history группирует по коду; brief-reviewer.md обновить на коды.
  **Приёмка:** тесты: set-review с кодом пишет код; rejection-history группирует и считает по кодам; дашборд-форма шлёт код; старые записи без кода не ломают отчёт.
  **Effort:** S

- [x] **P5.13 · Честный A/B промпт-версий (interleaving)** — `src/cf/cli.py:431-434` (log-prompt-version), вкладка CF Prompt Versions, `prompts/agents/brief-generator.md`, `prompts/agents/eval-agent.md`
  **Суть:** активна всегда одна версия — сравнение версий это before/after со смешанными факторами, статистически пустое. Статус `candidate` в CF Prompt Versions (log-prompt-version --candidate, не деактивирует active); brief-generator при наличии candidate чередует версии между брифами одной формулы (брифы уже несут prompt_version); eval сравнивает только параллельные когорты одного периода и формулы, вердикт при ≥5 замеренных reels на каждую версию, иначе insufficient_data. Зависимости: P5.1 (замеры), P5.8 (датасет), P5.9 (пороги).
  **Приёмка:** тесты: candidate не деактивирует active; распределение версий по брифам ~50/50 на фикстуре; eval-датасет помечает когорты; вердикт при 4 vs 6 reels → insufficient_data.
  **Effort:** L

- [x] **P5.14 · Apify: уйти с run-sync «всё или ничего»** — `n8n/cf01-tiktok/workflow.json:37,107,118-121`, `n8n/cf01-tiktok/code/Build-TikTok-Actor-Input.js:3,14`, аналогично instagram (3 актора) и snowball
  **Суть:** один POST run-sync с потолком ~300 с на весь вход (14 хэштегов + 10 запросов) и onError: stopWorkflow — один медленный прогон убивает весь сбор с нулём строк. Минимальный вариант (рекомендация): SplitInBatches по 3–4 источника, каждый батч — независимый run-sync с onError: continue + счётчик потерь в Ingestion Gate; полный вариант: async POST /runs → Wait → dataset items. Заодно вынести id акторов в конфиг (замена забаненного актора без правки трёх воркфлоу).
  **Приёмка:** тестовый прогон с искусственно битым батчем (несуществующий хэштег/таймаут): остальные батчи доехали, потери видны в гейте/Run Log; id акторов читаются из одного места.
  **Effort:** M

- [x] **P5.15 · RUNBOOK восстановления** — новый `docs/RUNBOOK.md`
  **Суть:** bus-factor = один ноутбук; креды частично существуют только внутри n8n. Одна страница: (1) чек-лист восстановления — клон репо, venv, secrets из менеджера паролей (пути из P0.3), cf backup/restore, запуск дашборда, автозадания (P5.3, P5.10); (2) копии ВСЕХ кредов (service account, n8n API key, Apify token, webhook-токен P0.2) в менеджере паролей; (3) полугодовой drill с замером времени.
  **Приёмка:** документ существует и перечисляет все секреты с местом хранения; сверка: каждый ключ из cf.config.json/n8n имеет строку в RUNBOOK; drill внесён в календарь/ритуалы (P5.7).
  **Effort:** S

---

## Соответствие аудиту

Все 108 находок аудита покрыты задачами выше; повторы одного дефекта в разных секциях аудита (например, `decisions.py:119-120` ×3, `ensure_tab` ×3, секция «Оптимизация» дублирует major/minor) слиты в одну задачу каждая. Бизнес-предложения Линзы 1–3 распределены: вебхуки/secrets → P0.2/P0.3, бэкап → P1.1, локи → P1.9, квота Sheets → P3, ловушка ▶ → P4.1, остальное → P5.

После завершения фазы: полный `pytest -q`, ручной smoke дашборда (`python -m cf dashboard`), для P1 — тестовые прогоны n8n-вебхуков, коммит фазы и отметка чекбоксов.
