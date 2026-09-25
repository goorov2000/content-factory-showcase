# 02 — Гибрид TikTok: хэштеги на дешёвом акторе, search на старом

**What to build:** Хэштег-батчи (74 активных источника) уходят в
`clockworks~tiktok-hashtag-scraper` — $0.002/видео, 99.8% успешных прогонов,
без actor-start и платных надбавок конструктивно (цены/статистика сверены по
API 10.08). Search-батчи (5 активных источников) остаются на прежнем акторе:
hashtag-scraper поиска не умеет, а терять покрытие нельзя (решение владельца).
На search-ветке включается фильтр популярности актора (событие $0.001 против
$0.003 за мусорный результат) — точное имя поля НУЖНО СВЕРИТЬ со схемой билда
`latest` основного актора через Apify API (в этой сессии сверялись только поля
субтитров; поле фильтра НЕ проверялось); если поля в актуальной схеме нет —
не отправлять ничего и записать факт в комментарий тикета.
Exploration-механика наследует поведение своего kind. Новый актор задаётся
ключом реестра акторов в конфиге (дефолт — в коде), откат — строкой конфига.
Формат строк в Sheets не меняется: по хэндоффу выдача hashtag-scraper — тот же
формат видео clockworks (парити-тест на фикстуре).

**Blocked by:** 01 (база payload без мёртвых полей).

**Status:** done

- [x] Батчи режутся по kind: hashtag-источники не смешиваются с search в одном
  батче; каждый вид уходит своему актору.
- [x] Payload hashtag-ветки — ровно поля схемы hashtag-scraper (`hashtags`,
  `resultsPerPage`, обложки, enum субтитров), снапшот-тест 1:1.
- [x] Payload search-ветки — прежний актор без мёртвых полей, с фильтром
  популярности (или зафиксированным в комментарии отказом), снапшот-тест 1:1.
- [x] Ключ нового актора читается из конфига, кодовый дефолт —
  `clockworks~tiktok-hashtag-scraper`; прежний ключ `tiktok` работает как раньше.
- [x] Выдача hashtag-scraper проходит нормализацию в канонические строки без
  единого изменения формата (фикстура + сверка с эталонной строкой).
- [x] Exploration-кандидаты обоих kind продолжают попадать в свои батчи.
- [x] Полный тестовый набор зелёный.

## Комментарии

2026-08-10, реализация.

**Как порезаны батчи.** Kind режет батчи в `make_batches` (sources.py) — он
используется только tiktok-путём (instagram батчится своим `hashtag_batches`
на `chunk`): хэштеги шардируются отдельно от запросов, граница kind'а даёт
неполный батч, покрытие/порядок/сквозная нумерация не меняются, `source_query`
остаётся полным списком обоих kind. Живой реестр (74 hashtag + 5 search)
теперь даёт 19 hashtag-батчей + 2 search-батча вместо 20 смешанных.
Exploration — `_exploration_batches` в tiktok.py: до двух батчей по kind,
каждый судит только своих кандидатов (сбой search-зонда не крадёт прогон у
hashtag-зондов). Маршрутизация в `collect.run_one` по содержимому батча:
`search_queries` → ключ конфига `apify.actors.tiktok`, иначе →
`apify.actors.tiktok_hashtag` (кодовый дефолт `clockworks~tiktok-hashtag-scraper`,
добавлен в cf.config.json). Snowball не тронут — свои батчи `postURLs`, прежний
актор.

**Сверка фильтра популярности.** Схема билда `latest` (0.0.583, finished
2026-08-07) основного актора `clockworks~tiktok-scraper` получена по API
(taggedBuilds.latest.buildId → /v2/actor-builds/…): поле ЕСТЬ, имя —
**`leastDiggs`** (integer, minimum 1, unit hearts, «Scrapes only videos with no
less hearts…»; парное `mostDiggs` — верхняя граница, не используем).
В search-payload включено **`leastDiggs: 100`**: ~порог анализа
`min_views=1000` при медианной доле лайков ~8% от просмотров у живых строк
(медиана ER 0.07–0.09); событие Bronze $0.001 против $0.003 за мусорный
результат. С date-фильтрами поле несовместимо по схеме — их нет с тикета 01.
Заодно сверена схема билда `latest` hashtag-scraper: ровно 7 полей, REQUIRED
`[hashtags]`, `proxyCountryCode`/`searchQueries` отсутствуют — payload
hashtag-ветки шлёт только поля схемы.

**Тесты** (полный прогон: 2065 passed, 0 failed):
- `test_collect_sources.py::test_batches_never_mix_kinds`,
  обновлён `test_batch_sources_markers_by_kind`;
- `test_collect_tiktok.py::test_build_hashtag_payload_snapshot_exact_schema_fields`,
  `test_build_search_payload_snapshot_no_dead_fields_no_hashtags` (снапшоты 1:1),
  `test_hashtag_and_search_batches_routed_to_their_actors`,
  `test_actor_keys_overridden_by_config`,
  `test_exploration_candidates_go_to_actors_of_their_kind`,
  `test_lost_search_probe_does_not_steal_hashtag_probe_run`;
- `test_collect_normalize.py::test_tiktok_row_hashtag_scraper_parity_same_canonical_format`
  (фикстура по хэндоффу «тот же формат clockworks», подтверждение живыми
  данными — в runbook смоука 19.08, тикет 07).

**2026-08-10, смоук search-ветки (runbook §2.2):** фильтр `leastDiggs=100`
актор молча игнорирует — событий `popularity-filter-applied` 0, начислений 0,
в выдаче likes от 2 (поле в схеме билда 0.0.583 есть, но рантайм его не
применяет — судьба «UNDER MAINTENANCE», пока бесплатная). Поле снято из
payload тем же днём: no-op, который может молча проснуться с тарификацией,
нарушает US5. Включение обратно — осознанным решением с замером
(build_search_payload + комментарий в tiktok.py).
