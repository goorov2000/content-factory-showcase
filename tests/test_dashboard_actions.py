import logging

import pytest

from cf.collect.metrika import ORDER_HEADERS
from cf.dashboard.actions import (ReviewResult, add_manual_order, review_brief,
                                  set_order_status)
from cf.sheets import UnknownFieldsError
from tests.fakes import FakeSheets


def make_sheets():
    return FakeSheets({
        "briefs": [{"brief_id": "B1", "review_status": "pending",
                    "rejection_reason": "", "reviewer_notes": ""}],
        "run_log": [],
    })


def test_approve_updates_status_and_logs():
    sheets = make_sheets()
    result = review_brief(sheets, "B1", "approved", notes="хороший хук")
    assert result == ReviewResult(found=True, logged=True)
    brief = sheets.tables["briefs"][0]
    assert brief["review_status"] == "approved"
    assert brief["reviewer_notes"] == "хороший хук"
    assert brief["rejection_reason"] == ""
    (tab, row), = sheets.appended
    assert tab == "run_log" and row["agent"] == "dashboard-review"
    assert row["status"] == "success" and "B1" in row["input_summary"]


def test_reject_fills_rejection_reason():
    # Без reason_code — мягкая деградация к прежнему поведению (заметка как причина).
    sheets = make_sheets()
    review_brief(sheets, "B1", "rejected", notes="слабый CTA")
    brief = sheets.tables["briefs"][0]
    assert brief["review_status"] == "rejected"
    assert brief["rejection_reason"] == "слабый CTA"


def test_reject_with_reason_code_writes_bracket_and_keeps_notes():
    # P5.12: код причины -> префикс [code] в rejection_reason (ключ группировки),
    # свободный текст ревьюера остаётся в reviewer_notes.
    sheets = make_sheets()
    review_brief(sheets, "B1", "rejected", notes="URL X — женщина",
                 reason_code="female_reference")
    brief = sheets.tables["briefs"][0]
    assert brief["rejection_reason"] == "[female_reference]"
    assert brief["reviewer_notes"] == "URL X — женщина"


def test_reject_unknown_reason_code_falls_back_to_notes():
    # Неизвестный/пустой код не роняет форму: причина = свободная заметка (legacy).
    sheets = make_sheets()
    review_brief(sheets, "B1", "rejected", notes="что-то не так", reason_code="bogus")
    assert sheets.tables["briefs"][0]["rejection_reason"] == "что-то не так"


def test_pending_resets_status_and_clears_rejection():
    sheets = FakeSheets({
        "briefs": [{"brief_id": "B1", "review_status": "rejected",
                    "rejection_reason": "слабый CTA",
                    "reviewer_notes": "старая заметка"}],
        "run_log": [],
    })
    assert review_brief(sheets, "B1", "pending") == ReviewResult(found=True, logged=True)
    brief = sheets.tables["briefs"][0]
    assert brief["review_status"] == "pending"
    assert brief["rejection_reason"] == ""
    assert brief["reviewer_notes"] == "старая заметка"   # отмена не стирает заметку
    last_tab, last_row = sheets.appended[-1]
    assert last_tab == "run_log"
    assert last_row["input_summary"] == "B1: pending"


def test_unknown_brief_returns_false_and_no_log():
    sheets = make_sheets()
    result = review_brief(sheets, "NOPE", "approved")
    assert result.found is False
    assert sheets.appended == []


def test_invalid_decision_raises():
    with pytest.raises(ValueError):
        review_brief(make_sheets(), "B1", "maybe")


def test_missing_column_surfaces_failure_and_does_not_save(caplog):
    # У вкладки нет колонок rejection_reason/reviewer_notes -> решение оператора
    # не должно молча «сохраниться». review_brief всплывает ошибкой, а не рапортует успех.
    sheets = FakeSheets({
        "briefs": [{"brief_id": "B1", "review_status": "pending"}],
        "run_log": [],
    })
    with caplog.at_level(logging.WARNING, logger="cf.dashboard.actions"):
        with pytest.raises(UnknownFieldsError):
            review_brief(sheets, "B1", "approved", notes="ок")
    assert sheets.tables["briefs"][0]["review_status"] == "pending"  # не сохранено
    assert sheets.appended == []  # run_log не пополнен
    assert any("B1" in rec.message for rec in caplog.records
               if rec.levelno == logging.WARNING)


def test_run_log_failure_surfaces_in_result_but_keeps_decision(caplog):
    # P2.14: сбой записи в Run Log НЕ откатывает решение (оно уже в Sheets), но
    # честно виден в композитном результате logged=False — чтобы вызвавший слой
    # (post_review) мог показать это оператору, а не проглотить молча.
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("run_log append failed")

    sheets = BrokenLogSheets({
        "briefs": [{"brief_id": "B1", "review_status": "pending",
                    "rejection_reason": "", "reviewer_notes": ""}],
        "run_log": [],
    })
    with caplog.at_level(logging.WARNING, logger="cf.dashboard.actions"):
        result = review_brief(sheets, "B1", "approved", notes="ок")
    assert result == ReviewResult(found=True, logged=False)
    assert sheets.tables["briefs"][0]["review_status"] == "approved"
    assert any("run log" in rec.message for rec in caplog.records
               if rec.levelno == logging.WARNING)


# ── Реестр заказов (UTM-контур, тикет 03) ────────────────────────────────────


CANDIDATE = {"order_id": "mk-40129", "source": "metrika_ecommerce",
             "order_date": "2026-07-29", "revenue": 1990, "utm_source": "tiktok",
             "utm_campaign": "tiktok-1", "utm_content": "b42",
             "account": "tiktok-1", "reel_id": "b42", "status": "candidate",
             "status_changed_at": "", "decided_by": "", "notes": "",
             "collected_at": "2026-07-30T08:20:00+00:00"}


def make_orders_sheets(rows=(CANDIDATE,)):
    return FakeSheets({"orders": [dict(r) for r in rows], "run_log": []},
                      headers={"orders": list(ORDER_HEADERS)})


def test_set_order_status_confirms_and_logs():
    sheets = make_orders_sheets()
    result = set_order_status(sheets, "mk-40129", "confirmed")
    assert result == ReviewResult(found=True, logged=True)
    row = sheets.tables["orders"][0]
    assert row["status"] == "confirmed"
    assert row["decided_by"] == "human"
    assert row["status_changed_at"]                     # момент решения записан
    assert row["revenue"] == 1990                       # сумма не тронута
    (tab, log_row), = sheets.appended
    assert tab == "run_log" and log_row["agent"] == "dashboard-order"
    assert "mk-40129" in log_row["input_summary"]
    assert "confirmed" in log_row["input_summary"]


def test_set_order_status_allows_changing_mind_including_back_to_candidate():
    # Статусы меняет только человек — в том числе передумать: возврат/не наш/
    # обратно в кандидаты. Сборщик решённые строки не трогает, поэтому это
    # единственный путь смены статуса.
    sheets = make_orders_sheets([dict(CANDIDATE, status="confirmed",
                                      decided_by="human")])
    assert set_order_status(sheets, "mk-40129", "returned").found
    assert sheets.tables["orders"][0]["status"] == "returned"
    assert set_order_status(sheets, "mk-40129", "candidate").found
    assert sheets.tables["orders"][0]["status"] == "candidate"


def test_set_order_status_unknown_order_and_invalid_status():
    sheets = make_orders_sheets()
    assert set_order_status(sheets, "NOPE", "confirmed").found is False
    assert sheets.appended == []                        # run_log не пополнен
    with pytest.raises(ValueError):
        set_order_status(sheets, "mk-40129", "paid")


def test_set_order_status_missing_columns_fails_loud():
    # В листе заказов нет колонок решения — решение по деньгам НЕ теряется молча.
    sheets = FakeSheets({"orders": [{"order_id": "mk-40129"}], "run_log": []})
    with pytest.raises(UnknownFieldsError):
        set_order_status(sheets, "mk-40129", "confirmed")


def test_add_manual_order_writes_confirmed_row_and_creates_tab():
    # Вкладки может ещё не быть (сбор Метрики не запускался) — ручной заказ
    # создаёт её сам, со схемой сборщика.
    sheets = FakeSheets({"run_log": []})
    result = add_manual_order(sheets, "2026-07-29", "1990.50", "tiktok-1",
                              notes="перевод на карту")
    assert result.order_id.startswith("manual-")
    row = sheets.tables["orders"][0]
    assert row["source"] == "manual"
    assert row["status"] == "confirmed"                 # сразу подтверждён
    assert row["decided_by"] == "human"
    assert row["revenue"] == 1990.5
    assert row["order_date"] == "2026-07-29"
    assert row["account"] == "tiktok-1"
    assert row["notes"] == "перевод на карту"
    assert row["utm_campaign"] == "" and row["reel_id"] == ""
    (tab, log_row) = sheets.appended[-1]
    assert tab == "run_log" and log_row["agent"] == "dashboard-order"


def test_add_manual_order_double_submit_does_not_duplicate():
    # order_id детерминирован от содержимого: даблклик по кнопке шлёт два POST,
    # а строка с деньгами должна появиться одна.
    sheets = FakeSheets({"run_log": []})
    first = add_manual_order(sheets, "2026-07-29", "500", "tiktok-1")
    second = add_manual_order(sheets, "2026-07-29", "500", "tiktok-1")
    assert first.order_id == second.order_id
    assert len(sheets.tables["orders"]) == 1
    # различимый заказ (другой комментарий) — отдельная строка
    add_manual_order(sheets, "2026-07-29", "500", "tiktok-1", notes="второй")
    assert len(sheets.tables["orders"]) == 2


def test_add_manual_order_validation():
    sheets = FakeSheets({"run_log": []})
    for bad_revenue in ("0", "-5", "abc", "", "inf", "nan"):
        with pytest.raises(ValueError):
            add_manual_order(sheets, "2026-07-29", bad_revenue, "tiktok-1")
    for bad_date in ("вчера", "29.07.2026", ""):
        with pytest.raises(ValueError):
            add_manual_order(sheets, bad_date, "500", "tiktok-1")
    assert "orders" not in sheets.tables                # ничего не записано
