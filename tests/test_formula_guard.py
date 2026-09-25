import argparse
import json
from datetime import date
from pathlib import Path

import pytest

from cf.cli import cmd_formula_guard

from tests.fakes import FakeSheets


@pytest.fixture(autouse=True)
def _chdir_root(tmp_path, monkeypatch):
    # В бою CLI гоняется из корня репозитория: относительные entry["path"] в индексе
    # разрешаются от CWD, а канонизация опирается на тот же корень.
    monkeypatch.chdir(tmp_path)


def ns(**kw):
    return argparse.Namespace(**kw)


def _formula(tmp_path, name="test-formula", niche="стритвир", status="approved"):
    p = tmp_path / "formulas" / niche / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "niche": niche, "version": 1,
                             "status": status}), encoding="utf-8")
    return p


def _index(tmp_path, entries):
    p = tmp_path / "formulas" / "_approved" / "index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"approved": entries}), encoding="utf-8")
    return p


def _entry(path, name="test-formula", niche="стритвир"):
    # Канонический относительный путь (как пишет set_formula_status): formulas/<ниша>/<файл>.
    rel = "/".join(Path(path).parts[-3:])
    return {"name": name, "niche": niche, "path": rel,
            "version": 1, "approved_at": "2026-07-01T00:00:00+00:00"}


def _brief(i, formula_id="test-formula", review_status="approved"):
    return {"brief_id": f"b-{i}", "formula_id": formula_id, "review_status": review_status,
            "generated_at": f"2026-07-{i:02d}T00:00:00+00:00"}


def test_three_of_five_rejects_pauses_formula(tmp_path):
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [
        _brief(1, review_status="approved"),
        _brief(2, review_status="approved"),
        _brief(3, review_status="rejected"),
        _brief(4, review_status="rejected"),
        _brief(5, review_status="rejected"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "paused"
    assert "3" in data["status_reason"]
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []
    run_rows = sheets.read_rows("run_log")
    assert len(run_rows) == 1
    assert run_rows[0]["agent"] == "formula-guard"
    assert run_rows[0]["status"] == "success"


def test_endless_revisions_pause_formula(tmp_path):
    # Дыра, открывшаяся вместе с машинными воротами рецептов (26.07): рецепт,
    # который бесконечно даёт «на доработку», по правилу reject-3-из-5 не
    # тормозится ничем и не выпускает ни одного сценария. Гвардия — единственная
    # страховка авто-одобренного рецепта, пока нет ни строки performance.
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [
        _brief(1, review_status="approved"),
        _brief(2, review_status="revised"),
        _brief(3, review_status="revised"),
        _brief(4, review_status="revised"),
        _brief(5, review_status="rejected"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "paused"
    assert "забракован" in data["status_reason"]


def test_single_revision_does_not_pause(tmp_path):
    # revise — «исправимо»: одинокая доработка не повод снимать рецепт.
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [_brief(i, review_status=s) for i, s in enumerate(
        ["approved", "approved", "revised", "approved", "approved"], start=1)]
    sheets = FakeSheets({"briefs": briefs})

    assert cmd_formula_guard(sheets, ns(index=str(index))) == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "approved"


def test_old_rejects_outside_window_do_not_pause(tmp_path):
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    # ascending: rej, rej, rej, app, app, app, app -> last5 = [rej, app, app, app, app] = 1 reject
    briefs = [
        _brief(1, review_status="rejected"),
        _brief(2, review_status="rejected"),
        _brief(3, review_status="rejected"),
        _brief(4, review_status="approved"),
        _brief(5, review_status="approved"),
        _brief(6, review_status="approved"),
        _brief(7, review_status="approved"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "approved"
    assert len(json.loads(index.read_text(encoding="utf-8"))["approved"]) == 1
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "formula-guard"
    assert runs[0]["status"] == "success"


def test_only_two_briefs_both_rejected_does_not_pause(tmp_path):
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [
        _brief(1, review_status="rejected"),
        _brief(2, review_status="rejected"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "approved"
    assert len(json.loads(index.read_text(encoding="utf-8"))["approved"]) == 1
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "formula-guard"
    assert runs[0]["status"] == "success"


def test_empty_index_prints_all_ok_and_does_not_crash(tmp_path, capsys):
    index = _index(tmp_path, [])
    sheets = FakeSheets({"briefs": []})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "все формулы в норме" in out
    # пустой индекс — это тоже валидный прогон гвардии: должен оставить след
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["status"] == "success"


def test_all_formulas_ok_logs_success_run(tmp_path):
    # «все формулы в норме» — раньше молчало, теперь оставляет одну success-строку
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [_brief(i, review_status="approved") for i in range(1, 6)]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1
    assert runs[0]["agent"] == "formula-guard"
    assert runs[0]["status"] == "success"


def test_pause_failure_logs_failed_run(tmp_path):
    # формула должна встать на паузу, но set_formula_status падает (файла нет) —
    # это реальный сбой действия: пишем строку failed, не молчим
    missing_path = tmp_path / "formulas" / "стритвир" / "gone.json"
    index = _index(tmp_path, [_entry(missing_path, name="gone-formula")])
    briefs = [_brief(i, formula_id="gone-formula", review_status="rejected")
              for i in range(1, 4)]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1
    assert runs[0]["agent"] == "formula-guard"
    assert runs[0]["status"] == "failed"


def test_missing_index_file_prints_all_ok(tmp_path):
    index = tmp_path / "missing" / "index.json"
    sheets = FakeSheets({"briefs": []})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0


def test_invalid_json_formula_warns_and_continues(tmp_path, capsys):
    # битый JSON формулы (ValueError) не должен ронять весь прогон гвардии
    broken = tmp_path / "formulas" / "стритвир" / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{не json", encoding="utf-8")
    p2 = _formula(tmp_path, name="ok-formula")
    briefs = (
        [_brief(i, formula_id="broken-formula", review_status="rejected") for i in range(1, 4)]
        + [_brief(i, formula_id="ok-formula", review_status="rejected") for i in range(4, 7)]
    )
    index = _index(tmp_path, [
        _entry(broken, name="broken-formula"),
        _entry(p2, name="ok-formula"),
    ])
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "broken-formula" in out
    # вторая формула из индекса обработана несмотря на битую первую
    assert json.loads(p2.read_text(encoding="utf-8"))["status"] == "paused"


def test_two_formulas_paused_in_one_run(tmp_path):
    # защита от «оптимизации» с пере-чтением индекса внутри цикла
    p1 = _formula(tmp_path, name="formula-a")
    p2 = _formula(tmp_path, name="formula-b")
    briefs = (
        [_brief(i, formula_id="formula-a", review_status="rejected") for i in range(1, 4)]
        + [_brief(i, formula_id="formula-b", review_status="rejected") for i in range(4, 7)]
    )
    index = _index(tmp_path, [_entry(p1, name="formula-a"), _entry(p2, name="formula-b")])
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(p1.read_text(encoding="utf-8"))["status"] == "paused"
    assert json.loads(p2.read_text(encoding="utf-8"))["status"] == "paused"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []
    assert len(sheets.read_rows("run_log")) == 2


def test_reject_counting_normalizes_review_status(tmp_path):
    # P1.10: review_status 'REJECTED'/'Rejected ' (заглавные/пробел) считается как
    # reject — иначе гвардия недосчитывает и формула не встаёт на паузу.
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [
        _brief(1, review_status="approved"),
        _brief(2, review_status="approved"),
        _brief(3, review_status="REJECTED"),
        _brief(4, review_status="Rejected "),
        _brief(5, review_status="rejected"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "paused"


def test_broken_formula_entry_warns_and_continues(tmp_path, capsys):
    missing_path = tmp_path / "formulas" / "стритвир" / "gone.json"
    p2 = _formula(tmp_path, name="ok-formula")
    briefs_bad = [
        _brief(1, formula_id="gone-formula", review_status="rejected"),
        _brief(2, formula_id="gone-formula", review_status="rejected"),
        _brief(3, formula_id="gone-formula", review_status="rejected"),
    ]
    index = _index(tmp_path, [
        _entry(missing_path, name="gone-formula"),
        _entry(p2, name="ok-formula"),
    ])
    sheets = FakeSheets({"briefs": briefs_bad})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "gone-formula" in out
    data2 = json.loads(p2.read_text(encoding="utf-8"))
    assert data2["status"] == "approved"


# --- P5.10: performance-правило (авто-пауза по медиане) ------------------------


def _approved_brief(bid, formula_id, day=5):
    return {"brief_id": bid, "formula_id": formula_id, "review_status": "approved",
            "generated_at": f"2026-07-{day:02d}T00:00:00+00:00"}


def _perf(reel_id, brief_id, views, measured_at="2026-07-08"):
    return {"reel_id": reel_id, "brief_id": brief_id, "views": str(views),
            "er": "0.01", "measured_at": measured_at}


def test_performance_rule_pauses_below_half_median(tmp_path, monkeypatch):
    # 3 замеренных reels, медиана 1500 vs общая 5000 (0.3×) < 0.5× -> авто-пауза
    # с цифрами в reason; здоровая формула не тронута; прогон логируется (P1.12).
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))  # окно since=06-23
    weak = _formula(tmp_path, name="weak-f")
    healthy = _formula(tmp_path, name="healthy-f")
    index = _index(tmp_path, [_entry(weak, name="weak-f"), _entry(healthy, name="healthy-f")])
    briefs = ([_approved_brief(f"bw{i}", "weak-f") for i in range(3)]
              + [_approved_brief(f"bh{i}", "healthy-f") for i in range(4)])
    reels = ([{"reel_id": f"rw{i}", "brief_id": f"bw{i}", "published_at": "2026-07-01"}
              for i in range(3)]
             + [{"reel_id": f"rh{i}", "brief_id": f"bh{i}", "published_at": "2026-07-01"}
                for i in range(4)])
    perf = ([_perf(f"rw{i}", f"bw{i}", 1500) for i in range(3)]
            + [_perf(f"rh{i}", f"bh{i}", 5000) for i in range(4)])
    sheets = FakeSheets({"briefs": briefs, "reels": reels, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    wdata = json.loads(weak.read_text(encoding="utf-8"))
    assert wdata["status"] == "paused"
    reason = wdata["status_reason"]
    assert "performance" in reason
    assert "3 reels" in reason
    assert "1500" in reason and "5000" in reason   # реальные цифры
    assert "0.3" in reason                          # ratio 1500/5000
    assert "28" in reason                           # окно
    # здоровая формула не тронута
    assert json.loads(healthy.read_text(encoding="utf-8"))["status"] == "approved"
    approved = json.loads(index.read_text(encoding="utf-8"))["approved"]
    assert [e["name"] for e in approved] == ["healthy-f"]   # weak убрана из индекса
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1
    assert runs[0]["agent"] == "formula-guard" and runs[0]["status"] == "success"


def test_performance_rule_two_reels_not_paused(tmp_path, monkeypatch):
    # всего 2 замеренных reels (пусть и с мизерной медианой) -> порог ≥3 не пройден
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    small = _formula(tmp_path, name="small-f")
    healthy = _formula(tmp_path, name="healthy-f")
    index = _index(tmp_path, [_entry(small, name="small-f"), _entry(healthy, name="healthy-f")])
    briefs = ([_approved_brief(f"bs{i}", "small-f") for i in range(2)]
              + [_approved_brief(f"bh{i}", "healthy-f") for i in range(4)])
    perf = ([_perf(f"rs{i}", f"bs{i}", 100) for i in range(2)]
            + [_perf(f"rh{i}", f"bh{i}", 5000) for i in range(4)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(small.read_text(encoding="utf-8"))["status"] == "approved"
    assert len(json.loads(index.read_text(encoding="utf-8"))["approved"]) == 2
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["status"] == "success"


def test_performance_rule_healthy_formula_not_paused(tmp_path, monkeypatch):
    # обе формулы с сопоставимой медианой -> ни одна не ниже 0.5× общей
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    a = _formula(tmp_path, name="f-a")
    b = _formula(tmp_path, name="f-b")
    index = _index(tmp_path, [_entry(a, name="f-a"), _entry(b, name="f-b")])
    briefs, perf = [], []
    for fid, base in (("f-a", 4000), ("f-b", 5000)):
        for i in range(3):
            bid = f"{fid}-{i}"
            briefs.append(_approved_brief(bid, fid))
            perf.append(_perf(f"r-{bid}", bid, base))
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(a.read_text(encoding="utf-8"))["status"] == "approved"
    assert json.loads(b.read_text(encoding="utf-8"))["status"] == "approved"
    assert len(json.loads(index.read_text(encoding="utf-8"))["approved"]) == 2


def test_performance_rule_window_and_dedup_are_load_bearing(tmp_path, monkeypatch):
    # Дискриминирующий тест: у формулы РОВНО 2 замеренных reel в окне (медиана низкая),
    # плюс шумы, которые окно/дедуп обязаны отсеять:
    #   - rw1: 2 ежедневных замера -> дедуп к 1 reel (иначе +1 «reel»);
    #   - rold: 1 замер вне 28-дневного окна -> выпадает (иначе +1 reel).
    # Правильные окно+дедуп: 2 reels < 3 -> НЕ пауза. Сломай любое из двух -> 3 reels ->
    # низкая медиана -> пауза -> тест падает.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    f = _formula(tmp_path, name="edge-f")
    healthy = _formula(tmp_path, name="healthy-f")
    index = _index(tmp_path, [_entry(f, name="edge-f"), _entry(healthy, name="healthy-f")])
    briefs = [
        _approved_brief("bd1", "edge-f"),
        _approved_brief("bd2", "edge-f"),
        {"brief_id": "bold", "formula_id": "edge-f", "review_status": "approved",
         "generated_at": "2026-05-05T00:00:00+00:00"},
    ] + [_approved_brief(f"bh{i}", "healthy-f") for i in range(3)]
    reels = [
        {"reel_id": "rw1", "brief_id": "bd1", "published_at": "2026-07-01"},
        {"reel_id": "rw2", "brief_id": "bd2", "published_at": "2026-07-01"},
        {"reel_id": "rold", "brief_id": "bold", "published_at": "2026-05-01"},
    ] + [{"reel_id": f"rh{i}", "brief_id": f"bh{i}", "published_at": "2026-07-01"}
         for i in range(3)]
    perf = [
        _perf("rw1", "bd1", 100, measured_at="2026-07-06"),   # тот же reel, два замера
        _perf("rw1", "bd1", 100, measured_at="2026-07-08"),   # -> дедуп к одному
        _perf("rw2", "bd2", 100, measured_at="2026-07-08"),   # второй свежий reel
        _perf("rold", "bold", 100, measured_at="2026-05-08"),  # вне окна since=06-23
    ] + [_perf(f"rh{i}", f"bh{i}", 5000) for i in range(3)]
    sheets = FakeSheets({"briefs": briefs, "reels": reels, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    # 2 замеренных reel в окне (<3) -> не паузится, несмотря на низкую медиану
    assert json.loads(f.read_text(encoding="utf-8"))["status"] == "approved"


def test_performance_rule_boundary_exactly_half_not_paused(tmp_path, monkeypatch):
    # граница ровно 0.5×: порог строгий (<), поэтому median == 0.5× общей НЕ паузит.
    # edge-f: 3 reel @ 2500; база: 5 reel @ 5000 -> общая медиана 5000, порог 2500.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    f = _formula(tmp_path, name="edge-f")
    index = _index(tmp_path, [_entry(f, name="edge-f")])
    briefs = ([_approved_brief(f"be{i}", "edge-f") for i in range(3)]
              + [_approved_brief(f"bb{i}", "base-f") for i in range(5)])
    perf = ([_perf(f"re{i}", f"be{i}", 2500) for i in range(3)]
            + [_perf(f"rb{i}", f"bb{i}", 5000) for i in range(5)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(f.read_text(encoding="utf-8"))["status"] == "approved"


def test_performance_rule_broken_views_not_counted(tmp_path, monkeypatch):
    # 3 reel с непарсибельными views (->0.0) отсеиваются из статистики -> у формулы
    # 0 валидных reels (<3) -> ложной паузы «median 0 vs ...» нет.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    f = _formula(tmp_path, name="broken-views-f")
    index = _index(tmp_path, [_entry(f, name="broken-views-f")])
    briefs = ([_approved_brief(f"bx{i}", "broken-views-f") for i in range(3)]
              + [_approved_brief(f"bb{i}", "base-f") for i in range(3)])
    perf = ([_perf(f"rx{i}", f"bx{i}", "нет данных") for i in range(3)]   # views -> 0.0
            + [_perf(f"rb{i}", f"bb{i}", 10000) for i in range(3)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(f.read_text(encoding="utf-8"))["status"] == "approved"


def test_performance_run_log_carries_numbers(tmp_path, monkeypatch):
    # Run Log перф-паузы несёт реальные цифры причины (боевой путь выбрасывает stdout)
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    weak = _formula(tmp_path, name="weak-f")
    index = _index(tmp_path, [_entry(weak, name="weak-f")])
    briefs = ([_approved_brief(f"bw{i}", "weak-f") for i in range(3)]
              + [_approved_brief(f"bb{i}", "base-f") for i in range(3)])
    perf = ([_perf(f"rw{i}", f"bw{i}", 1500) for i in range(3)]
            + [_perf(f"rb{i}", f"bb{i}", 5000) for i in range(3)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["status"] == "success"
    summary = runs[0]["input_summary"]
    assert "performance" in summary and "1500" in summary   # цифры в Run Log


def test_performance_read_failure_keeps_reject_rule(tmp_path, monkeypatch, capsys):
    # хвост: вкладка performance недоступна -> warning + перф-правило пропущено,
    # но правило 1 (3 reject) продолжает работать.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    p = _formula(tmp_path)
    index = _index(tmp_path, [_entry(p)])
    briefs = [_brief(i, review_status="rejected") for i in range(1, 4)]
    sheets = FakeSheets({"briefs": briefs},
                        fail_read_tabs={"performance": ConnectionError("perf down")})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "performance" in out and "недоступна" in out          # warning напечатан
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "paused"  # reject-правило живо


def test_performance_reels_failure_falls_back_to_dedup(tmp_path, monkeypatch):
    # хвост: reels недоступны -> reels=None -> дедуп упадёт в fallback (последний
    # measured_at), перф-правило всё равно считается и паузит слабую формулу.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    weak = _formula(tmp_path, name="weak-f")
    index = _index(tmp_path, [_entry(weak, name="weak-f")])
    briefs = ([_approved_brief(f"bw{i}", "weak-f") for i in range(3)]
              + [_approved_brief(f"bb{i}", "base-f") for i in range(3)])
    perf = ([_perf(f"rw{i}", f"bw{i}", 1500) for i in range(3)]
            + [_perf(f"rb{i}", f"bb{i}", 5000) for i in range(3)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf},
                        fail_read_tabs={"reels": ConnectionError("reels down")})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(weak.read_text(encoding="utf-8"))["status"] == "paused"


def test_performance_and_reject_rules_coexist(tmp_path, monkeypatch):
    # правило reject срабатывает раньше правила performance для одной формулы;
    # для другой формулы срабатывает performance -> обе на паузе одним прогоном.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    rej = _formula(tmp_path, name="rej-f")
    perf_f = _formula(tmp_path, name="perf-f")
    index = _index(tmp_path, [_entry(rej, name="rej-f"), _entry(perf_f, name="perf-f")])
    briefs = (
        [_brief(i, formula_id="rej-f", review_status="rejected") for i in range(1, 4)]
        + [_approved_brief(f"bp{i}", "perf-f") for i in range(3)]
        # база сравнения — отдельная формула base-f (в индекс не входит, но её reels
        # считаются в общей медиане периода)
        + [_approved_brief(f"bh{i}", "base-f", day=6) for i in range(3)]
    )
    perf = ([_perf(f"rp{i}", f"bp{i}", 1000) for i in range(3)]
            + [_perf(f"rbase{i}", f"bh{i}", 6000) for i in range(3)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(rej.read_text(encoding="utf-8"))["status"] == "paused"
    perf_data = json.loads(perf_f.read_text(encoding="utf-8"))
    assert perf_data["status"] == "paused"
    assert "performance" in perf_data["status_reason"]
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []
    assert len(sheets.read_rows("run_log")) == 2


def test_formula_violating_both_rules_reject_primary_perf_appended(tmp_path, monkeypatch):
    # п.9: формула нарушает оба правила -> reject первичен, perf-причина дописана
    # второй фразой в status_reason.
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    both = _formula(tmp_path, name="both-f")
    index = _index(tmp_path, [_entry(both, name="both-f")])
    briefs = (
        [{"brief_id": f"bo{i}", "formula_id": "both-f", "review_status": "rejected",
          "generated_at": f"2026-07-0{i}T00:00:00+00:00"} for i in range(1, 4)]
        + [_approved_brief(f"bb{i}", "base-f") for i in range(3)]
    )
    perf = ([_perf(f"ro{i}", f"bo{i}", 100) for i in range(1, 4)]      # низкие -> perf-правило
            + [_perf(f"rb{i}", f"bb{i}", 5000) for i in range(3)])     # база
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(both.read_text(encoding="utf-8"))
    assert data["status"] == "paused"
    reason = data["status_reason"]
    assert "reject" in reason and "performance" in reason     # обе причины
    assert reason.index("reject") < reason.index("performance")  # reject первичен


# --- H14: окно «3 из 5» ограничено эпохой одобрения (аудит 2026-07-24) ------

def test_rejects_before_approved_at_do_not_pause(tmp_path):
    # Ре-апрув оператора (approved_at свежее reject'ов) сбрасывает окно:
    # старые reject больше не пере-паузят формулу ежедневным циклом.
    p = _formula(tmp_path)
    entry = _entry(p)
    entry["approved_at"] = "2026-07-10T00:00:00+00:00"
    index = _index(tmp_path, [entry])
    briefs = [
        _brief(3, review_status="rejected"),
        _brief(4, review_status="rejected"),
        _brief(5, review_status="rejected"),
        _brief(11, review_status="approved"),
        _brief(12, review_status="approved"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "approved"
    assert len(json.loads(index.read_text(encoding="utf-8"))["approved"]) == 1


def test_unparseable_generated_at_excluded_when_approved_at_set(tmp_path):
    # При заданном approved_at бриф с нечитаемой датой не атрибутируем эпохе —
    # исключаем из окна (иначе он вечно пере-паузил бы ре-одобренную формулу).
    p = _formula(tmp_path)
    entry = _entry(p)
    entry["approved_at"] = "2026-07-10T00:00:00+00:00"
    index = _index(tmp_path, [entry])
    briefs = [
        {"brief_id": "b-x", "formula_id": "test-formula",
         "review_status": "rejected", "generated_at": "мусор"},
        {"brief_id": "b-y", "formula_id": "test-formula",
         "review_status": "rejected", "generated_at": ""},
        _brief(11, review_status="rejected"),
        _brief(12, review_status="approved"),
        _brief(13, review_status="approved"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    # в окне эпохи только b-11..13 -> 1 reject из 3 -> в норме
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "approved"


def test_entry_without_approved_at_keeps_legacy_window(tmp_path):
    p = _formula(tmp_path)
    entry = _entry(p)
    entry.pop("approved_at", None)
    index = _index(tmp_path, [entry])
    briefs = [
        _brief(3, review_status="rejected"),
        _brief(4, review_status="rejected"),
        _brief(5, review_status="rejected"),
        _brief(6, review_status="approved"),
        _brief(7, review_status="approved"),
    ]
    sheets = FakeSheets({"briefs": briefs})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "paused"


def test_guard_pauses_snapshot_entry_without_touching_snapshot(tmp_path):
    # Запись индекса указывает на снапшот (C1/H1): пауза убирает запись и пишет
    # статус в РАБОЧИЙ файл, снапшот остаётся байт-в-байт.
    p = _formula(tmp_path)
    snap = tmp_path / "formulas" / "_approved" / "стритвир" / "test-formula-v1.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    entry = {"name": "test-formula", "niche": "стритвир",
             "path": "formulas/_approved/стритвир/test-formula-v1.json",
             "version": 1, "approved_at": "2026-07-01T00:00:00+00:00"}
    index = _index(tmp_path, [entry])
    briefs = [
        _brief(3, review_status="rejected"),
        _brief(4, review_status="rejected"),
        _brief(5, review_status="rejected"),
    ]
    sheets = FakeSheets({"briefs": briefs})
    before = snap.read_text(encoding="utf-8")

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert snap.read_text(encoding="utf-8") == before
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []
    working = json.loads(p.read_text(encoding="utf-8"))
    assert working["status"] == "paused"


def test_demo_measurements_do_not_pause_a_formula(tmp_path, monkeypatch, capsys):
    """Аудит 2026-08-04: авто-пауза сняла с производства два живых рецепта по
    ФИКТИВНЫМ замерам demo-seed (store-native-skit «median 765 vs 7300»). Демо-строки
    обязаны отсеиваться до расчёта медианы — правило №2: «данных нет» честнее
    выдуманного вердикта."""
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    f = _formula(tmp_path, name="skit-f")
    index = _index(tmp_path, [_entry(f, name="skit-f")])
    briefs = ([_approved_brief(f"bs{i}", "skit-f") for i in range(4)]
              + [_approved_brief(f"bb{i}", "base-f") for i in range(3)])
    # Демо-провал формулы (765 против базы 7300) — ровно форма боевого инцидента.
    perf = ([_perf(f"DEMO-sns-{i}", f"bs{i}", 765) for i in range(4)]
            + [_perf(f"DEMO-base-{i}", f"bb{i}", 7300) for i in range(3)])
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    assert json.loads(f.read_text(encoding="utf-8"))["status"] == "approved"
    assert "боевых замеров нет" in capsys.readouterr().out


def test_live_measurements_still_pause_when_demo_rows_mixed_in(tmp_path, monkeypatch):
    """Дискриминирующий: фильтр обязан снимать ТОЛЬКО демо. Боевой провал рядом с
    демо-шумом по-прежнему паузит — иначе фильтр выключил бы правило целиком."""
    monkeypatch.setattr("cf.cli.today", lambda: date(2026, 7, 21))
    f = _formula(tmp_path, name="real-fail-f")
    index = _index(tmp_path, [_entry(f, name="real-fail-f")])
    briefs = ([_approved_brief(f"br{i}", "real-fail-f") for i in range(3)]
              + [_approved_brief(f"bb{i}", "base-f") for i in range(3)]
              + [_approved_brief(f"bd{i}", "real-fail-f") for i in range(3)])
    perf = ([_perf(f"rr{i}", f"br{i}", 700) for i in range(3)]        # боевой провал
            + [_perf(f"rb{i}", f"bb{i}", 7000) for i in range(3)]     # боевая база
            + [_perf(f"DEMO-x{i}", f"bd{i}", 99000) for i in range(3)])  # демо-шум
    sheets = FakeSheets({"briefs": briefs, "performance": perf})

    rc = cmd_formula_guard(sheets, ns(index=str(index)))

    assert rc == 0
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["status"] == "paused"
    # медиана считается по 3 боевым (700), а не по 6 с демо-выбросами 99000
    assert "3 reels" in data["status_reason"] and "median 700" in data["status_reason"]
