# 03 — Instagram: метрики без транскрипт-аддона, расшифровка после гейта

**What to build:** Reel-сборщик Instagram перестаёт платить $0.041/начатую
минуту за расшифровку каждого собранного ролика. Метрики собираются прежним
актором без `includeTranscript`. Расшифровка становится отдельным этапом ПОСЛЕ
гейта — как субтитры у TikTok: URL роликов, прошедших гейт и ещё не имеющих
транскрипта, уходят одним вызовом в `apple_yang~instagram-transcripts-scraper`
(поле `bulkUrls` — сверено по схеме билда; $0.001/результат + speech2text
$0.0035/начатую минуту — в 12 раз дешевле), полученный текст приклеивается в
каноническое поле строки с тем же processing_status-контрактом, что у
актор-транскриптов сейчас. Сбой транскрипт-этапа не роняет сбор: строки уходят
в Sheets без транскрипта, в Run Log — внятная причина (прайор-арт — деградация
discovery). Формат ВЫХОДА apple_yang неизвестен (README без примера) — маппинг
пробует известные текстовые ключи, неузнанный формат даёт громкую деградацию с
причиной и примером ключей, а не молчаливые пустые транскрипты; допущение
подтверждается в runbook 19.08 (тикет 07). Актор задаётся новым ключом реестра
акторов в конфиге (дефолт в коде). Замер качества против нынешних
расшифровок — runbook 19.08, не этот тикет.

**Blocked by:** None — can start immediately.

**Status:** done

- [x] Payload reel-сборщика не содержит `includeTranscript`; снапшот-тест 1:1.
- [x] В `bulkUrls` транскрипт-актора попадают только ролики, прошедшие гейт и
  без готового транскрипта; уже расшифрованное не расшифровывается повторно.
- [x] Успешный ответ apple_yang приклеивает текст в то же каноническое поле и
  тот же processing_status, что нынешний актор-транскрипт, — нормализация,
  гейт и анализ разницы не видят.
- [x] Сбой/таймаут/неузнанный формат транскрипт-этапа: сбор завершается, строки
  в Sheets без транскрипта, в Run Log — причина деградации.
- [x] Ключ транскрипт-актора читается из конфига, кодовый дефолт —
  `apple_yang~instagram-transcripts-scraper`.
- [x] Полный тестовый набор зелёный.

## Комментарии

Дизайн-заметка (план, не отчёт): текст искать по ключам `transcript` → `text`
→ `segments[].text` (склейка), URL сопоставлять по `url`/`inputUrl`/
`shortCode`; это ДОПУЩЕНИЕ из описания актора, подтверждение — runbook 19.08.

Сделано (2026-08-10):
- `src/cf/collect/instagram.py`: `build_reel_payload` без `includeTranscript`;
  новый ключ `instagram_transcripts` в `ACTORS_DEFAULT`; `build_transcript_payload`
  (`{"bulkUrls": [...]}`); этап `attach_bulk_transcripts` в `collect()` после
  гейта/нормализации/дедупа — один bulk-вызов только для строк без готового
  текста. Готовые тексты прежних сборов переносятся из вкладки по `raw_id`
  (upsert IG без coalesce перезаписал бы их пустым и оплачивал бы расшифровку
  заново каждый прогон); пустой pending — платного вызова нет. Успех кладёт
  текст в `transcript_text` + `raw_saved_actor_transcript`, без текста строка
  остаётся `raw_saved_no_actor_transcript` — контракт normalize 1:1
  (коэрция текста — тем же `_ig_transcript_text`, normalize не менялся).
- Сбой этапа (исключение / не-ok ран / неузнанный формат / 0 items) — деградация
  по образцу discovery: статус сбора не портится, причина в сводке
  (`transcripts ok=... (сбой этапа: ...)`), в errors Run Log (`transcripts: ...`)
  и в `summary["transcripts"]`; неузнанный формат несёт ключи первого item.
- `cf.config.json`: `apify.actors.instagram_transcripts =
  "apple_yang~instagram-transcripts-scraper"`.
- Тесты (`tests/test_collect_instagram.py`): снапшоты
  `test_reel_payload_snapshot_no_transcript_addon`,
  `test_transcript_payload_snapshot_bulk_urls`; e2e
  `test_e2e_transcripts_one_bulk_call_after_gate` (три формы маппинга + дубль
  URL в выдаче), `test_e2e_only_rows_without_transcript_go_to_bulk`,
  `test_e2e_transcript_from_previous_run_reused_no_paid_call`,
  `test_e2e_transcript_actor_key_overridden_by_config`,
  `test_e2e_transcript_stage_exception_degrades_not_fails`,
  `test_e2e_transcript_run_not_ok_degrades_with_reason`,
  `test_e2e_transcript_unknown_format_loud_with_sample_keys`.
  Полный прогон: 2058 passed, 0 failed.
- ВАЖНО: маппинг выхода apple_yang (transcript → text → segments[].text;
  url/inputUrl/shortCode) остаётся ДОПУЩЕНИЕМ до подтверждения на живом ответе
  в runbook 19.08 (тикет 07); фикстуры тестов построены из этого допущения.

**2026-08-10, runbook §3.1: маппинг подтверждён живым ответом** (запуск
случился 10.08 — лимит подняли до $130, runbook исполнен раньше 19-го).
Ключи item: текст — `text` (+`segments` с таймкодами), идентификатор — `url`
(есть и `code`=shortCode); наша цепочка `transcript→text→segments` находит
`text`. Пустой транскрипт приходит с `errMsg:''` и заполненным `audioUrl`
(ролик без распознанной речи) — строка честно остаётся
`raw_saved_no_actor_transcript`. Слово ДОПУЩЕНИЕ снято; качество первого
живого текста — чистый русский с пунктуацией (замер — §5 runbook).

**2026-08-10, runbook §5: замер качества — apple_yang в бою** (решение
владельца). 15 пар против оплаченных текстов: медианный jaccard 0.88,
len_ratio 1.0; на всех 8 речевых роликах тексты идентичны (чистый русский,
пунктуация на месте); расхождения только на музыке/мусоре (там и эталон
мусорен) и одном недоступном посте (`no audio url found`). Слабое место —
whisper-артефакт («Субтитры делал…») на кино-нарезке, редкий шум.
