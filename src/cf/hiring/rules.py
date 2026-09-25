"""Deterministic contract around the evidence-based application judge.

The module has no Google, subprocess or filesystem dependencies.  Structural
omissions request completion; semantic uncertainty always goes to human review.
There is no semantic auto-rejection in the v2 pilot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cf.hiring.spec import (
    APPLICATION_REQUIRED,
    RULESET_VERSION,
    SEMANTIC_DIMENSIONS,
)

APPLICATION_RULESET = RULESET_VERSION
DIMENSION_MAX = 2
PILOT_MIN_TOTAL = 11
PILOT_MIN_DIMENSION = 1
PILOT_MIN_INTEGRITY = 2
MIN_SEMANTIC_CONFIDENCE = 0.8


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_present(item) for item in value)
    return bool(value)


def _normalise_semantic(semantic: Any) -> dict[str, Any] | None:
    if not isinstance(semantic, Mapping):
        return None
    if set(semantic) != {*SEMANTIC_DIMENSIONS, "confidence"}:
        return None

    scores: dict[str, int] = {}
    for dimension in SEMANTIC_DIMENSIONS:
        value = semantic.get(dimension)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if not 0 <= value <= DIMENSION_MAX:
            return None
        scores[dimension] = value

    confidence = semantic.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        return None
    return {**scores, "confidence": float(confidence)}


def application_decision(
    data: Mapping[str, Any] | None,
    semantic: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply v2 structural gates and the interview pilot threshold.

    A complete application reaches ``defense_invite`` only at the pilot
    minimum-competence threshold: 11+ of 14, every dimension at least 1,
    integrity exactly 2 and confidence at least 0.8.  This is not a target
    rejection rate.  Every other semantic outcome is ``application_review``.
    """
    data = data if isinstance(data, Mapping) else {}
    required_fields = {
        field: _present(data.get(field)) for field in APPLICATION_REQUIRED
    }
    reasons = [
        f"missing_{field}"
        for field, present in required_fields.items()
        if not present
    ]
    email_present = _present(data.get("email"))
    if not email_present:
        reasons.insert(0, "missing_email")

    evidence: dict[str, Any] = {
        "email_present": email_present,
        "required_fields": required_fields,
        # Context only: neither declared band contributes points.
        "team_size_band": data.get("team_size_band", ""),
        "weekly_output_band": data.get("weekly_output_band", ""),
    }
    if reasons:
        return {
            "decision": "needs_completion",
            "scores": None,
            "total_score": None,
            "confidence": None,
            "evidence": evidence,
            "reasons": reasons,
            "ruleset": APPLICATION_RULESET,
        }

    if semantic is None:
        return {
            "decision": "application_review",
            "scores": None,
            "total_score": None,
            "confidence": None,
            "evidence": {**evidence, "semantic_scores": None},
            "reasons": ["semantic_missing"],
            "ruleset": APPLICATION_RULESET,
        }

    normalized = _normalise_semantic(semantic)
    if normalized is None:
        return {
            "decision": "application_review",
            "scores": None,
            "total_score": None,
            "confidence": None,
            "evidence": {**evidence, "semantic_scores": None},
            "reasons": ["semantic_invalid"],
            "ruleset": APPLICATION_RULESET,
        }

    scores = {dimension: normalized[dimension] for dimension in SEMANTIC_DIMENSIONS}
    confidence = normalized["confidence"]
    total = sum(scores.values())
    threshold_reasons: list[str] = []
    if total < PILOT_MIN_TOTAL:
        threshold_reasons.append("total_below_pilot_minimum")
    below_floor = [
        dimension
        for dimension, score in scores.items()
        if score < PILOT_MIN_DIMENSION
    ]
    if below_floor:
        threshold_reasons.append("dimension_below_floor")
    if scores["integrity_communication"] < PILOT_MIN_INTEGRITY:
        threshold_reasons.append("integrity_below_floor")
    if confidence < MIN_SEMANTIC_CONFIDENCE:
        threshold_reasons.append("semantic_low_confidence")

    if threshold_reasons:
        decision = "application_review"
        decision_reasons = threshold_reasons
    else:
        decision = "defense_invite"
        decision_reasons = ["pilot_minimum_competence_met"]

    return {
        "decision": decision,
        "scores": scores,
        "total_score": total,
        "confidence": confidence,
        "evidence": {
            **evidence,
            "semantic_scores": scores,
            "semantic_confidence": confidence,
        },
        "reasons": decision_reasons,
        "ruleset": APPLICATION_RULESET,
    }
