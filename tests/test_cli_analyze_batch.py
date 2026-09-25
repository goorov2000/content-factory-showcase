import argparse

from cf.cli import cmd_analyze_batch
from cf.io import read_json

from tests.fakes import FakeSheets, make_raw_row


def ns(**kw):
    return argparse.Namespace(**kw)


def test_cmd_analyze_batch_writes_report_and_logs_success(tmp_path):
    rows = [make_raw_row(i, niche="стритвир", views=2000, likes=i * 10,
                         transcript_text="т", collected_at="2026-07-01")
            for i in range(16)]
    sheets = FakeSheets({"raw_tiktok": rows})
    rc = cmd_analyze_batch(sheets, ns(tab="raw_tiktok", niche="стритвир", since=None,
                                      min_rows=12, min_views=1000, out_dir=str(tmp_path)))
    assert rc == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-стритвир-analysis.json")))
    assert report["status"] == "ok"
    assert len(report["winners"]) == 4
    assert report["meta"]["source_tab"] == "raw_tiktok"
    assert report["meta"]["niche"] == "стритвир"
    tab, row = sheets.appended[0]
    assert tab == "run_log" and row["agent"] == "batch-analyzer" and row["status"] == "success"


def test_cmd_analyze_batch_logs_insufficient_data(tmp_path):
    rows = [make_raw_row(i, niche="стритвир", views=1) for i in range(3)]
    sheets = FakeSheets({"raw_tiktok": rows})
    rc = cmd_analyze_batch(sheets, ns(tab="raw_tiktok", niche="стритвир", since=None,
                                      min_rows=12, min_views=1000, out_dir=str(tmp_path)))
    assert rc == 0
    report = read_json(next(tmp_path.glob("*-analysis.json")))
    assert report["status"] == "insufficient_data"
    tab, row = sheets.appended[0]
    assert tab == "run_log" and row["status"] == "insufficient_data"
