# Умный сбор raw-данных (smart ingestion) — дизайн

Дата: 2026-07-15. Статус: утверждён оператором на уровне подхода («гейт → атрибуция →
воронка → snowball → петля скоринга»), детали — в этом документе.

## Проблема (evidence, диагностика 2026-07-15)

Сбор идёт по статичному рукописному списку хэштегов/запросов. Диагностика raw-вкладок
(`agent-runtime/tmp-tt.json`, `tmp-ig.json`, профили в `agent-runtime/profiles/`):

**CF Raw TikTok, 1448 строк после дедупа:**
- Целевая ниша «мужские-образы» — 104 строки (7%); «other» — 324 (22%); без ниши — 565 (39%).
- 32% строк с views < 1000 — метод анализа v2 их отбрасывает порогом; Apify-бюджет потрачен зря.
- 187 строк старше 90 дней (до 2020 г.) — несмотря на `videoSearchDateFilter=PAST_WEEK`
  (фильтр действует только на searchQueries, hashtag-раздел тянет старьё).
- transcript_text заполнен у 33%.
- Топ-авторы — каталожные WB-аккаунты: wbinsidik (14), wb.naxodkaaa (12), wb.kiko2 (9).
- 82 строки isAd/isSponsored — реклама не фильтруется.
- Языки (textLanguage): en 653, ru 327, un 319, прочие (es/pt/de/vi/…) ~120 — явный мусор.
- `source_query = "managed_tiktok_keywords"` у всех строк — атрибуции нет. При этом
  в `raw_json` она ЕСТЬ: `searchHashtag.name` у 673 строк, `searchQuery` у 773.

**CF Raw Instagram, 257 строк:** целевая ниша 25%; 42% постов старше 90 дней
(hashtag-scraper отдаёт топ хэштега за всё время); 14% капшенов — простыни >=10 хэштегов;
`source_query` — общая строка со списком хэштегов, per-reel атрибуция теряется в узле
«Normalize Instagram Hashtags» (схлопывает всё в один список).

Следствия ниже по пайплайну задокументированы в
`.claude/memory/decisions/2026-07-14-pipeline-quality-gates.md`: загрязнение ниш,
брифы-мусор, ручная чистка на каждом шаге.

## Цель

1. Precision: доля строк целевых ниш и полнота транскриптов растут; реклама, каталоги,
   старьё и нецелевые языки не попадают в таблицу.
2. Recall: сбор выходит за пределы рукописного списка (related videos от winners).
3. Самообучение: списки хэштегов/запросов управляются данными через петлю
   «атрибуция → скоринг → proposal → одобрение оператора» — по образцу prompt-proposals.

Не-цели (вынесено на потом): velocity-замеры (повторный скрейп через 48ч), авторский
канал сбора (profile-scraping), FYP-аккаунты, платные тренд-платформы.

## Архитектура

Разделение зон по CLAUDE.md сохраняется: n8n = runtime-сбор (правится через n8n API,
воркфлоу CF 01 TikTok `w2FRPA06jd2Eqb5u`, CF 01 Instagram `wSGoTAJ34612XYLO`);
Claude Code = intelligence (CLI-команды, агент, proposals).

### 1. Атрибуция source_query (n8n, обе платформы)

- TikTok, узел «Normalize TikTok Raw Rows»: `source_query` = `hashtag:#<searchHashtag.name>`
  либо `query:<searchQuery>` из соответствующих полей Apify-итема; fallback `unknown`.
- Instagram: hashtag-scraper этап строит map `reel_url → hashtag`; узлы «Prepare …» и
  «Normalize Instagram Raw Rows» протаскивают её до строки: `hashtag:#<tag>`;
  discovery-хэштеги (не из seed-списка) помечаются `hashtag+disc:#<tag>`.
- Бэкфилл истории: новая CLI-команда `cf backfill-attribution <tab>` парсит `raw_json`
  существующих строк и заполняет `source_query` батч-апдейтом (только там, где сейчас
  заглушка). TikTok покрывается полностью; IG-строки без данных остаются `unknown`.

### 2. Гейт на входе (n8n, до записи в Sheets и до скачивания субтитров)

Новый Code-узел «Ingestion Gate» в каждом CF 01, конфиг — констант-блок в начале узла.
Отбрасывает строку, если:
- `isAd || isSponsored` (TikTok);
- `posted_at` старше `maxAgeDays` (default 30);
- `textLanguage` не в allowlist `{ru, en, un, ''}` (TikTok; у IG поля нет — пропуск);
- автор в блок-листе или матчится паттерном каталога (`^wb[._]`, `^вб`, явный список:
  wbinsidik, wb.naxodkaaa, wb.kiko2, baza.store.kz, kingsman.premium, dom_sumok42 …);
- капшен содержит >= 10 хэштегов;
- `views < minViewsStale` (default 500) при возрасте поста > 14 дней (свежим строкам
  низкие views прощаются — они ещё растут).
Каждый дроп логируется счётчиком по причинам в execution-лог n8n (console.log узла
Ingestion Gate), чтобы гейт был наблюдаемым, а не тихим.

### 3. Двухступенчатая воронка (n8n)

Смысл: платить за тяжёлое (субтитры, обложки, reel-scraping) только для строк, прошедших гейт.
- TikTok: гейт ставится ПЕРЕД «Has TikTok Subtitle URL? → Download Subtitle»;
  `resultsPerPage` поднимается 8 → 20 (охват x2.5 при экономии на отбракованных).
- Instagram: после hashtag-scraper (дешёвые метаданные) — гейт; reel-scraper
  (транскрипты) вызывается только для выживших URL; потолок `slice(0,10)` заменяется
  на лимит после фильтрации (30).

### 4. Snowball от winners (новый воркфлоу «CF 01b Snowball TikTok»)

- Источник затравок: новая вкладка Google Sheets `CF Seeds` (колонки: seed_url, seed_type
  [winner|author], niche, added_at, active). Наполняется командой `cf export-seeds` из
  верифицированных evidence-URL паттернов/формул; оператор может добавлять руками.
- Воркфлоу (запуск еженедельно, вс 09:00 + manual): читает active-строки CF Seeds →
  Apify `clockworks/tiktok-scraper` c `postURLs` + `scrapeRelatedVideos: true` →
  та же нормализация и гейт → `source_query = snowball:<seed_url>` → CF Raw TikTok.
- Instagram в MVP snowball не входит (нет дешёвого related-API).

### 5. Петля скоринга источников (intelligence)

- CLI `cf source-stats <tab> [--since]` — детерминированная агрегация по `source_query`:
  строк всего / с целевой нишей (`target_niches` из cf.config.json) / с views>=1000 /
  с транскриптом / winners (по паттерн-файлам, если есть); вывод в
  `agent-runtime/source-stats/YYYY-MM-DD-<tab>.json` + печать таблицы.
- Агент `/cf-tune-sources` (prompts/agents/source-tuner.md): читает свежие source-stats,
  предлагает: убрать источники с низким yield, добавить кандидатов (частотные хэштеги
  из капшенов строк целевых ниш с высоким ER — считает CLI, не LLM). Выход — proposal
  в `proposals/` со схемой (см. ниже), НИКОГДА не меняет списки сам.
- Применение: после одобрения оператором `cf apply-sources <proposal>` патчит
  констант-блоки узлов «Build … Input» через n8n API + git-коммит proposal со статусом.
  Human-in-the-loop идентичен циклу prompt-proposals (железное правило №3).
- `target_niches` добавляется в cf.config.json: `["мужские-образы","мужской-стиль","стритвир"]`.

### 6. Схема proposal источников

`schemas/source-proposal.schema.json`: { platform, remove: [{source, reason, stats}],
add: [{source, kind: hashtag|query, evidence}], generated_at, status:
pending|approved|rejected, applied_at }. Каждый remove/add обязан ссылаться на цифры
из source-stats (evidence-first, правило №1).

## Поток данных (итог)

n8n CF 01/01b: Apify (метаданные) → Normalize (+атрибуция) → Ingestion Gate →
[тяжёлое обогащение только выжившим] → Sheets. Еженедельно: `cf source-stats` →
`/cf-tune-sources` → proposal → оператор → `cf apply-sources` → n8n API.
Snowball-затравки: паттерны/формулы → `cf export-seeds` → CF Seeds → CF 01b.

## Обработка ошибок

- `raw_json` без атрибуционных полей → `source_query=unknown`, строка не выбрасывается.
- n8n API недоступен при apply → команда падает с текстом, proposal остаётся approved,
  повтор идемпотентен (патч сравнивает текущий код узла с ожидаемым).
- Гейт при пустом конфиге (нет блок-листа и т.п.) пропускает всё, кроме isAd — фейл-открытый.
- backfill-attribution пишет только пустые/заглушечные source_query — повторный запуск
  ничего не портит.

## Тестирование

- Unit (pytest, репо CF): парсер атрибуции из raw_json (фикстуры с searchHashtag /
  searchQuery / пусто), агрегация source-stats, генерация seeds, идемпотентность apply-патча.
- n8n: после правки — ручной запуск воркфлоу через API, сверка выборки строк в Sheets
  (атрибуция заполнена, дропы залогированы) — практика «сквозной пробы», как с фиксом likes.
- Приёмка через неделю сбора: повторный `cf profile` + `cf source-stats`; ожидание —
  доля строк с posted_at>30д ~0%, реклама 0%, транскрипты у прошедших гейт строк >80%,
  атрибуция у 100% новых строк.

## Порядок работ

1. Гейт + атрибуция + воронка в CF 01 TikTok (n8n API).
2. То же для CF 01 Instagram (с протаскиванием map hashtag→url).
3. `cf backfill-attribution` + прогон по обеим вкладкам.
4. `cf source-stats` + `target_niches` в конфиге.
5. Вкладка CF Seeds, `cf export-seeds`, воркфлоу CF 01b Snowball.
6. Промпт `/cf-tune-sources`, схема proposal, `cf apply-sources`.
7. Обновить docs/n8n-integration.md и CLAUDE.md (новые команды/ритм).
