# C1.1 — порт чистых хелперов из n8n/*/code/*.js (эталоны: Normalize-TikTok-Raw-Rows.js,
# Build-Snowball-Input.js). Инварианты: normalize.test.js (toIso), snowball-input.test.js
# (normalizeSeedUrl); parity-значения stable_hash сняты живым node с JS-функции hash().
from datetime import datetime, timezone

from cf.collect.util import (
    age_hours,
    engagement_rate,
    first,
    is_apify_error,
    normalize_url,
    num,
    stable_hash,
    to_iso,
)


# --- to_iso (normalize.test.js, P1.18(d)) ---

def test_to_iso_epoch_string():
    out = to_iso("1721030000")
    assert out == "2024-07-15T07:53:20.000Z"
    assert out.startswith("2024")


def test_to_iso_epoch_number_same_as_string():
    assert to_iso(1721030000) == "2024-07-15T07:53:20.000Z"
    assert to_iso("1721030000") == to_iso(1721030000)


def test_to_iso_iso_passthrough():
    assert to_iso("2024-07-15T00:00:00.000Z") == "2024-07-15T00:00:00.000Z"


def test_to_iso_millisecond_epoch_string():
    assert to_iso("1721030000000").startswith("2024")


def test_to_iso_garbage_empty():
    assert to_iso("not a date") == ""
    assert to_iso("abc123") == ""
    assert to_iso("") == ""
    assert to_iso(None) == ""


# --- normalize_url (snowball-input.test.js, P1.20) ---

def test_normalize_url_query_ignored():
    a = "https://www.tiktok.com/@user/video/1?is_from_webapp=1&sender_device=pc"
    b = "https://www.tiktok.com/@user/video/1"
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_fragment_ignored():
    assert normalize_url("https://www.tiktok.com/@user/video/1#hash") == \
        normalize_url("https://www.tiktok.com/@user/video/1")


def test_normalize_url_trailing_slash_ignored():
    assert normalize_url("https://www.tiktok.com/@user/video/1/") == \
        normalize_url("https://www.tiktok.com/@user/video/1")


def test_normalize_url_slash_plus_query():
    assert normalize_url("https://www.tiktok.com/@user/video/1/?is_from_webapp=1") == \
        normalize_url("https://www.tiktok.com/@user/video/1")


def test_normalize_url_not_over_normalized():
    assert normalize_url("https://www.tiktok.com/@user/video/1") != \
        normalize_url("https://www.tiktok.com/@user/video/2")
    assert normalize_url("https://www.tiktok.com/@alice/video/1") != \
        normalize_url("https://www.tiktok.com/@bob/video/1")


def test_normalize_url_bad_url_fallback_trimmed():
    assert normalize_url("   not a url  ") == "not a url"


def test_normalize_url_none_empty():
    assert normalize_url(None) == ""


# --- stable_hash: parity с JS hash() (raw_id должен совпадать с рядами n8n) ---

def test_stable_hash_js_parity():
    assert stable_hash("") == "0"
    assert stable_hash(None) == "0"
    assert stable_hash("abc") == "22ci"
    assert stable_hash("https://www.tiktok.com/@user/video/1подпись ролика") == "yid4z9"
    assert stable_hash("x" * 100) == "rsmuww"
    # не-BMP символ: JS charCodeAt работает по UTF-16 code units
    assert stable_hash("🔥эмодзи test") == "r4j3d0"
    assert stable_hash("https://instagram.com/reel/C8abc/?igsh=1") == "y576wo"


# --- first: JS-семантика (пропускает null/undefined/'', НЕ пропускает 0 и False) ---

def test_first_skips_none_and_empty():
    assert first(None, "", "a", "b") == "a"
    assert first() == ""
    assert first(None, None) == ""


def test_first_keeps_zero_and_false():
    assert first(0, 5) == 0
    assert first(False, "x") is False


# --- num: JS Number()-коэрция с NaN→0 ---

def test_num_coercion():
    assert num("12") == 12
    assert num("12.5") == 12.5
    assert num(" 12 ") == 12
    assert num("") == 0
    assert num(None) == 0
    assert num("abc") == 0
    assert num(7) == 7
    assert num(True) == 1
    assert num("12.0") == 12
    assert num({}) == 0


# --- is_apify_error: строгий детект (есть error И нет id/url) ---

def test_is_apify_error_marker():
    assert is_apify_error({"__apify_error": "boom"}) is True


def test_is_apify_error_error_without_id():
    assert is_apify_error({"error": "timeout"}) is True


def test_is_apify_error_error_with_id_is_data():
    assert is_apify_error({"error": "partial", "id": "v1"}) is False
    assert is_apify_error({"error": "partial", "url": "https://x"}) is False
    assert is_apify_error({"error": "x", "webVideoUrl": "https://x"}) is False


def test_is_apify_error_falsy_error_is_data():
    assert is_apify_error({"error": None}) is False
    assert is_apify_error({"error": False}) is False
    assert is_apify_error({"error": ""}) is False


def test_is_apify_error_non_dict():
    assert is_apify_error(None) is False
    assert is_apify_error([1]) is False
    assert is_apify_error("error") is False


# --- engagement_rate: JS-округление до 4 знаков, отрицательные лайки = скрытые ---

def test_engagement_rate_basic():
    # (100+20+10+5)/1000 = 0.135
    assert engagement_rate(100, 20, 10, 5, 1000) == 0.135


def test_engagement_rate_zero_views():
    assert engagement_rate(100, 20, 10, 5, 0) == 0


def test_engagement_rate_negative_likes_treated_as_zero():
    # скрытые лайки (likesRaw<0) не входят в числитель
    assert engagement_rate(-1, 30, 0, 0, 1000) == 0.03


def test_engagement_rate_js_rounding():
    # 1/3000 = 0.000333... -> Math.round(*10000)/10000 = 0.0003
    assert engagement_rate(1, 0, 0, 0, 3000) == 0.0003


# --- age_hours: инжектируемый now, JS-округление до 0.1, не отрицательный ---

def test_age_hours_rounding():
    now = datetime(2024, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert age_hours("2024-07-15T09:33:00.000Z", now=now) == 2.5
    assert age_hours("2024-07-15T12:00:00.000Z", now=now) == 0


def test_age_hours_future_clamped_to_zero():
    now = datetime(2024, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert age_hours("2024-07-16T00:00:00.000Z", now=now) == 0


def test_age_hours_invalid_empty():
    assert age_hours("") == ""
    assert age_hours("not a date") == ""
    assert age_hours(None) == ""
