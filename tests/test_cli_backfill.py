import argparse

from cf.cli import cmd_backfill_attribution

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def make_rows():
    return [
        {"raw_id": "t1", "source_query": "managed_tiktok_keywords",
         "raw_json": '{"searchHashtag": {"name": "menswear"}}'},
        {"raw_id": "t2", "source_query": "managed_tiktok_keywords",
         "raw_json": '{"searchQuery": "pov парень"}'},
        {"raw_id": "t3", "source_query": "hashtag:#уже-заполнено",
         "raw_json": '{"searchQuery": "не трогать"}'},
        {"raw_id": "t4", "source_query": "тег1,тег2,тег3", "raw_json": "{broken"},
    ]


def test_backfill_fills_placeholders_only(capsys):
    sheets = FakeSheets({"raw_tiktok": make_rows()})
    assert cmd_backfill_attribution(sheets, ns(tab="raw_tiktok", dry_run=False)) == 0
    rows = {r["raw_id"]: r["source_query"] for r in sheets.tables["raw_tiktok"]}
    assert rows["t1"] == "hashtag:#menswear"
    assert rows["t2"] == "query:pov парень"
    assert rows["t3"] == "hashtag:#уже-заполнено"
    assert rows["t4"] == "unknown"


def test_backfill_dry_run_changes_nothing(capsys):
    sheets = FakeSheets({"raw_tiktok": make_rows()})
    assert cmd_backfill_attribution(sheets, ns(tab="raw_tiktok", dry_run=True)) == 0
    assert sheets.tables["raw_tiktok"][0]["source_query"] == "managed_tiktok_keywords"
    assert "would update 3" in capsys.readouterr().out
