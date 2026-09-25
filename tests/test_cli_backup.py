import argparse
import json
from datetime import date

from cf.cli import cmd_backup, cmd_restore

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def fixed_today(monkeypatch, d=date(2026, 7, 21)):
    monkeypatch.setattr("cf.cli.today", lambda: d)


# ── backup ────────────────────────────────────────────────────────────────────

def test_backup_writes_file_per_config_tab_with_counts(tmp_path, monkeypatch, capsys):
    fixed_today(monkeypatch)
    sheets = FakeSheets({
        "raw_tiktok": [{"raw_id": "t1", "views": 1000},
                       {"raw_id": "t2", "views": 2500}],
        "briefs": [{"brief_id": "b1", "hook": "хук", "script": "скрипт",
                    "caption": "капшн", "formula_id": "f1",
                    "review_status": "pending", "raw_json": '{"a": 1}'}],
    })
    assert cmd_backup(sheets, ns(out_dir=str(tmp_path))) == 0

    raw_file = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    briefs_file = tmp_path / "2026-07-21-briefs.jsonl"
    assert raw_file.exists() and briefs_file.exists()

    raw_lines = raw_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 2
    assert json.loads(raw_lines[0])["views"] == 1000  # числовая колонка сохранена как число

    briefs_lines = briefs_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(briefs_lines) == 1
    row = json.loads(briefs_lines[0])
    assert row["raw_json"] == '{"a": 1}'           # тяжёлая колонка не потеряна
    assert set(row) >= {"brief_id", "hook", "script", "caption", "raw_json"}

    out = capsys.readouterr().out
    assert "raw_tiktok" in out and "briefs" in out

    # операционное действие логируется в run_log
    assert any(t == "run_log" and r["agent"] == "backup" and r["status"] == "success"
               for t, r in sheets.appended)


def test_backup_prunes_old_files_keeps_recent_and_unrelated(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    old = tmp_path / "2026-06-01-raw_tiktok.jsonl"      # 50 дней -> удалить
    recent = tmp_path / "2026-07-15-raw_tiktok.jsonl"   # 6 дней  -> оставить
    unrelated = tmp_path / "notes.txt"                  # не бэкап -> оставить
    weird = tmp_path / "export.jsonl"                   # jsonl без даты -> оставить
    for f in (old, recent, unrelated, weird):
        f.write_text("x\n", encoding="utf-8")

    sheets = FakeSheets({"raw_tiktok": [{"raw_id": "t1"}]})
    assert cmd_backup(sheets, ns(out_dir=str(tmp_path))) == 0

    assert not old.exists()          # старый бэкап вычищен
    assert recent.exists()           # свежий бэкап цел
    assert unrelated.exists()        # посторонний файл не тронут
    assert weird.exists()            # jsonl без даты не тронут


def test_backup_keeps_file_exactly_30_days_old(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    edge = tmp_path / "2026-06-21-raw_tiktok.jsonl"  # ровно 30 дней -> оставить
    edge.write_text("x\n", encoding="utf-8")
    sheets = FakeSheets({"raw_tiktok": [{"raw_id": "t1"}]})
    assert cmd_backup(sheets, ns(out_dir=str(tmp_path))) == 0
    assert edge.exists()


def test_first_backup_after_long_pause_keeps_last_pre_pause_snapshot(tmp_path, monkeypatch):
    # Ревью 14.09.2026: завод стоял с 13.08, все снимки старше 30 дней. Первый же
    # `cf backup` после расконсервации записал бы сегодняшний снимок и стёр ВСЮ
    # допаузную историю. Ротация обязана оставить хотя бы последний прежний снимок.
    fixed_today(monkeypatch, date(2026, 9, 14))
    older = tmp_path / "2026-08-13-raw_tiktok.jsonl"
    last = tmp_path / "2026-08-14-raw_tiktok.jsonl"
    for f in (older, last):
        f.write_text("x\n", encoding="utf-8")
    sheets = FakeSheets({"raw_tiktok": [{"raw_id": "t1"}]})
    assert cmd_backup(sheets, ns(out_dir=str(tmp_path))) == 0
    assert (tmp_path / "2026-09-14-raw_tiktok.jsonl").exists()
    assert last.exists()             # последний допаузный снимок уцелел
    assert not older.exists()        # остальное старьё ротация чистит как прежде


# ── restore (roundtrip) ────────────────────────────────────────────────────────

def test_backup_restore_roundtrip_exact(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    original = [
        {"raw_id": "t1", "views": 1000, "niche": "pets",
         "raw_json": '{"a": 1, "текст": "длинный"}', "caption": "капшн"},
        {"raw_id": "t2", "views": 2500, "niche": "food",
         "raw_json": '{"b": 2}', "caption": ""},
    ]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in original]})
    assert cmd_backup(sheets, ns(out_dir=str(tmp_path))) == 0
    backup_file = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    assert backup_file.exists()

    # порча данных: перезапись целой колонки
    sheets.set_column_by_key("raw_tiktok", "raw_id", "niche",
                             {"t1": "СТЁРТО", "t2": "СТЁРТО"})
    assert sheets.read_rows("raw_tiktok")[0]["niche"] == "СТЁРТО"

    assert cmd_restore(sheets, ns(tab="raw_tiktok", file=str(backup_file),
                                  yes=True)) == 0
    # вкладка восстановлена точь-в-точь (включая числовую и тяжёлую колонки).
    # include_heavy: сверяем и raw_json, который дефолтная проекция чтения прячет.
    assert sheets.read_rows("raw_tiktok", include_heavy=True) == original


def test_restore_missing_file_returns_1_and_leaves_tab_intact(tmp_path):
    intact = [{"raw_id": "t1", "niche": "pets"}]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in intact]})
    rc = cmd_restore(sheets, ns(tab="raw_tiktok",
                                file=str(tmp_path / "nope.jsonl"), yes=True))
    assert rc == 1
    assert sheets.read_rows("raw_tiktok") == intact


def test_restore_empty_file_returns_1_and_leaves_tab_intact(tmp_path):
    empty = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    empty.write_text("", encoding="utf-8")
    intact = [{"raw_id": "t1", "niche": "pets"}]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in intact]})
    rc = cmd_restore(sheets, ns(tab="raw_tiktok", file=str(empty), yes=True))
    assert rc == 1
    assert sheets.read_rows("raw_tiktok") == intact


def test_restore_without_yes_aborts_and_leaves_tab_intact(tmp_path, capsys):
    backup = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    backup.write_text(json.dumps({"raw_id": "t1", "niche": "orig"},
                                 ensure_ascii=False) + "\n", encoding="utf-8")
    changed = [{"raw_id": "t1", "niche": "changed"}]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in changed]})
    rc = cmd_restore(sheets, ns(tab="raw_tiktok", file=str(backup), yes=False))
    assert rc == 1
    assert sheets.read_rows("raw_tiktok") == changed   # без подтверждения — без записи
    assert "--yes" in capsys.readouterr().out


# ── parser wiring ──────────────────────────────────────────────────────────────

def test_parser_wires_backup_and_restore():
    from cf.cli import build_parser, cmd_backup as cb, cmd_restore as cr
    a = build_parser().parse_args(["backup"])
    assert a.func is cb and a.out_dir == "agent-runtime/backups"
    b = build_parser().parse_args(
        ["restore", "--tab", "raw_tiktok", "--file", "x.jsonl", "--yes"])
    assert b.func is cr and b.tab == "raw_tiktok" and b.file == "x.jsonl" and b.yes is True
