import argparse

from cf.cli import cmd_set_review

from tests.fakes import FakeSheets


def ns(**kw):
    # Дефолты как у argparse (--reason-code/--reason/--notes default="").
    kw.setdefault("reason_code", "")
    kw.setdefault("reason", "")
    kw.setdefault("notes", "")
    return argparse.Namespace(**kw)


def test_set_review_updates_brief_row():
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="rejected",
                                   reason="слабый референс", notes="см. review-файл"))
    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "rejected"
    # Без --reason-code — прежнее поведение: свободный текст как причина.
    assert row["rejection_reason"] == "слабый референс"


def test_set_review_with_reason_code_writes_bracket_prefix():
    # P5.12: код причины кладётся структурным префиксом [code] в rejection_reason.
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="rejected",
                                   reason_code="reference_mismatch",
                                   reason="URL X — женский образ", notes="деталь"))
    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["rejection_reason"] == "[reference_mismatch] URL X — женский образ"
    assert row["reviewer_notes"] == "деталь"


def test_set_review_bare_reason_code_no_free_text():
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    cmd_set_review(sheets, ns(brief_id="b-001", status="rejected",
                              reason_code="female_reference"))
    assert sheets.read_rows("briefs")[0]["rejection_reason"] == "[female_reference]"


def test_set_review_unknown_reason_code_errors_without_write():
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="rejected",
                                   reason_code="bogus_code"))
    assert rc == 1
    # Решение не записано — статус остался pending.
    assert sheets.read_rows("briefs")[0]["review_status"] == "pending"


def test_set_review_reason_code_rejected_for_approved_status():
    # --reason-code осмыслен только у reject/revise; с approved -> exit 1 без записи
    # мусорного [code] в rejection_reason одобренного брифа.
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="approved",
                                   reason_code="weak_hook"))
    assert rc == 1
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "pending"      # не записано
    assert row["rejection_reason"] == ""


def test_set_review_reason_code_allowed_for_revised():
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="revised",
                                   reason_code="weak_hook"))
    assert rc == 0
    assert sheets.read_rows("briefs")[0]["rejection_reason"] == "[weak_hook]"


def test_set_review_unknown_brief_returns_error():
    rc = cmd_set_review(FakeSheets({"briefs": []}),
                        ns(brief_id="nope", status="approved", reason="", notes=""))
    assert rc == 1


# ── Доработка сценария заводом (2026-07-27) ──────────────────────────────────
# Вердикт «доработка» был тупиком: ревьюер называл, что поправить, а поправить
# было некому, и брифы копились в очереди продюсера как ложные решения.

def _fixed_brief(tmp_path, brief_id="b-001"):
    import json
    payload = {
        "brief_id": brief_id, "formula_id": "alpha", "prompt_version": "v2",
        "hook": "новый хук в первые три секунды",
        "script": "переписанный сценарий без копирования референса",
        "visual_direction": "съёмка на телефон, один креатор",
        "cta": "вопрос-выбор в конце",
        "references": ["https://www.tiktok.com/@a/video/1"],
        "source_pattern_ids": ["p-01"],
    }
    path = tmp_path / "fixed.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _revise_ns(brief_id, path, notes="переписан CTA"):
    return argparse.Namespace(brief_id=brief_id, file=str(path), notes=notes,
                              run_started_at="2026-07-27T00:00:00+00:00")


def test_revise_brief_rewrites_and_returns_to_review(tmp_path):
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [
        {"brief_id": "b-001", "review_status": "revised", "rejection_reason": "[other]",
         "reviewer_notes": "CTA заявляет несуществующий факт", "hook": "старый",
         "script": "старый", "visual_direction": "", "cta": "", "references": "",
         "prompt_version": "v2", "source_pattern_ids": "", "payload_json": "{}"},
    ]})

    rc = cmd_revise_brief(sheets, _revise_ns("b-001", _fixed_brief(tmp_path)))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "pending"          # снова на ревью
    assert row["script"].startswith("переписанный")
    assert row["rejection_reason"] == ""
    assert "доработано заводом" in row["reviewer_notes"]
    assert any(r.get("agent") == "brief-fixer" and r.get("status") == "success"
               for r in sheets.read_rows("run_log"))


def test_revise_brief_refuses_second_attempt(tmp_path):
    # Одна попытка на бриф: иначе завод и ревьюер пингуют друг друга бесконечно.
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [
        {"brief_id": "b-001", "review_status": "revised", "rejection_reason": "",
         "reviewer_notes": "доработано заводом: CTA", "hook": "", "script": "",
         "visual_direction": "", "cta": "", "references": "", "prompt_version": "",
         "source_pattern_ids": "", "payload_json": "{}"},
    ]})

    rc = cmd_revise_brief(sheets, _revise_ns("b-001", _fixed_brief(tmp_path)))

    assert rc == 0                                     # отказ, а не сбой пайплайна
    assert sheets.read_rows("briefs")[0]["review_status"] == "revised"
    assert any(r.get("status") == "insufficient_data"
               for r in sheets.read_rows("run_log"))


def test_revise_brief_refuses_when_not_in_revision(tmp_path):
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [
        {"brief_id": "b-001", "review_status": "approved", "rejection_reason": "",
         "reviewer_notes": "", "hook": "", "script": "", "visual_direction": "",
         "cta": "", "references": "", "prompt_version": "", "source_pattern_ids": "",
         "payload_json": "{}"},
    ]})

    assert cmd_revise_brief(sheets, _revise_ns("b-001", _fixed_brief(tmp_path))) == 0
    assert sheets.read_rows("briefs")[0]["review_status"] == "approved"


def test_revise_brief_refuses_foreign_payload(tmp_path):
    # Чужой payload переписал бы не тот сценарий — отказ до единой записи.
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [
        {"brief_id": "b-001", "review_status": "revised", "rejection_reason": "",
         "reviewer_notes": "", "hook": "старый", "script": "старый",
         "visual_direction": "", "cta": "", "references": "", "prompt_version": "",
         "source_pattern_ids": "", "payload_json": "{}"},
    ]})

    rc = cmd_revise_brief(sheets, _revise_ns("b-001", _fixed_brief(tmp_path, "b-999")))

    assert rc == 1
    assert sheets.read_rows("briefs")[0]["script"] == "старый"


# ── Отказ фиксера: бриф уходит из очереди доработки к человеку (2026-07-27) ───
# Метку ставила ТОЛЬКО успешная правка, поэтому честный отказ агента (сценарий
# требует события, которого у бренда нет) не оставлял следа вовсе: бриф вечно
# возвращался в очередь доработки и жёг платный вызов агента каждый цикл. На
# 27.07 так заперты b-own-event-announcement-…-founder-opening и …-popup-offers.

REFUSED_NOTE = "сценарий требует события, которого у бренда нет"


def _refused_ns(brief_id, notes=REFUSED_NOTE):
    # Форма вызова из раннера (dashboard/runner.py): --refused --brief-id --notes,
    # без позиционного аргумента и без --file. Контракт межгрупповой.
    return argparse.Namespace(brief_id=None, brief_id_flag=brief_id, file=None,
                              refused=True, notes=notes,
                              run_started_at="2026-07-27T00:00:00+00:00")


def _revised_row(**kw):
    row = {"brief_id": "b-001", "review_status": "revised",
           "rejection_reason": "[not_producible] события нет",
           "reviewer_notes": "CTA зовёт на открытие, которого не будет",
           "hook": "старый", "script": "старый", "visual_direction": "", "cta": "",
           "references": "", "prompt_version": "v2", "source_pattern_ids": "",
           "payload_json": "{}"}
    row.update(kw)
    return row


def test_revise_brief_refused_marks_brief_and_frees_the_queue():
    from cf.cli import FIX_MARKER, brief_was_fixed, cmd_revise_brief, norm_status
    sheets = FakeSheets({"run_log": [], "briefs": [_revised_row()]})

    rc = cmd_revise_brief(sheets, _refused_ns("b-001"))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    # Контракт с раннером: метка + пояснение, почему не переписан, — и замечания
    # ревьюера сохраняются (ревью 14.09.2026: их затирали, и продюсер получал бриф в
    # доработке без единого слова о том, что с ним не так).
    assert row["reviewer_notes"] == (f"{FIX_MARKER} (не переписан заводом): "
                                     f"{REFUSED_NOTE}; замечания ревьюера: "
                                     f"CTA зовёт на открытие, которого не будет")
    assert row["script"] == "старый"                     # сценарий не тронут
    assert norm_status(row["review_status"]) == "revised"  # решение за человеком
    assert brief_was_fixed(row)                          # но из очереди фиксера ушёл
    assert any(r.get("agent") == "brief-fixer"
               and r.get("status") == "insufficient_data"
               for r in sheets.read_rows("run_log"))


def test_revise_brief_refused_keeps_existing_marker_notes():
    # Метку мог поставить и сам агент (когда разрешения харнесса ему позволяют).
    # Переписав notes второй раз, мы затёрли бы текст, уже показанный человеку.
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [
        _revised_row(reviewer_notes="доработано заводом: переписан CTA")]})

    assert cmd_revise_brief(sheets, _refused_ns("b-001")) == 0

    assert sheets.read_rows("briefs")[0]["reviewer_notes"] == \
        "доработано заводом: переписан CTA"


def test_revise_brief_refused_skips_brief_outside_revision():
    # Вне «доработки» метка не значит ничего, а навредить может: ревьюер позже
    # поставит «доработка», и фиксер пропустит бриф, решив, что уже пробовал.
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [
        _revised_row(review_status="approved", reviewer_notes="")]})

    assert cmd_revise_brief(sheets, _refused_ns("b-001")) == 0

    assert sheets.read_rows("briefs")[0]["reviewer_notes"] == ""
    assert any(r.get("status") == "insufficient_data"      # след всё равно есть
               for r in sheets.read_rows("run_log"))


def test_revise_brief_refused_with_file_is_a_usage_error(tmp_path):
    # --refused ничего не переписывает: сочетание с --file — почти всегда путаница
    # режимов, и молча игнорировать файл нельзя.
    from cf.cli import cmd_revise_brief
    args = _refused_ns("b-001")
    args.file = str(_fixed_brief(tmp_path))
    sheets = FakeSheets({"run_log": [], "briefs": [_revised_row()]})

    assert cmd_revise_brief(sheets, args) == 1
    assert sheets.read_rows("briefs")[0]["script"] == "старый"


def test_revise_brief_without_file_answers_instead_of_crashing():
    # --file больше не required у argparse (иначе --refused не выразить), поэтому
    # обычный вызов без файла обязан отвечать внятно, а не падать ниже по коду.
    from cf.cli import cmd_revise_brief
    sheets = FakeSheets({"run_log": [], "briefs": [_revised_row()]})
    args = argparse.Namespace(brief_id="b-001", brief_id_flag=None, file=None,
                              refused=False, notes="", run_started_at=None)

    assert cmd_revise_brief(sheets, args) == 1
    assert sheets.read_rows("briefs")[0]["script"] == "старый"


def test_revise_brief_parser_supports_both_call_forms():
    from cf.cli import build_parser, cmd_revise_brief, revise_brief_id
    refused = build_parser().parse_args(
        ["revise-brief", "--refused", "--brief-id", "b-001", "--notes", REFUSED_NOTE])
    assert refused.func is cmd_revise_brief and refused.refused is True
    assert revise_brief_id(refused) == "b-001" and refused.file is None
    # Исторический позиционный вызов не сломан.
    normal = build_parser().parse_args(["revise-brief", "b-002", "--file", "f.json"])
    assert revise_brief_id(normal) == "b-002" and normal.refused is False


def test_refused_brief_stays_visible_to_human(tmp_path):
    # Снять бриф с очереди фиксера — не то же самое, что спрятать его от человека:
    # иначе отказ превратился бы в тихое кладбище сценариев. Очередь внимания
    # дашборда считает «доработку» целиком, без оглядки на метку.
    from datetime import datetime

    from cf.cli import cmd_revise_brief
    from cf.dashboard.data import DataCache, overview_metrics
    sheets = FakeSheets({"run_log": [], "briefs": [
        _revised_row(generated_at="2026-07-26T10:00:00+00:00")]})

    assert cmd_revise_brief(sheets, _refused_ns("b-001")) == 0

    metrics = overview_metrics(DataCache(sheets, ttl=60), days=30,
                               today=datetime(2026, 7, 27), root=str(tmp_path))
    assert metrics["briefs_revised"] == 1
    assert metrics["briefs_attention"] == 1


# ── Стоп-кран пинг-понга на нормальном пути ревью (2026-07-27) ───────────────

def test_set_review_keeps_fix_marker_in_notes():
    # set-review перезаписывал reviewer_notes целиком и стирал метку «завод уже
    # правил». Ревьюер ставит переписанному сценарию «доработка» со своими
    # замечаниями — и бриф снова уходил фиксеру: платный вызов каждый цикл.
    from cf.cli import brief_was_fixed
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "",
         "reviewer_notes": "доработано заводом: переписан CTA"},
    ]})

    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="revised",
                                   reason_code="weak_hook",
                                   notes="хук всё ещё слабый"))

    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert brief_was_fixed(row)                       # метка пережила ревью
    assert "хук всё ещё слабый" in row["reviewer_notes"]   # и замечание на месте


def test_set_review_does_not_invent_fix_marker():
    # Обратная сторона: метку нельзя ставить брифу, которого завод не касался, —
    # иначе фиксер пропустит сценарий, который он обязан переписать.
    from cf.cli import brief_was_fixed
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "",
         "reviewer_notes": "замечание ревьюера"},
    ]})

    cmd_set_review(sheets, ns(brief_id="b-001", status="revised",
                              notes="сюжет скопирован с референса"))

    row = sheets.read_rows("briefs")[0]
    assert row["reviewer_notes"] == "сюжет скопирован с референса"
    assert not brief_was_fixed(row)
