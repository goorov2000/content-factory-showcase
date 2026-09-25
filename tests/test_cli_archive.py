import argparse
import json
from datetime import date

from cf.cli import cmd_archive, cmd_restore

from tests.fakes import FakeSheets


def ns(out_dir, older_than="45d", yes=False):
    return argparse.Namespace(out_dir=out_dir, older_than=older_than, yes=yes)


def fixed_today(monkeypatch, d=date(2026, 7, 21)):
    # cutoff по умолчанию (45д) = 2026-06-06: строки строго старше него архивируются.
    monkeypatch.setattr("cf.cli.today", lambda: d)


def raw(raw_id, collected_at, **over):
    row = {"raw_id": raw_id, "collected_at": collected_at, "views": 1000,
           "niche": "pets", "raw_json": '{"a": 1}'}
    row.update(over)
    return row


def log(run_id, completed_at, agent="backup", status="success"):
    return {"run_id": run_id, "agent": agent, "trigger_type": "scheduled",
            "started_at": completed_at, "completed_at": completed_at,
            "status": status, "input_summary": "x",
            "output_paths": "[]", "errors": "[]"}


# ── partition ───────────────────────────────────────────────────────────────────

def test_archive_partitions_old_rows_keeps_new_and_undated(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    old = raw("t1", "2026-05-01T10:00:00.000Z", views=1000, niche="a",
              raw_json='{"x": 1, "текст": "старьё"}')
    new = raw("t2", "2026-07-20T08:00:00.000Z", views=2500, niche="b",
              raw_json='{"y": 2}')
    undated = raw("t3", "", views=900, niche="", raw_json='{"z": 3}')
    sheets = FakeSheets({"raw_tiktok": [dict(old), dict(new), dict(undated)]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    # вкладка: ровно оставленные строки (новая + недатированная), в исходном порядке
    assert sheets.read_rows("raw_tiktok", include_heavy=True) == [new, undated]

    # архив: ровно старая строка, байт-в-байт включая тяжёлый raw_json
    arch = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    lines = arch.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == old


def test_archive_edge_exactly_cutoff_kept(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    # 2026-06-06 == cutoff -> НЕ строго старше -> остаётся (как ровно-30-дней в backup)
    edge = raw("t1", "2026-06-06")
    older = raw("t2", "2026-06-05")
    sheets = FakeSheets({"raw_tiktok": [dict(edge), dict(older)]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0
    kept = sheets.read_rows("raw_tiktok", include_heavy=True)
    assert [r["raw_id"] for r in kept] == ["t1"]
    arch = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    assert [json.loads(l)["raw_id"]
            for l in arch.read_text(encoding="utf-8").strip().splitlines()] == ["t2"]


# ── run_log self-archiving ──────────────────────────────────────────────────────

def test_archive_run_log_own_log_row_survives(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    old = log("r1", "2026-05-01T00:00:00+00:00")      # старше cutoff -> в архив
    recent = log("r2", "2026-07-20T00:00:00+00:00")   # свежий -> остаётся
    sheets = FakeSheets({"run_log": [dict(old), dict(recent)]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    logs = sheets.read_rows("run_log")
    # старая run_log-строка удалена, свежая осталась
    assert not any(r["run_id"] == "r1" for r in logs)
    assert any(r["run_id"] == "r2" for r in logs)
    # собственная строка прогона archive пережила перезапись run_log
    assert any(r["agent"] == "archive" and r["status"] == "success" for r in logs)

    # архив run_log содержит СТАРУЮ строку, но НЕ собственную строку archive-прогона
    arch = tmp_path / "2026-07-21-run_log.jsonl"
    lines = [json.loads(l) for l in arch.read_text(encoding="utf-8").strip().splitlines()]
    assert [r["run_id"] for r in lines] == ["r1"]
    assert all(r.get("agent") != "archive" for r in lines)


# ── dry-run (без --yes) ─────────────────────────────────────────────────────────

def test_archive_dry_run_without_yes_deletes_nothing(tmp_path, monkeypatch, capsys):
    fixed_today(monkeypatch)
    original = [raw("t1", "2026-05-01"), raw("t2", "2026-07-20")]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in original]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=False)) == 1

    # ничего не удалено, ни один файл не создан, run_log не тронут
    assert sheets.read_rows("raw_tiktok", include_heavy=True) == original
    assert not any(tmp_path.iterdir())
    assert not any(t == "run_log" for t, _ in sheets.appended)
    out = capsys.readouterr().out
    assert "raw_tiktok" in out and "--yes" in out


# ── повторный прогон в тот же день дописывает файл (не затирает) ──────────────────

def test_archive_same_day_rerun_appends_not_clobbers(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    sheets = FakeSheets({"raw_tiktok": [raw("t1", "2026-05-01"), raw("t2", "2026-07-20")]})
    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0
    arch = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    assert len(arch.read_text(encoding="utf-8").strip().splitlines()) == 1

    # позже в тот же день появилась ещё одна старая строка -> второй прогон ДОПИСЫВАЕТ
    sheets.append_row("raw_tiktok", raw("t3", "2026-04-01"))
    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    lines = arch.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # строка первого прогона не затёрта
    assert {json.loads(l)["raw_id"] for l in lines} == {"t1", "t3"}


# ── нечего архивировать -> no-op ─────────────────────────────────────────────────

def test_archive_nothing_old_is_noop(tmp_path, monkeypatch, capsys):
    fixed_today(monkeypatch)
    original = [raw("t1", "2026-07-19"), raw("t2", "2026-07-20")]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in original]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    assert sheets.read_rows("raw_tiktok", include_heavy=True) == original
    assert not any(tmp_path.iterdir())                     # архивных файлов нет
    assert not any(t == "run_log" for t, _ in sheets.appended)  # no-op не логируется
    assert "нечего архивировать" in capsys.readouterr().out.lower()


# ── недатированная / нечитаемая дата никогда не архивируется ──────────────────────

def test_archive_never_archives_undated_or_unparseable(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    original = [raw("t1", ""), raw("t2", "неизвестно"), raw("t3", "2026-05-01")]
    sheets = FakeSheets({"raw_tiktok": [dict(r) for r in original]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    kept = sheets.read_rows("raw_tiktok", include_heavy=True)
    assert [r["raw_id"] for r in kept] == ["t1", "t2"]   # пустая + мусорная дата остались
    arch = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    assert [json.loads(l)["raw_id"]
            for l in arch.read_text(encoding="utf-8").strip().splitlines()] == ["t3"]


# ── UTM-контур (тикет 06): дневные строки ротируются, месячные и заказы — нет ────

def utm(utm_id, kind, date_val, **over):
    row = {"utm_id": utm_id, "row_kind": kind, "date": date_val,
           "month": date_val[:7] if date_val else "2026-05", "account": "acc-1",
           "utm_campaign": "acc-1", "utm_content": "", "reel_id": "",
           "visits": 10, "users": 8, "collected_at": "2026-05-02T08:05:00+00:00"}
    row.update(over)
    return row


def test_archive_utm_daily_rotates_monthly_billing_stays_forever(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    old_daily = utm("d-2026-05-01-acc-1--", "daily", "2026-05-01")
    new_daily = utm("d-2026-07-20-acc-1--", "daily", "2026-07-20",
                    collected_at="2026-07-20T08:05:00+00:00")
    # месячная строка СТАРОГО сбора: date пустой всегда — партиция идёт по date,
    # а не по collected_at, иначе биллинговая цифра уехала бы в JSONL
    monthly = utm("m-2026-05-acc-1", "monthly", "", users=250)
    sheets = FakeSheets({"utm_traffic": [dict(old_daily), dict(new_daily), dict(monthly)]})

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    # в листе — свежая дневная и месячная (навсегда), в исходном порядке
    assert sheets.read_rows("utm_traffic", include_heavy=True) == [new_daily, monthly]
    arch = tmp_path / "2026-07-21-utm_traffic.jsonl"
    lines = [json.loads(l) for l in arch.read_text(encoding="utf-8").strip().splitlines()]
    assert lines == [old_daily]


def test_archive_orders_tab_is_never_rotated(tmp_path, monkeypatch):
    from cf.cli import ARCHIVE_TABS

    fixed_today(monkeypatch)
    # CF Orders — источник истины по деньгам: не в ARCHIVE_TABS вообще
    assert "orders" not in dict(ARCHIVE_TABS)
    ancient = {"order_id": "mk-1", "source": "metrika_ecommerce",
               "order_date": "2025-01-01", "revenue": 4990, "status": "confirmed",
               "collected_at": "2025-01-02T00:00:00+00:00"}
    sheets = FakeSheets({"orders": [dict(ancient)],
                         "utm_traffic": [utm("d-old", "daily", "2026-05-01")]})

    # прогон боевой (utm-строка ушла в архив), но заказы не тронуты и файла нет
    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0
    assert sheets.read_rows("orders", include_heavy=True) == [ancient]
    assert not (tmp_path / "2026-07-21-orders.jsonl").exists()
    assert (tmp_path / "2026-07-21-utm_traffic.jsonl").exists()


# ── roundtrip: архивные строки восстанавливаются через cf restore ────────────────

def test_archive_rows_restorable_via_restore(tmp_path, monkeypatch):
    fixed_today(monkeypatch)
    old = raw("t1", "2026-05-01", views=1000, niche="pets",
              raw_json='{"текст": "живой"}')
    new = raw("t2", "2026-07-20", views=2000, niche="food", raw_json='{}')
    sheets = FakeSheets({"raw_tiktok": [dict(old), dict(new)]})
    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0
    arch = tmp_path / "2026-07-21-raw_tiktok.jsonl"

    # архивный JSONL — того же формата, что бэкап: cf restore заливает его обратно
    target = FakeSheets({"raw_tiktok": [raw("z", "", views=0, niche="", raw_json="")]})
    assert cmd_restore(target, argparse.Namespace(
        tab="raw_tiktok", file=str(arch), yes=True)) == 0
    assert target.read_rows("raw_tiktok", include_heavy=True) == [old]


# ── parser wiring ────────────────────────────────────────────────────────────────

def test_parser_wires_archive():
    from cf.cli import build_parser, cmd_archive as ca
    a = build_parser().parse_args(["archive"])
    assert a.func is ca
    assert a.older_than == "45d"
    assert a.out_dir == "agent-runtime/archive"
    assert a.yes is False
    b = build_parser().parse_args(
        ["archive", "--older-than", "30", "--out-dir", "x", "--yes"])
    assert b.older_than == "30" and b.out_dir == "x" and b.yes is True


# ── Аудит 2026-07-24 (H8/M39): локи и пере-чтение перед replace ───────────────

def test_archive_skipped_when_pipeline_busy(tmp_path, monkeypatch):
    from cf import pipeline_lock
    from cf.lock import ProcessLock, stage_lock_path

    fixed_today(monkeypatch)
    old = raw("t1", "2026-05-01T10:00:00.000Z")
    sheets = FakeSheets({"raw_tiktok": [dict(old)], "run_log": []})
    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "raw"))
    assert held.acquire()
    try:
        assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0
        # вкладка не тронута, архив не создан, след skipped оставлен
        assert sheets.read_rows("raw_tiktok", include_heavy=True) == [old]
        assert not list(tmp_path.glob("*.jsonl"))
        assert sheets.tables["run_log"][0]["status"] == "skipped"
    finally:
        held.release()


def test_archive_rereads_tab_before_replace(tmp_path, monkeypatch):
    # M39: строка, дописанная другим процессом после планирования, не должна
    # молча исчезнуть при replace_rows. Эмулируем дозапись через подмену
    # read_rows: второе чтение вкладки приносит свежую строку.
    fixed_today(monkeypatch)
    old = raw("t1", "2026-05-01T10:00:00.000Z")
    fresh = raw("t2", "2026-07-20T08:00:00.000Z")
    sheets = FakeSheets({"raw_tiktok": [dict(old)], "run_log": []})

    reads = {"n": 0}
    real_read = sheets.read_rows

    def read_rows(tab_key, include_heavy=False):
        if tab_key == "raw_tiktok":
            reads["n"] += 1
            if reads["n"] == 2 and fresh not in sheets.tables["raw_tiktok"]:
                sheets.tables["raw_tiktok"].append(dict(fresh))  # «параллельная» дозапись
        return real_read(tab_key, include_heavy=include_heavy)

    monkeypatch.setattr(sheets, "read_rows", read_rows)

    assert cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True)) == 0

    kept = sheets.tables["raw_tiktok"]
    assert fresh in kept                     # свежая строка пережила replace
    assert old not in kept                   # старая ушла в архив
    arch = tmp_path / "2026-07-21-raw_tiktok.jsonl"
    assert json.loads(arch.read_text(encoding="utf-8").strip()) == old


# ── M14 (аудит 2026-07-24): restore сверяет вкладку из имени снимка ───────────

def _restore_ns(tab, file, force=False):
    return argparse.Namespace(tab=tab, file=file, yes=True, force=force)


def test_restore_tab_mismatch_refused(tmp_path):
    f = tmp_path / "2026-07-20-raw_tiktok.jsonl"
    f.write_text('{"raw_id": "t1"}\n', encoding="utf-8")
    sheets = FakeSheets({"raw_instagram": [{"raw_id": "старая"}], "run_log": []})
    rc = cmd_restore(sheets, _restore_ns("raw_instagram", str(f)))
    assert rc == 1
    assert sheets.tables["raw_instagram"] == [{"raw_id": "старая"}]  # не затёрта


def test_restore_tab_mismatch_force_overrides(tmp_path):
    f = tmp_path / "2026-07-20-raw_tiktok.jsonl"
    f.write_text('{"raw_id": "t1"}\n', encoding="utf-8")
    sheets = FakeSheets({"raw_instagram": [{"raw_id": "старая"}], "run_log": []})
    rc = cmd_restore(sheets, _restore_ns("raw_instagram", str(f), force=True))
    assert rc == 0


def test_restore_nonstandard_filename_allowed_with_warning(tmp_path, capsys):
    f = tmp_path / "экспорт-руками.jsonl"
    f.write_text('{"raw_id": "t1"}\n', encoding="utf-8")
    sheets = FakeSheets({"raw_tiktok": [], "run_log": []})
    rc = cmd_restore(sheets, _restore_ns("raw_tiktok", str(f)))
    assert rc == 0
    assert "сверка вкладки невозможна" in capsys.readouterr().out


def test_archive_failure_midway_still_leaves_failed_run_log_row(tmp_path, monkeypatch):
    # Ревью 14.09.2026 (правило №6): raw_tiktok уже заархивирован и удалён из листа,
    # а на следующей вкладке падает запись — исключение улетало в main(), и следа
    # деструктивной операции не оставалось ни в Run Log, ни в cf status.
    import pytest
    fixed_today(monkeypatch)
    sheets = FakeSheets({
        "raw_tiktok": [raw("t1", "2026-05-01T10:00:00.000Z")],
        "raw_instagram": [raw("i1", "2026-05-01T10:00:00.000Z")],
        "run_log": []})
    real_replace = sheets.replace_rows

    def replace_rows(tab_key, rows):
        if tab_key == "raw_instagram":
            raise RuntimeError("Sheets 500")
        return real_replace(tab_key, rows)

    monkeypatch.setattr(sheets, "replace_rows", replace_rows)
    with pytest.raises(RuntimeError):
        cmd_archive(sheets, ns(out_dir=str(tmp_path), yes=True))
    [row] = [r for r in sheets.tables["run_log"] if r["agent"] == "archive"]
    assert row["status"] == "failed"
    assert "raw_tiktok" in row["input_summary"] and "Sheets 500" in row["errors"]
