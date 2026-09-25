# Интеграция с n8n

> **АРХИВ (решение оператора 2026-07-24, работа над аудитом): n8n-сбор
> СПИСАН.** Единственный путь сбора и дозамера — Python (`cf collect
> tiktok|instagram|snowball|performance`). Все CF-воркфлоу в живом n8n
> деактивированы (включая CF 04 — его вебхук cf-stats был открыт без auth,
> находка H7). Каталог `n8n/` заморожен как исторический эталон портирования;
> pull-n8n/push-n8n оставлены только для чтения архива. План Б при аварии —
> чинить Python-путь, а не реактивировать дрейфнувшие воркфлоу (находки H2,
> M4/M15: фолбэк затирал бы транскрипты и атрибуцию). Инстанс n8n живёт для
> будущего publish-звена (этап 6).
> Всё ниже — ИСТОРИЧЕСКОЕ описание контура до списания.

**Изменение 2026-07-10 (решение оператора):** генерация брифов перенесена из n8n
в агентскую систему — /cf-generate-briefs (prompts/agents/brief-generator.md).
n8n больше не читает промпты из GitHub и не пишет брифы. Воркфлоу CF 03 (старый
генератор через OpenAI) устарел; CF 03v2 удалён.

## Воркфлоу версионируются в репозитории (2026-07-15)
Все 4 воркфлоу — CF 01 TikTok, CF 01 Instagram, CF 01b Snowball и CF 04 Performance
(`n8n/cf04-performance/`, с 2026-07-22) — лежат в `n8n/<slug>/`
(workflow.json + код Code-узлов в `code/*.js`). Правки — только через репозиторий:
`cf pull-n8n <id> --dir n8n/<slug>` забирает текущее состояние,
`cf push-n8n n8n/<slug>` заливает обратно. Правки в UI n8n не делать
(или сразу забирать pull'ом), иначе push их затрёт.

Сбор проходит через **Ingestion Gate** (Code-узел в каждом CF 01): дропает рекламу
(isAd/isSponsored), посты старше 30 дней, нецелевые языки (не ru/en/un, только TikTok),
блок-лист каталожных авторов (wb.* и явный список), капшены с >9 хэштегами и залежавшиеся
посты (<500 views при возрасте >14 дней). Счётчики дропов И потерь батчей (P5.14:
`batches_failed`/`sources_lost`/`lost_sources`) — в execution-логе n8n (console.log
узла). Константы гейта — в `n8n/<slug>/code/Ingestion-Gate*.js`.

Атрибуция: каждая строка несёт `source_query` (`hashtag:#x` / `query:x` /
`snowball:<seed_url>` / `unknown`). Списки хэштегов/запросов живут в SOURCES-блоке
`n8n/cf01-*/code/Build-*.js` и меняются только через proposal-цикл:
`/cf-tune-sources` → `proposals/*-sources-*.json` (pending) → ревью оператора →
`cf apply-sources <proposal>` → `cf push-n8n` → git commit.

CF 01b Snowball (еженедельно, вс 09:00 + webhook `cf-snowball-tiktok`): читает вкладку
`CF Seeds` (заполняется `cf export-seeds` из approved-формул), тянет related videos
каждой затравки через Apify (`postURLs` + `scrapeRelatedVideos`), гейт тот же.

## P1.16 · Повторный сбор не затирает transcript_text/source_query/collected_at (ручная доводка графа)

**Проблема.** «Write CF Raw TikTok» пишет в лист `CF Raw TikTok` в режиме
`appendOrUpdate` с `matchingColumns=['raw_id']`. На update Google-Sheets-нода
перезаписывает **все 23 маппленные колонки** — по колонке пропустить нельзя.
Поэтому повторный сбор того же видео без субтитров кладёт `transcript_text=''`
поверх уже скачанного транскрипта; snowball вдобавок переписывает `source_query`
(`hashtag:#x` → `snowball:...`), ломая атрибуцию первого сбора; `collected_at`
(first-seen) тоже перетирается свежим временем.

**Решение — coalesce перед записью.** Готовая и покрытая юнит-тестами чистая
логица лежит в `n8n/cf01-tiktok/code/Coalesce-Existing-Raw.js` (и байт-в-байт
копия `n8n/cf01b-snowball/code/Coalesce-Existing-Raw.js`). Для СУЩЕСТВУЮЩИХ
`raw_id` она сохраняет `transcript_text` (когда новое значение пустое →
оставить прежний), `source_query` и `collected_at`; для новых `raw_id` пишет
incoming как есть; все прочие колонки (метрики, caption, processing_status,
raw_json …) всегда берут свежее значение. Тесты — `n8n/__tests__/coalesce.test.js`.

**Почему не залито в workflow.json автоматически (P1.16).** Граф — боевой,
живого n8n для проверки нет, а корректность зависит от рантайм-семантики n8n,
которую офлайн-валидацией JSON не проверить: (1) `$('Read Existing CF Raw').all()`
вернёт строки только если read-нода **успела выполниться** в этом же прогоне —
а Google-Sheets-read заменяет items листовыми строками, поэтому её нельзя
поставить «в линию» основного потока, только боковой веткой; (2) в Coalesce
сходятся **две** ветки-фидера (no-subtitle + Attach), а Code-нода с несколькими
входами исполняется отдельно на каждую ветку. Кривой граф молча ломает сбор,
поэтому доводка — руками в UI с живым тестом. Оркан-файл `Coalesce-Existing-Raw.js`
безопасен: `cf push-n8n` инъектит код только в узлы, присутствующие в workflow.json,
так что до появления ноды файл просто игнорируется.

**Шаги оператора (для КАЖДОГО из CF 01 TikTok и CF 01b Snowball):**
1. Добавить Google-Sheets-ноду **`Read Existing CF Raw`** (operation `read`),
   скопировав у «Write CF Raw TikTok» ровно: `documentId` =
   `<SPREADSHEET_ID>`, `sheetName` = `CF Raw TikTok`,
   credential **CF Sheets Service Account** (`EL6SGqYTslRa3i9v`). Оба воркфлоу
   пишут в один и тот же лист, конфиг read-ноды одинаков.
2. Добавить Code-ноду **`Coalesce Existing Raw`** (`n8n-nodes-base.code`, режим
   *Run Once for All Items*) и вставить в `jsCode` **дословно** содержимое
   `n8n/cf01-tiktok/code/Coalesce-Existing-Raw.js` (для обоих воркфлоу — тот же
   файл; имя ноды обязано быть `Coalesce Existing Raw`, иначе
   `$('Read Existing CF Raw')` и round-trip кода сломаются). Имя файла
   `Coalesce-Existing-Raw.js` = `code_filename('Coalesce Existing Raw')`.
3. Перекоммутировать связи:
   - было: `Has TikTok Subtitle URL?` (ветка **false**, без субтитров) → `Write CF Raw TikTok`;
     `Attach TikTok Transcript` → `Write CF Raw TikTok`.
   - стало: обе эти связи → **`Coalesce Existing Raw`**, и `Coalesce Existing Raw` → `Write CF Raw TikTok`.
   - `Read Existing CF Raw` подключить боковой веткой от раннего узла, чтобы она
     ГАРАНТИРОВАННО выполнилась до Coalesce: рекомендуется от выхода
     `Normalize TikTok Raw Rows` (CF 01) / `Ingestion Gate` (CF 01b) вторым ребром
     (её выход никуда дальше не идёт — Coalesce берёт данные по имени через
     `$('Read Existing CF Raw').all()`). Так короткая read-ветка успевает
     завершиться раньше, чем длинная субтитровая ветка дойдёт до Coalesce.
4. **Обязательная проверка живым двойным сбором** (без неё не доверять графу):
   запустить воркфлоу дважды на одном и том же видео (первый прогон со скачанным
   транскриптом, второй — с пустым/без субтитров) и в листе `CF Raw TikTok` убедиться:
   `transcript_text` не обнулился, `source_query` = атрибуция первого сбора,
   `collected_at` = время первого сбора, а `views/likes/…` и `processing_status`
   обновились свежими. В данных выполнения Coalesce-ноды проверить, что
   `$('Read Existing CF Raw')` вернул строки (не пусто) — иначе поправить порядок веток.
5. Синхронизация: после доводки в UI забрать `cf pull-n8n <id> --dir n8n/<slug>`
   для обоих воркфлоу. Pull перезапишет `code/Coalesce-Existing-Raw.js` кодом из
   ноды — поэтому в шаге 2 код вставляется дословно из файла, чтобы round-trip
   был чистым. Затем git commit. Дальнейшие правьте только через репозиторий
   (`cf push-n8n`), как и остальные ноды.

## P5.14 · Apify: шардинг вместо run-sync «всё или ничего»

**Проблема.** Раньше каждый CF 01 слал ОДИН `run-sync-get-dataset-items` со всем
входом (TikTok: 14 хэштегов + 10 запросов; Instagram: до 28 тегов) с потолком
300 с и `onError: stopWorkflow`. Один медленный/битый прогон (несуществующий
хэштег, таймаут, забаненный актор) валил весь дневной сбор с нулём строк.

**Решение (минимальный вариант плана) — шардинг + onError:continue + учёт потерь.**
Живого n8n нет, поэтому JS-логика (батчинг, форвардинг ошибок, счётчик) вынесена
в `code/*.js` и покрыта node-тестами; правки HTTP-нод (onError/timeout/URL) — в
`workflow.json`. Реализовано во всех трёх воркфлоу:

1. **Шардинг = Build отдаёт N item'ов.** HTTP-нода n8n исполняется ОДИН РАЗ НА
   ВХОДНОЙ item, поэтому отдельная SplitInBatches-нода не нужна: Build дробит вход
   на батчи по ≤4 источника и возвращает N item'ов → N независимых run-sync.
   - TikTok `Build-TikTok-Actor-Input.js`: 24 источника → 6 батчей по ≤4
     (`tiktok_hashtags`/`tiktok_search_queries` — подмножества батча).
   - Instagram `Normalize-Instagram-Hashtags.js`: до 28 тегов → батчи по ≤4;
     `source_query` на КАЖДОМ батче = полный список (downstream Prepare/Normalize
     читают его через `.first()`, резать нельзя).
   - Instagram **reel-стадия** `Prepare-Instagram-Reel-Transcript-Input.js`: ≤30
     reel-URL → батчи по ≤6 → N run-sync к reel-scraper. `tag_by_code` и
     `source_query` — ПОЛНЫЕ на каждом батче (downstream `Normalize Instagram Raw
     Rows` читает их `.first()`). Пустой вход → `[]` (reel-fetch не стартует).
   - Snowball `Build-Snowball-Input.js`: уже был один item на seed (естественный
     батч на затравку); добавлены только batch-метаданные и onError:continue.
   - Каждый батч несёт `batch_index`, `batch_total`, `batch_size`, `batch_sources`
     (`['hashtag:#x','query:y','snowball:<url>']`) — для учёта потерь.
2. **`onError: continueRegularOutput`** на всех run-sync Apify-нодах (было
   `stopWorkflow`): упавший батч не роняет остальные. Таймаут снижен до 180 с на
   батч там, где батчи стали мелкими: TikTok fetch, IG hashtag-scraper И **IG
   reel-scraper** (батчи ≤6, было 300 с). IG discovery (180 с) и snowball (300 с
   на seed) — без изменений. У Instagram discovery тоже стал continue: провал
   дискавери деградирует до seed-хэштегов, а не убивает сбор.
   - **Архитектурная цена.** HTTP-нода n8n обрабатывает входные item'ы
     ПОСЛЕДОВАТЕЛЬНО (не параллельно), поэтому суммарное время растёт: worst-case
     TikTok ≈ 6 батчей × 180 с × (до 2 попыток retryOnFail) ≈ до ~36 мин. Для
     ежедневного крона это ок; при ручном запуске с дашборд-вебхука помнить о
     задержке (вебхук ответит только по завершении графа).
3. **Учёт потерь.** Упавший run-sync при continue отдаёт item-маркер с `error`.
   - `Normalize-*` (TikTok/Snowball) больше НЕ фабрикует из него мусорный ряд:
     распознаёт маркер (`isApifyError`: есть `error` и нет id/url) и форвардит
     **loss-маркер** `{__apify_error, batch_index, batch_sources}` (источники
     батча берутся из Build по `itemMatching`).
   - **Ingestion Gate** делит вход на loss-маркеры и реальные ряды, исключает
     маркеры из данных и пишет в execution-лог сводку с `batches_failed`,
     `sources_lost`, `lost_sources`. `batches_failed` считается по УНИКАЛЬНЫМ
     `batch_index` (retry одного батча не двоит счётчик). У Instagram IG-Gate
     распознаёт нативные `{error}` прямо на выходе hashtag-scraper (Normalize между
     ними нет), берёт `batch_sources`/`batch_index` из `Normalize Instagram Hashtags`
     по `itemMatching`; детект строгий (есть `error` И нет id/url — per-hashtag
     ошибка внутри успешного прогона актора не считается упавшим батчем).
   - **Полный провал — громкий throw во всех трёх гейтах.** Если реальных рядов
     ноль, но был ≥1 упавший батч (`dataItems.length === 0 && losses.length > 0`) —
     гейт валит ран (симметрично reel-стадии), чтобы «зелёный ран с нулём строк»
     (регресс против прежнего `stopWorkflow`) не проходил тихо. Частичный успех
     (есть хоть один реальный ряд) — не throw, потери в сводке.
   - **Instagram reel-стадия — учёт в `Normalize Instagram Raw Rows`** (IG-Gate стоит
     ВЫШЕ reel-стадии и её не видит; между Normalize-Raw и Write гейта нет). Раньше
     он делал `throw`, если ни один reel-item не годен, — при одном run-sync это
     роняло весь ран, хотя hashtag-батчи доехали. Теперь при **частичном успехе**
     упавшие reel-батчи пропускаются (мусор в Sheets не пишем), а сводка
     `batches_failed`/`reels_lost` идёт в execution-лог; `reel_urls` упавшего батча
     берутся из `Prepare` по `itemMatching`. `throw` остаётся ТОЛЬКО на полный ноль
     (item'ы пришли, но 0 годных по ВСЕМ reel-батчам) — чтобы полный провал был
     громким.
4. **Id акторов — в ОДНОМ месте на воркфлоу.** Прежде id был захардкожен в URL
   каждой HTTP-ноды. Теперь:
   - TikTok/Snowball: `const ACTOR_PATH` в Build → `actor_path` на КАЖДОМ item;
     URL-нода читает `={{ '.../acts/' + $json.actor_path + '/run-sync...' }}` (item
     уже несёт поле — не зависим от имени Build-ноды).
   - Instagram: `const ACTORS = {discovery, hashtag, reel}` в Build → `actors`;
     три URL-ноды читают `$('Build Instagram Search Input').first().json.actors.*`.
   - **Забанили/сменили актора → правка в одном месте** (Build-нода), без трогания
     трёх URL. Замену actor-id подхватывает `cf push-n8n`.
   - «Одно место на ВСЕ три воркфлоу» офлайн невозможно (Code-ноды n8n не читают
     ФС репо, cf.config.json читает только python-сторона). Кросс-воркфлоу-источник
     — это n8n **Variables** (`$vars.apify_actor_*`), но их заведение операторское
     (UI/env self-hosted). Оставлено как апгрейд (см. шаги оператора).

**Инвариант (проверяется node-тестом `workflow-structure.test.js`).** Все run-sync
Apify-ноды обязаны иметь `onError=continueRegularOutput` и URL-экспрешн из
actor-конфига (не хардкод id); связи графа ссылаются на существующие ноды; у каждой
Code-ноды есть файл `code/*.js`. Тесты логики — `batching.test.js`, `losses.test.js`.

**Что НЕ проверено офлайн (концерны — операторская приёмка на живом n8n):**
- Точная форма item-маркера при `continueRegularOutput` зависит от версии n8n.
  Логика ждёт item с полем `error` и без id/url; если конкретный n8n кладёт ошибку
  иначе — поправить `isApifyError` (в `Normalize-*`/`Ingestion-Gate*`). Пер-source
  гранулярность (`sources_lost`) держится на `itemMatching` от Build/Normalize к
  упавшему item — если pairedItem не доходит, `batches_failed` всё равно считается,
  а `sources_lost` может быть 0.
- **Приёмочный прогон с искусственно битым батчем** (для каждого воркфлоу):
  подсунуть несуществующий хэштег/таймаут в один батч и убедиться, что остальные
  батчи доехали (строки в листе есть), а в execution-логе Ingestion Gate виден
  `batches_failed>=1` и перечень `lost_sources`. **Instagram — отдельно проверить
  reel-стадию**: сломать один reel-батч (несуществующий/битый reel-URL или таймаут)
  и убедиться, что остальные reel-батчи записались, а `Normalize Instagram Raw Rows`
  в логе показал `batches_failed>=1`/`reels_lost>0` и НЕ упал (throw только когда
  падают ВСЕ reel-батчи).
- `activeVersion.nodes` в workflow.json — инертный снапшот n8n (push шлёт весь JSON,
  но n8n использует top-level `nodes`); P5.14 правит только top-level. При следующем
  `cf pull-n8n` снапшот перегенерируется.
- **Run Log — это execution-лог n8n**, а не лист CF Run Log (в CF Run Log пишет
  только python-сторона через `runlog.log_run`; n8n туда не пишет). Если нужны
  потери в листе CF Run Log — операторский follow-up: добавить в конце воркфлоу
  Google-Sheets-ноду, пишущую строку `run_log` с `errors=lost_sources` (вне скоупа
  минимального варианта).

**Шаги оператора:**
1. `cf push-n8n n8n/cf01-tiktok` (и `…/cf01-instagram`, `…/cf01b-snowball`) —
   зальёт код `code/*.js` в ноды и HTTP-правки. Без push Build не начнёт отдавать
   `actor_path`/`actors`, и URL-экспрешн даст `.../acts/undefined/...`. Заливать
   только через `cf push-n8n`: ручной импорт `workflow.json` в UI n8n даст СТАРЫЙ
   код у нескольких нетронутых P5.14 нод (inline jsCode в workflow.json дрейфует —
   истина в `code/*.js`, push инъектит из файлов).
2. Прогнать приёмочный тест с битым батчем (см. выше) на каждом воркфлоу.
3. (Опц.) Кросс-воркфлоу actor-id: завести n8n Variables `apify_actor_tiktok`,
   `apify_actor_ig_*` и заменить `const ACTOR*`-блоки на чтение `$vars.*`.

## Auth на вебхуки дашборда (X-CF-Token, 2026-07-21)
Webhook-ноды CF 01 TikTok / CF 01 Instagram / CF 01b Snowball требуют Header Auth:
запрос без заголовка `X-CF-Token` получает 403. Дашборд (`_default_post` в
`src/cf/dashboard/runner.py`) читает токен из файла `n8n.webhook_token_file`
в cf.config.json (`~/.cf/secrets/webhook-token.txt` — вне репо и папки синка,
на Windows это `%USERPROFILE%\.cf\secrets\`) и шлёт заголовок
сам. Нет файла или пуст — warning в лог и POST без заголовка (мягкая деградация
на время настройки).

Шаги оператора при первичной настройке или ротации токена:
1. Сгенерировать токен: `python -c "import secrets; print(secrets.token_hex(32))"`
   → положить в `~/.cf/secrets/webhook-token.txt` (одной строкой).
2. В UI n8n создать credential типа **Header Auth** с именем `CF Webhook Token`:
   Name = `X-CF-Token`, Value = токен из файла.
3. В workflow.json репозитория у webhook-нод credential указан с
   `id: REPLACE_AFTER_CREATING_CREDENTIAL` — после создания credential либо
   выбрать его на нодах в UI и забрать `cf pull-n8n`, либо вписать реальный id
   и залить `cf push-n8n n8n/<slug>` для каждого воркфлоу.
4. Вебхук `cf-stats` живёт в CF 04 Performance (версионирован в `n8n/cf04-performance/`) —
   на его webhook-ноде включить Header Auth с тем же credential «CF Webhook Token»
   прямо в UI n8n (затем забрать `cf pull-n8n`).
5. Проверка: `curl -X POST https://n8n.example.com/webhook/cf-raw-tiktok` без
   заголовка → 403 (и так для cf-raw-instagram, cf-snowball-tiktok, cf-stats);
   запуск звена raw с дашборда проходит.

Токен кэшируется на процесс: при ротации нужен перезапуск дашборда. Первичная
настройка подхватывается на лету — файл, появившийся после старта, дашборд
начнёт слать со следующего POST без рестарта.

## Что n8n пишет в Google Sheets
- Raw-данные TikTok/Instagram → CF Raw TikTok / CF Raw Instagram, ежедневно (CF 01).
  Имена колонок n8n (url, author, created_at) маппятся на канонические
  (source_url, account, posted_at) через column_aliases в cf.config.json — n8n менять не нужно.
  Колонку `niche` n8n оставляет пустой — её заполняет /cf-classify-niche по контенту.
- Performance-строки → CF Performance (CF 04, Apify; запуск вручную или вебхуком
  `cf-stats`, крона нет).

Доступ n8n к таблице — credential «CF Sheets Service Account» (тот же service account,
что у CLI; не протухает). OAuth-credential «Google Sheets account» устарел.

## P5.1 · Контур публикации: CF Published Reels + CF 04 Performance

Цикл замыкается, только когда после публикации ролика в лист **CF Published Reels**
попадает строка: без неё `cf trace` показывает `MISSING published_reels`, а eval и
атрибуция не работают (нечего join'ить по `brief_id`/`prompt_version`).

**Кто пишет CF Published Reels.** НЕ n8n — оператор, одним из двух путей (общая логика
`cf.publish.mark_published`, поэтому CLI и дашборд не расходятся):
- CLI: `cf mark-published <brief_id> <url> [--notes "…"]`;
- дашборд: форма «Опубликован → URL рилса» на карточке одобренного брифа
  (`POST /briefs/{id}/published`). После записи бриф в списке переходит в третье
  состояние «опубликован» (approved + есть рил, join по `brief_id`).

`reel_id` разбирается из URL (TikTok `/video/<id>`, Instagram `/reel|/reels|/p/<code>`;
иначе — последний сегмент пути, без краша); `prompt_version` подтягивается из строки
брифа; `published_at` — штамп с временем; валидация: `brief_id` обязан существовать в
CF Creative Briefs, URL обязан быть http(s), иначе ошибка и НИЧЕГО не записано.

**Контракт колонок CF Published Reels** (канонические имена; фактические — через
`column_aliases.reels` в cf.config.json):

| канон | факт в листе | источник | заметка |
|---|---|---|---|
| `reel_id` | `published_id` | разбор URL | алиас уже в cf.config.json |
| `brief_id` | `brief_id` | аргумент | FK на CF Creative Briefs |
| `platform` | `platform` | хост URL | `tiktok` / `instagram` / пусто |
| `post_url` | `post_url` | аргумент | полный http(s)-URL рилса |
| `published_at` | `published_at` | `now_iso()` | штамп с временем |
| `prompt_version` | `prompt_version` | строка брифа | **новая колонка — см. ниже** |
| `production_notes` | `production_notes` | `--notes` | отступления при съёмке |

⚠️ **Действие оператора (одноразовое): добавить колонку `prompt_version` в лист
CF Published Reels.** До сих пор контракт листа был без неё (см. README). Пока колонки
нет, `mark_published` отработает, но значение `prompt_version` тихо отбросится на записи
(`append_row` пишет только по заголовкам листа и печатает warning) — eval потеряет
привязку рила к версии промпта. Колонки `reel_id`→`published_id` уже смаплены; остальные
колонки контракта в листе есть.

**CF 04 Performance — обязательные поля для атрибуции.** Строки CF 04 (Apify; запуск
вручную или вебхуком `cf-stats`, крона нет) ОБЯЗАНЫ нести **`brief_id`** и **`measured_at`**, иначе цепочка
brief→reel→performance рвётся:
- без `brief_id` не join'ится performance с брифом/формулой (`cf trace` покажет
  `MISSING performance_rows`, `cf eval-prep`/`build_eval_dataset` не сгруппируют по
  `prompt_version`);
- без `measured_at` `cf eval-prep --since` и ретенция/архив не отфильтруют замеры по дате.
Также нужен `reel_id` (алиас `published_id`) и `er` (алиас `engagement_rate`) — оба уже
в `column_aliases.performance`. CF 04 берёт `reel_id`/`brief_id` из CF Published Reels
по `post_url`, поэтому строка публикации (выше) — предусловие корректной атрибуции.

✅ **Выполнено: CF 04 версионирован в `n8n/cf04-performance/`** (коммит 40bf69b,
2026-07-22). Как и у прочих воркфлоу (раздел «Воркфлоу версионируются…»), правки CF 04
(в т.ч. добавление колонок `brief_id`/`measured_at` в Google-Sheets-ноду) — только
через репозиторий (`cf push-n8n n8n/cf04-performance`), не в UI.

## Кто пишет брифы
/cf-generate-briefs: читает formulas/_approved/index.json и промпт ниши
prompts/briefs/{niche}/reel.md (активная версия из CF Prompt Versions), генерирует бриф,
валидирует по schemas/brief.schema.json и пишет pending-строку через `cf add-brief`.
Запускается после утверждения формулы или по запросу оператора.

## Триггер ревью брифов (решение открытого вопроса №3 спеки)
MVP: оператор запускает `/cf-review-brief --pending` — обрабатываются все pending разом.
Отдельный webhook-сервер не строим — вне скоупа MVP.

## Ритм
| Поток | Кто | Частота |
|---|---|---|
| Сбор raw-данных | n8n (CF 01) | ежедневно |
| Snowball от winners | n8n (CF 01b) | еженедельно (вс 09:00) |
| Тюнинг источников | /cf-tune-sources → proposal → оператор | еженедельно |
| Генерация брифов | /cf-generate-briefs | после утверждения формулы / по запросу |
| Ревью брифов | /cf-review-brief --pending | по мере появления pending |
| Performance | n8n (CF 04) | вручную / вебхук `cf-stats` (крона нет) |
| Авто-пауза формул (`cf formula-guard`) | шаг 5 фан-аута (`_fanout_guard`) | ежедневно (в утреннем цикле P5.3) |
| Eval | /cf-eval | еженедельно |
