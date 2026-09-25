# CF — план порта сбора в Python (`src/cf/collect`), этап 2 миграции на VPS

> Статус: **выполнено 22/22** (2026-07-24, ветка collect-port; 1031 pytest +
> 145 node зелёные, parity-карта — tests/test_collect_parity.py). Approve
> оператора — 2026-07-23. Основа: спека
> [2026-07-22-vps-autonomy-design.md](../superpowers/specs/2026-07-22-vps-autonomy-design.md)
> §2, §2.1, §7; инвентарь JS-стороны снят 2026-07-23.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> (рекомендуется) или superpowers:executing-plans. Чекбоксы `- [ ]` — для трекинга.

**Goal:** Сбор (TikTok, Instagram, Snowball, Performance) работает как
`cf collect <stage>` без n8n; все ~145 JS-инвариантов зелёные в pytest;
SOURCES-списки — данные в `sources/`; звено raw дашборда гоняет subprocess.
n8n не трогаем (живёт до этапа 6), `n8n/` остаётся в репо как эталон приёмки.

**Architecture:** Новый пакет `src/cf/collect/`: общие модули (`util.py`,
`apify.py`, `subtitles.py`, `gate.py`, `normalize.py`, `coalesce.py`,
`sources.py`) + пайплайны (`tiktok.py`, `instagram.py`, `snowball.py`,
`performance.py`). Три пары байт-в-байт дублей JS (Attach/Coalesce/Gate
tiktok↔snowball) схлопываются в один модуль каждая. Sheets-запись — новый
`Sheets.upsert_rows` поверх существующего `sheets.py`; ретраи — `retry.py`;
лог — `runlog.log_run`. Реестр источников `sources/{tiktok,instagram}.json`
заменяет SOURCES-блоки в JS (сами JS-файлы не правим — они заморожены как
эталон).

**Tech Stack:** Python 3.12, httpx (уже в зависимостях), pytest; без новых
зависимостей.

**Правила выполнения**

- Ground truth: `.venv/bin/python -m pytest -q` — на старте **777 passed**;
  после каждой задачи зелёный, счётчик растёт.
- TDD: сначала падающий тест (порт соответствующих JS-инвариантов), потом код.
- Портируем поведение **1:1**, включая пороги (`maxAgeDays=30`,
  `minViewsStale=500`, `staleAfterDays=14`, `maxCaptionHashtags=9`,
  `languagesAllow=[ru,en,un,'']`, батчи ≤4/≤6, reel-URL ≤30, discovered-теги
  ≤24, `results_per_page=20`, `raw_json` ≤45000). Отступления — только
  зафиксированные спекой (async Apify, upsert вместо appendOrUpdate).
- Контракты, которые нельзя сломать: имена колонок CF Raw TikTok (23) /
  CF Raw Instagram / CF Performance; грамматика `source_query`
  (`hashtag:#…`, `query:…`, `snowball:…`, `unknown`); значения
  `processing_status`; `batch_sources`-маркеры; учёт потерь
  (`batches_failed` по уникальным `batch_index`, throw при «0 годных рядов и
  ≥1 упавший батч»).
- Коммиты: `feat(C2.3): ...` — ID задачи в сообщении.
- Сеть в тестах — только моки/фейки; смоук с живым Apify — отдельный шаг
  приёмки, вручную.

Сводка: 22 задачи — C1 фундамент: 7 · C2 пайплайны: 6 · C3 CLI и дашборд: 5 ·
C4 умные источники: 4.

---

## C1 · Фундамент (7 задач)

Общие модули; каждый закрывает свой кластер JS-тестов. Порядок: C1.1–C1.2
первыми (от них зависят все пайплайны), остальные независимы.

- [x] **C1.1 · `collect/util.py` — общие хелперы** — источник:
  дублированные хелперы `first/num/hash/toIso/ageHours/normalizeUrl/isApifyError`
  во всех Normalize-файлах
  **Суть:** один модуль вместо 4+ копий: `first`, `num`, `stable_hash`,
  `to_iso` (epoch-строка/число/ISO/мусор — инварианты normalize.test.js),
  `age_hours`, `normalize_url` (origin+path, без query/fragment/хвостового
  слэша — snowball-input.test.js), `is_apify_error` (строгий: есть `error` И
  нет id/url), `engagement_rate`.
  **Приёмка:** pytest-порт хелперных инвариантов normalize.test.js (10) и
  snowball-input.test.js по normalizeSeedUrl; хелперы без состояния и I/O.
  **Effort:** S

- [x] **C1.2 · `collect/apify.py` — клиент start + poll** — спека §2;
  замена run-sync-get-dataset-items
  **Суть:** `run_actor(actor_path, payload) -> RunResult`: POST
  `/v2/acts/{actor}/runs` → поллинг `GET /runs/{id}` до терминального статуса
  (SUCCEEDED/FAILED/ABORTED/TIMED-OUT) → `GET /datasets/{id}/items`. Токен из
  `apify.token_file` конфига (`~/.cf/secrets/apify-token.txt`). Поллинг с
  бэкоффом и общим таймаутом (конфиг, дефолт 15 мин/ран); httpx-вызовы через
  `with_retry` (сетевые/5xx/429 ретраятся, 4xx — нет). Батчи параллельно:
  `run_batches(batches, max_workers)` на ThreadPoolExecutor — сбой одного
  батча возвращает loss-маркер (`__apify_error`, `batch_index`,
  `batch_sources`), не роняя остальные.
  **Приёмка:** юнит-тесты на моках httpx: happy-path; FAILED/ABORTED/TIMED-OUT
  → loss-маркер с причиной; таймаут поллинга → loss-маркер; 429 ретраится,
  404 — нет; параллельный прогон с одним упавшим батчем отдаёт items остальных
  + один маркер.
  **Effort:** M

- [x] **C1.3 · `Sheets.upsert_rows` + `collect/coalesce.py`** —
  `src/cf/sheets.py`; JS-эталон `Coalesce-Existing-Raw.js` (обе копии
  идентичны), coalesce.test.js (16)
  **Суть:** (а) `upsert_rows(tab_key, key_column, rows, merge=None)`: одно
  чтение ключей → батч-update существующих (через механику
  `update_rows_where`/values-batch, unformatted, P1.6) + `append_rows` новых;
  результат `{updated, appended, skipped_fields}`. (б) `coalesce.py`: для
  существующих `raw_id` — `transcript_text` (новый пустой/whitespace →
  прежний), `source_query` и `collected_at` всегда прежние; остальные
  колонки свежие; новый raw_id — passthrough. Подключается как `merge`-hook.
  **Приёмка:** pytest-порт всех 16 инвариантов coalesce.test.js на FakeSheets;
  upsert: 2 существующих + 3 новых = 1 чтение + 1 батч-update + 1 append
  (счётчики фейка); конкурентный повтор не плодит дублей (хвост-скан P1.5).
  **Effort:** M

- [x] **C1.4 · `collect/subtitles.py` — парсер субтитров** — JS-эталон
  `Attach-TikTok-Transcript.js` (обе копии идентичны), subtitle.test.js (21)
  **Суть:** `parse_subtitle(body)`: VTT / JSON3 (`events[].segs[].utf8`) /
  plain; пусто на HTML-страницы и JSON-тела ошибок (`status_code`/
  `status_msg`/`error`-ключ); `attach_transcript(row, body)` →
  `transcript_text` + `processing_status ∈ {raw_saved_actor_transcript,
  raw_saved_subtitle_download_failed}`, откат на прежний transcript при
  провале скачивания. Скачивание субтитра — httpx GET с ретраем (тут же).
  **Приёмка:** pytest-порт всех 21 инварианта subtitle.test.js; сырой JSON не
  «утекает» в transcript_text.
  **Effort:** S

- [x] **C1.5 · `collect/gate.py` — гейты TikTok и Instagram** — JS-эталон
  `Ingestion-Gate.js` (tiktok≡snowball) и `Ingestion-Gate-IG.js`;
  gate.test.js (3), части losses/instagram
  **Суть:** `tiktok_gate(items, cfg)` и `instagram_gate(items, cfg)` с общим
  каркасом учёта потерь: дропы (is_ad, blocked-авторы, язык вне allow,
  hashtag-stuffing `/#[^\s#]+/` >9, too_old >30д, stale_low_views), развод
  loss-маркеров и рядов, `batches_failed` по уникальным `batch_index`,
  `sources_lost`, throw при «0 годных и ≥1 упавший батч». IG-вариант: свои
  поля (`ownerUsername/videoViewCount`), разворачивание `{items:[]}`/
  `{data:[]}`, 0 просмотров не освобождает от stale (P1.19).
  **Приёмка:** pytest-порт gate.test.js + гейтовых инвариантов losses.test.js
  (параметризация TikTok/Snowball — одна функция, два прогона) и
  instagram.test.js.
  **Effort:** M

- [x] **C1.6 · `collect/normalize.py` — нормализация строк** — JS-эталон
  трёх Normalize-файлов; normalize.test.js (10), части losses/instagram
  **Суть:** `tiktok_row(raw, attribution)` — 23 колонки CF Raw TikTok, выбор
  субтитр-ссылки rus→ASR→eng, transcript-coercion (не-строка → качаем
  субтитры), `raw_json` ≤45000; `instagram_row(raw, tag_by_code)` — колонки
  CF Raw Instagram, `source_query` через shortcode→тег, отбраковка
  error/пустых item (не пишем `instagram_0`), `reels_lost`; атрибуция —
  параметр (`hashtag:#…`/`query:…` для tiktok, `snowball:<seed>` для
  snowball — вместо `$('Build Snowball Input').itemMatching`).
  **Приёмка:** pytest-порт normalize.test.js, снoowball-атрибуции
  (snowball.test.js) и normalize-части instagram/losses; ряд с типовой
  фикстуры clockworks даёт все 23 колонки с теми же значениями, что JS.
  **Effort:** M

- [x] **C1.7 · Реестр `sources/` + `collect/sources.py` + батчер** —
  спека §2.1; JS-эталон SOURCES-блоков и Build-файлов; batching.test.js (21)
  **Суть:** (а) `sources/tiktok.json` (14 hashtags + 10 searchQueries из
  Build-TikTok-Actor-Input.js) и `sources/instagram.json` (12 + 8) — записи
  `{query, kind: hashtag|search, niche, status: active|candidate|paused|retired,
  origin: operator|tuner|harvest|discovery, added_at}`; все текущие —
  `status=active, origin=operator`. (б) `sources.py`: чтение реестра
  (active + exploration-выборка, C4.1), схема в `schemas/`, `cf validate
  sources`. (в) батчер: шардинг ≤4 источника (reel-URL ≤6), полное покрытие
  без дублей, последовательный `batch_index`, единый `batch_total`,
  `source_query` = полный список на каждом батче, `batch_sources`-маркеры.
  **Приёмка:** pytest-порт всех инвариантов batching.test.js (границы 4→1,
  25→7, 30→5, усечение >30 и т.д.); JSON-реестры валидны по схеме и
  содержат ровно источники из JS (diff по значениям); JS-файлы не изменены.
  **Effort:** M

---

## C2 · Пайплайны (6 задач)

Сборка стадий из модулей C1. Каждая стадия: вход → Apify → гейт → нормализация
→ upsert с coalesce → сводка потерь в лог процесса + `runlog.log_run`
(success / insufficient_data при 0 строк / failed) — правило №6 CLAUDE.md.

- [x] **C2.1 · `collect/tiktok.py`** — JS-эталон cf01-tiktok (5 файлов)
  **Суть:** active-источники + exploration (C4.1) → батчи → clockworks
  (`actor_path` из конфига `apify.actors.tiktok`) → tiktok_gate → выбор
  субтитр-ссылки → скачивание субтитров (параллельно, только у прошедших
  гейт) → attach_transcript → upsert(coalesce) в raw_tiktok → сводка
  (rows/dropped/batches_failed/sources_lost) в run_log.
  **Приёмка:** e2e-тест на моках apify/httpx и FakeSheets: happy-path пишет
  строки со всеми колонками; битый батч → строки остальных + учтённые потери
  в run_log; 0 годных + упавший батч → exit≠0, log_run failed; повторный
  прогон не затирает transcript_text/source_query/collected_at (coalesce).
  **Effort:** M

- [x] **C2.2 · `collect/instagram.py` — 3 стадии** — JS-эталон cf01-instagram
  (5 файлов)
  **Суть:** discovery (search-scraper, `search_limit_per_query=4`) → мерж
  seed+discovered тегов (нишевый фильтр по границе слова, регистронезависимый
  дедуп, ≤24) → hashtag-scraper батчами → instagram_gate → уникальные
  reel-URL ≤30, `tag_by_code` → reel-scraper батчами ≤6 → instagram_row →
  upsert в raw_instagram. Сбой discovery — деградация до seed-тегов
  (в n8n у discovery onError=continue); двухточечный учёт потерь
  (hashtag-стадия + reel-стадия) сохраняется.
  **Приёмка:** e2e на моках: happy-path; discovery упал → сбор по seed;
  частичный провал reel-батча → reels_lost в сводке, без throw; полный провал
  → failed; `men`-фильтр не матчит `women`.
  **Effort:** M

- [x] **C2.3 · `collect/snowball.py`** — JS-эталон cf01b-snowball
  **Суть:** seeds из CF Seeds (`active===TRUE`, tiktok.com), дедуп по
  normalize_url, 1 item/батч на seed, атрибуция `snowball:<seed_url>`;
  дальше общий tiktok-тракт (gate → субтитры → upsert с coalesce — обязателен
  здесь, пишет в тот же лист).
  **Приёмка:** pytest-порт snowball-input.test.js (11) и snowball.test.js (7);
  e2e: два seed с одинаковым нормализованным URL → один ран.
  **Effort:** S

- [x] **C2.4 · `collect/performance.py`** — JS-эталон cf04-performance
  (2 файла)
  **Суть:** CF Published Reels → фильтр published + полные поля → ветвление
  актора по платформе → Apify-замер → матч ответа к запросу по
  normalize_url → строки CF Performance (`performance_id, brief_id,
  prompt_version, measured_at, hours_since_publish, метрики,
  views_per_hour, result_label='unknown'`) → append. Контракт спеки: строки
  несут `brief_id`, `measured_at`, `reel_id`, `er`.
  **Приёмка:** e2e на моках: фильтрация неполных строк; матч по URL;
  недоступный пост → `metrics_empty_or_unavailable`, ряд без метрик не пишется
  с мусором; run_log со сводкой.
  **Effort:** M

- [x] **C2.5 · Идентичность порта: прогон JS-фикстур через Python** —
  критерий готовности порта из гайда
  **Суть:** сводный тест-модуль `tests/test_collect_parity.py`: реальные
  фикстуры из JS-тестов (типовой clockworks-ответ, IG reel, error-тела
  субтитров, loss-интерливинги) прогоняются через Python-тракт; таблица
  соответствия «JS-тест → pytest» в докстринге модуля — все ~145 рантайм-
  инвариантов замаплены (часть закрыта в C1/C2 поимённо, остальное здесь).
  **Приёмка:** каждый test()-блок из 10 JS-файлов имеет строку соответствия;
  непереносимые (workflow-structure.test.js — 15 рантайм-кейсов про граф n8n)
  помечены `n/a (граф n8n, умирает на этапе 6)`; счётчик pytest ≥ +130 к
  старту плана.
  **Effort:** M

- [x] **C2.6 · `--dry-run` во всех стадиях** — спека §7 (smoke)
  **Суть:** `cf collect <stage> --dry-run`: 1 батч, реальный Apify, БЕЗ записи
  в Sheets — нормализованные строки и сводка потерь в JSONL
  `agent-runtime/collect/dry-run-<stage>-<ts>.jsonl` + stdout-резюме. Этим же
  форматом пойдёт diff приёмки этапа 4 (сравнение с рядами n8n).
  **Приёмка:** тест: dry-run не делает ни одного вызова записи FakeSheets,
  JSONL валиден и содержит те же строки, что ушли бы в upsert; run_log
  помечает `trigger_type=dry-run`.
  **Effort:** S

---

## C3 · CLI и дашборд (5 задач)

- [x] **C3.1 · `cf collect` в CLI** — `src/cf/cli.py`, `cf.config.json`
  **Суть:** подкоманда `collect {tiktok|instagram|snowball|performance}`
  (+ `--dry-run`); блок конфига `apify`: `token_file`, `actors`
  (tiktok/instagram-discovery/-hashtag/-reel/performance-*), таймауты,
  `max_workers`; exit-коды: 0 success/insufficient_data (0 строк — честный
  статус, не авария), 1 failed. Обновить CLAUDE.md (список подкоманд) и
  `.claude/settings.json` allowlist (collect — только с prompt: пишет в
  боевые листы).
  **Приёмка:** `cf collect tiktok --dry-run` на моках проходит из теста CLI;
  `cf --help` показывает collect; конфиг-ключи читаются, дефолты работают.
  **Effort:** S

- [x] **C3.2 · Звено raw дашборда → subprocess** — `src/cf/dashboard/runner.py`
  (STAGES, run_sync, `_raw_detail`), спека §2
  **Суть:** kind звена raw: `n8n` → `cli`; запуск `python -m cf collect
  tiktok`, затем `instagram` (последовательно, под тем же ProcessLock и
  mutex raw↔factory); сводка стадий — в «Отчёты звеньев» из stdout процесса
  (паттерн звена factory); `_wait_for_raw` (поллинг count_rows) удаляется —
  subprocess синхронный. Звено stats аналогично → `cf collect performance`.
  Вебхучный контур (`_default_post`, `_webhook_token`, dashboard.workflows)
  пока НЕ удаляется — он нужен приёмке этапа 4 как путь запуска n8n; удаление
  — этап 6 (уже расписан в спеке §6 п.6).
  **Приёмка:** тесты раннера: ▶ raw вызывает run_command с collect-командами
  по очереди, сбой первой не прячет вторую (обе сводки в отчёте, статус
  error); stats зовёт performance; mutex raw↔factory сохранён (существующие
  тесты зелёные).
  **Effort:** M

- [x] **C3.3 · `cf apply-sources` → реестр `sources/`** — `src/cf/sourcepatch.py`,
  `src/cf/cli.py` (cmd_apply_sources), промпт `prompts/agents/source-tuner.md`
  **Суть:** proposal применяется к `sources/*.json` (add/pause/retire записей)
  вместо правки SOURCES-блока в JS; git-коммит вместо `cf push-n8n`;
  `parse_sources_block`/JS-путь остаётся как legacy-ветка до этапа 6 (n8n
  продолжает собирать по своим спискам — расхождение реестра и JS допустимо
  и завершится этапом 5). source-tuner.md обновить: целевой формат — реестр.
  **Приёмка:** тест: proposal add+pause на фейковом реестре → корректный JSON,
  статусы/origin/added_at проставлены; JS-файлы не тронуты; validate sources
  зелёный после применения.
  **Effort:** M

- [x] **C3.4 · Прогон `cf validate` по новым артефактам** — `src/cf/validate.py`,
  `schemas/`
  **Суть:** схема `sources.schema.json` (запись реестра), валидация JSONL
  dry-run (строка = канонические колонки вкладки); `cf validate sources`.
  **Приёмка:** битая запись реестра (нет kind/status) → invalid с внятной
  ошибкой; валидный реестр — ok.
  **Effort:** S

- [x] **C3.5 · Документация порта** — CLAUDE.md, README, docs/n8n-integration.md,
  RUNBOOK
  **Суть:** CLAUDE.md: подкоманда collect, реестр sources/; README §11: как
  гонять pytest-порт, судьба `node --test` (эталон до этапа 6);
  n8n-integration.md: баннер «сбор портирован, n8n — эталон приёмки до
  этапа 6»; RUNBOOK §1: строка про apify-token.txt (уже на VPS и в менеджере
  паролей — сверить формулировку).
  **Приёмка:** сверка CLAUDE.md с `cf --help` без пропусков; grep по
  устаревшим утверждениям («сбор только в n8n») пуст.
  **Effort:** S

---

## C4 · Умные источники — exploration-контур (4 задачи, спека §2.1)

Детерминированный runtime, интеллект — в proposals. Пороги ниже — первичные
константы (`sources.exploration` в cf.config.json), калибруются после месяца
данных.

- [x] **C4.1 · Exploration-квота в ежедневном сборе** — `collect/sources.py`,
  `collect/tiktok.py`, `collect/instagram.py`
  **Суть:** к active-батчам добавляется 1 батч (≤4) кандидатов на платформу:
  детерминированная ротация `status=candidate` по `added_at` + счётчику
  прогонов (`runs_count`, `last_run_at` пишутся в запись реестра после
  прогона; коммитит раннер — механика decision_git). Без LLM.
  **Приёмка:** тест: 6 кандидатов → в двух последовательных прогонах разные
  четвёрки (ротация); 0 кандидатов → только active-батчи; поле runs_count
  инкрементится.
  **Effort:** M

- [x] **C4.2 · Harvest кандидатов из raw** — новый шаг после сбора в
  tiktok.py/instagram.py
  **Суть:** детерминированная выжимка: хэштеги из caption строк, прошедших
  гейт с `engagement_rate ≥ 0.05` ИЛИ `views ≥ p75` батча; минус уже
  известные (любой status), минус стоп-лист платформы; топ-N по частоте
  (N=5/прогон) → записи `status=candidate, origin=harvest`. IG-discovery
  остаётся отдельным источником кандидатов (`origin=discovery`, уже в C2.2).
  **Приёмка:** тест на фикстуре raw-строк: известные/стоп-лист отсечены, топ
  по частоте корректен, записи с полным набором полей; повторный harvest
  идемпотентен.
  **Effort:** M

- [x] **C4.3 · Взросление и отсев кандидатов** — `collect/sources.py`
  (`age_candidates`), вызов из ежедневного цикла
  **Суть:** кандидат с `runs_count ≥ 2`: (а) yield-порог — суммарно
  `rows_passed_gate < 3` → `status=retired` автоматически (git-коммит +
  строка в сводке для Telegram-отчёта этапа 3); (б) `rows_passed_gate ≥ 8`
  И `median_er ≥ 0.8 ×` медианы active-источников платформы за 14 дней (по
  атрибуции source_query, механика sourcestats) → proposal на перевод в
  active (файл в `proposals/`, статус proposed). Между порогами — ждёт
  следующих прогонов. Активные списки руками не меняются — только approve
  proposal в Лаборатории (паттерн формул).
  **Приёмка:** тесты: кандидат 2 прогона / 1 строка → retired; 10 строк с
  высоким ER → proposal-файл с evidence (цифры, source_urls); середина → без
  изменений; авто-retire логируется в run_log.
  **Effort:** M

- [x] **C4.4 · /cf-tune-sources на реестр** — `.claude/commands/cf-tune-sources.md`,
  `prompts/agents/source-tuner.md`, `src/cf/sourcestats.py`
  **Суть:** еженедельный агент читает реестр + source-stats (грамматика
  source_query не меняется) и готовит evidence-backed proposal:
  повысить/приостановить/добавить; формат proposal — операции над реестром
  (совместим с C3.3); при новой нише (/cf-classify-niche) — стартовый набор
  источников отдельным proposal.
  **Приёмка:** сухой прогон промпта на фикстурах даёт валидный proposal
  (`cf validate proposal`); в промптах нет упоминаний правки JS/push-n8n.
  **Effort:** S

---

## Порядок и зависимости

C1.1 → C1.2..C1.7 (параллельны) → C2.1..C2.4 (C2.3 после C2.1; C2.4
независим) → C2.5 → C2.6 → C3 (C3.2 после C2.6; C3.3/C3.4 после C1.7) →
C4 (C4.1 после C2.1/C2.2; C4.3 после C4.1).

После завершения: полный `pytest -q`, смоук `cf collect tiktok --dry-run` с
живым Apify (1 батч), ручное ревью JSONL, коммит фазы. Дальше — этап 3
гайда (systemd, Caddy, таймеры — отдельный план).

## Вне скоупа этого плана

Telegram-уведомления (`notify.py`, спека §5 — этап 3), systemd/Caddy/деплой
(этап 3), удаление n8n-контура из кода и конфига (этап 6), TikTok-discovery
(спека: опционально, позже).
