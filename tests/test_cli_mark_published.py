import argparse

from cf.cli import cmd_mark_published
from cf.trace import build_trace

from tests.fakes import FakeSheets


def ns(**kw):
    # account="" — как у парсера: --account необязателен (обратная совместимость).
    base = {"notes": "", "account": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def _sheets():
    return FakeSheets({
        "briefs": [{"brief_id": "b-1", "prompt_version": "v3",
                    "source_pattern_ids": "p-1", "formula_id": "f-1",
                    "review_status": "approved"}],
        "reels": [],
        "run_log": [],
    })


def test_mark_published_writes_reel_and_logs():
    sheets = _sheets()
    rc = cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/7652315929052810516",
        notes="снято днём"))
    assert rc == 0
    reel = sheets.tables["reels"][0]
    assert reel["reel_id"] == "7652315929052810516"
    assert reel["brief_id"] == "b-1"
    assert reel["prompt_version"] == "v3"                # подтянут из брифа
    assert reel["production_notes"] == "снято днём"
    assert reel["published_at"]                          # штамп с временем проставлен
    assert sheets.tables["run_log"][0]["agent"] == "mark-published"


def test_mark_published_unknown_brief_errors():
    sheets = _sheets()
    rc = cmd_mark_published(sheets, ns(brief_id="nope",
                                       url="https://tiktok.com/@x/video/1"))
    assert rc == 1
    assert sheets.tables["reels"] == []


def test_mark_published_non_http_url_errors():
    sheets = _sheets()
    rc = cmd_mark_published(sheets, ns(brief_id="b-1", url="tiktok.com/@x/video/1"))
    assert rc == 1
    assert sheets.tables["reels"] == []


# --- --account: привязка к аккаунту из реестра (UTM-контур, тикет 01) --------

ACCOUNTS_CFG = {"accounts": [
    {"slug": "tiktok-1", "platform": "tiktok", "handle": "@x", "active": True},
    {"slug": "instagram-1", "platform": "instagram", "handle": "PLACEHOLDER",
     "active": False},
]}


def _sheets_with_accounts():
    return FakeSheets({
        "briefs": [{"brief_id": "b-1", "prompt_version": "v3",
                    "review_status": "approved"}],
        "reels": [], "run_log": [],
    }, config=ACCOUNTS_CFG)


def test_mark_published_with_account_writes_column():
    sheets = _sheets_with_accounts()
    rc = cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/1",
        account="tiktok-1"))
    assert rc == 0
    assert sheets.tables["reels"][0]["account"] == "tiktok-1"


def test_mark_published_unknown_account_errors_and_writes_nothing(capsys):
    sheets = _sheets_with_accounts()
    rc = cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/1", account="вася"))
    assert rc == 1
    assert sheets.tables["reels"] == []
    assert sheets.tables["run_log"] == []
    assert "вася" in capsys.readouterr().err


def test_mark_published_inactive_account_rejected():
    # active: false — выключатель слота: заготовка с хэндлом-заглушкой не должна
    # принимать публикации, пока владелец её не включил.
    sheets = _sheets_with_accounts()
    rc = cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/1",
        account="instagram-1"))
    assert rc == 1
    assert sheets.tables["reels"] == []


def test_mark_published_without_account_still_works():
    # Обратная совместимость: без --account публикация проходит, колонка пустая.
    sheets = _sheets_with_accounts()
    rc = cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/1"))
    assert rc == 0
    assert sheets.tables["reels"][0]["account"] == ""


def test_mark_published_on_live_headers_keeps_notes_and_chain():
    # Gap#3: на заголовках боевой вкладки (published_id/notes) без column_aliases
    # заметки и reel_id теряются молча. С алиасами из cf.config.json команда кладёт
    # их в фактические колонки, а trace по каноническим именам остаётся целым.
    sheets = FakeSheets(
        {"briefs": [{"brief_id": "b-1", "prompt_version": "v3",
                     "source_pattern_ids": "p-1", "formula_id": "f-1",
                     "review_status": "approved"}],
         "reels": [], "run_log": []},
        headers={"reels": ["published_id", "brief_id", "platform", "post_url",
                           "published_at", "creator", "content_owner",
                           "prompt_version", "status", "notes"]},
        config={"column_aliases": {"reels": {"reel_id": "published_id",
                                             "production_notes": "notes"}}})
    assert cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/555",
        notes="снято днём")) == 0
    stored = sheets.tables["reels"][0]
    assert stored["notes"] == "снято днём"        # production_notes -> notes
    assert stored["published_id"] == "555"        # reel_id -> published_id
    assert stored["prompt_version"] == "v3"
    trace = build_trace("b-1", sheets.read_rows("briefs"),
                        sheets.read_rows("reels"), sheets.read_rows("performance"))
    assert trace["reels"][0]["reel_id"] == "555"
    assert "published_reels" not in trace["missing_links"]


def test_trace_finds_chain_after_mark_published():
    # приёмка: после mark-published cf trace находит brief->reel (нет MISSING published_reels)
    sheets = _sheets()
    assert cmd_mark_published(sheets, ns(
        brief_id="b-1", url="https://www.tiktok.com/@x/video/555")) == 0
    trace = build_trace("b-1", sheets.read_rows("briefs"),
                        sheets.read_rows("reels"), sheets.read_rows("performance"))
    assert trace["found"] is True
    assert trace["reels"][0]["reel_id"] == "555"
    assert "published_reels" not in trace["missing_links"]
