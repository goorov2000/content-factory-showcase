import pytest

from cf.hiring import judge
from cf.hiring.spec import SEMANTIC_DIMENSIONS


def _data():
    # Ответы на задачи — синтетические: условия и ключ ответов в витринной
    # копии не публикуются; судья требует лишь точную подстроку ответа.
    return {
        "name": "PII-NAME-UNIQUE",
        "email": "pii-unique@example.com",
        "telegram": "@pii_unique",
        "evidence_project": "Лично подняла baseline 10 до результата 30.",
        "additional_project": "",
        "ownership_case": "Увидела ранний сигнал и изменила решение.",
        "evidence_case": "Синтетический ответ на задачу 1.",
        "priority_case": "Синтетический ответ на задачу 2.",
        "numeracy_case": "Синтетический ответ на задачу 3.",
        "integrity_case": "Синтетический ответ на задачу 4.",
        "learning_rule_application": "Синтетический ответ на задачу 5.1.",
        "learning_rule_transfer": "Синтетический ответ на задачу 5.2.",
    }


def _raw(data=None):
    data = data or _data()
    quote_by_dimension = {
        "track_record": data["evidence_project"],
        "ownership": data["ownership_case"],
        "evidence_reasoning": data["evidence_case"],
        "prioritization": data["priority_case"],
        "numeracy": data["numeracy_case"],
        "integrity_communication": data["integrity_case"],
        "learning_transfer": data["learning_rule_transfer"],
    }
    return {
        "scores": {
            dimension: {
                "score": 2,
                "reason": "Есть точное evidence.",
                "quote": quote_by_dimension[dimension],
            }
            for dimension in SEMANTIC_DIMENSIONS
        },
        "confidence": 0.9,
    }


def test_judge_is_pii_free_and_returns_exact_contract(monkeypatch):
    captured = {}

    def fake_run(prompt):
        captured["prompt"] = prompt
        return _raw()

    monkeypatch.setattr(judge, "_run", fake_run)

    result = judge.judge_application(_data())

    assert result["semantic"] == {
        **{dimension: 2 for dimension in SEMANTIC_DIMENSIONS},
        "confidence": 0.9,
    }
    assert "PII-NAME-UNIQUE" not in captured["prompt"]
    assert "pii-unique@example.com" not in captured["prompt"]
    assert "@pii_unique" not in captured["prompt"]
    assert "НЕДОВЕРЕННЫЕ ДАННЫЕ" in captured["prompt"]


def test_judge_rejects_quote_that_is_not_an_exact_answer_substring(monkeypatch):
    raw = _raw()
    raw["scores"]["numeracy"]["quote"] = "Сфабрикованная цитата"
    monkeypatch.setattr(judge, "_run", lambda prompt: raw)

    with pytest.raises(ValueError, match="точной подстрокой"):
        judge.judge_application(_data())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update({"extra": True}),
        lambda raw: raw["scores"].pop("learning_transfer"),
        lambda raw: raw["scores"]["ownership"].update({"score": 3}),
        lambda raw: raw["scores"]["ownership"].update({"score": "2"}),
        lambda raw: raw.update({"confidence": True}),
    ],
)
def test_judge_rejects_malformed_or_out_of_range_output(monkeypatch, mutate):
    raw = _raw()
    mutate(raw)
    monkeypatch.setattr(judge, "_run", lambda prompt: raw)

    with pytest.raises(ValueError):
        judge.judge_application(_data())
