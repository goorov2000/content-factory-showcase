"""Pure v2 hiring rules: no Google API, files or subprocesses."""

import pytest

from cf.hiring.rules import APPLICATION_RULESET, application_decision
from cf.hiring.spec import APPLICATION_REQUIRED, SEMANTIC_DIMENSIONS


def _application(**overrides):
    # Ответы на задачи 1-5 — синтетические: условия задач и ключ ответов
    # в витринной копии не публикуются (rules.py проверяет только непустоту).
    data = {
        "email": "producer@example.com",
        "source": "Telegram",
        "name": "Анна",
        "telegram": "@anna",
        "evidence_project": (
            "Лично вела запуск с января по март: baseline 10k просмотров, "
            "после изменений 35k, ссылка https://example.com/case"
        ),
        "additional_project": "",
        "team_size_band": "3–5",
        "weekly_output_band": "20–39",
        "ownership_case": (
            "Цель 20 публикаций. Выбрала пакетную съёмку, на второй день "
            "увидела рост брака, сократила смену, итог — 19 качественных."
        ),
        "evidence_case": (
            "Синтетический ответ на задачу 1 (условие и ключ ответов "
            "в витринной копии не публикуются)."
        ),
        "priority_case": (
            "Синтетический ответ на задачу 2 (условие и ключ ответов "
            "в витринной копии не публикуются)."
        ),
        "numeracy_case": (
            "Синтетический ответ на задачу 3 (условие и ключ ответов "
            "в витринной копии не публикуются)."
        ),
        "integrity_case": (
            "Синтетический ответ на задачу 4 (условие и ключ ответов "
            "в витринной копии не публикуются)."
        ),
        "learning_rule_application": (
            "Синтетический ответ на задачу 5.1 (условие и ключ ответов "
            "в витринной копии не публикуются)."
        ),
        "learning_rule_transfer": (
            "Синтетический ответ на задачу 5.2 (условие и ключ ответов "
            "в витринной копии не публикуются)."
        ),
        "income_expectation": "100000",
        "start_date": "Через неделю",
        "ai_usage": "Не использовал(а)",
        "ai_verification": "Проверила ответы самостоятельно.",
        "completion_time": "",
        "consent": "Согласен",
    }
    data.update(overrides)
    return data


def _semantic(**overrides):
    result = {dimension: 2 for dimension in SEMANTIC_DIMENSIONS}
    result["confidence"] = 0.9
    result.update(overrides)
    return result


def test_required_contract_excludes_only_the_two_optional_fields():
    assert set(APPLICATION_REQUIRED) == {
        key
        for key in _application()
        if key not in {"email", "additional_project", "completion_time"}
    }


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"email": ""}, "missing_email"),
        ({"name": ""}, "missing_name"),
        ({"evidence_project": ""}, "missing_evidence_project"),
        ({"ownership_case": ""}, "missing_ownership_case"),
        ({"evidence_case": ""}, "missing_evidence_case"),
        ({"priority_case": ""}, "missing_priority_case"),
        ({"numeracy_case": ""}, "missing_numeracy_case"),
        ({"integrity_case": ""}, "missing_integrity_case"),
        (
            {"learning_rule_application": ""},
            "missing_learning_rule_application",
        ),
        ({"learning_rule_transfer": ""}, "missing_learning_rule_transfer"),
        ({"ai_verification": ""}, "missing_ai_verification"),
        ({"consent": ""}, "missing_consent"),
    ],
)
def test_structural_omission_needs_completion(override, reason):
    result = application_decision(_application(**override), _semantic())

    assert result["decision"] == "needs_completion"
    assert reason in result["reasons"]
    assert result["scores"] is None
    assert result["ruleset"] == APPLICATION_RULESET


def test_optional_fields_may_be_empty():
    result = application_decision(
        _application(additional_project="", completion_time=""),
        _semantic(),
    )

    assert result["decision"] == "defense_invite"


def test_declared_team_and_output_are_context_not_points():
    low = application_decision(
        _application(team_size_band="0", weekly_output_band="Меньше 10"),
        _semantic(),
    )
    high = application_decision(
        _application(team_size_band="9+", weekly_output_band="70+"),
        _semantic(),
    )

    assert low["total_score"] == high["total_score"] == 14
    assert low["evidence"]["team_size_band"] == "0"
    assert high["evidence"]["weekly_output_band"] == "70+"


def test_missing_semantic_result_goes_to_review_not_rejection():
    result = application_decision(_application(), semantic=None)

    assert result["decision"] == "application_review"
    assert result["reasons"] == ["semantic_missing"]


@pytest.mark.parametrize(
    "semantic",
    [
        _semantic(track_record=3),
        _semantic(track_record=2.0),
        _semantic(confidence=True),
        {key: value for key, value in _semantic().items() if key != "numeracy"},
        {**_semantic(), "extra": 1},
    ],
)
def test_semantic_contract_is_exact_and_malformed_goes_to_review(semantic):
    result = application_decision(_application(), semantic)

    assert result["decision"] == "application_review"
    assert result["reasons"] == ["semantic_invalid"]


def test_pilot_minimum_eleven_with_every_dimension_present_invites_defense():
    result = application_decision(
        _application(),
        _semantic(
            track_record=2,
            ownership=2,
            evidence_reasoning=2,
            prioritization=1,
            numeracy=1,
            integrity_communication=2,
            learning_transfer=1,
            confidence=0.8,
        ),
    )

    assert result["total_score"] == 11
    assert result["decision"] == "defense_invite"
    assert result["reasons"] == ["pilot_minimum_competence_met"]


def test_total_below_eleven_goes_to_review_never_auto_rejects():
    result = application_decision(
        _application(),
        _semantic(
            track_record=2,
            ownership=2,
            evidence_reasoning=1,
            prioritization=1,
            numeracy=1,
            integrity_communication=1,
            learning_transfer=1,
        ),
    )

    assert result["total_score"] == 9
    assert result["decision"] == "application_review"
    assert "total_below_pilot_minimum" in result["reasons"]


def test_zero_dimension_forces_review_even_when_total_is_high():
    result = application_decision(
        _application(),
        _semantic(learning_transfer=0),
    )

    assert result["total_score"] == 12
    assert result["decision"] == "application_review"
    assert "dimension_below_floor" in result["reasons"]


def test_integrity_must_receive_the_full_two_points():
    result = application_decision(
        _application(),
        _semantic(integrity_communication=1),
    )

    assert result["decision"] == "application_review"
    assert "integrity_below_floor" in result["reasons"]


def test_low_confidence_goes_to_review_even_at_max_score():
    result = application_decision(
        _application(),
        _semantic(confidence=0.79),
    )

    assert result["total_score"] == 14
    assert result["decision"] == "application_review"
    assert result["reasons"] == ["semantic_low_confidence"]
