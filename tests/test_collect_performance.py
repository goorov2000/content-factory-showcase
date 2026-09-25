# C2.4 — порт cf04-performance: Build-Apify-Metric-Requests.js +
# Normalize-Performance-Rows.js. Контракт спеки §2: performance-строка несёт
# brief_id и measured_at; join по post_url из CF Published Reels.
from cf.collect.apify import RunResult
from cf.collect.performance import (
    build_metric_requests,
    collect,
    normalize_performance_row,
    perf_normalize_url,
)
from cf.collect.util import stable_hash

from tests.fakes import FakeSheets

MEASURED = "2026-07-23T12:34:56.000Z"


def reel_row(**over):
    row = {
        "status": "published",
        "post_url": "https://www.tiktok.com/@acc/video/1?is_from_webapp=1",
        "platform": "tiktok",
        "brief_id": "brief-1",
        "prompt_version": "brief-мужские-образы-reel v2",
        "published_id": "pub_t_1",
        "published_at": "2026-07-20T10:00:00.000Z",
        "creator": "оператор",
        "content_owner": "cf",
    }
    row.update(over)
    return row


# --- perf_normalize_url (свой normalizeUrl cf04 — не snowball-версия) ---

def test_perf_normalize_url_instagram_canonical():
    for form in ("https://instagram.com/p/C8abc/?igsh=1",
                 "https://www.instagram.com/reel/C8abc",
                 "https://instagram.com/reels/C8abc#x"):
        assert perf_normalize_url(form) == "https://www.instagram.com/reel/C8abc/"


def test_perf_normalize_url_strips_query():
    assert perf_normalize_url("https://www.tiktok.com/@a/video/1?x=1") == \
        "https://www.tiktok.com/@a/video/1"
    assert perf_normalize_url("") == ""


# --- build_metric_requests ---

def test_build_requests_tiktok_payload():
    reqs = build_metric_requests([reel_row()])
    assert len(reqs) == 1
    r = reqs[0]
    assert r["source_actor"] == "clockworks/tiktok-scraper"
    assert r["actor_path"] == "clockworks~tiktok-scraper"
    assert r["payload"]["postURLs"] == ["https://www.tiktok.com/@acc/video/1"]
    assert r["payload"]["resultsPerPage"] == 1
    # shouldDownloadSubtitles вендор удалил из схемы — мёртвое поле не шлётся
    # (ревью 2026-08-10, спека Out of Scope: «проверка на мёртвые поля — да»)
    assert "shouldDownloadSubtitles" not in r["payload"]
    assert r["post_url"] == "https://www.tiktok.com/@acc/video/1"
    assert r["published_at"] == "2026-07-20T10:00:00.000Z"


def test_build_requests_instagram_payload():
    reqs = build_metric_requests([reel_row(
        platform="instagram", post_url="https://instagram.com/reel/C8abc/?igsh=1")])
    r = reqs[0]
    assert r["source_actor"] == "apify/instagram-reel-scraper"
    assert r["actor_path"] == "apify~instagram-reel-scraper"
    # контракт reel-scraper: reel-URL в поле username (странность актора, JS 1:1)
    assert r["payload"]["username"] == ["https://www.instagram.com/reel/C8abc/"]
    assert r["payload"]["resultsLimit"] == 1


def test_build_requests_filters_incomplete():
    rows = [
        reel_row(status="draft"),
        reel_row(brief_id=""),
        reel_row(prompt_version=""),
        reel_row(post_url=""),
        reel_row(platform="", post_url="https://example.com/x"),  # платформа не определяется
        reel_row(),
    ]
    reqs = build_metric_requests(rows)
    assert len(reqs) == 1


def test_build_requests_platform_from_url():
    r = build_metric_requests([reel_row(platform="")])[0]
    assert r["platform"] == "tiktok"  # выведена из tiktok.com в URL


def test_build_requests_generates_published_id():
    row = reel_row(published_id="")
    r = build_metric_requests([row])[0]
    expected = "pub_tiktok_" + stable_hash("brief-1" + "https://www.tiktok.com/@acc/video/1")
    assert r["published_id"] == expected


# --- normalize_performance_row ---

def _request(**over):
    base = build_metric_requests([reel_row(**over)])[0]
    return base


def test_normalize_matches_item_by_url():
    items = [
        {"url": "https://www.tiktok.com/@a/video/999", "playCount": 1},
        {"url": "https://www.tiktok.com/@acc/video/1?share=1", "playCount": 42,
         "diggCount": 10, "commentCount": 5, "shareCount": 2, "collectCount": 1},
    ]
    row = normalize_performance_row(_request(), items, MEASURED)
    assert row["views"] == 42
    assert row["likes"] == 10
    # (10+5+2+1)/42 -> JS-округление до 4 знаков
    assert row["engagement_rate"] == 0.4286
    assert row["measured_at"] == MEASURED
    assert row["brief_id"] == "brief-1"
    assert row["result_label"] == "unknown"
    assert "collection_status=metrics_collected" in row["eval_notes"]


def test_normalize_fallback_first_item():
    items = [{"url": "https://other/x", "playCount": 7}]
    row = normalize_performance_row(_request(), items, MEASURED)
    assert row["views"] == 7


def test_normalize_empty_response_marks_unavailable():
    row = normalize_performance_row(_request(), [], MEASURED)
    assert row["views"] == 0
    assert "collection_status=metrics_empty_or_unavailable" in row["eval_notes"]


def test_normalize_hours_and_views_per_hour():
    # 2026-07-20T10:00 -> 2026-07-23T12:34:56 = 74.6 ч; 746 просмотров -> 10.0/ч
    items = [{"url": "https://www.tiktok.com/@acc/video/1", "playCount": 746}]
    row = normalize_performance_row(_request(), items, MEASURED)
    assert row["hours_since_publish"] == 74.6
    assert row["views_per_hour"] == 10.0


def test_normalize_performance_id_format():
    row = normalize_performance_row(_request(), [], MEASURED)
    # publishedId_YYYYMMDDHHMM_hash(url)
    assert row["performance_id"].startswith("pub_t_1_202607231234_")


# --- collect e2e на фейках ---

def make_fake(rows):
    return FakeSheets(tables={"reels": rows, "performance": [], "run_log": []},
                      headers={"performance": [
                          "performance_id", "published_id", "brief_id", "prompt_version",
                          "platform", "measured_at", "hours_since_publish", "views",
                          "likes", "comments", "shares", "saves", "engagement_rate",
                          "views_per_hour", "result_label", "result_reason", "eval_notes"],
                          "run_log": ["run_id", "agent", "trigger_type", "started_at",
                                      "completed_at", "status", "input_summary",
                                      "output_paths", "errors"]})


class StubClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def run_actor(self, actor_path, payload):
        self.calls.append((actor_path, payload))
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_collect_e2e_writes_performance_and_runlog():
    sheets = make_fake([reel_row(), reel_row(brief_id="")])
    client = StubClient([RunResult(status="SUCCEEDED", items=[
        {"url": "https://www.tiktok.com/@acc/video/1", "playCount": 100,
         "diggCount": 10, "commentCount": 0, "shareCount": 0, "collectCount": 0}])])
    summary = collect(sheets, client, now_iso=MEASURED)
    assert summary["status"] == "success"
    assert len(client.calls) == 1  # неполная строка не поехала в Apify
    perf = sheets.tables["performance"]
    assert len(perf) == 1
    assert perf[0]["brief_id"] == "brief-1"
    assert perf[0]["views"] == 100
    runlog = sheets.tables["run_log"]
    assert len(runlog) == 1
    assert runlog[0]["agent"] == "collect-performance"
    assert runlog[0]["status"] == "success"


def test_collect_failed_run_becomes_empty_metrics():
    # ЧАСТИЧНЫЙ сбой рана Apify -> строка с metrics_empty_or_unavailable (как
    # error-item у HTTP-ноды с onError:continue), потеря видна в summary.
    # Полный отказ всех ранов — отдельная семантика failed (M27, тест ниже).
    sheets = make_fake([
        reel_row(),
        reel_row(published_id="pub_t_2",
                 post_url="https://www.tiktok.com/@acc/video/2"),
    ])
    client = StubClient([
        RuntimeError("actor died"),
        RunResult(status="SUCCEEDED", items=[
            {"url": "https://www.tiktok.com/@acc/video/2", "playCount": 50,
             "diggCount": 1, "commentCount": 0, "shareCount": 0,
             "collectCount": 0}]),
    ])
    summary = collect(sheets, client, now_iso=MEASURED)
    assert summary["status"] == "success"
    assert summary["runs_failed"] == 1
    perf = sheets.tables["performance"]
    assert len(perf) == 2
    failed = next(r for r in perf if r["views"] == 0)
    assert "metrics_empty_or_unavailable" in failed["eval_notes"]


def test_collect_all_runs_failed_logs_failed_no_rows():
    # M27 (аудит 2026-07-24): полный отказ Apify — failed (алерт оператору через
    # cmd_collect), нулевые строки НЕ пишутся в CF Performance.
    sheets = make_fake([reel_row()])
    client = StubClient([RuntimeError("token expired")])
    summary = collect(sheets, client, now_iso=MEASURED)
    assert summary["status"] == "failed"
    assert summary["runs_failed"] == 1
    assert sheets.tables["performance"] == []
    runlog = sheets.tables["run_log"]
    assert len(runlog) == 1 and runlog[0]["status"] == "failed"


def test_collect_no_published_rows_insufficient_data():
    sheets = make_fake([reel_row(status="draft")])
    client = StubClient([])
    summary = collect(sheets, client, now_iso=MEASURED)
    assert summary["status"] == "insufficient_data"
    assert client.calls == []
    assert sheets.tables["performance"] == []
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"


def test_collect_dry_run_no_sheet_writes():
    sheets = make_fake([reel_row()])
    client = StubClient([RunResult(status="SUCCEEDED", items=[])])
    summary = collect(sheets, client, now_iso=MEASURED, dry_run=True)
    assert sheets.tables["performance"] == []
    assert summary["rows"]  # строки возвращены для JSONL
    # dry-run логируется с честным trigger_type
    assert sheets.tables["run_log"][0]["trigger_type"] == "dry-run"


def test_normalize_honest_zero_is_metrics_zero():
    # Ревью аудита: честный ноль (актор вернул item с 0 просмотров) — валидный
    # замер, а не «unavailable»: eval не должен выбрасывать его из окна.
    items = [{"url": "https://www.tiktok.com/@acc/video/1", "playCount": 0}]
    row = normalize_performance_row(_request(), items, MEASURED)
    assert "collection_status=metrics_zero" in row["eval_notes"]


def test_normalize_run_failed_always_marks_unavailable():
    row = normalize_performance_row(_request(), [], MEASURED, run_failed=True)
    assert "collection_status=metrics_empty_or_unavailable" in row["eval_notes"]


def test_dry_run_limit_caps_paid_runs():
    # Ревью 14.09.2026: cli вычислял limit «1 батч», но в performance.collect его не
    # передавал — dry-run делал столько же платных ранов Apify, сколько боевой прогон.
    sheets = make_fake([reel_row(published_id=f"pub_t_{i}",
                                 post_url=f"https://www.tiktok.com/@acc/video/{i}")
                        for i in range(1, 6)])
    client = StubClient([RunResult(status="SUCCEEDED", items=[]) for _ in range(5)])
    collect(sheets, client, now_iso=MEASURED, dry_run=True, limit=1)
    assert len(client.calls) == 1
