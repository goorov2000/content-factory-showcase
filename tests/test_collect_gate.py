# C1.5 — порт гейтов из n8n (эталоны: cf01-tiktok/code/Ingestion-Gate.js ≡
# cf01b-snowball, cf01-instagram/code/Ingestion-Gate-IG.js). Инварианты:
# gate.test.js (все 3), losses.test.js (гейтовые части, параметризация
# TikTok/Snowball — один код, два прогона), instagram.test.js (IG Gate-часть).
import re
from datetime import datetime, timedelta, timezone

import pytest

from cf.collect.gate import CollectGateError, instagram_gate, tiktok_gate


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


NOW = datetime.now(timezone.utc)
RECENT_ISO = _iso(NOW)  # не too_old / не stale


def days_ago(d):
    return _iso(NOW - timedelta(days=d))


# ------------------------------------------------ gate.test.js (P1.18(e))

STUFFED = {"caption": "#a#b#c#d#e#f#g#h#i#j", "text_language": "ru",
           "created_at": RECENT_ISO, "views": 1000}


# JS: 'слитные хэштеги "#a#b#c...#j" (10) -> дроп hashtag_stuffing, чистый ряд остаётся'
def test_stuffed_hashtags_dropped_clean_row_kept():
    clean = {"caption": "nice video #one #two", "text_language": "ru",
             "created_at": RECENT_ISO, "views": 1000}
    res = tiktok_gate([dict(STUFFED), clean])
    assert len(res["rows"]) == 1, "stuffed-ряд должен быть отброшен"
    assert res["rows"][0]["caption"] == "nice video #one #two"


# JS: 'ровно 9 слитных хэштегов проходит (граница maxCaptionHashtags=9)'
def test_exactly_nine_merged_hashtags_pass():
    nine = {"caption": "#a#b#c#d#e#f#g#h#i", "text_language": "ru",
            "created_at": RECENT_ISO, "views": 1000}
    assert len(tiktok_gate([nine])["rows"]) == 1


# JS: 'регресс: старый /#\S+/ посчитал бы слитную цепочку как 1'
def test_regress_old_regex_counts_merged_chain_as_one():
    caption = "#a#b#c#d#e#f#g#h#i#j"
    assert len(re.findall(r"#\S+", caption)) == 1        # старое (баг)
    assert len(re.findall(r"#[^\s#]+", caption)) == 10   # новое (фикс)


# ------------------- losses.test.js (P5.14) — TikTok / Snowball Gate
# Ingestion-Gate.js в cf01 и cf01b идентичен — один tiktok_gate, два «профиля».

GOOD_ROW = {"caption": "nice #one", "text_language": "ru",
            "created_at": RECENT_ISO, "views": 1000}
LOSS_MARKER = {"__apify_error": "ETIMEDOUT", "source_query": "unknown",
               "batch_index": 3, "batch_sources": ["hashtag:#p", "query:q"]}

profiles = pytest.mark.parametrize("label", ["tiktok", "snowball"])


# JS: '{label} Gate: loss-маркер посчитан (batches_failed/sources_lost) и НЕ попал в kept'
@profiles
def test_loss_marker_counted_not_in_kept(label):
    res = tiktok_gate([dict(GOOD_ROW), dict(LOSS_MARKER)])
    assert len(res["rows"]) == 1, "валидный ряд остаётся, маркер не в данных"
    assert res["rows"][0]["caption"] == "nice #one"
    s = res["summary"]
    assert s["batches_failed"] == 1
    assert s["sources_lost"] == 2
    assert s["lost_sources"] == ["hashtag:#p", "query:q"]
    assert s["input"] == 1, "input считает только реальные ряды, не маркеры"


# JS: '{label} Gate: без потерь -> batches_failed=0, старое поведение drops сохранено'
@profiles
def test_no_losses_drops_preserved(label):
    res = tiktok_gate([dict(GOOD_ROW), dict(STUFFED)])
    assert len(res["rows"]) == 1, "stuffed отброшен, чистый остался"
    s = res["summary"]
    assert s["batches_failed"] == 0
    assert s["sources_lost"] == 0
    assert s["drops"]["hashtag_stuffing"] == 1


# Разбор 2026-07-27: причина отказа Apify («потолок трат» → раны не стартуют)
# доезжала до маркера, но умирала в сводке — в Run Log лежали одни имена
# источников, и «кончились деньги» было не отличить от «протух токен».
@profiles
def test_lost_reasons_carry_apify_cause(label):
    res = tiktok_gate([dict(GOOD_ROW), dict(LOSS_MARKER)])
    assert res["summary"]["lost_reasons"] == ["батч 3: ETIMEDOUT"]


# Причина одного батча в сводке одна: retry даёт два маркера с тем же текстом,
# дублировать его в errors Run Log незачем (batches_failed считается так же).
@profiles
def test_lost_reasons_unique_per_batch(label):
    m1 = {"__apify_error": "apify run FAILED (clockworks~tiktok-scraper)",
          "batch_index": 2, "batch_sources": ["hashtag:#a"]}
    res = tiktok_gate([dict(GOOD_ROW), dict(m1), dict(m1)])
    assert res["summary"]["lost_reasons"] == [
        "батч 2: apify run FAILED (clockworks~tiktok-scraper)"]


# JS: '{label} Gate: ПОЛНЫЙ провал (только loss-маркеры, 0 рядов) -> throw громко'
@profiles
def test_total_failure_raises(label):
    with pytest.raises(CollectGateError, match="0 реальных рядов"):
        tiktok_gate([dict(LOSS_MARKER)])


# Разбор 2026-07-27: при полном провале _result не вызывается вообще, поэтому
# самый громкий отказ был самым бессодержательным — исключение несёт имена и
# причины само.
@profiles
def test_total_failure_error_carries_sources_and_reasons(label):
    with pytest.raises(CollectGateError) as exc:
        tiktok_gate([dict(LOSS_MARKER)])
    assert exc.value.lost_sources == ["hashtag:#p", "query:q"]
    assert exc.value.lost_reasons == ["батч 3: ETIMEDOUT"]


# JS: '{label} Gate: batches_failed по уникальным batch_index (retry одного батча не двоится)'
@profiles
def test_batches_failed_unique_batch_index(label):
    m1 = {"__apify_error": "e", "batch_index": 2, "batch_sources": ["hashtag:#a"]}
    m2 = {"__apify_error": "e2", "batch_index": 2, "batch_sources": ["hashtag:#a"]}
    res = tiktok_gate([dict(GOOD_ROW), m1, m2])
    assert len(res["rows"]) == 1
    assert res["summary"]["batches_failed"] == 1, \
        "два маркера одного batch_index = 1 упавший батч"


# JS: '{label} Gate: пустой вход (0 рядов, 0 потерь) -> НЕ throw'
@profiles
def test_empty_input_no_raise(label):
    assert tiktok_gate([])["rows"] == []


# --------------------------------- losses.test.js (P5.14) — IG Gate

IG_GOOD = {"ownerUsername": "someguy", "caption": "nice",
           "timestamp": days_ago(1), "videoViewCount": 1000}


# JS: 'IG Gate: нативный {error}-маркер упавшего батча посчитан, не в kept; хорошие доезжают'
def test_ig_native_error_marker_counted_not_in_kept():
    res = instagram_gate(
        [{"items": [dict(IG_GOOD)]}, {"error": "run-sync 300s timeout"}],
        batches=[{}, {"batch_sources": ["hashtag:#menswear", "hashtag:#menstyle"]}],
    )
    assert len(res["rows"]) == 1
    assert res["rows"][0]["ownerUsername"] == "someguy"
    s = res["summary"]
    assert s["batches_failed"] == 1
    assert s["sources_lost"] == 2
    assert s["lost_sources"] == ["hashtag:#menswear", "hashtag:#menstyle"]


# JS: 'IG Gate: без ошибок -> batches_failed=0, форма {items:[...]} по-прежнему фильтруется'
def test_ig_no_errors_items_form_still_filtered():
    blocked = {"ownerUsername": "meijorofficial", "caption": "ad",
               "timestamp": days_ago(1), "videoViewCount": 1000}
    res = instagram_gate([{"items": [dict(IG_GOOD), blocked]}])
    assert len(res["rows"]) == 1
    assert res["summary"]["batches_failed"] == 0


# JS: 'IG Gate: ПОЛНЫЙ провал (только {error}, 0 годных) -> throw громко'
def test_ig_total_failure_raises():
    with pytest.raises(CollectGateError, match="0 годных item"):
        instagram_gate([{"error": "run-sync 300s timeout"}])


# Разбор 2026-07-27: ночью легли ВСЕ 5 hashtag-батчей IG, и это ровно тот путь,
# где терялись даже ИМЕНА источников — raise идёт до _result.
def test_ig_total_failure_error_carries_sources_and_reasons():
    with pytest.raises(CollectGateError) as exc:
        instagram_gate(
            [{"error": "apify run FAILED (apify~instagram-hashtag-scraper)"}],
            batches=[{"batch_sources": ["hashtag:#menswear"], "batch_index": 0}])
    assert exc.value.lost_sources == ["hashtag:#menswear"]
    assert exc.value.lost_reasons == [
        "батч 0: apify run FAILED (apify~instagram-hashtag-scraper)"]


# Сводка IG-гейта несёт причины так же, как TikTok — контракт у гейта один.
def test_ig_lost_reasons_in_summary():
    res = instagram_gate(
        [{"items": [dict(IG_GOOD)]}, {"error": "run-sync 300s timeout"}],
        batches=[{}, {"batch_sources": ["hashtag:#menstyle"], "batch_index": 1}])
    assert res["summary"]["lost_reasons"] == ["батч 1: run-sync 300s timeout"]


# JS: 'IG Gate: top-level item С error И id (per-hashtag ошибка успешного рана) -> НЕ упавший батч'
def test_ig_error_with_id_is_not_failed_batch():
    err_with_id = {
        "id": "X1", "url": "https://www.instagram.com/reel/X1/",
        "error": "field-level note", "ownerUsername": "guy", "caption": "x",
        "timestamp": days_ago(1), "videoViewCount": 1000,
    }
    res = instagram_gate([err_with_id])
    assert res["summary"]["batches_failed"] == 0, \
        "item с id не считается упавшим батчем (строгий isApifyError)"
    assert len(res["rows"]) == 1, "реальный item проходит"


# JS: 'IG Gate: два {error} одного hashtag-батча (retry) -> batches_failed=1'
def test_ig_retry_same_batch_index_counted_once():
    res = instagram_gate([{"items": [dict(IG_GOOD)]},
                          {"error": "a", "batch_index": 3},
                          {"error": "b", "batch_index": 3}])
    assert len(res["rows"]) == 1
    assert res["summary"]["batches_failed"] == 1


# ------------------------- instagram.test.js (P1.19) — IG Gate-часть

BLOCKED_POST = {"ownerUsername": "meijorofficial", "caption": "ad",
                "timestamp": days_ago(1), "videoViewCount": 1000}


# JS: 'Gate: форма {items:[...]} разворачивается И фильтруется (не проходит целиком)'
def test_ig_items_form_unwrapped_and_filtered():
    res = instagram_gate([{"items": [dict(IG_GOOD), dict(BLOCKED_POST)]}])
    assert len(res["rows"]) == 1, "заблокированный автор внутри items[] должен быть отброшен"
    assert res["rows"][0]["ownerUsername"] == "someguy", \
        "ряд — развёрнутый пост, а не {items:[...]}"


# JS: 'Gate: форма {data:[...]} разворачивается И фильтруется'
def test_ig_data_form_unwrapped_and_filtered():
    res = instagram_gate([{"data": [dict(IG_GOOD), dict(BLOCKED_POST)]}])
    assert len(res["rows"]) == 1
    assert res["rows"][0]["ownerUsername"] == "someguy"


# JS: 'Gate: 0-view пост старше 14 дней -> дроп stale_low_views'
def test_ig_zero_view_stale_dropped():
    stale0 = {"ownerUsername": "guy", "caption": "x",
              "timestamp": days_ago(20), "videoViewCount": 0}
    res = instagram_gate([stale0])
    assert res["rows"] == [], "0-view stale больше НЕ милуется falsy-guard'ом"
    assert res["summary"]["drops"] == {"stale_low_views": 1}


# JS: 'Gate: stale-пост с 100 просмотрами тоже дропается (порог 500)'
def test_ig_stale_100_views_dropped():
    stale100 = {"ownerUsername": "guy", "caption": "x",
                "timestamp": days_ago(20), "videoViewCount": 100}
    assert instagram_gate([stale100])["rows"] == []


# JS: 'Gate: свежий 0-view пост -> остаётся (не stale)'
def test_ig_fresh_zero_view_kept():
    fresh0 = {"ownerUsername": "guy", "caption": "x",
              "timestamp": days_ago(1), "videoViewCount": 0}
    assert len(instagram_gate([fresh0])["rows"]) == 1


# JS: 'Gate: stale-пост с 1000 просмотров -> остаётся (>= порога)'
def test_ig_stale_high_views_kept():
    stale_hi = {"ownerUsername": "guy", "caption": "x",
                "timestamp": days_ago(20), "videoViewCount": 1000}
    assert len(instagram_gate([stale_hi])["rows"]) == 1


# JS: 'Gate: нормальный свежий пост -> остаётся'
def test_ig_normal_fresh_post_kept():
    assert len(instagram_gate([dict(IG_GOOD)])["rows"]) == 1
