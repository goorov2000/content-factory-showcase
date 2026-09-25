r"""C2.5 — сводная parity-карта порта JS-тестов n8n (node --test) на pytest.

Каждый рантайм-кейс из n8n/__tests__/*.test.js (параметризованные циклы
losses.test.js ×2 профиля и workflow-structure.test.js ×3 воркфлоу развёрнуты
в отдельные строки) замаплен на pytest-порт из задач C1.1–C2.4. Непереносимые
кейсы (проверки графа n8n) помечены `n/a (граф n8n, умирает на этапе 6)`.
Тесты в этом модуле держат карту честной: (а) карта не отстаёт от JS-сьюта,
(б) все ссылки указывают на существующие pytest-функции, (в) n/a <= 20.

Формат строки таблицы: <js-файл>.test.js :: <test-название> → <tests/файл.py::функция [примечание] | n/a (причина)>

— gate.test.js: 3 —
gate.test.js :: слитные хэштеги "#a#b#c...#j" (10) -> дроп hashtag_stuffing, чистый ряд остаётся → tests/test_collect_gate.py::test_stuffed_hashtags_dropped_clean_row_kept
gate.test.js :: ровно 9 слитных хэштегов проходит (граница maxCaptionHashtags=9) → tests/test_collect_gate.py::test_exactly_nine_merged_hashtags_pass
gate.test.js :: регресс: старый /#\\S+/ посчитал бы слитную цепочку как 1 → tests/test_collect_gate.py::test_regress_old_regex_counts_merged_chain_as_one

— batching.test.js: 21 —
batching.test.js :: TikTok Build: >1 батча (шардинг, не «всё одним run-sync») → tests/test_collect_sources.py::test_real_tiktok_registry_shards_into_multiple_batches
batching.test.js :: TikTok Build: в каждом батче <=4 источника → tests/test_collect_sources.py::test_each_batch_carries_1_to_4_sources
batching.test.js :: TikTok Build: батчи покрывают ВСЕ источники ровно один раз (нет потерь/дублей) → tests/test_collect_sources.py::test_full_coverage_without_losses_or_dupes
batching.test.js :: TikTok Build: batch_index последователен, batch_total одинаков → tests/test_collect_sources.py::test_batch_index_sequential_batch_total_uniform
batching.test.js :: TikTok Build: actor-id читается из одного места (actor_path), tilde-форма для URL → tests/test_collect_apify.py::test_run_actor_happy_path (функц. эквивалент: tilde-путь в REST URL; одно место — ACTOR_PATH_DEFAULT/конфиг apify.actors; display-форма source_actor — tests/test_collect_normalize.py::test_tiktok_row_clockworks_parity_with_js)
batching.test.js :: TikTok Build: batch_sources помечены видом (hashtag:/query:) → tests/test_collect_sources.py::test_batch_sources_markers_by_kind
batching.test.js :: Instagram Build: id трёх акторов в одном месте (actors map, tilde-форма) → tests/test_collect_instagram.py::test_e2e_happy_path_three_stages (функц. эквивалент: одна map ACTORS_DEFAULT в instagram.py, диспатч трёх акторов по actor_path)
batching.test.js :: Instagram Hashtags: >4 тегов -> несколько батчей по <=4 → tests/test_collect_sources.py::test_seven_sources_two_batches_in_order (стадийная обёртка — tests/test_collect_instagram.py::test_hashtag_batches_full_source_query)
batching.test.js :: Instagram Hashtags: source_query = ПОЛНЫЙ список на каждом батче (downstream .first()) → tests/test_collect_sources.py::test_source_query_full_list_on_every_batch
batching.test.js :: Instagram Hashtags (абстрактно): ровно 4 источника -> ровно 1 батч (граница) → tests/test_collect_sources.py::test_exactly_four_sources_one_batch
batching.test.js :: Instagram Hashtags (абстрактно): 25 источников -> 7 батчей по <=4, покрытие полное → tests/test_collect_sources.py::test_25_sources_7_batches_tail_1
batching.test.js :: Instagram Hashtags: <=4 тегов -> один батч, старый контракт сохранён → tests/test_collect_sources.py::test_exactly_four_sources_one_batch (граница 4→1 общего шардера make_batches; hashtag_batches — обёртка, tests/test_collect_instagram.py::test_hashtag_batches_full_source_query)
batching.test.js :: Instagram Hashtags: пустой список тегов -> 0 item (downstream не исполняется, без пустого платного запроса) → tests/test_collect_sources.py::test_empty_sources_zero_batches
batching.test.js :: Prepare reel: пустой вход -> [] (downstream reel-fetch не стартует) → tests/test_collect_instagram.py::test_prepare_reel_batches_empty
batching.test.js :: Prepare reel: 1 URL -> 1 батч из 1 → tests/test_collect_sources.py::test_single_source_one_batch_size6
batching.test.js :: Prepare reel: ровно 6 URL -> 1 батч (граница REEL_BATCH_SIZE) → tests/test_collect_sources.py::test_exactly_six_sources_one_batch_size6
batching.test.js :: Prepare reel: 7 URL -> 2 батча (6 + 1) → tests/test_collect_sources.py::test_seven_sources_batch6_split_6_1
batching.test.js :: Prepare reel: 30 URL (предел) -> 5 батчей по <=6, покрытие полное, метаданные → tests/test_collect_sources.py::test_30_sources_5_batches_size6_metadata
batching.test.js :: Prepare reel: >30 URL -> усечение до 30 (5 батчей), не больше → tests/test_collect_sources.py::test_limit_truncates_over_30_to_30 (стадийно — tests/test_collect_instagram.py::test_prepare_reel_batches_cap_30)
batching.test.js :: Snowball Build: один батч на seed, batch_sources = snowball:<url>, actor_path в одном месте → tests/test_collect_snowball.py::test_build_dedupes_query_variants (payload/actor — tests/test_collect_snowball.py::test_payload_matches_n8n_node)
batching.test.js :: Snowball Build: дедуп seed_url (P1.20) сохранён после добавления батч-метаданных → tests/test_collect_snowball.py::test_build_slash_variant_collapses

— coalesce.test.js: 16 —
coalesce.test.js :: пустой новый transcript + существующий готовый -> прежний сохранён → tests/test_collect_coalesce.py::test_empty_new_transcript_keeps_existing
coalesce.test.js :: whitespace-only новый transcript -> прежний сохранён → tests/test_collect_coalesce.py::test_whitespace_transcript_keeps_existing
coalesce.test.js :: непустой новый transcript -> заменяет прежний → tests/test_collect_coalesce.py::test_new_transcript_replaces
coalesce.test.js :: новый transcript заменяет даже когда у существующего он пуст → tests/test_collect_coalesce.py::test_new_transcript_replaces_empty_existing
coalesce.test.js :: source_query -> сохранена исходная атрибуция (hashtag поверх snowball) → tests/test_collect_coalesce.py::test_source_query_keeps_original_attribution
coalesce.test.js :: collected_at -> сохранено время первого сбора → tests/test_collect_coalesce.py::test_collected_at_keeps_first_seen
coalesce.test.js :: метрики/caption/status берут свежее значение на существующем raw_id → tests/test_collect_coalesce.py::test_metrics_take_fresh_values
coalesce.test.js :: новый raw_id (нет в existing) -> incoming без изменений → tests/test_collect_coalesce.py::test_new_raw_id_passthrough_copy
coalesce.test.js :: existing без transcript_text -> откат на incoming (пустой), не undefined → tests/test_collect_coalesce.py::test_existing_without_transcript_column
coalesce.test.js :: firstNonEmpty: непустое a -> a, пустое a -> b → tests/test_collect_coalesce.py::test_first_non_empty
coalesce.test.js :: indexByRawId: игнорирует пустой/отсутствующий raw_id, последняя строка побеждает → tests/test_collect_coalesce.py::test_index_by_raw_id
coalesce.test.js :: coalesceRows: смесь существующих и новых raw_id → tests/test_collect_coalesce.py::test_coalesce_rows_mixed
coalesce.test.js :: e2e: повторный сбор — transcript/source_query/collected_at сохранены, метрики свежие → tests/test_collect_coalesce.py::test_e2e_repeat_collection
coalesce.test.js :: e2e: новое видео (пустой лист existing) -> passthrough → tests/test_collect_coalesce.py::test_e2e_new_video_passthrough
coalesce.test.js :: e2e снежок: копия snowball даёт тот же результат → tests/test_collect_snowball.py::test_e2e_snowball_does_not_overwrite_existing_attribution (функц. эквивалент: в Python единый модуль coalesce.py на оба тракта; идентичность JS-копий — tests/test_collect_coalesce.py::test_js_copies_identical)
coalesce.test.js :: обе Coalesce-копии байт-в-байт идентичны → tests/test_collect_coalesce.py::test_js_copies_identical

— instagram.test.js: 19 —
instagram.test.js :: isUsable: error-item отбраковывается → tests/test_collect_normalize.py::test_is_usable_error_item_rejected
instagram.test.js :: isUsable: пустой item / без id-url отбраковывается → tests/test_collect_normalize.py::test_is_usable_empty_or_idless_rejected
instagram.test.js :: isUsable: реальный reel (id или url) годен → tests/test_collect_normalize.py::test_is_usable_real_reel_accepted
instagram.test.js :: Normalize: только error+пустой item -> throw (потеря батча видна, 0 мусорных рядов) → tests/test_collect_normalize.py::test_ig_error_plus_empty_only_raises
instagram.test.js :: Normalize: одиночный error-item -> throw, никакого instagram_0 → tests/test_collect_normalize.py::test_ig_single_error_item_raises
instagram.test.js :: Normalize: валидный + error + пустой -> ровно 1 ряд, нет instagram_0 → tests/test_collect_normalize.py::test_ig_mixed_items_yield_only_valid_row
instagram.test.js :: Normalize: только валидный item -> нормальный ряд → tests/test_collect_normalize.py::test_ig_valid_item_normal_row
instagram.test.js :: Normalize регресс: старый фильтр raw && Object.keys пропустил бы error-item → tests/test_collect_normalize.py::test_ig_regression_old_filter_would_pass_error_item
instagram.test.js :: Gate: форма {items:[...]} разворачивается И фильтруется (не проходит целиком) → tests/test_collect_gate.py::test_ig_items_form_unwrapped_and_filtered
instagram.test.js :: Gate: форма {data:[...]} разворачивается И фильтруется → tests/test_collect_gate.py::test_ig_data_form_unwrapped_and_filtered
instagram.test.js :: Gate: 0-view пост старше 14 дней -> дроп stale_low_views → tests/test_collect_gate.py::test_ig_zero_view_stale_dropped
instagram.test.js :: Gate: stale-пост с 100 просмотрами тоже дропается (порог 500) → tests/test_collect_gate.py::test_ig_stale_100_views_dropped
instagram.test.js :: Gate: свежий 0-view пост -> остаётся (не stale) → tests/test_collect_gate.py::test_ig_fresh_zero_view_kept
instagram.test.js :: Gate: stale-пост с 1000 просмотров -> остаётся (>= порога) → tests/test_collect_gate.py::test_ig_stale_high_views_kept
instagram.test.js :: Gate: нормальный свежий пост -> остаётся → tests/test_collect_gate.py::test_ig_normal_fresh_post_kept
instagram.test.js :: Hashtags: matchesNiche — "men" НЕ матчит "women" → tests/test_collect_instagram.py::test_matches_niche_word_boundary
instagram.test.js :: Hashtags регресс: старый /men/ подстрокой ловил "women", новый — нет → tests/test_collect_instagram.py::test_matches_niche_word_boundary (регресс-часть: women -> False по границе слова)
instagram.test.js :: Hashtags: dedupeHashtags — регистр-независимый дедуп, первая форма сохраняется → tests/test_collect_instagram.py::test_dedupe_hashtags_case_insensitive
instagram.test.js :: Hashtags e2e: Menswear+menswear дедуплятся в один, women отброшен → tests/test_collect_instagram.py::test_merge_hashtags_seed_first_filter_and_limits

— losses.test.js: 22 (5 гейтовых шаблонов ×2 профиля + 12 статических) —
losses.test.js :: TikTok Normalize: битый батч -> loss-маркер, НЕ мусорный ряд; валидный доезжает → tests/test_collect_normalize.py::test_broken_batch_forwards_loss_marker[tiktok]
losses.test.js :: TikTok Normalize: без ошибок -> только ряды, ни одного loss-маркера → tests/test_collect_normalize.py::test_no_errors_no_loss_markers[tiktok]
losses.test.js :: Snowball Normalize: битый seed-батч -> loss-маркер, остальные seed доезжают → tests/test_collect_normalize.py::test_broken_batch_forwards_loss_marker[snowball]
losses.test.js :: TikTok Gate: loss-маркер посчитан (batches_failed/sources_lost) и НЕ попал в kept → tests/test_collect_gate.py::test_loss_marker_counted_not_in_kept[tiktok]
losses.test.js :: Snowball Gate: loss-маркер посчитан (batches_failed/sources_lost) и НЕ попал в kept → tests/test_collect_gate.py::test_loss_marker_counted_not_in_kept[snowball]
losses.test.js :: TikTok Gate: без потерь -> batches_failed=0, старое поведение drops сохранено → tests/test_collect_gate.py::test_no_losses_drops_preserved[tiktok]
losses.test.js :: Snowball Gate: без потерь -> batches_failed=0, старое поведение drops сохранено → tests/test_collect_gate.py::test_no_losses_drops_preserved[snowball]
losses.test.js :: TikTok Gate: ПОЛНЫЙ провал (только loss-маркеры, 0 рядов) -> throw громко → tests/test_collect_gate.py::test_total_failure_raises[tiktok]
losses.test.js :: Snowball Gate: ПОЛНЫЙ провал (только loss-маркеры, 0 рядов) -> throw громко → tests/test_collect_gate.py::test_total_failure_raises[snowball]
losses.test.js :: TikTok Gate: batches_failed по уникальным batch_index (retry одного батча не двоится) → tests/test_collect_gate.py::test_batches_failed_unique_batch_index[tiktok]
losses.test.js :: Snowball Gate: batches_failed по уникальным batch_index (retry одного батча не двоится) → tests/test_collect_gate.py::test_batches_failed_unique_batch_index[snowball]
losses.test.js :: TikTok Gate: пустой вход (0 рядов, 0 потерь) -> НЕ throw → tests/test_collect_gate.py::test_empty_input_no_raise[tiktok]
losses.test.js :: Snowball Gate: пустой вход (0 рядов, 0 потерь) -> НЕ throw → tests/test_collect_gate.py::test_empty_input_no_raise[snowball]
losses.test.js :: IG Gate: нативный {error}-маркер упавшего батча посчитан, не в kept; хорошие доезжают → tests/test_collect_gate.py::test_ig_native_error_marker_counted_not_in_kept
losses.test.js :: IG Gate: без ошибок -> batches_failed=0, форма {items:[...]} по-прежнему фильтруется → tests/test_collect_gate.py::test_ig_no_errors_items_form_still_filtered
losses.test.js :: IG Gate: ПОЛНЫЙ провал (только {error}, 0 годных) -> throw громко → tests/test_collect_gate.py::test_ig_total_failure_raises
losses.test.js :: IG Gate: top-level item С error И id (per-hashtag ошибка успешного рана) -> НЕ упавший батч → tests/test_collect_gate.py::test_ig_error_with_id_is_not_failed_batch
losses.test.js :: IG Gate: два {error} одного hashtag-батча (retry) -> batches_failed=1 → tests/test_collect_gate.py::test_ig_retry_same_batch_index_counted_once
losses.test.js :: IG Raw: частичный успех (valid + битый reel-батч) -> ряд дошёл, потери посчитаны, без throw → tests/test_collect_normalize.py::test_ig_partial_success_counts_losses_without_throw
losses.test.js :: IG Raw: полный ноль по ВСЕМ reel-батчам (item есть, 0 годных) -> throw (громко) → tests/test_collect_normalize.py::test_ig_total_zero_raises
losses.test.js :: IG Raw: несколько битых батчей при частичном успехе -> reels_lost суммируется → tests/test_collect_normalize.py::test_ig_multiple_broken_batches_sum_reels_lost
losses.test.js :: IG Raw: два {error} одного reel-батча (retry) -> batches_failed=1 → tests/test_collect_normalize.py::test_ig_retry_same_batch_counts_once

— normalize.test.js: 10 —
normalize.test.js :: toIso: epoch-строка "1721030000" -> валидный ISO ~2024 → tests/test_collect_util.py::test_to_iso_epoch_string
normalize.test.js :: toIso: epoch-число 1721030000 -> тот же ISO → tests/test_collect_util.py::test_to_iso_epoch_number_same_as_string
normalize.test.js :: toIso: epoch-строка и epoch-число дают одинаковый результат → tests/test_collect_util.py::test_to_iso_epoch_number_same_as_string
normalize.test.js :: toIso: ISO-строка проходит без изменений → tests/test_collect_util.py::test_to_iso_iso_passthrough
normalize.test.js :: toIso: миллисекундная epoch-строка тоже парсится ~2024 → tests/test_collect_util.py::test_to_iso_millisecond_epoch_string
normalize.test.js :: toIso: мусор -> пусто → tests/test_collect_util.py::test_to_iso_garbage_empty
normalize.test.js :: transcript = {} -> нет транскрипта, качаем субтитры → tests/test_collect_normalize.py::test_transcript_empty_dict_means_no_transcript
normalize.test.js :: transcript = [] -> нет транскрипта, качаем субтитры → tests/test_collect_normalize.py::test_transcript_empty_list_means_no_transcript
normalize.test.js :: transcript = "" -> нет транскрипта, качаем субтитры → tests/test_collect_normalize.py::test_transcript_empty_string_means_no_transcript
normalize.test.js :: transcript = реальная строка -> используется, субтитры не качаем → tests/test_collect_normalize.py::test_transcript_real_string_used

— snowball-input.test.js: 11 —
snowball-input.test.js :: normalizeSeedUrl: query-параметр не влияет на ключ → tests/test_collect_util.py::test_normalize_url_query_ignored
snowball-input.test.js :: normalizeSeedUrl: fragment не влияет на ключ → tests/test_collect_util.py::test_normalize_url_fragment_ignored
snowball-input.test.js :: normalizeSeedUrl: хвостовой слэш не влияет на ключ → tests/test_collect_util.py::test_normalize_url_trailing_slash_ignored
snowball-input.test.js :: normalizeSeedUrl: слэш + query вместе сводятся к одному ключу → tests/test_collect_util.py::test_normalize_url_slash_plus_query
snowball-input.test.js :: normalizeSeedUrl: разные ролики -> разные ключи (не over-normalize) → tests/test_collect_util.py::test_normalize_url_not_over_normalized
snowball-input.test.js :: normalizeSeedUrl: разные аккаунты -> разные ключи → tests/test_collect_util.py::test_normalize_url_not_over_normalized (вторая половина той же функции: @alice != @bob)
snowball-input.test.js :: normalizeSeedUrl: кривой URL -> фолбэк на сырую строку, не теряется → tests/test_collect_util.py::test_normalize_url_bad_url_fallback_trimmed
snowball-input.test.js :: normalizeSeedUrl: null/undefined -> пустая строка, без throw → tests/test_collect_util.py::test_normalize_url_none_empty
snowball-input.test.js :: build: два query-варианта одного ролика + другой ролик -> 2 actor-item → tests/test_collect_snowball.py::test_build_dedupes_query_variants
snowball-input.test.js :: build: слэш-вариант тоже схлопывается, первый выигрывает → tests/test_collect_snowball.py::test_build_slash_variant_collapses
snowball-input.test.js :: build: неактивные и не-tiktok строки отфильтровываются (регресс) → tests/test_collect_snowball.py::test_build_filters_inactive_and_non_tiktok

— snowball.test.js: 7 —
snowball.test.js :: snowball toIso: epoch-строка "1721030000" -> валидный ISO ~2024 → tests/test_collect_normalize.py::test_snowball_to_iso_epoch_string
snowball.test.js :: snowball toIso: epoch-число совпадает со строкой → tests/test_collect_normalize.py::test_snowball_to_iso_number_matches_string
snowball.test.js :: snowball toIso: ISO passthrough и мусор → tests/test_collect_normalize.py::test_snowball_to_iso_passthrough_and_garbage
snowball.test.js :: snowball: transcript = {} -> нет транскрипта, качаем субтитры → tests/test_collect_normalize.py::test_snowball_transcript_empty_dict
snowball.test.js :: snowball: transcript = [] -> нет транскрипта → tests/test_collect_normalize.py::test_snowball_transcript_empty_list
snowball.test.js :: snowball: transcript = реальная строка -> используется → tests/test_collect_normalize.py::test_snowball_transcript_real_string
snowball.test.js :: snowball gate: слитные хэштеги "#a..#j" (10) -> дроп hashtag_stuffing → tests/test_collect_gate.py::test_stuffed_hashtags_dropped_clean_row_kept (Ingestion-Gate.js tiktok≡snowball — один tiktok_gate; snowball-профиль — tests/test_collect_gate.py::test_no_losses_drops_preserved[snowball])

— subtitle.test.js: 21 —
subtitle.test.js :: parseSubtitle: 403 JSON error body -> пусто → tests/test_collect_subtitles.py::test_parse_403_json_error_body_empty
subtitle.test.js :: parseSubtitle: JSON error с statusCode/error -> пусто → tests/test_collect_subtitles.py::test_parse_json_error_status_code_error_empty
subtitle.test.js :: parseSubtitle: HTML-страница ошибки (doctype) -> пусто → tests/test_collect_subtitles.py::test_parse_html_doctype_empty
subtitle.test.js :: parseSubtitle: HTML без doctype, но со структурными тегами -> пусто → tests/test_collect_subtitles.py::test_parse_html_without_doctype_empty
subtitle.test.js :: parseSubtitle: валидный JSON3 {events:[{segs:[{utf8}]}]} -> текст (fix segs) → tests/test_collect_subtitles.py::test_parse_json3_segs_utf8
subtitle.test.js :: parseSubtitle: JSON3 с несколькими segs -> склейка → tests/test_collect_subtitles.py::test_parse_json3_multiple_segs_joined
subtitle.test.js :: collectText рекурсит в segs → tests/test_collect_subtitles.py::test_collect_text_recurses_into_segs
subtitle.test.js :: caption-wins: JSON3 c error:null рядом -> реальный текст (строка) → tests/test_collect_subtitles.py::test_caption_wins_error_null_string
subtitle.test.js :: caption-wins: JSON3 c error:null рядом -> реальный текст (объект) → tests/test_collect_subtitles.py::test_caption_wins_error_null_object
subtitle.test.js :: caption-wins: {text:"real caption", error:"x"} -> реальный текст → tests/test_collect_subtitles.py::test_caption_wins_text_key_with_error
subtitle.test.js :: caption-wins: чистое тело ошибки без текста -> "" (не изменилось) → tests/test_collect_subtitles.py::test_caption_wins_pure_error_body_empty
subtitle.test.js :: parseSubtitle: нормальный VTT -> корректный текст → tests/test_collect_subtitles.py::test_parse_vtt
subtitle.test.js :: parseSubtitle: простой plain-caption -> сам текст → tests/test_collect_subtitles.py::test_parse_plain_caption
subtitle.test.js :: parseSubtitle: JSON без caption-ключей -> пусто (НЕ сырой дамп) → tests/test_collect_subtitles.py::test_parse_json_without_caption_keys_no_raw_dump
subtitle.test.js :: Attach e2e: JSON error body (в $json.body) -> статус failed, транскрипт не мусор → tests/test_collect_subtitles.py::test_attach_json_error_body_failed_no_garbage
subtitle.test.js :: Attach e2e: JSON error body напрямую в $json (объект) -> статус failed → tests/test_collect_subtitles.py::test_attach_json_error_object_failed
subtitle.test.js :: Attach e2e: HTML error body -> статус failed, без текста страницы → tests/test_collect_subtitles.py::test_attach_html_error_body_failed
subtitle.test.js :: Attach e2e: провал скачивания -> транскрипт откатывается на base.transcript_text → tests/test_collect_subtitles.py::test_attach_failed_download_rolls_back_to_base_transcript
subtitle.test.js :: Attach e2e: успешный VTT -> транскрипт и статус success → tests/test_collect_subtitles.py::test_attach_vtt_success
subtitle.test.js :: Attach e2e: успешный JSON3 -> транскрипт и статус success → tests/test_collect_subtitles.py::test_attach_json3_success
subtitle.test.js :: обе Attach-копии байт-в-байт идентичны → tests/test_collect_subtitles.py::test_both_attach_copies_byte_identical

— workflow-structure.test.js: 15 (5 шаблонов ×3 воркфлоу) — проверки графа n8n —
workflow-structure.test.js :: n8n/cf01-tiktok: связи ссылаются на существующие ноды, имена уникальны → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-instagram: связи ссылаются на существующие ноды, имена уникальны → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01b-snowball: связи ссылаются на существующие ноды, имена уникальны → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-tiktok: каждая Code-нода имеет файл code/*.js, без клэша имён файлов → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-instagram: каждая Code-нода имеет файл code/*.js, без клэша имён файлов → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01b-snowball: каждая Code-нода имеет файл code/*.js, без клэша имён файлов → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-tiktok: все run-sync Apify-ноды — onError=continueRegularOutput + actor-конфиг в URL → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-instagram: все run-sync Apify-ноды — onError=continueRegularOutput + actor-конфиг в URL → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01b-snowball: все run-sync Apify-ноды — onError=continueRegularOutput + actor-конфиг в URL → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-tiktok: нет orphan code/*.js (кроме известных pre-existing) → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-instagram: нет orphan code/*.js (кроме известных pre-existing) → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01b-snowball: нет orphan code/*.js (кроме известных pre-existing) → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-tiktok: $('...') внутри jsCode код-нод ссылаются на существующие ноды → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01-instagram: $('...') внутри jsCode код-нод ссылаются на существующие ноды → n/a (граф n8n, умирает на этапе 6)
workflow-structure.test.js :: n8n/cf01b-snowball: $('...') внутри jsCode код-нод ссылаются на существующие ноды → n/a (граф n8n, умирает на этапе 6)

Итог: 145 рантайм-кейсов = 130 замаплено на pytest + 15 n/a (граф n8n).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "n8n" / "__tests__"
# Витринная копия: каталог n8n/ не публикуется; сверка с JS-сьютом пропускается,
# проверки (б) и (в) карты работают по докстрингу и живым pytest-файлам.
needs_js_suite = pytest.mark.skipif(
    not JS_DIR.is_dir(), reason="n8n/__tests__ (JS-эталон) отсутствует в витринной копии")

# test('...', test("...", test(`...` — имя первым аргументом, одна строка.
TEST_NAME_RE = re.compile(r"^\s*test\(\s*(['\"`])(?P<name>.*?)\1", re.M)
# Параметризация в JS: цикл по профилям гейта и по воркфлоу-директориям.
LABEL_LOOP_RE = re.compile(r"for \(const \[label\b[^\n]*? of \[(?P<arr>.+)\]\s*\)")
DIRS_RE = re.compile(r"const DIRS = \[(?P<arr>[^\]]*)\]")
# Строка таблицы докстринга: имя JS-файла слева от ' :: '.
JS_FILE_RE = re.compile(r"^[\w.-]+\.test\.js$")
# Ссылки на pytest в правой части (включая примечания в скобках).
PYTEST_REF_RE = re.compile(r"tests/(?P<file>\w+\.py)::(?P<func>\w+)")


def js_runtime_case_names(src):
    """Все рантайм-кейсы файла: параметризованные шаблоны развёрнуты."""
    names = []
    for m in TEST_NAME_RE.finditer(src):
        name = m.group("name")
        if "${label}" in name:
            arr = LABEL_LOOP_RE.search(src).group("arr")
            labels = re.findall(r"\[\s*'([^']+)'", arr)
            assert labels, "цикл по label найден, а сами label — нет"
            names += [name.replace("${label}", lb) for lb in labels]
        elif "${dir}" in name:
            arr = DIRS_RE.search(src).group("arr")
            dirs = re.findall(r"'([^']+)'", arr)
            assert dirs, "DIRS найден, а директории — нет"
            names += [name.replace("${dir}", d) for d in dirs]
        else:
            names.append(name)
    return names


def parse_table():
    """Строки '<js> :: <имя> → <цель>' из докстринга модуля."""
    rows = []
    for raw_line in (__doc__ or "").splitlines():
        line = raw_line.strip()
        if " → " not in line or " :: " not in line:
            continue
        left, target = line.split(" → ", 1)
        js_file, _, name = left.partition(" :: ")
        if not JS_FILE_RE.match(js_file.strip()):
            continue  # заголовки/прочий текст
        rows.append((js_file.strip(), name.strip(), target.strip()))
    return rows


# --- (а) карта не отстаёт от JS-сьюта: рантайм-кейсы 1:1 со строками таблицы ---

@needs_js_suite
def test_parity_map_matches_js_suite():
    expected = {p.name: sorted(js_runtime_case_names(p.read_text(encoding="utf-8")))
                for p in sorted(JS_DIR.glob("*.test.js"))}
    assert expected, "JS-сьют не найден — n8n/__tests__ пропал?"

    table = {}
    for js_file, name, _ in parse_table():
        table.setdefault(js_file, []).append(name)
    table = {k: sorted(v) for k, v in table.items()}

    assert set(table) == set(expected), (
        f"файлы в карте и в JS-сьюте расходятся: "
        f"лишние {sorted(set(table) - set(expected))}, "
        f"пропущенные {sorted(set(expected) - set(table))}")
    for fname, exp_names in expected.items():
        assert len(table[fname]) == len(exp_names), (
            f"{fname}: {len(table[fname])} строк в карте против "
            f"{len(exp_names)} рантайм-кейсов в JS")
        assert table[fname] == exp_names, (
            f"{fname}: имена кейсов в карте расходятся с JS-сьютом")
    # ~145 инвариантов на момент заморозки JS (эталон до этапа 6)
    assert sum(len(v) for v in expected.values()) >= 140


# --- (б) каждая ссылка карты указывает на реально существующую функцию ---

def test_mapped_pytest_functions_exist():
    checked = 0
    seen_files = {}
    for js_file, name, target in parse_table():
        if target.startswith("n/a"):
            continue
        refs = PYTEST_REF_RE.findall(target)
        assert refs, f"строка без pytest-ссылки и без n/a: {js_file} :: {name}"
        for file_name, func in refs:
            path = ROOT / "tests" / file_name
            if file_name not in seen_files:
                assert path.is_file(), f"{js_file} :: {name} -> нет файла {path}"
                seen_files[file_name] = path.read_text(encoding="utf-8")
            assert re.search(rf"^def {func}\(", seen_files[file_name], re.M), (
                f"{js_file} :: {name} -> в tests/{file_name} нет функции {func}")
        checked += 1
    assert checked >= 130, f"замаплено {checked} кейсов — карта похудела"


# --- (в) защита от «всё n/a»: непереносимых не больше 20 ---

def test_na_rows_at_most_20():
    na = [(js, name) for js, name, target in parse_table()
          if target.startswith("n/a")]
    assert len(na) <= 20, f"n/a-кейсов {len(na)} > 20: {na}"
    # сейчас n/a — только проверки графа n8n из workflow-structure.test.js
    assert {js for js, _ in na} == {"workflow-structure.test.js"}
