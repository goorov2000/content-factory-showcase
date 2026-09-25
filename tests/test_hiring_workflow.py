from copy import deepcopy

import pytest

from cf.hiring.google import UpsertResult
from cf.hiring.rules import application_decision
from cf.hiring.spec import (
    APPLICATION_TITLES,
    RULESET_VERSION,
    SEMANTIC_DIMENSIONS,
)
from cf.hiring.workflow import HiringWorkflow


class FakeForms:
    def __init__(self, forms, responses):
        self.forms = forms
        self.responses = responses
        self.get_calls = []
        self.response_calls = []

    def get_form(self, form_id):
        self.get_calls.append(form_id)
        return deepcopy(self.forms[form_id])

    def list_responses(self, form_id):
        self.response_calls.append(form_id)
        return deepcopy(self.responses.get(form_id, []))


class MemoryStore:
    def __init__(self, resources, **tabs):
        self.resources = dict(resources)
        self.tabs = {
            "Candidates": [],
            "Assessments": [],
            "Decisions": [],
            "Outbox": [],
            "Sync State": [],
            **{key: deepcopy(value) for key, value in tabs.items()},
        }

    def config_values(self):
        return dict(self.resources)

    def rows(self, title):
        return deepcopy(self.tabs.get(title, []))

    def upsert(self, title, key, row, *, dry_run=False):
        rows = self.tabs.setdefault(title, [])
        index = next(
            (
                i
                for i, existing in enumerate(rows)
                if str(existing.get(key, "")) == str(row.get(key, ""))
            ),
            None,
        )
        if index is None:
            if not dry_run:
                rows.append(deepcopy(dict(row)))
            return UpsertResult(True, True, len(rows) + 1)
        changed = rows[index] != dict(row)
        if changed and not dry_run:
            rows[index] = deepcopy(dict(row))
        return UpsertResult(False, changed, index + 2)

    def append_once(self, title, key, row, *, dry_run=False):
        rows = self.tabs.setdefault(title, [])
        if any(
            str(existing.get(key, "")) == str(row.get(key, ""))
            for existing in rows
        ):
            return False
        if not dry_run:
            rows.append(deepcopy(dict(row)))
        return True


def _form(titles=APPLICATION_TITLES):
    return {
        "items": [
            {
                "title": title,
                "questionItem": {"question": {"questionId": key}},
            }
            for key, title in titles.items()
        ]
    }


def _response(response_id, email, values, *, submitted="2026-07-31T12:00:00Z"):
    return {
        "responseId": response_id,
        "lastSubmittedTime": submitted,
        "respondentEmail": email,
        "answers": {
            key: {"textAnswers": {"answers": [{"value": str(value)}]}}
            for key, value in values.items()
        },
    }


def _resources():
    return {
        "APPLICATION_FORM_ID": "application",
        "APPLICATION_FORM_URL": "https://forms.example/application",
    }


def _values(**overrides):
    # Ответы на задачи — синтетические: условия и ключ ответов в витринной
    # копии не публикуются; workflow проверяет только непустоту полей.
    values = {
        "source": "Telegram",
        "name": "Анна",
        "telegram": "@anna",
        "evidence_project": "Лично подняла baseline 10 до 30 за три месяца.",
        "additional_project": "",
        "team_size_band": "3–5",
        "weekly_output_band": "20–39",
        "ownership_case": "Увидела ранний сигнал, изменила решение, получила результат.",
        "evidence_case": "Синтетический ответ на задачу 1.",
        "priority_case": "Синтетический ответ на задачу 2.",
        "numeracy_case": "Синтетический ответ на задачу 3.",
        "integrity_case": "Синтетический ответ на задачу 4.",
        "learning_rule_application": "Синтетический ответ на задачу 5.1.",
        "learning_rule_transfer": "Синтетический ответ на задачу 5.2.",
        "income_expectation": "100000",
        "start_date": "Через неделю",
        "ai_usage": "Не использовал(а)",
        "ai_verification": "Проверила ответы сама.",
        "completion_time": "",
        "consent": "Согласен",
    }
    values.update(overrides)
    return values


def _semantic(**overrides):
    result = {dimension: 2 for dimension in SEMANTIC_DIMENSIONS}
    result["confidence"] = 0.9
    result.update(overrides)
    return result


def _judge(semantic=None):
    value = semantic or _semantic()
    return lambda data: {
        "semantic": deepcopy(value),
        "evidence": {
            dimension: {"quote": data["evidence_project"]}
            for dimension in SEMANTIC_DIMENSIONS
        },
    }


def _workflow(
    responses=(),
    *,
    candidates=(),
    assessments=(),
    sync_state=(),
    judge=None,
    ruleset=RULESET_VERSION,
    form=None,
):
    forms = FakeForms(
        {"application": form or _form()},
        {"application": list(responses)},
    )
    store = MemoryStore(
        _resources(),
        Candidates=list(candidates),
        Assessments=list(assessments),
        **{"Sync State": list(sync_state)},
    )
    workflow = HiringWorkflow(
        forms,
        store,
        application_rule=application_decision,
        application_judge=_judge() if judge is None else judge,
        ruleset=ruleset,
    )
    return workflow, store, forms


def test_one_response_creates_candidate_assessment_decision_and_one_email():
    response = _response("app-1", "ANNA@example.com", _values())
    workflow, store, forms = _workflow([response])
    calls = []
    original = workflow.application_judge
    workflow.application_judge = lambda data: (
        calls.append(data["response_id"]) or original(data)
    )

    first = workflow.sync()
    second = workflow.sync()

    assert first["counts"]["application_created"] == 1
    assert first["counts"]["assessment_created"] == 1
    assert calls == ["app-1"]
    assert forms.get_calls == ["application", "application"]
    assert forms.response_calls == ["application", "application"]
    candidate = store.tabs["Candidates"][0]
    assert candidate["normalized_email"] == "anna@example.com"
    assert candidate["status"] == "defense_invite"
    assert candidate["total_score"] == 14
    assert store.tabs["Assessments"][0]["learning_transfer"] == 2
    assert len(store.tabs["Decisions"]) == 1
    assert [row["template"] for row in store.tabs["Outbox"]] == [
        "defense_invite"
    ]
    assert second["counts"] == {"application_skipped": 1}


def test_structural_gap_needs_completion_without_calling_judge():
    response = _response(
        "app-2",
        "x@example.com",
        _values(learning_rule_transfer=""),
    )
    calls = []
    workflow, store, _ = _workflow([response], judge=lambda data: calls.append(data))

    workflow.sync()

    assert calls == []
    assert store.tabs["Candidates"][0]["status"] == "needs_completion"
    assert "missing_learning_rule_transfer" in store.tabs["Candidates"][0][
        "decision_reason"
    ]
    assert store.tabs["Outbox"][0]["template"] == "application_completion"


def test_weak_semantic_result_is_review_never_auto_rejection():
    response = _response("app-3", "x@example.com", _values())
    workflow, store, _ = _workflow(
        [response],
        judge=_judge(
            _semantic(
                track_record=1,
                ownership=1,
                evidence_reasoning=1,
                prioritization=1,
                numeracy=0,
                integrity_communication=1,
                learning_transfer=0,
            )
        ),
    )

    workflow.sync()

    assert store.tabs["Candidates"][0]["status"] == "application_review"
    assert store.tabs["Outbox"] == []


def test_judge_failure_fails_closed_to_review_without_email():
    response = _response("app-4", "x@example.com", _values())

    def broken(_data):
        raise RuntimeError("judge unavailable")

    workflow, store, _ = _workflow([response], judge=broken)
    result = workflow.sync()

    assert store.tabs["Candidates"][0]["status"] == "application_review"
    assert store.tabs["Outbox"] == []
    assert result["warnings"] and "judge unavailable" in result["warnings"][0]


def test_same_email_new_response_reuses_candidate_and_keeps_both_assessments():
    older = _response(
        "app-old",
        "anna@example.com",
        _values(name="Анна старая"),
        submitted="2026-07-31T10:00:00Z",
    )
    newer = _response(
        "app-new",
        "ANNA@example.com",
        _values(name="Анна новая"),
        submitted="2026-07-31T11:00:00Z",
    )
    workflow, store, _ = _workflow([newer, older])

    workflow.sync()

    assert len(store.tabs["Candidates"]) == 1
    assert store.tabs["Candidates"][0]["name"] == "Анна новая"
    assert store.tabs["Candidates"][0]["application_response_id"] == "app-new"
    assert len(store.tabs["Assessments"]) == 2
    # The first transition already invited the candidate; the second equal
    # outcome cannot enqueue another message.
    assert len(store.tabs["Outbox"]) == 1


def test_edited_response_version_updates_once_and_can_transition_from_review():
    weak = _response(
        "app-edit",
        "anna@example.com",
        _values(name="До правки"),
        submitted="2026-07-31T10:00:00Z",
    )
    strong = _response(
        "app-edit",
        "anna@example.com",
        _values(name="После правки"),
        submitted="2026-07-31T11:00:00Z",
    )
    judgments = iter(
        [
            _judge(_semantic(numeracy=0)),
            _judge(),
        ]
    )
    workflow, store, _ = _workflow(
        [strong, weak],
        judge=lambda data: next(judgments)(data),
    )

    workflow.sync()
    workflow.sync()

    assert len(store.tabs["Candidates"]) == 1
    assert len(store.tabs["Assessments"]) == 1
    assert store.tabs["Candidates"][0]["name"] == "После правки"
    assert store.tabs["Candidates"][0]["status"] == "defense_invite"
    keys = [row["key"] for row in store.tabs["Sync State"]]
    assert len([key for key in keys if key.startswith("application:")]) == 2
    assert len(store.tabs["Outbox"]) == 1


def test_ruleset_bump_rescores_but_does_not_duplicate_email_without_transition():
    response = _response("app-5", "anna@example.com", _values())
    workflow, store, forms = _workflow([response])
    workflow.sync()
    original_outbox = deepcopy(store.tabs["Outbox"])
    calls = []

    next_workflow = HiringWorkflow(
        forms,
        store,
        application_rule=application_decision,
        application_judge=lambda data: (
            calls.append(data["response_id"]) or _judge()(data)
        ),
        ruleset="hiring-v3-future",
    )
    result = next_workflow.sync()

    assert result["counts"]["application_seen"] == 1
    assert calls == ["app-5"]
    assert store.tabs["Outbox"] == original_outbox
    assert len(store.tabs["Decisions"]) == 1


def test_retry_recovers_outbox_after_failure_without_duplicate_decision():
    response = _response("app-retry", "anna@example.com", _values())
    workflow, store, _ = _workflow([response])
    original = store.append_once
    failed = {"value": False}

    def flaky(title, key, row, *, dry_run=False):
        if title == "Outbox" and not failed["value"]:
            failed["value"] = True
            raise RuntimeError("temporary outbox failure")
        return original(title, key, row, dry_run=dry_run)

    store.append_once = flaky

    with pytest.raises(RuntimeError, match="temporary outbox failure"):
        workflow.sync()
    assert len(store.tabs["Decisions"]) == 1
    assert store.tabs["Candidates"] == []
    assert store.tabs["Sync State"] == []

    workflow.sync()

    assert len(store.tabs["Decisions"]) == 1
    assert len(store.tabs["Outbox"]) == 1
    assert len(store.tabs["Candidates"]) == 1
    assert len(store.tabs["Sync State"]) == 1


def test_live_form_contract_mismatch_fails_before_any_sheet_write():
    broken_titles = dict(APPLICATION_TITLES)
    broken_titles.pop("learning_rule_transfer")
    response = _response("app-6", "x@example.com", _values())
    workflow, store, _ = _workflow([response], form=_form(broken_titles))

    with pytest.raises(RuntimeError, match="question contract mismatch"):
        workflow.sync()

    assert store.tabs["Candidates"] == []
    assert store.tabs["Assessments"] == []
    assert store.tabs["Sync State"] == []


def test_empty_legacy_form_is_harmless_before_external_cutover():
    resources = {
        "SCREENING_FORM_ID": "application",
        "SCREENING_FORM_URL": "https://forms.example/legacy",
    }
    forms = FakeForms({"application": _form({})}, {"application": []})
    store = MemoryStore(resources)
    workflow = HiringWorkflow(
        forms,
        store,
        application_rule=application_decision,
        application_judge=_judge(),
    )

    assert workflow.sync()["counts"] == {}
    assert store.tabs["Candidates"] == []


def test_dry_run_does_not_mutate_any_tab():
    response = _response("app-7", "x@example.com", _values())
    workflow, store, _ = _workflow([response])

    result = workflow.sync(dry_run=True)

    assert result["counts"]["application_created"] == 1
    assert all(rows == [] for rows in store.tabs.values())
