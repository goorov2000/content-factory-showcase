# 04 — Адаптер apidojo за конфиг-флагом

**What to build:** Сбор TikTok умеет работать через `apidojo~tiktok-scraper`
($0.0003/пост флэт, без единого платного события, 99.0% успеха на 12.4M
прогонов — сверено по API 10.08) — но прод на него НЕ переключается: включение
возможно только сменой значения актора в конфиге, и по плану происходит после
живого парити-замера 19.08 (тикет 07). Когда ключ актора указывает на apidojo,
сборщик строит payload его схемы (поля схемы `latest`: `keywords`, `startUrls`,
`maxItems`, `sortType`, `dateRange`; как подавать хэштеги — выбрать при
реализации по README/схеме актора и зафиксировать выбор в комментарии тикета),
а выдача проходит через адаптер нормализации в те же канонические строки.
Формат ВЫХОДА apidojo неизвестен до 19.08 — фикстура строится как допущение по
документации актора и помечается на подтверждение в runbook. Неузнанный формат
ответа — громкий провал батча с причиной в Run Log, не молчаливые кривые
строки. Субтитры apidojo под вопросом — если выдача их не содержит, строки
честно получают статус «без актор-транскрипта», и это фиксируется тестом.

**Blocked by:** 02 (маршрутизация батчей по актору).

**Status:** done

- [x] При конфиге `tiktok: apidojo~tiktok-scraper` payload собирается по схеме
  apidojo (снапшот-тест 1:1), при clockworks-значениях — прежние payload.
- [x] Фикстура ответа apidojo (допущение по документации, помечена на
  подтверждение в runbook) нормализуется в канонические строки: raw_id, url,
  метрики, даты.
- [x] Ответ без обязательных полей адаптера проваливает батч громко, с
  причиной в Run Log.
- [x] Включение/выключение — только значение ключа в конфиге; кодовый дефолт
  остаётся clockworks.
- [x] Полный тестовый набор зелёный.

## Комментарии

2026-08-10, реализация.

**Как включается.** Семейство определяется по префиксу значения конфиг-ключа
(`is_apidojo_actor`: строка начинается с `apidojo`) — работает для ОБЕИХ веток
независимо: `apify.actors.tiktok_hashtag` переводит на apidojo хэштеги,
`apify.actors.tiktok` — search. Кодовые дефолты не менялись (clockworks),
`cf.config.json` не тронут — включение произойдёт в runbook 19.08 после живого
парити (тикет 07), откат — той же строкой конфига. Маршрут и выбор ветки
нормализации — в `collect.actor_for/run_one` (src/cf/collect/tiktok.py);
snowball/instagram/clockworks-ветки не изменены, их снапшот-тесты прежние.

**Как поданы хэштеги.** Keywords-путём `"#тег"`, не tag-URL в `startUrls`:
один шаблон payload на оба kind (батчи однородны по kind с тикета 02),
кириллические теги не требуют URL-энкодинга, и серверные фильтры актора
действуют только на search-путь. Payload — РОВНО `{"keywords", "maxItems"}`
(схема билда latest 0.0.1055, сверка по API 10.08, REQUIRED []); maxItems =
RESULTS_PER_PAGE × источников батча — объём соразмерен clockworks.
`sortType`/`dateRange`/`location` НЕ шлём: enum-значения в схеме документированы,
но недефолтное значение меняло бы семантику сбора без evidence, а дефолт явным
полем — мусор в запросе. `includeSearchKeywords` НЕ шлём: имя поля ВЫХОДА в
README не документировано, атрибуция и так восстанавливается из батча.

**Источник маппинга выхода — README актора** (раздел «Example Output Object»,
получен по API из билда latest 0.0.1055): id/title/views/likes/comments/
shares/bookmarks/hashtags/channel{username,name}/uploadedAt(Formatted)/
video{duration,url,cover,thumbnail}/subtitleInformation{lang,language_code,
is_auto_generated,url}/postPage. Это документация вендора, не голое допущение,
но живых ответов до 19.08 нет — фикстуры (APIDOJO_RAW в test_collect_normalize,
apidojo_item в test_collect_tiktok) помечены на подтверждение в runbook смоука.
Адаптер — `apidojo_tiktok_rows/_row` (src/cf/collect/normalize.py): те же
колонки в том же порядке, что у clockworks-ряда (тест сверяет column-order).
Атрибуция source_query — из batch_sources по документированному полю
`hashtags` item'а (без регистра/`#`, как M28); нет совпадения — единственный
источник батча; несколько и ни одного совпадения — честный `unknown`.
Субтитры: готового ТЕКСТА транскрипта в документированном выходе нет —
`transcript_text` всегда пуст; ссылка из subtitleInformation (rus → авто →
eng → первый) уходит в существующий контур скачивания
(`subtitle_pending_download` → attach_transcripts), без ссылки ряд честно
получает `raw_saved_no_actor_transcript`. Обязательные поля адаптера — url
(postPage) и стабильный идентификатор (id, при его отсутствии — hash от
url+caption, как у clockworks); item без url — «формат выхода не распознан»:
один loss-маркер `__apify_error` на батч → гейт → причина в Run Log,
распознанные item'ы того же батча доезжают (деградация, не молчание).

**Тесты** (полный прогон: 2075 passed, 0 failed):
- `test_collect_tiktok.py::test_build_apidojo_payload_hashtag_snapshot`,
  `test_build_apidojo_payload_search_snapshot` (снапшоты 1:1),
  `test_apidojo_hashtag_actor_switched_by_config_only`,
  `test_apidojo_search_actor_switched_by_config_only`,
  `test_apidojo_unrecognized_output_degrades_run_with_reason`;
- `test_collect_normalize.py::test_apidojo_row_parity_canonical_format`,
  `test_apidojo_without_subtitles_honest_no_transcript_status`,
  `test_apidojo_source_query_restored_from_batch`,
  `test_apidojo_item_without_url_fails_batch_loudly`,
  `test_apidojo_url_without_id_hashes_raw_id`.

**2026-08-10, runbook §3.2 + §4: формат выхода подтверждён живым ответом**
(запуск раньше 19-го — лимит подняли до $130). Ключи item — как в фикстуре
(id/title/метрики/hashtags/channel/uploadedAt*/video/postPage), плюс
недокументированные collabInfo/poi/song/inputSource. `subtitleInformation`
приходит null у части item (не всегда список) — адаптер это переживает.
Находка: **inputSource несёт искомый keyword** («#тег») — атрибуция переведена
на него (фолбэк — hashtags): было 36% unknown (keyword-поиск возвращает ролики
без искомого тега в hashtags), стало 0%. Парити §4.3 после фикса: объём 108%
(28/26), колонки 1:1, unknown 0/28, транскрипт-доля 46% против 31% эталона —
все четыре критерия §4.4 зелёные. Переключение ключа — решение владельца.

**2026-08-10: владелец включил apidojo для хэштегов** (решение по зелёному
парити 4/4; конфиг-коммит — включение).
