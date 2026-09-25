import argparse
import json

from cf.cli import cmd_source_stats

from tests.fakes import FakeSheets


def test_source_stats_writes_report(tmp_path, capsys):
    sheets = FakeSheets({"raw_tiktok": [
        {"source_query": "hashtag:#a", "niche": "мужские-образы", "views": 5000,
         "transcript_text": "т", "caption": "", "source_url": "https://x/1",
         "posted_at": "2026-07-10"},
    ]})
    args = argparse.Namespace(tab="raw_tiktok", since=None, out_dir=str(tmp_path),
                              target=None)
    assert cmd_source_stats(sheets, args) == 0
    report_path = next(tmp_path.glob("*-raw_tiktok-source-stats.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sources"][0]["source"] == "hashtag:#a"
    assert "hashtag:#a" in capsys.readouterr().out
