"""Маркеры прогресса сбора: контракт между `cf collect` и дашбордом (спека §7)."""

from cf.collect import progress as marker
from cf.collect.apify import run_batches


def test_emit_and_parse_roundtrip():
    printed = []
    marker.emit(printed.append, "fetch", done=3, total=8)
    assert printed == ["CF_PROGRESS phase=fetch done=3 total=8"]
    assert marker.parse(printed[0]) == {"phase": "fetch", "done": 3, "total": 8}


def test_emit_without_counters_and_without_log():
    printed = []
    marker.emit(printed.append, "gate")
    assert printed == ["CF_PROGRESS phase=gate"]
    marker.emit(None, "gate")            # без log — тихо, сбор от UI не зависит


def test_parse_ignores_foreign_and_broken_lines():
    assert marker.parse("collect tiktok: success") is None
    assert marker.parse("") is None
    # битое число — фаза остаётся, счётчик отбрасывается
    assert marker.parse("CF_PROGRESS phase=fetch done=abc total=8") == {
        "phase": "fetch", "total": 8}


def test_is_marker_keeps_telemetry_out_of_reports():
    assert marker.is_marker("CF_PROGRESS phase=write")
    assert not marker.is_marker("batches=7 rows=519 kept=257")


def test_emit_survives_broken_log():
    def boom(_line):
        raise RuntimeError("stdout closed")

    marker.emit(boom, "fetch", done=1, total=2)      # прогресс не роняет сбор


def test_emit_survives_broken_counter():
    # rows/new приезжают из sheets.upsert_rows — уже ПОСЛЕ записи строк. Битое
    # число не должно ронять collect: печатаем фазу, счётчик отбрасываем —
    # ровно так же, как это делает parse.
    printed = []
    marker.emit(printed.append, "write", rows="много", new=None)
    assert printed == ["CF_PROGRESS phase=write"]
    marker.emit(printed.append, "fetch", done=object(), total=8)
    assert printed[-1] == "CF_PROGRESS phase=fetch total=8"


def test_run_batches_reports_every_finished_batch():
    seen = []
    batches = [{"batch_index": i, "batch_sources": []} for i in range(4)]
    results, losses = run_batches(batches, lambda b: [b["batch_index"]],
                                  max_workers=2,
                                  on_done=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert len(results) == 4 and losses == []


def test_run_batches_counts_failed_batches_too_and_survives_bad_callback():
    def runner(batch):
        if batch["batch_index"] == 1:
            raise RuntimeError("apify упал")
        return [batch["batch_index"]]

    seen = []

    def on_done(done, total):
        seen.append(done)
        raise RuntimeError("колбэк упал")            # не должен ломать сбор

    batches = [{"batch_index": i, "batch_sources": ["s"]} for i in range(3)]
    results, losses = run_batches(batches, runner, max_workers=1, on_done=on_done)
    assert seen == [1, 2, 3]                          # прогресс считает и потери
    assert len(results) == 2 and len(losses) == 1


def test_marker_carries_result_counts():
    # Лента показывает добычу («118 роликов · 24 новых»), поэтому итог единицы
    # работы приезжает числом в том же маркере, а не прозой сводки.
    line = "CF_PROGRESS phase=write rows=60 new=12"
    assert marker.parse(line) == {"phase": "write", "rows": 60, "new": 12}
    printed = []
    marker.emit(printed.append, "write", rows=60, new=12)
    assert printed == [line]
    # битые числа отбрасываются, фаза остаётся
    assert marker.parse("CF_PROGRESS phase=write rows=x") == {"phase": "write"}
