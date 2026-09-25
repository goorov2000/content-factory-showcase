import argparse
import json

from cf.cli import cmd_export_seeds

from tests.fakes import FakeSheets


def test_export_seeds_appends_new_urls_only(tmp_path, capsys):
    formula = {"name": "f1", "niche": "мужские-образы",
               "evidence": {"source_urls": [
                   "https://www.tiktok.com/@a/video/1",
                   "https://www.instagram.com/reel/X/",   # не tiktok — пропустить
                   "https://www.tiktok.com/@b/video/2",
               ]}}
    fpath = tmp_path / "f1.json"
    fpath.write_text(json.dumps(formula, ensure_ascii=False), encoding="utf-8")
    index = {"approved": [{"name": "f1", "niche": "мужские-образы",
                           "path": str(fpath).replace("\\", "/"), "version": 1}]}
    ipath = tmp_path / "index.json"
    ipath.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    sheets = FakeSheets({"seeds": [
        {"seed_url": "https://www.tiktok.com/@a/video/1", "active": "TRUE"},
    ]}, headers={"seeds": ["seed_url", "seed_type", "niche", "added_at", "active"]})
    args = argparse.Namespace(index=str(ipath))
    assert cmd_export_seeds(sheets, args) == 0
    urls = [r["seed_url"] for r in sheets.tables["seeds"]]
    assert urls == ["https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@b/video/2"]
    assert sheets.tables["seeds"][1]["niche"] == "мужские-образы"


def test_export_seeds_creates_tab_when_missing(tmp_path):
    ipath = tmp_path / "index.json"
    ipath.write_text('{"approved": []}', encoding="utf-8")
    sheets = FakeSheets({})
    assert cmd_export_seeds(sheets, argparse.Namespace(index=str(ipath))) == 0
    assert "seeds" in sheets.tables


def _write_index(tmp_path, source_urls, niche="pets"):
    formula = {"name": "f1", "niche": niche, "evidence": {"source_urls": source_urls}}
    fpath = tmp_path / "f1.json"
    fpath.write_text(json.dumps(formula, ensure_ascii=False), encoding="utf-8")
    index = {"approved": [{"name": "f1", "niche": niche,
                           "path": str(fpath).replace("\\", "/"), "version": 1}]}
    ipath = tmp_path / "index.json"
    ipath.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return ipath


def _seeds_sheets(rows):
    return FakeSheets(
        {"seeds": rows},
        headers={"seeds": ["seed_url", "seed_type", "niche", "added_at", "active"]})


def test_export_seeds_single_batch_append_for_many_urls(tmp_path, capsys):
    # 30 winner-URL: раньше цикл append_row = 30 записей (по 3 HTTP -> квота -> 429).
    # Теперь всё уходит ОДНИМ batch-append.
    urls = [f"https://www.tiktok.com/@acc/video/{i}" for i in range(30)]
    ipath = _write_index(tmp_path, urls)
    sheets = _seeds_sheets([])
    assert cmd_export_seeds(sheets, argparse.Namespace(index=str(ipath))) == 0
    assert sheets.append_rows_calls == 1   # ровно один батч
    assert sheets.append_row_calls == 0    # ни одного одиночного append_row
    seeds = sheets.tables["seeds"]
    assert [r["seed_url"] for r in seeds] == urls
    assert all(r["seed_type"] == "winner" and r["active"] == "TRUE"
               and r["niche"] == "pets" for r in seeds)
    assert "добавлено 30" in capsys.readouterr().out


def test_export_seeds_dedup_only_new_in_batch(tmp_path):
    # Уже присутствующий seed_url и дубль внутри самого батча не попадают в append.
    ipath = _write_index(tmp_path, [
        "https://www.tiktok.com/@a/video/1",   # уже в листе
        "https://www.tiktok.com/@b/video/2",   # новый
        "https://www.tiktok.com/@b/video/2",   # дубль внутри батча
    ])
    sheets = _seeds_sheets([
        {"seed_url": "https://www.tiktok.com/@a/video/1", "active": "TRUE"},
    ])
    assert cmd_export_seeds(sheets, argparse.Namespace(index=str(ipath))) == 0
    assert sheets.append_rows_calls == 1
    assert sheets.append_row_calls == 0
    urls = [r["seed_url"] for r in sheets.tables["seeds"]]
    assert urls == ["https://www.tiktok.com/@a/video/1",
                    "https://www.tiktok.com/@b/video/2"]


def test_export_seeds_zero_new_no_append(tmp_path, capsys):
    # Все URL уже есть -> append не вызывается вовсе (no-op), сводка "добавлено 0".
    ipath = _write_index(tmp_path, ["https://www.tiktok.com/@a/video/1"])
    sheets = _seeds_sheets([
        {"seed_url": "https://www.tiktok.com/@a/video/1", "active": "TRUE"},
    ])
    assert cmd_export_seeds(sheets, argparse.Namespace(index=str(ipath))) == 0
    assert sheets.append_rows_calls == 0
    assert sheets.append_row_calls == 0
    assert sheets.appended == []
    assert "добавлено 0, всего 1" in capsys.readouterr().out
