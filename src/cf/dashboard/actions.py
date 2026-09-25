import logging
import math
from datetime import datetime
from typing import NamedTuple

from cf.collect.metrika import ORDER_HEADERS
from cf.collect.util import num, stable_hash
from cf.runlog import log_run, now_iso
from cf.sheets import UnknownFieldsError

logger = logging.getLogger(__name__)

VALID_DECISIONS = {"approved", "rejected", "pending"}

# Статусы реестра заказов (UTM-контур): candidate ставит сборщик Метрики,
# остальные — ТОЛЬКО человек кнопками в пульте. «Передумать» легально в любую
# сторону, включая возврат в кандидаты (зеркало decision=pending у ревью
# сценариев): сборщик решённые строки не трогает, так что смена статуса
# происходит только здесь.
VALID_ORDER_STATUSES = ("candidate", "confirmed", "returned", "rejected")


class ReviewResult(NamedTuple):
    """Композитный итог решения оператора: применено ли оно и записан ли след.

    found и logged — независимые исходы. Решение может примениться в Sheets (found),
    а запись в CF Run Log сорваться (logged=False): сбой лога НЕ откатывает решение,
    но и не должен теряться молча — вызывающий слой обязан показать это оператору
    (как runner._finish честно показывает «запись в Run Log не удалась» в UI)."""
    found: bool
    logged: bool


def review_brief(sheets, brief_id, decision, notes="", reason_code=""):
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}")
    if decision == "pending":
        # отмена решения: заметку ревьюера не трогаем — это история, зачем бриф одобряли
        fields = {"review_status": "pending", "rejection_reason": ""}
    else:
        # P5.12: при reject код причины из дропдауна кладётся префиксом [code] в
        # rejection_reason (ключ группировки rejection-history), свободный текст
        # ревьюера остаётся в reviewer_notes. Нет/неизвестный код — мягкая деградация
        # к прежнему поведению (заметка становится причиной): решение не теряется и
        # форма не падает, хотя повторы такой причины не копятся.
        rejection_reason = ""
        if decision == "rejected":
            from cf.reasons import format_reason, is_reason_code
            rejection_reason = (format_reason(reason_code)
                                if is_reason_code(reason_code) else notes)
        fields = {
            "review_status": decision,
            "rejection_reason": rejection_reason,
            "reviewer_notes": notes,
        }
    # reviewed_at — момент решения (M31: кап auto-approve считает по нему);
    # optional-поле: до появления колонки в briefs запись — no-op.
    reviewed_at = "" if decision == "pending" else now_iso()
    try:
        found = sheets.update_row_fields("briefs", "brief_id", brief_id, fields,
                                         optional_fields={"reviewed_at": reviewed_at})
    except UnknownFieldsError:
        # В таблице briefs нет нужных колонок — решение НЕ сохранено. Всплываем
        # ошибкой, а не рапортуем «сохранено»: молчаливая потеря решения недопустима.
        logger.warning("review_brief: решение по %s не сохранено — колонок нет в briefs",
                       brief_id, exc_info=True)
        raise
    if not found:
        return ReviewResult(found=False, logged=False)
    try:
        log_run(sheets, agent="dashboard-review", status="success",
                input_summary=f"{brief_id}: {decision}", trigger_type="dashboard")
        logged = True
    except Exception:
        # Решение уже в Sheets — сбой лога его не откатывает. Но и не глотаем молча:
        # возвращаем logged=False, чтобы post_review показал предупреждение оператору.
        logger.warning("dashboard-review: failed to append run log entry", exc_info=True)
        logged = False
    return ReviewResult(found=True, logged=logged)


def assign_creator(sheets, brief_id, slot):
    """Назначить исполнителя сценарию; пустой слот снимает назначение.

    Колонки ОБЯЗАТЕЛЬНЫЕ, а не optional_fields (как reviewed_at у ревью): здесь
    назначение — суть действия, и молча потерять его нельзя. Нет колонок в листе
    — UnknownFieldsError наверх, продюсер видит ошибку. У необязательных полей
    вроде даты ревью no-op допустим, здесь — нет.

    Сценарий из очереди съёмки НЕ уходит: метка «криэйтор-1, с 28.07» остаётся
    рядом с ним, назначение можно сменить и снять (решение владельца 2026-07-28).
    """
    slot = str(slot or "").strip()
    # Снятие обнуляет и дату: «слот пуст, а дата назначения есть» — состояние,
    # которого не бывает, и оно врало бы плитке «Ждут назначения».
    fields = {"creator_slot": slot, "assigned_at": now_iso() if slot else ""}
    try:
        found = sheets.update_row_fields("briefs", "brief_id", brief_id, fields)
    except UnknownFieldsError:
        logger.warning("assign_creator: назначение %s не сохранено — колонок нет "
                       "в briefs", brief_id, exc_info=True)
        raise
    if not found:
        return ReviewResult(found=False, logged=False)
    try:
        log_run(sheets, agent="dashboard-assign", status="success",
                input_summary=f"{brief_id}: {slot or 'назначение снято'}",
                trigger_type="dashboard")
        logged = True
    except Exception:
        # Назначение уже в Sheets — сбой лога его не откатывает, но и не глотается.
        logger.warning("dashboard-assign: failed to append run log entry",
                       exc_info=True)
        logged = False
    return ReviewResult(found=True, logged=logged)


# ── Реестр заказов (UTM-контур, тикет 03) ────────────────────────────────────


def _log_order_action(sheets, summary):
    """След решения по заказу в CF Run Log; сбой лога решение не откатывает."""
    try:
        log_run(sheets, agent="dashboard-order", status="success",
                input_summary=summary, trigger_type="dashboard")
        return True
    except Exception:
        logger.warning("dashboard-order: failed to append run log entry",
                       exc_info=True)
        return False


def set_order_status(sheets, order_id, status):
    """Решение человека по заказу: статус + момент решения + подпись decided_by.

    Подтверждённая выручка — единственное, с чего считаются 10% продюсера,
    поэтому колонки ОБЯЗАТЕЛЬНЫЕ (как у assign_creator): нет колонок в листе —
    UnknownFieldsError наверх, а не молча потерянное решение о деньгах.
    """
    if status not in VALID_ORDER_STATUSES:
        raise ValueError(f"status must be one of {list(VALID_ORDER_STATUSES)}, "
                         f"got {status!r}")
    fields = {"status": status, "status_changed_at": now_iso(),
              "decided_by": "human"}
    try:
        found = sheets.update_row_fields("orders", "order_id", order_id, fields)
    except UnknownFieldsError:
        logger.warning("set_order_status: решение по %s не сохранено — колонок "
                       "нет в orders", order_id, exc_info=True)
        raise
    if not found:
        return ReviewResult(found=False, logged=False)
    return ReviewResult(found=True,
                        logged=_log_order_action(sheets, f"{order_id}: {status}"))


class ManualOrderResult(NamedTuple):
    order_id: str
    logged: bool


def add_manual_order(sheets, order_date, revenue, account, notes=""):
    """Ручной заказ владельца — сразу confirmed (e-commerce событие не дошло,
    а 10% продюсера с продажи всё равно считаются).

    Валидация даты и суммы здесь (ValueError -> 422 в роуте); аккаунт по
    реестру проверяет роут — реестр живёт у приложения. order_id детерминирован
    от содержимого: даблклик по кнопке шлёт два POST, а строка с деньгами
    должна появиться одна (upsert по ключу). Два неразличимых заказа одного дня
    различаются комментарием. Сборщик Метрики ручные строки не трогает никогда
    (order_merge), так что запись отсюда — окончательная до решения человека.
    """
    order_date = str(order_date or "").strip()
    try:
        datetime.strptime(order_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"дата заказа должна быть в формате ГГГГ-ММ-ДД, "
                         f"получено {order_date!r}")
    try:
        value = float(str(revenue).strip().replace(",", "."))
    except ValueError:
        raise ValueError(f"сумма заказа — не число: {revenue!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"сумма заказа должна быть больше нуля, "
                         f"получено {revenue!r}")
    value = num(value)                # канонизация 1990.0 -> 1990, как у сборщика
    account = str(account or "").strip()
    notes = str(notes or "").strip()
    when = now_iso()
    order_id = "manual-" + stable_hash(f"{order_date}|{value}|{account}|{notes}")
    row = {"order_id": order_id, "source": "manual", "order_date": order_date,
           "revenue": value, "utm_source": "", "utm_campaign": "",
           "utm_content": "", "account": account, "reel_id": "",
           "status": "confirmed", "status_changed_at": when,
           "decided_by": "human", "notes": notes, "collected_at": when}
    # Вкладки может ещё не быть (сбор Метрики не запускался) — создаём её сами,
    # той же схемой, что у сборщика (единственный источник схемы — ORDER_HEADERS).
    sheets.ensure_tab("orders", ORDER_HEADERS)
    sheets.upsert_rows("orders", "order_id", [row])
    logged = _log_order_action(
        sheets, f"{order_id}: ручной заказ {value} ({account})")
    return ManualOrderResult(order_id=order_id, logged=logged)
