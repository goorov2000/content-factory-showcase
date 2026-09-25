# C2.3 — снежок: build-инварианты snowball-input.test.js (e2e-часть) +
# e2e пайплайна с атрибуцией snowball:<seed> и coalesce на общем листе.
import json

from cf.collect.apify import RunResult
from cf.collect.snowball import build_snowball_batches, build_payload, collect

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


def seed(url, niche="beauty", active="TRUE"):
    return {"seed_url": url, "niche": niche, "active": active}


# JS: «build: два query-варианта одного ролика + другой ролик -> 2 actor-item»
def test_build_dedupes_query_variants():
    batches = build_snowball_batches([
        seed("https://www.tiktok.com/@user/video/1?is_from_webapp=1"),
        seed("https://www.tiktok.com/@user/video/1"),
        seed("https://www.tiktok.com/@user/video/2"),
    ], NOW)
    assert len(batches) == 2
    assert batches[0]["seed_url"] == "https://www.tiktok.com/@user/video/1?is_from_webapp=1"
    assert batches[1]["seed_url"] == "https://www.tiktok.com/@user/video/2"
    assert batches[0]["platform"] == "tiktok"
    assert batches[0]["seed_niche"] == "beauty"
    assert batches[0]["batch_size"] == 1
    assert batches[0]["batch_sources"] == [
        "snowball:https://www.tiktok.com/@user/video/1?is_from_webapp=1"]


# JS: «build: слэш-вариант тоже схлопывается, первый выигрывает»
def test_build_slash_variant_collapses():
    batches = build_snowball_batches([
        seed("https://www.tiktok.com/@chef/video/9", niche="food"),
        seed("https://www.tiktok.com/@chef/video/9/", niche="food"),
    ], NOW)
    assert len(batches) == 1
    assert batches[0]["seed_url"] == "https://www.tiktok.com/@chef/video/9"


# JS: «build: неактивные и не-tiktok строки отфильтровываются (регресс)»
def test_build_filters_inactive_and_non_tiktok():
    batches = build_snowball_batches([
        seed("https://www.tiktok.com/@user/video/1", active="FALSE"),
        seed("https://www.instagram.com/reel/abc"),
        seed("https://www.tiktok.com/@user/video/3"),
    ], NOW)
    assert len(batches) == 1
    assert batches[0]["seed_url"] == "https://www.tiktok.com/@user/video/3"


def test_payload_matches_n8n_node():
    # Имя — якорь парити-карты (test_collect_parity.py ссылается на него из
    # архивного JS-сьюта), сам payload от n8n-ноды уже отходит: снапшот 1:1,
    # чтобы дрейф полей был виден на ревью, а не на счёте Apify.
    # shouldDownloadSubtitles вендор удалил из схемы актора; субтитры просит
    # enum downloadSubtitlesOptions, DOWNLOAD_SUBTITLES бесплатен (сверка со
    # схемой билда latest, 10.08).
    assert build_payload({"seed_url": "https://www.tiktok.com/@u/video/5"}) == {
        "postURLs": ["https://www.tiktok.com/@u/video/5"],
        "scrapeRelatedVideos": True,
        "resultsPerPage": 15,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": True,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
        "shouldDownloadSlideshowImages": False,
        "proxyCountryCode": "None",
    }


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run_actor(self, actor_path, payload):
        self.calls.append((actor_path, payload))
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def raw_item(vid, **over):
    item = {"id": vid, "text": f"видео {vid}", "createTimeISO": FRESH,
            "playCount": 1000, "webVideoUrl": f"https://tt/{vid}",
            "transcript": "текст"}
    item.update(over)
    return item


def make_fake(seeds, raw_rows=()):
    return FakeSheets(tables={"seeds": seeds, "raw_tiktok": list(raw_rows),
                              "run_log": []})


def test_e2e_snowball_attribution():
    sheets = make_fake([seed("https://www.tiktok.com/@u/video/1")])
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    summary = collect(sheets, client, now_iso=NOW)
    assert summary["status"] == "success"
    row = sheets.tables["raw_tiktok"][0]
    assert row["source_query"] == "snowball:https://www.tiktok.com/@u/video/1"
    assert sheets.tables["run_log"][0]["agent"] == "collect-snowball"


def test_e2e_snowball_does_not_overwrite_existing_attribution():
    existing = {"raw_id": "tiktok_v1", "collected_at": "2026-07-01T00:00:00.000Z",
                "source_query": "hashtag:#мужскаяодежда",
                "transcript_text": "готовый", "views": 5}
    sheets = make_fake([seed("https://www.tiktok.com/@u/video/1")], [existing])
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1", transcript="")])])
    collect(sheets, client, now_iso=NOW)
    row = sheets.tables["raw_tiktok"][0]
    # P1.16: снежок не перетирает source_query/transcript первого сбора
    assert row["source_query"] == "hashtag:#мужскаяодежда"
    assert row["transcript_text"] == "готовый"
    assert row["views"] == 1000


def test_e2e_broken_seed_isolated():
    sheets = make_fake([seed("https://www.tiktok.com/@u/video/1"),
                        seed("https://www.tiktok.com/@u/video/2")])
    client = StubClient([RuntimeError("seed died"),
                         RunResult("SUCCEEDED", items=[raw_item("v2")])])
    summary = collect(sheets, client, now_iso=NOW)
    # Разбор 2026-07-27: тот же контракт, что у tiktok — потерянный батч не
    # рапортует «Успех», иначе деградация проходит мимо оператора.
    assert summary["status"] == "insufficient_data"
    assert summary["batches_failed"] == 1
    assert len(sheets.tables["raw_tiktok"]) == 1
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"


def test_e2e_broken_seed_reports_lost_source_and_reason():
    # Разбор 2026-07-27: сводка снежка не печатала даже sources_lost (у tiktok
    # он был), а errors несли одни имена seed'ов без причины отказа Apify.
    sheets = make_fake([seed("https://www.tiktok.com/@u/video/1"),
                        seed("https://www.tiktok.com/@u/video/2")])
    client = StubClient([RuntimeError("apify run FAILED (monthly usage hard limit)"),
                         RunResult("SUCCEEDED", items=[raw_item("v2")])])
    summary = collect(sheets, client, now_iso=NOW)
    log = sheets.tables["run_log"][0]
    assert "sources_lost=1" in log["input_summary"]
    assert "monthly usage hard limit" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert "snowball:https://www.tiktok.com/@u/video/1" in errors
    assert any("monthly usage hard limit" in e for e in errors)
    assert "monthly usage hard limit" in summary["error"]
    # Имя потерянного seed'а — в сводку, а не только его число: по нему
    # cli печатает «источников не опрошено» (ревью 2026-07-27).
    assert summary["lost_sources"] == \
        ["snowball:https://www.tiktok.com/@u/video/1"]


def test_e2e_total_failure_logs_seed_and_reason():
    # Полный провал снежка: raise минует _result, имена seed'ов и причина
    # доезжают до Run Log только через атрибуты CollectGateError.
    sheets = make_fake([seed("https://www.tiktok.com/@u/video/1")])
    client = StubClient([RuntimeError("apify poll timeout after 900s")])
    summary = collect(sheets, client, now_iso=NOW)
    assert summary["status"] == "failed"
    log = sheets.tables["run_log"][0]
    assert "apify poll timeout after 900s" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert "snowball:https://www.tiktok.com/@u/video/1" in errors


def test_e2e_no_active_seeds_insufficient():
    sheets = make_fake([seed("https://www.tiktok.com/@u/video/1", active="FALSE")])
    summary = collect(sheets, StubClient([]), now_iso=NOW)
    assert summary["status"] == "insufficient_data"
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"
