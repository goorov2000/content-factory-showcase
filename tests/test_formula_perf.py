"""P5.11: cf formula-perf — накопительная own_performance формул в индексе + перенос
eval-verdict в status_reason. Витрина реюзает join/дедуп build_eval_dataset (P5.8),
как гвардия (P5.10), но БЕЗ 28-дневного окна — результаты накапливаются."""
import argparse
import json
from pathlib import Path

import pytest

from cf.cli import cmd_formula_perf

from tests.fakes import FakeSheets


@pytest.fixture(autouse=True)
def _chdir_root(tmp_path, monkeypatch):
    # CLI гоняется из корня репо: относительные entry["path"] разрешаются от CWD.
    monkeypatch.chdir(tmp_path)


def ns(**kw):
    return argparse.Namespace(**kw)


def _formula(tmp_path, name="test-formula", niche="стритвир", status="approved",
             status_reason=""):
    p = tmp_path / "formulas" / niche / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "niche": niche, "version": 1,
                             "status": status, "status_reason": status_reason}),
                 encoding="utf-8")
    return p


def _index(tmp_path, entries):
    p = tmp_path / "formulas" / "_approved" / "index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"approved": entries}), encoding="utf-8")
    return p


def _entry(path, name="test-formula", niche="стритвир", **extra):
    rel = "/".join(Path(path).parts[-3:])
    entry = {"name": name, "niche": niche, "path": rel,
             "version": 1, "approved_at": "2026-07-01T00:00:00+00:00"}
    entry.update(extra)
    return entry


def _brief(bid, formula_id, day=5):
    return {"brief_id": bid, "formula_id": formula_id, "review_status": "approved",
            "generated_at": f"2026-07-{day:02d}T00:00:00+00:00"}


def _perf(reel_id, brief_id, views, er="0.02", measured_at="2026-07-08"):
    row = {"reel_id": reel_id, "brief_id": brief_id, "views": str(views),
           "measured_at": measured_at}
    if er is not None:
        row["er"] = er
    return row


def _own(index):
    return json.loads(index.read_text(encoding="utf-8"))["approved"][0].get("own_performance")


def test_writes_aggregates_to_index(tmp_path):
    # приёмка: {reels, median_views, avg_er, last_measured_at} в индексе
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [_brief(f"b{i}", "f-a") for i in range(3)]
    reels = [{"reel_id": f"r{i}", "brief_id": f"b{i}", "published_at": "2026-07-01"}
             for i in range(3)]
    perf = [_perf("r0", "b0", 1000, er="0.01", measured_at="2026-07-08"),
            _perf("r1", "b1", 2000, er="0.02", measured_at="2026-07-09"),
            _perf("r2", "b2", 3000, er="0.03", measured_at="2026-07-10")]
    sheets = FakeSheets({"briefs": briefs, "reels": reels, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 3
    assert op["median_views"] == 2000
    assert abs(op["avg_er"] - 0.02) < 1e-9
    assert op["last_measured_at"] == "2026-07-10"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1
    assert runs[0]["agent"] == "formula-perf" and runs[0]["status"] == "success"


def test_formula_without_measurements_no_key(tmp_path):
    # приёмка: формула без замеров -> ключа own_performance нет (/lab: «нет данных»)
    p = _formula(tmp_path, name="f-empty")
    index = _index(tmp_path, [_entry(p, name="f-empty")])
    sheets = FakeSheets({"briefs": [], "performance": []})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    assert _own(index) is None


def test_broken_views_excluded(tmp_path):
    # views<=0 (непарсибельные) отсекаются как в гвардии -> не искажают медиану
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [_brief(f"b{i}", "f-a") for i in range(3)]
    perf = [_perf("r0", "b0", "нет данных"),    # -> 0.0, отсечён
            _perf("r1", "b1", 4000),
            _perf("r2", "b2", 6000)]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 2
    assert op["median_views"] == 5000


def test_cumulative_includes_old_measurements(tmp_path):
    # витрина накопительная: старый замер (вне 28-дн окна гвардии) учитывается
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [
        {"brief_id": "bold", "formula_id": "f-a", "review_status": "approved",
         "generated_at": "2026-01-01T00:00:00+00:00"},
        _brief("bnew", "f-a"),
    ]
    reels = [{"reel_id": "rold", "brief_id": "bold", "published_at": "2026-01-01"},
             {"reel_id": "rnew", "brief_id": "bnew", "published_at": "2026-07-01"}]
    perf = [_perf("rold", "bold", 500, measured_at="2026-01-08"),
            _perf("rnew", "bnew", 1500, measured_at="2026-07-08")]
    sheets = FakeSheets({"briefs": briefs, "reels": reels, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 2                       # старый замер НЕ выпал
    assert op["last_measured_at"] == "2026-07-08"


def test_avg_er_none_without_er(tmp_path):
    # er отсутствует в замерах -> avg_er None («нет данных»), а не 0.0
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [_brief(f"b{i}", "f-a") for i in range(3)]
    perf = [_perf(f"r{i}", f"b{i}", 1000, er=None) for i in range(3)]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 3
    assert op["avg_er"] is None


def test_only_approved_formulas_aggregated(tmp_path):
    # замеры формулы вне индекса не пишутся; своя формула считает только свои reels
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [_brief("ba", "f-a"), _brief("bx", "other-f")]
    perf = [_perf("ra", "ba", 1000), _perf("rx", "bx", 9999)]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 1
    assert op["median_views"] == 1000


def test_verdict_from_eval_written_to_status_reason(tmp_path):
    # verdict из formula_performance последнего weekly-eval -> status_reason (status цел)
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    evals = tmp_path / "agent-runtime" / "evals"
    evals.mkdir(parents=True)
    (evals / "2026-07-20-weekly-eval.json").write_text(json.dumps({
        "formula_performance": [
            {"formula_id": "f-a", "reels": 3, "verdict": "лучшая по медиане"}]
    }), encoding="utf-8")
    briefs = [_brief(f"b{i}", "f-a") for i in range(3)]
    perf = [_perf(f"r{i}", f"b{i}", 1000 + i) for i in range(3)]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status_reason"] == "лучшая по медиане"
    assert data["status"] == "approved"           # статус не тронут


def test_no_eval_file_leaves_status_reason(tmp_path):
    # без weekly-eval перенос verdict — no-op: существующий status_reason не затираем
    p = _formula(tmp_path, name="f-a", status_reason="прежняя причина")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [_brief(f"b{i}", "f-a") for i in range(3)]
    perf = [_perf(f"r{i}", f"b{i}", 1000 + i) for i in range(3)]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status_reason"] == "прежняя причина"


def test_stale_own_performance_removed(tmp_path):
    # результаты пропали -> устаревший own_performance убирается (актуальность витрины)
    p = _formula(tmp_path, name="f-a")
    entry = _entry(p, name="f-a", own_performance={
        "reels": 5, "median_views": 9999, "avg_er": 0.05, "last_measured_at": "2026-06-01"})
    index = _index(tmp_path, [entry])
    sheets = FakeSheets({"briefs": [], "performance": []})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    assert _own(index) is None


def test_empty_index_logs_success(tmp_path):
    index = _index(tmp_path, [])
    sheets = FakeSheets({"briefs": [], "performance": []})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1
    assert runs[0]["agent"] == "formula-perf" and runs[0]["status"] == "success"


def test_performance_unavailable_preserves_vitrine(tmp_path):
    # сбой чтения performance != «замеров нет»: накопленный own_performance ЦЕЛ, а
    # Run Log не рапортует ложное «обновлён у 0/N»
    p = _formula(tmp_path, name="f-a")
    prior = {"reels": 4, "median_views": 8000, "avg_er": 0.03,
             "last_measured_at": "2026-06-01"}
    index = _index(tmp_path, [_entry(p, name="f-a", own_performance=prior)])
    sheets = FakeSheets({"briefs": []},
                        fail_read_tabs={"performance": ConnectionError("perf down")})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    assert _own(index) == prior                     # витрина не стёрта
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["status"] == "success"
    summary = runs[0]["input_summary"]
    assert "не обновлена" in summary
    assert "0/" not in summary                      # не ложное «обновлён у 0/N»


def test_verdict_not_written_to_desynced_paused_file(tmp_path):
    # рассинхрон файл/индекс: файл формулы уже paused, а запись из индекса не убрана —
    # eval-verdict НЕ затирает pause-reason (гейт status==approved)
    p = _formula(tmp_path, name="f-a", status="paused",
                 status_reason="на паузе гвардией")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    evals = tmp_path / "agent-runtime" / "evals"
    evals.mkdir(parents=True)
    (evals / "2026-07-20-weekly-eval.json").write_text(json.dumps({
        "formula_performance": [{"formula_id": "f-a", "verdict": "новый verdict"}]
    }), encoding="utf-8")
    sheets = FakeSheets({"briefs": [], "performance": []})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status_reason"] == "на паузе гвардией"   # pause-reason не затёрт
    assert data["status"] == "paused"


def test_verdict_transfer_idempotent_not_recounted(tmp_path):
    # повторный прогон: verdict уже стоит -> no-op, «перенесён у 0» (не в счёт)
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    evals = tmp_path / "agent-runtime" / "evals"
    evals.mkdir(parents=True)
    (evals / "2026-07-20-weekly-eval.json").write_text(json.dumps({
        "formula_performance": [{"formula_id": "f-a", "verdict": "лучшая"}]
    }), encoding="utf-8")
    briefs = [_brief(f"b{i}", "f-a") for i in range(3)]
    perf = [_perf(f"r{i}", f"b{i}", 1000 + i) for i in range(3)]

    cmd_formula_perf(FakeSheets({"briefs": briefs, "performance": perf}),
                     ns(index=str(index)))            # 1-й прогон — переносит verdict
    sheets2 = FakeSheets({"briefs": briefs, "performance": perf})
    rc = cmd_formula_perf(sheets2, ns(index=str(index)))  # 2-й — verdict уже стоит

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status_reason"] == "лучшая"
    assert "перенесён у 0" in sheets2.read_rows("run_log")[0]["input_summary"]


def test_last_measured_at_ignores_unparseable(tmp_path):
    # рукописный мусор в measured_at не должен выиграть лексикографику max() над датой
    p = _formula(tmp_path, name="f-a")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    briefs = [_brief(f"b{i}", "f-a") for i in range(2)]
    perf = [_perf("r0", "b0", 1000, measured_at="2026-07-08"),
            _perf("r1", "b1", 2000, measured_at="нет данных")]   # непарсибельно
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 2
    assert op["last_measured_at"] == "2026-07-08"    # «нет данных» не выиграл


def test_demo_measurements_do_not_reach_own_performance(tmp_path):
    """Аудит 2026-08-04: витрина own_performance в /lab показывала медианы 8100-10400,
    собранные из 48/48 демо-строк. Демо не доходит до витрины — иначе оператор
    масштабирует формулу по выдуманным числам."""
    p = _formula(tmp_path, name="f-demo")
    index = _index(tmp_path, [_entry(p, name="f-demo")])
    briefs = [_brief(f"b{i}", "f-demo") for i in range(3)]
    perf = [_perf(f"DEMO-ssir-{i}", f"b{i}", 8100) for i in range(3)]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    assert _own(index) is None          # «нет данных», а не выдуманные 8100


def test_live_measurements_survive_demo_filter(tmp_path):
    """Дискриминирующий: фильтр снимает только метку DEMO-, боевые замеры остаются."""
    p = _formula(tmp_path, name="f-mix")
    index = _index(tmp_path, [_entry(p, name="f-mix")])
    briefs = [_brief(f"b{i}", "f-mix") for i in range(4)]
    perf = [_perf("r0", "b0", 1000, measured_at="2026-07-08"),
            _perf("r1", "b1", 3000, measured_at="2026-07-09"),
            _perf("DEMO-x0", "b2", 99000, measured_at="2026-07-10"),
            _perf("DEMO-x1", "b3", 99000, measured_at="2026-07-11")]
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    op = _own(index)
    assert op["reels"] == 2 and op["median_views"] == 2000
    assert op["last_measured_at"] == "2026-07-09"      # демо-даты не выиграли max()


def _eval_report(tmp_path, verdict="держать", provenance=None, reel_id="r0"):
    p = tmp_path / "agent-runtime" / "evals" / "2026-07-27-weekly-eval.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    dataset = {"measured_reels": 3}
    if provenance:
        dataset["provenance_warning"] = provenance
    p.write_text(json.dumps({
        "dataset": dataset,
        "rows": [{"reel_id": reel_id}],
        "formula_performance": [{"formula_id": "f-a", "verdict": verdict}],
    }), encoding="utf-8")
    return p


def test_verdict_from_a_demo_eval_is_not_transferred(tmp_path, capsys):
    """Eval-агент честно помечает отчёт на демо-данных (provenance_warning), а перенос
    эту пометку игнорировал: выдуманные медианы уезжали в status_reason формул
    («держать, медиана 9350»). Оператор читает именно эту строку."""
    p = _formula(tmp_path, name="f-a", status_reason="")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    _eval_report(tmp_path, verdict="держать. 10 роликов, медиана 9350",
                 provenance="Все 35 замеренных роликов — строки демо-прогона")
    sheets = FakeSheets({"briefs": [], "performance": []})

    rc = cmd_formula_perf(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status_reason"] == ""
    assert "посчитан на демо-данных" in capsys.readouterr().out


def test_verdict_is_not_transferred_when_every_reel_is_demo(tmp_path):
    # отчёт старого формата, без provenance_warning — распознаём по меткам reel_id
    p = _formula(tmp_path, name="f-a", status_reason="")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    _eval_report(tmp_path, reel_id="DEMO-ssir-01")
    sheets = FakeSheets({"briefs": [], "performance": []})

    cmd_formula_perf(sheets, ns(index=str(index)))

    assert json.loads(p.read_text(encoding="utf-8"))["status_reason"] == ""


def test_verdict_from_a_live_eval_is_still_transferred(tmp_path):
    # Дискриминирующий: фильтр не имеет права выключить перенос вообще
    p = _formula(tmp_path, name="f-a", status_reason="")
    index = _index(tmp_path, [_entry(p, name="f-a")])
    _eval_report(tmp_path, verdict="держать", reel_id="r-live-01")
    sheets = FakeSheets({"briefs": [], "performance": []})

    cmd_formula_perf(sheets, ns(index=str(index)))

    assert json.loads(p.read_text(encoding="utf-8"))["status_reason"] == "держать"
