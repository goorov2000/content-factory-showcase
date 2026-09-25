import argparse
import json
from datetime import datetime, timedelta, timezone

from cf.cli import cmd_auto_approve
from cf.runlog import now_iso

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def _review(tmp_path, verdict="recommend", brief_id="b-001", formula_id="test-formula"):
    p = tmp_path / "review.json"
    p.write_text(json.dumps({"brief_id": brief_id, "verdict": verdict,
                             "formula_id": formula_id, "reasons": []}), encoding="utf-8")
    return p


def _index(tmp_path, name="test-formula", niche="стритвир", version=1,
           file_status="approved", file_version=None):
    # Индекс в каноническом расположении (<root>/formulas/_approved/index.json) плюс
    # реальный файл формулы: гейт auto-approve перечитывает файл и сверяет статус/версию.
    root = tmp_path
    fdir = root / "formulas" / niche
    fdir.mkdir(parents=True, exist_ok=True)
    fv = version if file_version is None else file_version
    (fdir / f"{name}.json").write_text(
        json.dumps({"name": name, "niche": niche, "version": fv, "status": file_status}),
        encoding="utf-8")
    idx = root / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": [{
        "name": name, "niche": niche, "path": f"formulas/{niche}/{name}.json",
        "version": version, "approved_at": "2026-07-01T00:00:00+00:00"}]}),
        encoding="utf-8")
    return idx


def _brief(brief_id="b-001", formula_id="test-formula", review_status="pending",
          generated_at="2026-07-15T00:00:00+00:00"):
    return {"brief_id": brief_id, "formula_id": formula_id, "review_status": review_status,
           "generated_at": generated_at, "reviewer_notes": "", "rejection_reason": ""}


def _prompts(niche="стритвир", active=True):
    return [{"prompt_id": f"brief-{niche}-reel", "active": "TRUE" if active else "FALSE"}]


def test_approves_when_all_conditions_hold(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "approved"
    assert row["reviewer_notes"].startswith("авто-одобрен")
    assert row["rejection_reason"] == ""
    run_rows = sheets.read_rows("run_log")
    assert len(run_rows) == 1
    assert run_rows[0]["agent"] == "auto-approve"
    assert run_rows[0]["status"] == "success"


def test_formula_not_in_index_skips(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path, name="other-formula")
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_verdict_revise_skips(tmp_path):
    review = _review(tmp_path, verdict="revise")
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_prompt_inactive_skips(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts(active=False)})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_cap_reached_blocks_then_frees_when_entry_ages_out(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()

    approved_recent = [_brief(brief_id=f"b-r{i}", review_status="approved",
                              generated_at=recent) for i in range(5)]
    sheets = FakeSheets({"briefs": approved_recent + [_brief()],
                        "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[-1]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["status"] == "insufficient_data"

    # одна из «недавних» пары стала старше 7 дней — кап освобождается
    briefs2 = [dict(b) for b in approved_recent]
    briefs2[0]["generated_at"] = old
    sheets2 = FakeSheets({"briefs": briefs2 + [_brief()], "prompt_versions": _prompts()})

    rc2 = cmd_auto_approve(sheets2, ns(brief_id="b-001", review=str(review),
                                       index=str(index), cap=5))

    assert rc2 == 0
    assert sheets2.read_rows("briefs")[-1]["review_status"] == "approved"
    runs2 = sheets2.read_rows("run_log")
    assert len(runs2) == 1 and runs2[0]["status"] == "success"


def test_review_for_another_brief_skips(tmp_path):
    review = _review(tmp_path, brief_id="b-002")
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_cap_survives_naive_generated_at(tmp_path):
    # naive-дата в approved-строке (ручная правка таблицы) не должна ронять команду
    review = _review(tmp_path)
    index = _index(tmp_path)
    naive = _brief(brief_id="b-naive", review_status="approved",
                   generated_at="2026-07-14T00:00:00")
    sheets = FakeSheets({"briefs": [naive, _brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[-1]["review_status"] == "approved"


def test_brief_not_found_skips(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs") == []
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_stale_index_file_v3_proposed_skips(tmp_path):
    # Индекс говорит «v2 approved», но файл формулы уже переписан в v3 proposed
    # (ждёт ре-апрува оператором) — auto-approve не должен одобрять бриф.
    review = _review(tmp_path)
    index = _index(tmp_path, version=2, file_version=3, file_status="proposed")
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_stale_index_file_version_bump_skips(tmp_path):
    # Файл всё ещё approved, но версия ушла вперёд (v1 в индексе, v2 в файле) —
    # индекс протух, брифы могли собираться по старому телу формулы: не одобряем.
    review = _review(tmp_path)
    index = _index(tmp_path, version=1, file_version=2, file_status="approved")
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_index_points_to_missing_file_skips(tmp_path):
    # Индекс ссылается на несуществующий файл — гейт не может подтвердить статус,
    # значит не одобряет (fail-safe, а не fail-open).
    review = _review(tmp_path)
    index = _index(tmp_path)
    (tmp_path / "formulas" / "стритвир" / "test-formula.json").unlink()
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_brief_not_pending_skips(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief(review_status="rejected")],
                        "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "rejected"
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1 and runs[0]["agent"] == "auto-approve"
    assert runs[0]["status"] == "insufficient_data"


def test_pending_check_normalizes_review_status(tmp_path):
    # P1.10: review_status 'Pending ' (заглавные/пробел, ручная правка Sheets) — тот же
    # pending, что нормализует дашборд; бриф должен одобряться, а не отбрасываться.
    review = _review(tmp_path)
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief(review_status="Pending ")],
                        "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_cap_counting_normalizes_review_status(tmp_path):
    # P1.10: 5 approved-брифов с 'Approved ' (заглавные/пробел) должны заполнить кап,
    # иначе он недосчитывается и авто-одобрение уходит за лимит.
    review = _review(tmp_path)
    index = _index(tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    approved = [_brief(brief_id=f"b-a{i}", review_status="Approved ",
                       generated_at=recent) for i in range(5)]
    sheets = FakeSheets({"briefs": approved + [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[-1]["review_status"] == "pending"


def test_cap_counts_unparseable_dates(tmp_path, capsys):
    # P1.11: approved-брифы с нечитаемой generated_at ('15.07.2026' / '') должны
    # засчитываться в кап консервативно (как «в окне»), иначе кап обходится бесконечно.
    review = _review(tmp_path)
    index = _index(tmp_path)
    approved = [_brief(brief_id=f"b-u{i}", review_status="approved",
                       generated_at=("15.07.2026" if i % 2 else ""))
                for i in range(5)]
    sheets = FakeSheets({"briefs": approved + [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[-1]["review_status"] == "pending"
    assert "нечитаем" in capsys.readouterr().out.lower()


# --- Снапшоты и кап по reviewed_at (аудит 2026-07-24: C1/H1, M31) -----------

def _snapshot_index(tmp_path, name="test-formula", niche="стритвир", version=1,
                    snap_status="approved", snap_name=None, draft_version=3):
    """Индекс указывает на снапшот; рабочий файл — более новый proposed-черновик."""
    root = tmp_path
    snap_dir = root / "formulas" / "_approved" / niche
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{name}-v{version}.json").write_text(
        json.dumps({"name": snap_name or name, "niche": niche,
                    "version": version, "status": snap_status}), encoding="utf-8")
    fdir = root / "formulas" / niche
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / f"{name}.json").write_text(
        json.dumps({"name": name, "niche": niche, "version": draft_version,
                    "status": "proposed"}), encoding="utf-8")
    idx = root / "formulas" / "_approved" / "index.json"
    idx.write_text(json.dumps({"approved": [{
        "name": name, "niche": niche,
        "path": f"formulas/_approved/{niche}/{name}-v{version}.json",
        "version": version, "approved_at": "2026-07-01T00:00:00+00:00"}]}),
        encoding="utf-8")
    return idx


def test_snapshot_entry_approves_despite_working_file_drift(tmp_path):
    # Рабочий файл уже v3 proposed (formula-writer), но брифы генерятся по
    # снапшоту v1 approved — дрейф черновика одобрению больше не мешает.
    review = _review(tmp_path)
    index = _snapshot_index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_tampered_snapshot_refuses(tmp_path):
    # Снапшот руками переведён в proposed (или битая полу-миграция) — fail-safe.
    review = _review(tmp_path)
    index = _snapshot_index(tmp_path, snap_status="proposed")
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"


def test_snapshot_name_mismatch_refuses(tmp_path):
    review = _review(tmp_path)
    index = _snapshot_index(tmp_path, snap_name="другая-формула")
    sheets = FakeSheets({"briefs": [_brief()], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"


def test_cap_counts_by_reviewed_at_not_generated_at(tmp_path):
    # M31: бриф сгенерирован 10 дней назад, но одобрен вчера — занимает слот капа.
    review = _review(tmp_path)
    index = _index(tmp_path)
    old_gen = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    fresh_review = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    backlog = [dict(_brief(brief_id=f"old-{i}", review_status="approved",
                           generated_at=old_gen), reviewed_at=fresh_review)
               for i in range(5)]
    sheets = FakeSheets({"briefs": [_brief()] + backlog,
                         "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"  # кап исчерпан


def test_cap_falls_back_to_generated_at_without_reviewed_at(tmp_path):
    # Легаси-строки без reviewed_at: считаем по generated_at (старое поведение).
    review = _review(tmp_path)
    index = _index(tmp_path)
    old_gen = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    backlog = [_brief(brief_id=f"old-{i}", review_status="approved",
                      generated_at=old_gen) for i in range(5)]
    sheets = FakeSheets({"briefs": [_brief()] + backlog,
                         "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_approve_writes_reviewed_at(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [dict(_brief(), reviewed_at="")],
                         "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "approved"
    assert row["reviewed_at"]  # проставлен момент одобрения


# --- Кап из конфига и громкий дрейф схемы (аудит 2026-07-26) ----------------

def test_cap_comes_from_config_when_flag_not_passed(tmp_path):
    # --cap не передаётся ни слэш-командой, ни раннером: значение обязано
    # приходить из cf.config.json -> dashboard.fanout.approve_cap.
    review = _review(tmp_path)
    index = _index(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    backlog = [_brief(brief_id=f"old-{i}", review_status="approved",
                      generated_at=fresh) for i in range(3)]
    sheets = FakeSheets({"briefs": [_brief()] + backlog,
                         "prompt_versions": _prompts()},
                        config={"dashboard": {"fanout": {"approve_cap": 3}}})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=None))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"  # 3/3 занято


def test_explicit_cap_flag_beats_config(tmp_path):
    review = _review(tmp_path)
    index = _index(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    backlog = [_brief(brief_id=f"old-{i}", review_status="approved",
                      generated_at=fresh) for i in range(3)]
    sheets = FakeSheets({"briefs": [_brief()] + backlog,
                         "prompt_versions": _prompts()},
                        config={"dashboard": {"fanout": {"approve_cap": 3}}})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=10))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_broken_config_cap_falls_back_to_default(tmp_path):
    from cf.cli import approve_cap
    from cf.dashboard.runner import FANOUT_DEFAULTS
    default = FANOUT_DEFAULTS["approve_cap"]
    assert approve_cap(None) == default
    assert approve_cap({}) == default
    assert approve_cap({"dashboard": {"fanout": {"approve_cap": "мусор"}}}) == default
    assert approve_cap({"dashboard": {"fanout": {"approve_cap": 0}}}) == default
    assert approve_cap({"dashboard": {"fanout": {"approve_cap": -3}}}) == default
    assert approve_cap({"dashboard": {"fanout": {"approve_cap": 9}}}) == 9


def test_missing_reviewed_at_column_is_loud(tmp_path, capsys):
    # Боевое состояние 26.07: колонки reviewed_at в листе нет, кап молча считается
    # по generated_at. Подмена алгоритма обязана быть слышной.
    review = _review(tmp_path)
    index = _index(tmp_path)
    brief = _brief()
    brief.pop("reviewed_at", None)
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()},
                        headers={"briefs": [k for k in brief if k != "reviewed_at"]})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=None))

    assert rc == 0
    out = capsys.readouterr().out
    assert "ВНИМАНИЕ" in out and "reviewed_at" in out
    # и след в Run Log: по нему видно, работает ли M31
    logged = [r for r in sheets.read_rows("run_log") if r["agent"] == "auto-approve"]
    assert logged and "без колонки reviewed_at" in logged[-1]["input_summary"]


def test_cap_refusal_names_counting_mode_and_config_key(tmp_path, capsys):
    review = _review(tmp_path)
    index = _index(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    backlog = [_brief(brief_id=f"old-{i}", review_status="approved",
                      generated_at=fresh) for i in range(5)]
    sheets = FakeSheets({"briefs": [_brief()] + backlog,
                         "prompt_versions": _prompts()})

    cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                index=str(index), cap=None))

    out = capsys.readouterr().out
    assert "5/5 за 7 дней" in out
    assert "approve_cap" in out          # где крутить порог


# ── Отложенные капом сценарии (2026-07-27) ───────────────────────────────────
# До этой правки бриф, упёршийся в кап, оставался в «ожидает» бессрочно и
# изображал решение продюсера, которого тот принять не может: сценарий хороший,
# занята квота. Три таких брифа от 25.07 висели в очереди двое суток.

def _deferred_row(brief_id="b-001", formula_id="test-formula"):
    return {"brief_id": brief_id, "formula_id": formula_id,
            "review_status": "pending", "rejection_reason": "",
            "reviewer_notes": "отложено заводом: кап формулы 5/5 за 7 дней",
            "generated_at": "2026-07-25T00:00:00+00:00", "reviewed_at": ""}


def _versions(niche="стритвир"):
    return [{"prompt_id": f"brief-{niche}-reel", "version": "v1", "active": "TRUE"}]


def test_cap_refusal_marks_brief_as_deferred(tmp_path):
    from cf.dashboard.data import brief_is_deferred
    index = _index(tmp_path)
    approved = [{"brief_id": f"b-old-{i}", "formula_id": "test-formula",
                 "review_status": "approved", "reviewed_at": now_iso(),
                 "generated_at": now_iso()} for i in range(5)]
    sheets = FakeSheets({"briefs": approved + [
        {"brief_id": "b-001", "formula_id": "test-formula", "review_status": "pending",
         "rejection_reason": "", "reviewer_notes": "", "generated_at": now_iso(),
         "reviewed_at": ""}],
        "prompt_versions": _versions(), "run_log": []})

    cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(_review(tmp_path)),
                                index=str(index), cap=5,
                                run_started_at="2026-07-27T00:00:00+00:00"))

    row = next(r for r in sheets.read_rows("briefs") if r["brief_id"] == "b-001")
    assert row["review_status"] == "pending"       # статус не меняется
    assert brief_is_deferred(row)                  # но из очереди внимания уходит


def test_retry_deferred_approves_when_quota_frees(tmp_path):
    from cf.cli import cmd_retry_deferred
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_deferred_row()],   # одобренных в окне нет
                         "prompt_versions": _versions(), "run_log": []})

    rc = cmd_retry_deferred(sheets, ns(index=str(index), cap=5,
                                       run_started_at="2026-07-27T00:00:00+00:00"))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "approved"
    assert "квоты" in row["reviewer_notes"]
    assert any(r.get("agent") == "retry-deferred" for r in sheets.read_rows("run_log"))


def test_retry_deferred_holds_while_quota_is_busy(tmp_path):
    from cf.cli import cmd_retry_deferred
    index = _index(tmp_path)
    busy = [{"brief_id": f"b-old-{i}", "formula_id": "test-formula",
             "review_status": "approved", "reviewed_at": now_iso(),
             "generated_at": now_iso()} for i in range(5)]
    sheets = FakeSheets({"briefs": busy + [_deferred_row()],
                         "prompt_versions": _versions(), "run_log": []})

    cmd_retry_deferred(sheets, ns(index=str(index), cap=5,
                                  run_started_at="2026-07-27T00:00:00+00:00"))

    row = next(r for r in sheets.read_rows("briefs") if r["brief_id"] == "b-001")
    assert row["review_status"] == "pending"


def test_retry_deferred_does_not_let_two_briefs_past_one_free_slot(tmp_path):
    # Два отложенных брифа одной формулы при одном свободном месте: второй обязан
    # остаться отложенным, иначе кап пробивается собственной страховкой.
    from cf.cli import cmd_retry_deferred
    index = _index(tmp_path)
    busy = [{"brief_id": f"b-old-{i}", "formula_id": "test-formula",
             "review_status": "approved", "reviewed_at": now_iso(),
             "generated_at": now_iso()} for i in range(4)]
    sheets = FakeSheets({"briefs": busy + [_deferred_row("b-001"),
                                           _deferred_row("b-002")],
                         "prompt_versions": _versions(), "run_log": []})

    cmd_retry_deferred(sheets, ns(index=str(index), cap=5,
                                  run_started_at="2026-07-27T00:00:00+00:00"))

    rows = {r["brief_id"]: r["review_status"] for r in sheets.read_rows("briefs")}
    assert sorted([rows["b-001"], rows["b-002"]]) == ["approved", "pending"]


# --- проверки ремесла на воротах (2026-08-04) ---------------------------------------
# Аудит 114 сценариев: 8 смертельных дефектов из 12 прошли ворота, потому что ни одна
# из семи проверок ревьюера не смотрит на сам текст сценария. Здесь арифметика.

def _index_with_hook_timing(tmp_path, timing="0-3s", name="test-formula",
                            niche="стритвир"):
    idx = _index(tmp_path, name=name, niche=niche)
    fpath = tmp_path / "formulas" / niche / f"{name}.json"
    data = json.loads(fpath.read_text(encoding="utf-8"))
    data["hook_structure"] = {"timing": timing, "elements": []}
    fpath.write_text(json.dumps(data), encoding="utf-8")
    return idx


def test_brief_with_unfilled_placeholders_is_not_auto_approved(tmp_path):
    # own-event-announcement-...-collab-drop: [БРЕНД]×4, [ПАРТНЁР]×4 — и approved
    review = _review(tmp_path)
    index = _index_with_hook_timing(tmp_path)
    brief = _brief()
    brief.update({"hook": "Знакомьтесь — [ИМЯ МОДЕЛИ]",
                  "script_text": "0-3с: плашка «[НАЗВАНИЕ КОЛЛАБОРАЦИИ] — уже в магазине»"})
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "pending"          # остаётся человеку
    assert sheets.read_rows("run_log")[0]["status"] == "insufficient_data"


def test_brief_contradicting_itself_is_not_auto_approved(tmp_path):
    # grwm-...-series: скрипт «это ПЕРВЫЙ эпизод», капшен «Эпизод 4» — и approved
    review = _review(tmp_path)
    index = _index_with_hook_timing(tmp_path)
    brief = _brief()
    brief.update({"hook": "Правила ты знаешь: я собираю образ, ты судишь",
                  "script_text": "0-3с: это ПЕРВЫЙ эпизод серии, ссылок на прошлые нет",
                  "caption": "Эпизод 4: вещь выбрали вы, образ собираю я"})
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"


def test_unpronounceable_hook_is_not_auto_approved(tmp_path):
    # sales-window: 253 знака в окне 0-3с, одобрен в тот же день, когда ревьюер
    # отклонил за то же самое брифа послабее
    review = _review(tmp_path)
    index = _index_with_hook_timing(tmp_path, timing="0-3s")
    brief = _brief()
    brief["hook"] = ("Думаешь, маркетплейс — единственное место, где мужчине одеться "
                     "на распродаже? Началась финальная распродажа лета — и именно "
                     "сейчас так думать дороже всего. Три бренда, которые прямо "
                     "сейчас стоят как якорь, а выглядят втрое дороже")
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"
    assert "дефекты ремесла" in sheets.read_rows("run_log")[0]["input_summary"]


def test_stage_direction_in_hook_field_is_not_auto_approved(tmp_path):
    # v4-регрессия: 12 из 13 брифов версии несут в поле hook описание кадра
    review = _review(tmp_path)
    index = _index_with_hook_timing(tmp_path)
    brief = _brief()
    brief["hook"] = "0-1с: автор уже в кадре целиком в летнем светлом образе"
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"


def test_clean_brief_still_passes_the_craft_gate(tmp_path):
    # Дискриминирующий: гейт не имеет права быть глухой стеной. tee-fit — верх корпуса
    # по консенсусу трёх судей, он обязан проходить.
    review = _review(tmp_path)
    index = _index_with_hook_timing(tmp_path)
    brief = _brief()
    brief.update({
        "hook": "Большинство футболок сидят на тебе неправильно. Не «не тот бренд» — "
                "просто неправильно, и вот по каким трём признакам это видно",
        "script_text": "0-3с. Вердикт-запрет с первой секунды, на вещь, не на зрителя. "
                       "3-12с. Короткий аргумент. 12-22с. Замена, критерий 1.",
        "visual_direction": "Штатив, дневной свет, 9:16.",
        "caption": "Проверь свою.", "cta": "Вопрос-мнение в финале"})
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_formula_without_hook_timing_does_not_block_on_hook_length(tmp_path):
    # fail-open: рецепт без парсибельного hook_structure.timing не превращает
    # длинный хук в отказ — это суждение ревьюера, а не арифметика
    review = _review(tmp_path)
    index = _index(tmp_path)                       # без hook_structure вовсе
    brief = _brief()
    brief["hook"] = "очень длинный хук, " * 20
    sheets = FakeSheets({"briefs": [brief], "prompt_versions": _prompts()})

    rc = cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(review),
                                     index=str(index), cap=5))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


# ── ревью 14.09.2026: машинные записи notes не стирают метку «доработано заводом» ──

def test_cap_deferral_and_later_approval_keep_fix_marker(tmp_path):
    # Метка — единственный носитель «одна попытка фиксера израсходована». Отложенный
    # по капу правленый бриф терял её, следующий revise-вердикт отправлял его к
    # фиксеру второй раз (платный вызов), а retry-deferred стирал её при одобрении.
    from cf.cli import FIX_MARKER, brief_was_fixed, cmd_retry_deferred
    from cf.dashboard.data import brief_is_deferred
    index = _index(tmp_path)
    approved = [{"brief_id": f"b-old-{i}", "formula_id": "test-formula",
                 "review_status": "approved", "reviewed_at": now_iso(),
                 "generated_at": now_iso()} for i in range(5)]
    fixed = {"brief_id": "b-001", "formula_id": "test-formula", "review_status": "pending",
             "rejection_reason": "", "reviewer_notes": f"{FIX_MARKER}: хук переписан",
             "generated_at": now_iso(), "reviewed_at": ""}
    sheets = FakeSheets({"briefs": approved + [fixed],
                         "prompt_versions": _versions(), "run_log": []})
    cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(_review(tmp_path)),
                                index=str(index), cap=5,
                                run_started_at="2026-07-27T00:00:00+00:00"))
    row = next(r for r in sheets.read_rows("briefs") if r["brief_id"] == "b-001")
    assert brief_is_deferred(row) and brief_was_fixed(row)

    # квота освободилась: одобрение после ожидания метку тоже сохраняет
    for r in sheets.tables["briefs"]:
        if r["brief_id"].startswith("b-old-"):
            r["review_status"] = "rejected"
    cmd_retry_deferred(sheets, ns(index=str(index), cap=5,
                                  run_started_at="2026-07-27T00:00:00+00:00"))
    row = next(r for r in sheets.read_rows("briefs") if r["brief_id"] == "b-001")
    assert row["review_status"] == "approved" and brief_was_fixed(row)


def test_direct_auto_approval_keeps_fix_marker(tmp_path):
    from cf.cli import FIX_MARKER, brief_was_fixed
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "formula_id": "test-formula", "review_status": "pending",
         "rejection_reason": "", "reviewer_notes": f"{FIX_MARKER}: хук переписан",
         "generated_at": now_iso(), "reviewed_at": ""}],
        "prompt_versions": _versions(), "run_log": []})
    assert cmd_auto_approve(sheets, ns(brief_id="b-001", review=str(_review(tmp_path)),
                                       index=str(index), cap=5,
                                       run_started_at="2026-07-27T00:00:00+00:00")) == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "approved" and brief_was_fixed(row)
