# 01 — Мёртвые платные поля выкинуты, субтитры на новом enum

**What to build:** Сбор TikTok (основной и snowball) перестаёт отправлять актору
поля, за которые Apify берёт деньги при нулевой пользе или которых больше нет в
схеме вендора: `videoSearchSorting`, `videoSearchDateFilter` (оба помечены
вендором «UNDER MAINTENANCE», $2.90/мес в никуда) и булев
`shouldDownloadSubtitles` (удалён из схемы обоих clockworks-акторов — сверено
по Apify API 10.08, см. api-evidence.json). Субтитры запрашиваются новым полем
`downloadSubtitlesOptions` со значением `DOWNLOAD_SUBTITLES` — это бесплатный
вариант enum (готовые субтитры TikTok, без платной speech-to-text транскрипции;
enum сверен по схеме билда `latest`).

**Blocked by:** None — can start immediately.

**Status:** done

- [x] Payload основного TikTok-сборщика не содержит `videoSearchSorting`,
  `videoSearchDateFilter`, `shouldDownloadSubtitles` ни при каких входах.
- [x] Payload основного сборщика и snowball содержит
  `downloadSubtitlesOptions: "DOWNLOAD_SUBTITLES"`.
- [x] Снапшот-тесты фиксируют оба payload 1:1 — дрейф полей виден на ревью.
- [x] Существующий контур субтитров (скачивание по subtitle_url после гейта)
  продолжает работать на прежних фикстурах.
- [x] Полный тестовый набор зелёный.

## Комментарии

- Снапшоты 1:1: `test_build_payload_snapshot_no_dead_paid_fields`
  (test_collect_tiktok.py) и `test_payload_matches_n8n_node`
  (test_collect_snowball.py — имя оставлено как якорь парити-карты
  test_collect_parity.py, тело переписано в полный ожидаемый словарь).
- Оба `build_payload` шлют `downloadSubtitlesOptions: "DOWNLOAD_SUBTITLES"`;
  мёртвые поля убраны только из основного (у снежка их не было).
- Контур субтитров не менялся: `test_subtitles_downloaded_for_pending` зелёный
  на прежних фикстурах. `shouldDownloadSubtitles: False` в performance.py —
  вне скоупа (спека: актор performance не меняется, проверка — отдельно).
- Полный прогон: 2047 passed, 0 failed.
