"""cf recheck-briefs — пересверка УЖЕ ОДОБРЕННЫХ сценариев проверками ремесла.

Ворота auto-approve смотрят только pending: сценарии, одобренные до появления
проверки, не пересматриваются никогда и лежат в очереди съёмки как готовые. Эта
команда — недостающее звено между ними и воркером «Доработка сценариев», который
переписывает брифы `revised` сам.
"""
import argparse
import json
from datetime import timedelta

import pytest

from cf.cli import cmd_recheck_briefs

from tests.fakes import FakeSheets


@pytest.fixture(autouse=True)
def _chdir_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def ns(**kw):
    kw.setdefault("apply", False)
    kw.setdefault("limit", None)
    return argparse.Namespace(**kw)


def _index(tmp_path, name="test-formula", niche="стритвир", timing="0-3s"):
    fdir = tmp_path / "formulas" / niche
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / f"{name}.json").write_text(json.dumps({
        "name": name, "niche": niche, "version": 1, "status": "approved",
        "hook_structure": {"timing": timing, "elements": []}}), encoding="utf-8")
    idx = tmp_path / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": [{
        "name": name, "niche": niche, "path": f"formulas/{niche}/{name}.json",
        "version": 1, "approved_at": "2026-07-01T00:00:00+00:00"}]}), encoding="utf-8")
    return idx


def _brief(brief_id="b-1", hook="нормальный короткий хук", **extra):
    row = {"brief_id": brief_id, "formula_id": "test-formula",
           "review_status": "approved", "generated_at": "2026-08-01T00:00:00+00:00",
           "hook": hook, "script_text": "0-3с: «коротко». 3-9с: показ.",
           "visual_direction": "", "caption": "", "cta": "", "reviewer_notes": "",
           "rejection_reason": ""}
    row.update(extra)
    return row


def test_defective_brief_goes_to_revision(tmp_path):
    index = _index(tmp_path)
    brief = _brief(hook="Знакомьтесь — [ИМЯ МОДЕЛИ]")
    sheets = FakeSheets({"briefs": [brief], "reels": []})

    rc = cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "revised"
    assert "пересверкой" in row["reviewer_notes"]
    assert row["rejection_reason"] == "[unfilled_placeholder]"


def test_dry_run_changes_nothing(tmp_path):
    # По умолчанию команда только показывает: перевод N сценариев в доработку стоит
    # N платных вызовов агента на следующем цикле.
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief(hook="Знакомьтесь — [ИМЯ]")], "reels": []})

    rc = cmd_recheck_briefs(sheets, ns(index=str(index)))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"
    assert "НИЧЕГО не изменено" in sheets.read_rows("run_log")[0]["input_summary"]


def test_clean_brief_is_left_alone(tmp_path):
    index = _index(tmp_path)
    sheets = FakeSheets({"briefs": [_brief()], "reels": []})

    rc = cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"
    assert "дефектов нет" in sheets.read_rows("run_log")[0]["input_summary"]


def test_published_brief_is_never_touched(tmp_path):
    # Ролик уже вышел: переписывать его сценарий поздно, а метка порвала бы связку
    # brief -> reel -> performance.
    index = _index(tmp_path)
    brief = _brief(hook="Знакомьтесь — [ИМЯ МОДЕЛИ]")
    reels = [{"reel_id": "r1", "brief_id": "b-1", "post_url": "https://x/1",
              "published_at": "2026-08-02", "status": "published"}]
    sheets = FakeSheets({"briefs": [brief], "reels": reels})

    rc = cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_already_fixed_brief_is_skipped(tmp_path):
    # Одна попытка на бриф: иначе фиксер и ревьюер пингуют друг друга каждый цикл
    # за платный вызов.
    from cf.cli import FIX_MARKER
    index = _index(tmp_path)
    brief = _brief(hook="Знакомьтесь — [ИМЯ]", reviewer_notes=f"{FIX_MARKER}: правлено")
    sheets = FakeSheets({"briefs": [brief], "reels": []})

    rc = cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"
    assert "пропущено уже правленных: 1" in sheets.read_rows("run_log")[0]["input_summary"]


def test_pending_brief_is_not_in_scope(tmp_path):
    # pending разбирают ворота auto-approve; здесь только уже одобренные
    index = _index(tmp_path)
    brief = _brief(hook="Знакомьтесь — [ИМЯ]", review_status="pending")
    sheets = FakeSheets({"briefs": [brief], "reels": []})

    cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"


def test_limit_caps_the_batch(tmp_path):
    index = _index(tmp_path)
    briefs = [_brief(f"b-{i}", hook="Знакомьтесь — [ИМЯ]") for i in range(4)]
    sheets = FakeSheets({"briefs": briefs, "reels": []})

    cmd_recheck_briefs(sheets, ns(index=str(index), apply=True, limit=2))

    sent = [r for r in sheets.read_rows("briefs") if r["review_status"] == "revised"]
    assert len(sent) == 2
    assert "отложено лимитом 2" in sheets.read_rows("run_log")[0]["input_summary"]


def test_unreadable_reels_cancels_the_sweep(tmp_path):
    """Правило №2: без reels снятые неотличимы от неснятых, и пересверка отправила
    бы в доработку сценарий уже вышедшего ролика."""
    index = _index(tmp_path)

    class NoReels(FakeSheets):
        def read_rows(self, tab):
            if tab == "reels":
                raise RuntimeError("Sheets недоступна")
            return super().read_rows(tab)

    sheets = NoReels({"briefs": [_brief(hook="Знакомьтесь — [ИМЯ]")], "reels": []})

    rc = cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    assert rc == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"
    assert sheets.read_rows("run_log")[0]["status"] == "insufficient_data"


def test_original_of_a_duplicate_pair_is_not_blamed(tmp_path):
    """Сверка на повтор несимметрична: виноват тот, кто написан ПОЗЖЕ. На живых
    данных симметричный счёт давал 73 дефектных брифа против 55 при верном."""
    index = _index(tmp_path)
    text = " ".join(f"слов{chr(1072 + i % 30)}{chr(1072 + i // 30)}" for i in range(60))
    # Даты СКОЛЬЗЯЩИЕ: окно сверки на повтор — последние
    # DUPLICATE_WINDOW_DAYS суток от сегодня, и с фиксированными
    # 01-02.08.2026 тест зеленел ровно до 31.08, а потом предшественник
    # выпадал из окна и «виноватый» переставал находиться. Поймано 09.09.2026
    # на прогоне всего набора.
    from cf.cli import today
    from cf.craftcheck import DUPLICATE_WINDOW_DAYS

    inside = today() - timedelta(days=DUPLICATE_WINDOW_DAYS // 3)
    first = _brief("b-first", script_text=text,
                   generated_at=f"{inside.isoformat()}T00:00:00+00:00")
    second = _brief("b-second", script_text=text,
                    generated_at=f"{(inside + timedelta(days=1)).isoformat()}"
                                 f"T00:00:00+00:00")
    sheets = FakeSheets({"briefs": [first, second], "reels": []})

    cmd_recheck_briefs(sheets, ns(index=str(index), apply=True))

    rows = {r["brief_id"]: r["review_status"] for r in sheets.read_rows("briefs")}
    assert rows["b-first"] == "approved"      # оригинал не виноват
    assert rows["b-second"] == "revised"


def test_brief_already_given_to_creator_is_reported_not_sent_to_revision(tmp_path, capsys):
    # Ревью 14.09.2026: «Отдал в работу» оставляет бриф approved. --apply переводил его
    # в revised, фиксер переписывал текст, а криэйтор снимал по старой распечатке —
    # связка «снято ↔ сценарий» рвалась. Назначенный сценарий — решение человека.
    index = _index(tmp_path)
    assigned = _brief("b-assigned", hook="Знакомьтесь — [ИМЯ МОДЕЛИ]",
                      creator_slot="криэйтор-1", assigned_at="2026-08-05T10:00:00+00:00")
    free = _brief("b-free", hook="Знакомьтесь — [ИМЯ МОДЕЛИ]")
    sheets = FakeSheets({"briefs": [assigned, free], "reels": []})

    assert cmd_recheck_briefs(sheets, ns(index=str(index), apply=True)) == 0

    rows = {r["brief_id"]: r for r in sheets.read_rows("briefs")}
    assert rows["b-assigned"]["review_status"] == "approved"
    assert rows["b-free"]["review_status"] == "revised"
    out = capsys.readouterr().out
    assert "b-assigned" in out and "криэйтор-1" in out
    assert "у криэйтора" in sheets.read_rows("run_log")[-1]["input_summary"]
