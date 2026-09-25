# C1.6 — порт нормализации строк из n8n Code-node (эталоны:
# n8n/cf01-tiktok/code/Normalize-TikTok-Raw-Rows.js,
# n8n/cf01b-snowball/code/Normalize-TikTok-Raw-Rows.js,
# n8n/cf01-instagram/code/Normalize-Instagram-Raw-Rows.js).
# Инварианты: normalize.test.js (4 transcript-коэрции), snowball.test.js
# (нормализационные 6 из 7; 'snowball gate: слитные хэштеги' — C1.5),
# losses.test.js (Normalize-части; Gate-части — C1.5), instagram.test.js
# (Normalize-часть; IG Gate — C1.5, Hashtags — C2.2).
# Parity-значения (полные ряды clockworks/IG-reel) сняты живым node с JS-узлов
# при замороженном Date=2024-07-16T07:53:20.000Z.
import json

import pytest

from cf.collect.normalize import (
    CollectError,
    apidojo_tiktok_rows,
    instagram_finalize,
    instagram_row,
    instagram_rows,
    is_usable,
    tiktok_row,
    tiktok_rows,
)
from cf.collect.util import to_iso

COLLECTED_AT = "2024-07-16T07:53:20.000Z"

# baseRaw из normalize.test.js
BASE_RAW = {
    "id": "vid1",
    "text": "подпись",
    "createTime": 1721030000,
    "subtitleLinks": [{"downloadLink": "https://sub/x.vtt", "language": "rus"}],
}


def normalize_one(raw, source_query=None):
    rows, losses = tiktok_rows([raw], COLLECTED_AT, source_query=source_query)
    assert losses == []
    assert len(rows) == 1
    return rows[0]


# --------------------- normalize.test.js: (c) transcript guard, e2e ---------------------

# JS: 'transcript = {} -> нет транскрипта, качаем субтитры'
def test_transcript_empty_dict_means_no_transcript():
    j = normalize_one({**BASE_RAW, "transcript": {}})
    assert j["transcript_text"] == ""
    assert j["processing_status"] == "subtitle_pending_download"
    assert j["subtitle_url"]


# JS: 'transcript = [] -> нет транскрипта, качаем субтитры'
def test_transcript_empty_list_means_no_transcript():
    j = normalize_one({**BASE_RAW, "transcript": []})
    assert j["transcript_text"] == ""
    assert j["processing_status"] == "subtitle_pending_download"
    assert j["subtitle_url"]


# JS: 'transcript = "" -> нет транскрипта, качаем субтитры'
def test_transcript_empty_string_means_no_transcript():
    j = normalize_one({**BASE_RAW, "transcript": ""})
    assert j["transcript_text"] == ""
    assert j["processing_status"] == "subtitle_pending_download"


# JS: 'transcript = реальная строка -> используется, субтитры не качаем'
def test_transcript_real_string_used():
    j = normalize_one({**BASE_RAW, "transcript": "Реальный транскрипт"})
    assert j["transcript_text"] == "Реальный транскрипт"
    assert j["subtitle_url"] == ""
    assert j["processing_status"] == "raw_saved_actor_transcript"


# --------------------- snowball.test.js (нормализационные 6 из 7) ---------------------
# toIso в snowball-копии идентичен tiktok-версии; в Python один util.to_iso.

# JS: 'snowball toIso: epoch-строка "1721030000" -> валидный ISO ~2024'
def test_snowball_to_iso_epoch_string():
    out = to_iso("1721030000")
    assert out == "2024-07-15T07:53:20.000Z"
    assert out.startswith("2024")


# JS: 'snowball toIso: epoch-число совпадает со строкой'
def test_snowball_to_iso_number_matches_string():
    assert to_iso("1721030000") == to_iso(1721030000)


# JS: 'snowball toIso: ISO passthrough и мусор'
def test_snowball_to_iso_passthrough_and_garbage():
    assert to_iso("2024-07-15T00:00:00.000Z") == "2024-07-15T00:00:00.000Z"
    assert to_iso("not a date") == ""


# baseRaw из snowball.test.js: строковый epoch заодно проверяет (d) в боевом пути
SNOWBALL_RAW = {
    "id": "vid1",
    "text": "подпись",
    "createTime": "1721030000",
    "subtitleLinks": [{"downloadLink": "https://sub/x.vtt", "language": "rus"}],
}
SEED_QUERY = "snowball:https://seed"  # атрибуцию передаёт пайплайн (в JS — itemMatching)


# JS: 'snowball: transcript = {} -> нет транскрипта, качаем субтитры'
def test_snowball_transcript_empty_dict():
    j = normalize_one({**SNOWBALL_RAW, "transcript": {}}, source_query=SEED_QUERY)
    assert j["transcript_text"] == ""
    assert j["processing_status"] == "subtitle_pending_download"
    assert j["subtitle_url"]
    assert j["created_at"].startswith("2024")  # строковый epoch распарсился
    assert j["source_query"] == "snowball:https://seed"


# JS: 'snowball: transcript = [] -> нет транскрипта'
def test_snowball_transcript_empty_list():
    j = normalize_one({**SNOWBALL_RAW, "transcript": []}, source_query=SEED_QUERY)
    assert j["transcript_text"] == ""
    assert j["processing_status"] == "subtitle_pending_download"


# JS: 'snowball: transcript = реальная строка -> используется'
def test_snowball_transcript_real_string():
    j = normalize_one({**SNOWBALL_RAW, "transcript": "Реальный транскрипт"}, source_query=SEED_QUERY)
    assert j["transcript_text"] == "Реальный транскрипт"
    assert j["subtitle_url"] == ""
    assert j["processing_status"] == "raw_saved_actor_transcript"


# --------------------- losses.test.js: TikTok/Snowball Normalize loss-forward ---------------------
# В Python оба нормализатора — одна функция tiktok_rows (отличие — source_query),
# поэтому TikTok/Snowball гоняются параметризацией. Идентичность батча (batch_index,
# batch_sources) в порте едет внутри error-item (маркер apify.run_batches, C1.2) —
# аналог itemMatching -> Build в JS.

VALID_RAW = {"id": "v1", "text": "подпись", "createTime": 1721030000}


# JS: 'TikTok Normalize: битый батч -> loss-маркер, НЕ мусорный ряд; валидный доезжает'
# JS: 'Snowball Normalize: битый seed-батч -> loss-маркер, остальные seed доезжают'
@pytest.mark.parametrize("source_query,error_item,lost_sources", [
    (None,
     {"error": "ETIMEDOUT run-sync 300s", "batch_index": 1, "batch_sources": ["query:x", "query:y"]},
     ["query:x", "query:y"]),
    ("snowball:https://s/1",
     {"error": "boom", "batch_index": 1, "batch_sources": ["snowball:https://s/2"]},
     ["snowball:https://s/2"]),
], ids=["tiktok", "snowball"])
def test_broken_batch_forwards_loss_marker(source_query, error_item, lost_sources):
    rows, losses = tiktok_rows([VALID_RAW, error_item], COLLECTED_AT, source_query=source_query)
    assert len(rows) == 1, "валидный ряд один"
    assert rows[0]["raw_id"] == "tiktok_v1"
    assert len(losses) == 1, "ровно один loss-маркер"
    assert losses[0]["batch_sources"] == lost_sources
    assert losses[0]["source_query"] == "unknown"
    assert losses[0]["batch_index"] == 1
    assert not any('"error"' in (r["raw_json"] or "") for r in rows), "нет ряда с error в raw_json"


# JS: 'TikTok Normalize: без ошибок -> только ряды, ни одного loss-маркера'
@pytest.mark.parametrize("source_query", [None, "snowball:https://s/1"], ids=["tiktok", "snowball"])
def test_no_errors_no_loss_markers(source_query):
    rows, losses = tiktok_rows([VALID_RAW], COLLECTED_AT, source_query=source_query)
    assert len(rows) == 1
    assert losses == []


# Адаптация порта: идентичность батча параметрами вызова (пайплайн зовёт
# tiktok_rows по-батчево) — параметры играют роль Build.itemMatching и
# перекрывают поля error-item, как Build перекрывал raw.batch_index в JS.
def test_loss_marker_from_call_params():
    rows, losses = tiktok_rows(
        [{"error": "x", "batch_index": 9}], COLLECTED_AT,
        batch_index=3, batch_sources=["hashtag:#p", "query:q"])
    assert rows == []
    assert losses == [{
        "__apify_error": "x",
        "source_query": "unknown",
        "batch_index": 3,
        "batch_sources": ["hashtag:#p", "query:q"],
    }]


# Пустые/None item'ы отбрасываются молча — как JS-фильтр raw && Object.keys(raw).length.
def test_empty_items_dropped_without_rows_or_losses():
    assert tiktok_rows([{}, None], COLLECTED_AT) == ([], [])


# --------------------- losses.test.js: IG reel-стадия (Normalize Raw) ---------------------
# В JS все reel-батчи сливаются в один прогон узла; в порте — вызов
# instagram_rows на батч + агрегатный instagram_finalize (сводка и throw).

VALID_REEL = {
    "id": "abc123",
    "url": "https://www.instagram.com/reel/abc123/",
    "ownerUsername": "guy",
    "caption": "nice",
    "timestamp": "2024-07-16T00:53:20.000Z",
    "videoViewCount": 1000,
}


# JS: 'IG Raw: частичный успех (valid + битый reel-батч) -> ряд дошёл, потери посчитаны, без throw'
def test_ig_partial_success_counts_losses_without_throw():
    rows1, losses1, lost1 = instagram_rows([VALID_REEL], COLLECTED_AT, {})
    rows2, losses2, lost2 = instagram_rows(
        [{"error": "run-sync 300s timeout"}], COLLECTED_AT, {},
        batch_index=1, batch_sources=["reel:u1", "reel:u2", "reel:u3"])
    assert len(rows1) == 1, "один валидный reel-ряд дошёл"
    assert rows1[0]["raw_id"] == "instagram_abc123"
    assert rows2 == [] and lost1 == 0 and lost2 == 3
    summary = instagram_finalize(rows1 + rows2, losses1 + losses2, items_total=2)
    assert summary["batches_failed"] == 1
    assert summary["reels_lost"] == 3, "reels_lost из batch_sources упавшего батча"
    assert summary["kept"] == 1
    assert not any(r["raw_id"] == "instagram_0" for r in rows1 + rows2), "ни одного мусорного instagram_0"


# JS: 'IG Raw: полный ноль по ВСЕМ reel-батчам (item есть, 0 годных) -> throw (громко)'
def test_ig_total_zero_raises():
    rows, losses, _ = instagram_rows([{"error": "boom"}, {"error": "boom2"}], COLLECTED_AT, {})
    assert rows == []
    with pytest.raises(CollectError, match="0 годных reels по всем reel-батчам"):
        instagram_finalize(rows, losses, items_total=2)


# JS: 'IG Raw: несколько битых батчей при частичном успехе -> reels_lost суммируется'
# (в JS второй батч нёс reel_urls вместо batch_sources — в порте обе формы
# Prepare схлопнуты в параметр batch_sources, его наполняет батчер C1.7)
def test_ig_multiple_broken_batches_sum_reels_lost():
    r1, l1, _ = instagram_rows([VALID_REEL], COLLECTED_AT, {})
    r2, l2, _ = instagram_rows([{"error": "a"}], COLLECTED_AT, {},
                               batch_index=1, batch_sources=["reel:u1", "reel:u2"])
    r3, l3, _ = instagram_rows([{"error": "b"}], COLLECTED_AT, {},
                               batch_index=2, batch_sources=["x", "y", "z", "w"])
    assert len(r1) == 1
    summary = instagram_finalize(r1 + r2 + r3, l1 + l2 + l3, items_total=3)
    assert summary["batches_failed"] == 2
    assert summary["reels_lost"] == 6, "2 + 4"


# JS: 'IG Raw: два {error} одного reel-батча (retry) -> batches_failed=1'
def test_ig_retry_same_batch_counts_once():
    r1, l1, _ = instagram_rows([VALID_REEL], COLLECTED_AT, {})
    r2, l2, _ = instagram_rows(
        [{"error": "a", "batch_index": 1}, {"error": "b", "batch_index": 1}], COLLECTED_AT, {})
    assert len(r1) == 1
    summary = instagram_finalize(r1 + r2, l1 + l2, items_total=3)
    assert summary["batches_failed"] == 1, "retry одного reel-батча не двоится"


# --------------------- instagram.test.js: Normalize-часть ---------------------

# JS: 'isUsable: error-item отбраковывается'
def test_is_usable_error_item_rejected():
    assert is_usable({"error": "Request failed 408"}) is False
    assert is_usable({"errorDescription": "timeout"}) is False
    assert is_usable({"errorMessage": "boom"}) is False


# JS: 'isUsable: пустой item / без id-url отбраковывается'
def test_is_usable_empty_or_idless_rejected():
    assert is_usable({}) is False
    assert is_usable(None) is False
    assert is_usable({"caption": "только подпись, ни id ни url"}) is False


# JS: 'isUsable: реальный reel (id или url) годен'
def test_is_usable_real_reel_accepted():
    assert is_usable({"id": "abc"}) is True
    assert is_usable({"url": "https://www.instagram.com/reel/x/"}) is True
    assert is_usable({"shortCode": "Cx1"}) is True


IG_VALID = {
    "id": "abc123",
    "url": "https://www.instagram.com/reel/abc123/",
    "ownerUsername": "someguy",
    "caption": "nice reel",
    "timestamp": "2024-07-16T00:53:20.000Z",
    "videoViewCount": 1000,
}


def ig_run_and_finalize(items):
    rows, losses, _ = instagram_rows(items, COLLECTED_AT, {})
    instagram_finalize(rows, losses, items_total=len(items))
    return rows


# JS: 'Normalize: только error+пустой item -> throw (потеря батча видна, 0 мусорных рядов)'
def test_ig_error_plus_empty_only_raises():
    with pytest.raises(CollectError, match="0 годных reels|instagram_0 не пишем"):
        ig_run_and_finalize([{"error": "Request failed with 408"}, {}])


# JS: 'Normalize: одиночный error-item -> throw, никакого instagram_0'
def test_ig_single_error_item_raises():
    with pytest.raises(CollectError, match="годных reels"):
        ig_run_and_finalize([{"error": "gateway timeout"}])


# JS: 'Normalize: валидный + error + пустой -> ровно 1 ряд, нет instagram_0'
def test_ig_mixed_items_yield_only_valid_row():
    rows = ig_run_and_finalize([IG_VALID, {"error": "x"}, {}])
    assert len(rows) == 1, "мусорные item'ы должны быть отброшены"
    assert rows[0]["raw_id"] == "instagram_abc123"
    assert rows[0]["video_id"] == "abc123"
    assert not any(r["raw_id"] == "instagram_0" for r in rows), "ряда instagram_0 быть не должно"


# JS: 'Normalize: только валидный item -> нормальный ряд'
def test_ig_valid_item_normal_row():
    rows = ig_run_and_finalize([IG_VALID])
    assert len(rows) == 1
    assert rows[0]["raw_id"] == "instagram_abc123"
    assert rows[0]["views"] == 1000
    assert rows[0]["platform"] == "instagram"


# JS: 'Normalize регресс: старый фильтр raw && Object.keys пропустил бы error-item'
def test_ig_regression_old_filter_would_pass_error_item():
    err_item = {"error": "boom"}
    assert err_item and len(err_item), "старое условие -> истина (баг: писал instagram_0)"
    assert is_usable(err_item) is False, "новое условие -> дроп"


# --------------------- parity: полные ряды против живого JS ---------------------
# Значения сняты node-прогоном JS-узлов (Date заморожен на 2024-07-16T07:53:20.000Z).

CLOCKWORKS_RAW = {
    "id": "7381234567890123456",
    "text": "Как одеваться мужчине летом #menswear #style",
    "textLanguage": "ru",
    "createTimeISO": "2024-07-15T07:53:20.000Z",
    "authorMeta": {"name": "stylist.ru", "nickName": "Стилист"},
    "webVideoUrl": "https://www.tiktok.com/@stylist.ru/video/7381234567890123456",
    "videoMeta": {
        "duration": 34,
        "coverUrl": "https://cover/img.jpg",
        "subtitleLinks": [
            {"language": "eng-US", "downloadLink": "https://sub/eng.vtt", "source": "MT"},
            {"language": "rus-RU", "downloadLink": "https://sub/rus.vtt", "source": "ASR",
             "sourceUnabbreviated": "automatic_speech_recognition"},
        ],
    },
    "diggCount": 15200,
    "shareCount": 340,
    "playCount": 189000,
    "commentCount": 210,
    "collectCount": 890,
    "mediaUrls": ["https://video/dl.mp4"],
    "isAd": False,
    "searchHashtag": {"name": "menswear", "views": 123},
}


def test_tiktok_row_clockworks_parity_with_js():
    row = tiktok_row(CLOCKWORKS_RAW, COLLECTED_AT)
    expected = {
        "raw_id": "tiktok_7381234567890123456",
        "collected_at": "2024-07-16T07:53:20.000Z",
        "platform": "tiktok",
        "source_actor": "clockworks/tiktok-scraper",
        "source_query": "hashtag:#menswear",
        "is_ad": False,
        "text_language": "ru",
        "video_id": "7381234567890123456",
        "url": "https://www.tiktok.com/@stylist.ru/video/7381234567890123456",
        "author": "stylist.ru",
        "caption": "Как одеваться мужчине летом #menswear #style",
        "created_at": "2024-07-15T07:53:20.000Z",
        "duration_sec": 34,
        "views": 189000,
        "likes": 15200,
        "comments": 210,
        "shares": 340,
        "saves": 890,
        "engagement_rate": 0.088,
        "age_hours": 24,
        "thumbnail_url": "https://cover/img.jpg",
        "video_url": "https://video/dl.mp4",
        "transcript_text": "",
        "subtitle_url": "https://sub/rus.vtt",  # rus приоритетнее ASR/eng
        "subtitle_language": "rus-RU",
        "subtitle_source": "automatic_speech_recognition",
        "raw_json": '{"id":"7381234567890123456","text":"Как одеваться мужчине летом #menswear #style","textLanguage":"ru","createTimeISO":"2024-07-15T07:53:20.000Z","authorMeta":{"name":"stylist.ru","nickName":"Стилист"},"webVideoUrl":"https://www.tiktok.com/@stylist.ru/video/7381234567890123456","videoMeta":{"duration":34,"coverUrl":"https://cover/img.jpg","subtitleLinks":[{"language":"eng-US","downloadLink":"https://sub/eng.vtt","source":"MT"},{"language":"rus-RU","downloadLink":"https://sub/rus.vtt","source":"ASR","sourceUnabbreviated":"automatic_speech_recognition"}]},"diggCount":15200,"shareCount":340,"playCount":189000,"commentCount":210,"collectCount":890,"mediaUrls":["https://video/dl.mp4"],"isAd":false,"searchHashtag":{"name":"menswear","views":123}}',
        "processing_status": "subtitle_pending_download",
    }
    assert row == expected
    assert list(row) == list(expected), "порядок колонок как в JS-ряде"


# Item clockworks~tiktok-hashtag-scraper (гибрид, тикет 02): по хэндоффу
# 2026-08-10 выдача — ТОТ ЖЕ формат видео clockworks, что у основного актора
# (включая searchHashtag — на нём держится атрибуция source_query и exploration).
# Живых ответов до 19.08 нет: фикстура построена по этому допущению и входит
# в runbook смоука 19.08 на подтверждение (Testing Decisions спеки).
HASHTAG_SCRAPER_RAW = {
    "id": "7412345678901234567",
    "text": "Осенняя капсула мужского гардероба #мужскойстиль",
    "textLanguage": "ru",
    "createTimeISO": "2024-07-14T07:53:20.000Z",
    "authorMeta": {"name": "capsule.ru", "nickName": "Капсула"},
    "webVideoUrl": "https://www.tiktok.com/@capsule.ru/video/7412345678901234567",
    "videoMeta": {
        "duration": 21,
        "coverUrl": "https://cover/ht.jpg",
        "subtitleLinks": [
            {"language": "rus-RU", "downloadLink": "https://sub/ht-rus.vtt",
             "source": "ASR", "sourceUnabbreviated": "automatic_speech_recognition"},
        ],
    },
    "diggCount": 8000,
    "shareCount": 300,
    "playCount": 100000,
    "commentCount": 500,
    "collectCount": 1200,
    "mediaUrls": ["https://video/ht.mp4"],
    "isAd": False,
    "searchHashtag": {"name": "мужскойстиль", "views": 456},
}


def test_tiktok_row_hashtag_scraper_parity_same_canonical_format():
    # Смена актора для нормализации невидима: ни одного изменения формата —
    # те же колонки в том же порядке, source_actor остаётся display-формой
    # семейства clockworks (гейт, анализ и coalesce не замечают миграции).
    rows, losses = tiktok_rows([HASHTAG_SCRAPER_RAW], COLLECTED_AT)
    assert losses == []
    expected = {
        "raw_id": "tiktok_7412345678901234567",
        "collected_at": "2024-07-16T07:53:20.000Z",
        "platform": "tiktok",
        "source_actor": "clockworks/tiktok-scraper",
        "source_query": "hashtag:#мужскойстиль",
        "is_ad": False,
        "text_language": "ru",
        "video_id": "7412345678901234567",
        "url": "https://www.tiktok.com/@capsule.ru/video/7412345678901234567",
        "author": "capsule.ru",
        "caption": "Осенняя капсула мужского гардероба #мужскойстиль",
        "created_at": "2024-07-14T07:53:20.000Z",
        "duration_sec": 21,
        "views": 100000,
        "likes": 8000,
        "comments": 500,
        "shares": 300,
        "saves": 1200,
        "engagement_rate": 0.1,
        "age_hours": 48,
        "thumbnail_url": "https://cover/ht.jpg",
        "video_url": "https://video/ht.mp4",
        "transcript_text": "",
        "subtitle_url": "https://sub/ht-rus.vtt",
        "subtitle_language": "rus-RU",
        "subtitle_source": "automatic_speech_recognition",
        "raw_json": '{"id":"7412345678901234567","text":"Осенняя капсула мужского гардероба #мужскойстиль","textLanguage":"ru","createTimeISO":"2024-07-14T07:53:20.000Z","authorMeta":{"name":"capsule.ru","nickName":"Капсула"},"webVideoUrl":"https://www.tiktok.com/@capsule.ru/video/7412345678901234567","videoMeta":{"duration":21,"coverUrl":"https://cover/ht.jpg","subtitleLinks":[{"language":"rus-RU","downloadLink":"https://sub/ht-rus.vtt","source":"ASR","sourceUnabbreviated":"automatic_speech_recognition"}]},"diggCount":8000,"shareCount":300,"playCount":100000,"commentCount":500,"collectCount":1200,"mediaUrls":["https://video/ht.mp4"],"isAd":false,"searchHashtag":{"name":"мужскойстиль","views":456}}',
        "processing_status": "subtitle_pending_download",
    }
    assert rows[0] == expected
    assert list(rows[0]) == list(expected), "порядок колонок как у основного актора"
    # и та же колонка-в-колонку структура, что у ряда основного актора
    assert list(rows[0]) == list(tiktok_row(CLOCKWORKS_RAW, COLLECTED_AT))


# Item apidojo~tiktok-scraper (тикет 04, кандидат за конфиг-флагом). Формат —
# по README актора, раздел «Example Output Object» (билд latest 0.0.1055,
# сверка по API 10.08): id/title/views/likes/comments/shares/bookmarks/
# hashtags/channel/uploadedAt(Formatted)/video/song/subtitleInformation/
# postPage. Это документация вендора, не голое допущение, но живых ответов до
# 19.08 нет — фикстура помечена на подтверждение в runbook смоука (тикет 07).
APIDOJO_RAW = {
    "id": "7353781970163272993",
    "title": "Три образа на осень #мужскойстиль #capsule",
    "views": 101399,
    "likes": 7420,
    "comments": 201,
    "shares": 1236,
    "bookmarks": 7195,
    "hashtags": ["мужскойстиль", "capsule"],
    "channel": {
        "id": "6761142551312729093",
        "name": "Джексон",
        "username": "jacksonstips",
        "verified": True,
        "url": "https://www.tiktok.com/@jacksonstips",
        "followers": 566322,
    },
    "uploadedAt": 1721030000,
    "uploadedAtFormatted": "2024-07-15T07:53:20.000Z",
    "video": {
        "width": 576,
        "height": 1024,
        "ratio": "540p",
        "duration": 27.5,
        "url": "https://v45.tiktokcdn-eu.com/video.mp4",
        "cover": "https://p16-sign.tiktokcdn.com/cover.jpg",
        "thumbnail": "https://p16-sign.tiktokcdn.com/thumb.jpg",
    },
    "song": {"id": 7353782204394245000, "title": "original sound", "artist": "jacksonstips"},
    "subtitleInformation": [
        {"caption_format": "webvtt", "lang": "eng-US", "language_code": "en",
         "is_auto_generated": False,
         "url": "https://v16-cla.tiktokcdn.com/eng.vtt"},
        {"caption_format": "webvtt", "lang": "rus-RU", "language_code": "ru",
         "is_auto_generated": True,
         "url": "https://v16-cla.tiktokcdn.com/rus.vtt"},
    ],
    "postPage": "https://www.tiktok.com/@jacksonstips/video/7353781970163272993",
}

APIDOJO_BATCH_SOURCES = ["hashtag:#пиджак", "hashtag:#мужскойстиль"]


def test_apidojo_row_parity_canonical_format():
    # Адаптер невидим для гейта/анализа/coalesce: те же колонки в том же
    # порядке, что у clockworks-ряда. source_query восстановлен из
    # batch_sources по документированному полю hashtags (в выдаче apidojo
    # searchHashtag-а нет); субтитры — ссылкой в существующий контур
    # скачивания (rus приоритетнее eng, как в _pick_subtitle).
    rows, losses = apidojo_tiktok_rows([APIDOJO_RAW], COLLECTED_AT,
                                       batch_sources=APIDOJO_BATCH_SOURCES)
    assert losses == []
    expected = {
        "raw_id": "tiktok_7353781970163272993",
        "collected_at": "2024-07-16T07:53:20.000Z",
        "platform": "tiktok",
        "source_actor": "apidojo/tiktok-scraper",
        "source_query": "hashtag:#мужскойстиль",
        "is_ad": False,
        "text_language": "",
        "video_id": "7353781970163272993",
        "url": "https://www.tiktok.com/@jacksonstips/video/7353781970163272993",
        "author": "jacksonstips",
        "caption": "Три образа на осень #мужскойстиль #capsule",
        "created_at": "2024-07-15T07:53:20.000Z",
        "duration_sec": 27.5,
        "views": 101399,
        "likes": 7420,
        "comments": 201,
        "shares": 1236,
        "saves": 7195,
        "engagement_rate": 0.1583,
        "age_hours": 24,
        "thumbnail_url": "https://p16-sign.tiktokcdn.com/cover.jpg",
        "video_url": "https://v45.tiktokcdn-eu.com/video.mp4",
        "transcript_text": "",
        "subtitle_url": "https://v16-cla.tiktokcdn.com/rus.vtt",
        "subtitle_language": "rus-RU",
        "subtitle_source": "auto_generated",
        "raw_json": '{"id":"7353781970163272993","title":"Три образа на осень #мужскойстиль #capsule","views":101399,"likes":7420,"comments":201,"shares":1236,"bookmarks":7195,"hashtags":["мужскойстиль","capsule"],"channel":{"id":"6761142551312729093","name":"Джексон","username":"jacksonstips","verified":true,"url":"https://www.tiktok.com/@jacksonstips","followers":566322},"uploadedAt":1721030000,"uploadedAtFormatted":"2024-07-15T07:53:20.000Z","video":{"width":576,"height":1024,"ratio":"540p","duration":27.5,"url":"https://v45.tiktokcdn-eu.com/video.mp4","cover":"https://p16-sign.tiktokcdn.com/cover.jpg","thumbnail":"https://p16-sign.tiktokcdn.com/thumb.jpg"},"song":{"id":7353782204394245000,"title":"original sound","artist":"jacksonstips"},"subtitleInformation":[{"caption_format":"webvtt","lang":"eng-US","language_code":"en","is_auto_generated":false,"url":"https://v16-cla.tiktokcdn.com/eng.vtt"},{"caption_format":"webvtt","lang":"rus-RU","language_code":"ru","is_auto_generated":true,"url":"https://v16-cla.tiktokcdn.com/rus.vtt"}],"postPage":"https://www.tiktok.com/@jacksonstips/video/7353781970163272993"}',
        "processing_status": "subtitle_pending_download",
    }
    assert rows[0] == expected
    assert list(rows[0]) == list(expected), "порядок колонок как у clockworks"
    assert list(rows[0]) == list(tiktok_row(CLOCKWORKS_RAW, COLLECTED_AT))


def test_apidojo_without_subtitles_honest_no_transcript_status():
    # Готового текста транскрипта в документированном выходе apidojo нет
    # вообще, субтитры — только ссылками. Item без subtitleInformation честно
    # получает «без актор-транскрипта», а не пустую строку под видом текста.
    raw = {k: v for k, v in APIDOJO_RAW.items() if k != "subtitleInformation"}
    rows, losses = apidojo_tiktok_rows([raw], COLLECTED_AT,
                                       batch_sources=APIDOJO_BATCH_SOURCES)
    assert losses == []
    assert rows[0]["transcript_text"] == ""
    assert rows[0]["subtitle_url"] == ""
    assert rows[0]["processing_status"] == "raw_saved_no_actor_transcript"


def test_apidojo_source_query_restored_from_batch():
    # Первым — inputSource (живой парити 10.08: актор кладёт туда искомый
    # keyword, README молчит; без него 36% строк были unknown — keyword-поиск
    # возвращает ролики без искомого тега в hashtags). Фолбэк — hashtags
    # (без регистра и «#», как M28); совпадения нет — единственный источник
    # батча берётся как есть; несколько и ни одного совпадения — честный
    # unknown, не первый попавшийся.
    by_input, _ = apidojo_tiktok_rows(
        [{**APIDOJO_RAW, "inputSource": "#МужскойГардероб",
          "hashtags": ["другое", "вовсе"]}], COLLECTED_AT,
        batch_sources=["hashtag:#пиджак", "hashtag:#мужскойгардероб"])
    assert by_input[0]["source_query"] == "hashtag:#мужскойгардероб"

    by_input_query, _ = apidojo_tiktok_rows(
        [{**APIDOJO_RAW, "inputSource": "мужская мода", "hashtags": []}],
        COLLECTED_AT,
        batch_sources=["query:мужская мода", "query:осень"])
    assert by_input_query[0]["source_query"] == "query:мужская мода"

    match, _ = apidojo_tiktok_rows(
        [{**APIDOJO_RAW, "hashtags": ["МужскойСтиль"]}], COLLECTED_AT,
        batch_sources=["hashtag:#пиджак", "hashtag:#мужскойстиль"])
    assert match[0]["source_query"] == "hashtag:#мужскойстиль"

    single, _ = apidojo_tiktok_rows(
        [{**APIDOJO_RAW, "hashtags": []}], COLLECTED_AT,
        batch_sources=["query:мужская мода"])
    assert single[0]["source_query"] == "query:мужская мода"

    unknown, _ = apidojo_tiktok_rows(
        [{**APIDOJO_RAW, "hashtags": ["другое"]}], COLLECTED_AT,
        batch_sources=["hashtag:#пиджак", "hashtag:#часы"])
    assert unknown[0]["source_query"] == "unknown"


def test_apidojo_item_without_url_fails_batch_loudly():
    # Обязательные поля адаптера: url (postPage) и стабильный идентификатор
    # (id; при его отсутствии — сам url через hash). Item без url — неузнанный
    # формат: батч помечается провальным loss-маркером существующей механики
    # (__apify_error -> гейт -> причина в Run Log), а не молчаливой кривой
    # строкой. Валидные item'ы того же батча при этом доезжают.
    rows, losses = apidojo_tiktok_rows(
        [APIDOJO_RAW,
         {"title": "item без id и url"},
         {"id": "123", "title": "id есть, url нет"},
         "не-dict мусор"],
        COLLECTED_AT, batch_index=2, batch_sources=["hashtag:#мужскойстиль"])
    assert [r["raw_id"] for r in rows] == ["tiktok_7353781970163272993"]
    assert len(losses) == 1, "один маркер на батч, не на item"
    assert losses[0]["batch_index"] == 2
    assert losses[0]["batch_sources"] == ["hashtag:#мужскойстиль"]
    assert "формат выхода не распознан" in losses[0]["__apify_error"]
    assert "3" in losses[0]["__apify_error"], "сколько item'ов не распознано"


def test_apidojo_url_without_id_hashes_raw_id():
    # id отсутствует, url есть — стабильный идентификатор строится как у
    # clockworks: hash(url+caption). Пустые item'ы дропаются молча (как в
    # tiktok_rows), провалом батча они не считаются.
    raw = {"postPage": "https://www.tiktok.com/@u/video/x",
           "title": "подпись без id"}
    rows, losses = apidojo_tiktok_rows([raw, {}, None], COLLECTED_AT,
                                       batch_sources=["hashtag:#пиджак"])
    assert losses == []
    assert len(rows) == 1
    assert rows[0]["raw_id"].startswith("tiktok_")
    assert rows[0]["raw_id"] != "tiktok_"
    assert rows[0]["video_id"] == ""


def test_tiktok_row_no_id_hash_and_hidden_likes_parity_with_js():
    raw = {
        "url": "https://www.tiktok.com/@u/video/x",
        "desc": "подпись без id",
        "createTime": 1721030000,
        "diggCount": -1,
        "playCount": 1000,
        "commentCount": 30,
    }
    row = tiktok_row(raw, COLLECTED_AT)
    assert row["raw_id"] == "tiktok_24ssbv"  # hash(url+caption) — значение из JS
    assert row["source_query"] == "unknown"
    assert row["likes"] is None  # отрицательное = скрыто: пусто, не ноль
    assert row["engagement_rate"] == 0.03  # скрытые лайки не входят в числитель
    assert row["created_at"] == "2024-07-15T07:53:20.000Z"
    assert row["processing_status"] == "raw_saved_no_actor_transcript"


IG_REEL_RAW = {
    "id": "3412345678901234567",
    "shortCode": "C8abcDEfGh",
    "url": "https://www.instagram.com/reel/C8abcDEfGh/",
    "ownerUsername": "menstyle.moscow",
    "caption": "Три правила стиля #menswear",
    "timestamp": "2024-07-15T07:53:20.000Z",
    "videoViewCount": 50000,
    "likesCount": 4200,
    "commentsCount": 130,
    "videoDuration": 27.5,
    "displayUrl": "https://cdn/insta.jpg",
    "videoUrl": "https://cdn/video.mp4",
    "transcript": [{"text": "первое правило"}, {"text": "второе правило"}],
}


def test_instagram_row_parity_with_js():
    row = instagram_row(IG_REEL_RAW, COLLECTED_AT, {"C8abcDEfGh": "menswear"})
    expected = {
        "raw_id": "instagram_3412345678901234567",
        "collected_at": "2024-07-16T07:53:20.000Z",
        "platform": "instagram",
        "source_actor": "apify/instagram-reel-scraper",
        "source_query": "hashtag:#menswear",
        "video_id": "3412345678901234567",
        "url": "https://www.instagram.com/reel/C8abcDEfGh/",
        "author": "menstyle.moscow",
        "caption": "Три правила стиля #menswear",
        "created_at": "2024-07-15T07:53:20.000Z",
        "duration_sec": 27.5,
        "views": 50000,
        "likes": 4200,
        "comments": 130,
        "shares": 0,
        "saves": 0,
        "engagement_rate": 0.0866,
        "age_hours": 24,
        "thumbnail_url": "https://cdn/insta.jpg",
        "video_url": "https://cdn/video.mp4",
        "transcript_text": "первое правило второе правило",
        "raw_json": '{"id":"3412345678901234567","shortCode":"C8abcDEfGh","url":"https://www.instagram.com/reel/C8abcDEfGh/","ownerUsername":"menstyle.moscow","caption":"Три правила стиля #menswear","timestamp":"2024-07-15T07:53:20.000Z","videoViewCount":50000,"likesCount":4200,"commentsCount":130,"videoDuration":27.5,"displayUrl":"https://cdn/insta.jpg","videoUrl":"https://cdn/video.mp4","transcript":[{"text":"первое правило"},{"text":"второе правило"}]}',
        "processing_status": "raw_saved_actor_transcript",
    }
    assert row == expected
    assert list(row) == list(expected), "порядок колонок как в JS-ряде"


# --------------------- атрибуция source_query и границы ---------------------

def test_source_query_derivation():
    # searchHashtag.name с ведущим '#' — срезается один раз, как в JS replace(/^#/,'')
    assert tiktok_row({"id": "v", "searchHashtag": {"name": "#tag"}}, COLLECTED_AT)["source_query"] == "hashtag:#tag"
    assert tiktok_row({"id": "v", "searchQuery": "мужская мода"}, COLLECTED_AT)["source_query"] == "query:мужская мода"
    assert tiktok_row({"id": "v"}, COLLECTED_AT)["source_query"] == "unknown"
    # явная атрибуция (снежок) перекрывает вывод из raw
    row = tiktok_row({"id": "v", "searchQuery": "x"}, COLLECTED_AT, source_query="snowball:https://seed")
    assert row["source_query"] == "snowball:https://seed"


def test_ig_source_query_unknown_without_tag():
    row = instagram_row(IG_REEL_RAW, COLLECTED_AT, {})
    assert row["source_query"] == "unknown"


def test_raw_json_truncated_to_45000():
    raw = {"id": "v1", "text": "x" * 60000}
    row = tiktok_row(raw, COLLECTED_AT)
    full = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    assert row["raw_json"] == full[:45000]
    assert len(row["raw_json"]) == 45000
