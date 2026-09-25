import argparse

from cf.cli import cmd_profile
from cf.io import read_json

from tests.fakes import FakeSheets, make_raw_row


def ns(**kw):
    return argparse.Namespace(**kw)


def test_cmd_profile_writes_report_and_logs_success(tmp_path):
    sheets = FakeSheets({"raw_tiktok": [make_raw_row(i) for i in range(25)]})
    rc = cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                min_rows=20, out_dir=str(tmp_path)))
    assert rc == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-profile.json")))
    assert report["ready_for_analysis"] is True
    assert report["meta"]["source_tab"] == "raw_tiktok"
    tab, row = sheets.appended[0]
    assert tab == "run_log" and row["agent"] == "raw-batch-profiler" and row["status"] == "success"


def test_cmd_profile_logs_insufficient_data(tmp_path):
    sheets = FakeSheets({"raw_tiktok": [make_raw_row(1)]})
    rc = cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                min_rows=20, out_dir=str(tmp_path)))
    assert rc == 0
    assert sheets.appended[0][1]["status"] == "insufficient_data"


def test_cmd_profile_with_niche_writes_separate_report(tmp_path):
    """profile --niche не должен перезаписывать общий отчёт вкладки."""
    rows = [make_raw_row(i) for i in range(25)]
    rows += [make_raw_row(100 + i, niche="мужские образы") for i in range(25)]
    sheets = FakeSheets({"raw_tiktok": rows})
    assert cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                  min_rows=20, out_dir=str(tmp_path))) == 0
    assert cmd_profile(sheets, ns(tab="raw_tiktok", niche="мужские образы", since=None,
                                  min_rows=20, out_dir=str(tmp_path))) == 0
    reports = sorted(p.name for p in tmp_path.glob("*-profile.json"))
    assert len(reports) == 2
    niche_report = next(p for p in tmp_path.glob("*мужские-образы-profile.json"))
    assert read_json(niche_report)["meta"]["niche"] == "мужские образы"
