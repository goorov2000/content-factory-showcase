"""Idempotent single-form hiring workflow: response -> decision -> outbox."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from cf.hiring.google import (
    FormsClient,
    HiringSheet,
    normalise_form_response,
    validate_question_contract,
)
from cf.hiring.spec import (
    APPLICATION_TITLES,
    RULESET_VERSION,
    SEMANTIC_DIMENSIONS,
)

_CANDIDATE_FIELDS = (
    "source",
    "name",
    "telegram",
    "evidence_project",
    "additional_project",
    "team_size_band",
    "weekly_output_band",
    "income_expectation",
    "start_date",
    "ai_usage",
    "ai_verification",
    "completion_time",
    "consent",
)
_ASSESSMENT_FIELDS = (
    "ownership_case",
    "evidence_case",
    "priority_case",
    "numeracy_case",
    "integrity_case",
    "learning_rule_application",
    "learning_rule_transfer",
)
_MANUAL_TERMINAL_STATUSES = {
    "defense_invite",
    "interview",
    "offer",
    "hired",
    "rejected",
    "withdrawn",
    "contact_failed",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized_email(value: Any) -> str:
    return str(value or "").strip().casefold()


def candidate_id(response_id: str, email: str = "") -> str:
    # responseId is opaque and avoids deriving an identifier from PII.
    seed = str(response_id or email).strip()
    return "cand-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _response_time(response: Mapping[str, Any]) -> str:
    return str(
        response.get("lastSubmittedTime") or response.get("createTime") or ""
    )


def _sync_key(form_id: str, data: Mapping[str, Any]) -> str:
    # Ruleset is kept in Sync State.value rather than the ingestion identity.
    # A new ruleset may rescore this response, but email still requires a real
    # status transition.
    return (
        f"application:{form_id}:{data.get('response_id', '')}:"
        f"{data.get('submitted_at', '')}"
    )


def _status(decision: Mapping[str, Any], default: str) -> str:
    return str(
        decision.get("status")
        or decision.get("verdict")
        or decision.get("decision")
        or default
    )


def _reasons(decision: Mapping[str, Any]) -> list[str]:
    raw = decision.get("reasons") or decision.get("reason_codes") or []
    if isinstance(raw, str):
        return [raw] if raw else []
    return [str(value) for value in raw if str(value)]


def _evidence(decision: Mapping[str, Any]) -> Any:
    return decision.get("evidence") or decision.get("quotes") or {}


def _score(decision: Mapping[str, Any], dimension: str) -> Any:
    scores = decision.get("scores") or {}
    return scores.get(dimension, "") if isinstance(scores, Mapping) else ""


def _application_template(
    status: str, resources: Mapping[str, str]
) -> tuple[str, str, str] | None:
    if status == "needs_completion":
        form_url = resources.get("APPLICATION_FORM_URL", "")
        return (
            "application_completion",
            "JE LA PECHE — нужно дополнить анкету",
            "Здравствуйте! Спасибо за отклик. В ответе не хватает одного или "
            "нескольких обязательных блоков. Пожалуйста, отправьте единую "
            "анкету ещё раз, заполнив все обязательные поля:\n"
            f"{form_url}",
        )
    if status == "defense_invite":
        return (
            "defense_invite",
            "JE LA PECHE — следующий этап",
            "Здравствуйте! Спасибо за подробную анкету. Приглашаем вас на "
            "короткую структурированную защиту ответов. Свяжемся отдельно, "
            "чтобы согласовать время.",
        )
    return None


class HiringWorkflow:
    def __init__(
        self,
        forms: FormsClient,
        store: HiringSheet,
        *,
        application_rule: Callable[..., Mapping[str, Any]],
        application_judge: Callable[[Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
        ruleset: str = RULESET_VERSION,
    ):
        self.forms = forms
        self.store = store
        self.application_rule = application_rule
        self.application_judge = application_judge
        self.ruleset = ruleset
        self.warnings: list[str] = []

    def _resources(self) -> dict[str, str]:
        resources = self.store.config_values()
        # The fallback keeps an empty pre-cutover installation harmless while
        # the live Config row is renamed.  There is still only one active form.
        form_id = (
            resources.get("APPLICATION_FORM_ID", "")
            or resources.get("SCREENING_FORM_ID", "")
        )
        if not form_id or form_id == "PENDING_OWNER_SETUP":
            raise RuntimeError(
                "APPLICATION_FORM_ID не заполнен: сначала примените "
                "cf.hiring.provision к human-owned Google Form"
            )
        result = dict(resources)
        result["APPLICATION_FORM_ID"] = form_id
        if not result.get("APPLICATION_FORM_URL"):
            result["APPLICATION_FORM_URL"] = result.get("SCREENING_FORM_URL", "")
        return result

    def _enqueue(
        self,
        *,
        decision_id: str | None,
        candidate: str,
        recipient: str,
        template: tuple[str, str, str] | None,
        dry_run: bool,
    ) -> bool:
        # Email is a side effect of a real status transition, never of merely
        # rerunning a ruleset against the same state.
        if not decision_id or not template or not recipient:
            return False
        template_id, subject, body = template
        key = f"{decision_id}:{template_id}"
        return self.store.append_once(
            "Outbox",
            "idempotency_key",
            {
                "idempotency_key": key,
                "candidate_id": candidate,
                "created_at": now_iso(),
                "channel": "email",
                "to": recipient,
                "template": template_id,
                "subject": subject,
                "body": body,
                "state": "pending",
                "attempts": "0",
                "last_error": "",
                "sent_at": "",
            },
            dry_run=dry_run,
        )

    def _decision(
        self,
        *,
        candidate: str,
        old: str,
        new: str,
        event_key: str,
        reasons: list[str],
        evidence: Any,
        dry_run: bool,
    ) -> tuple[str | None, bool]:
        if old == new:
            return None, False
        decision_id = hashlib.sha256(
            (
                f"{candidate}:application:{old}:{new}:"
                f"{event_key}:{self.ruleset}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        created = self.store.append_once(
            "Decisions",
            "decision_id",
            {
                "decision_id": decision_id,
                "candidate_id": candidate,
                "decided_at": now_iso(),
                "stage": "application",
                "from_status": old,
                "to_status": new,
                "reason_codes": _json(reasons),
                "evidence_quotes": _json(evidence),
                "ruleset": self.ruleset,
                "actor": "automation",
            },
            dry_run=dry_run,
        )
        return decision_id, created

    def sync_applications(
        self, resources: Mapping[str, str], *, dry_run: bool = False
    ) -> Counter:
        form_id = resources["APPLICATION_FORM_ID"]
        form = self.forms.get_form(form_id)
        responses = sorted(self.forms.list_responses(form_id), key=_response_time)
        counts: Counter = Counter()
        if not responses:
            return counts

        # A stale form must fail before any candidate row can be written.
        validate_question_contract(form, APPLICATION_TITLES.values())
        title_to_key = {title: key for key, title in APPLICATION_TITLES.items()}
        processed = {
            str(row.get("key") or ""): str(row.get("value") or "")
            for row in self.store.rows("Sync State")
            if str(row.get("key") or "").startswith("application:")
        }
        candidates = self.store.rows("Candidates")
        assessments = self.store.rows("Assessments")
        by_response = {
            str(row.get("application_response_id") or ""): row
            for row in candidates
            if str(row.get("application_response_id") or "")
        }
        by_email = {
            normalized_email(row.get("normalized_email")): row
            for row in candidates
            if normalized_email(row.get("normalized_email"))
        }
        assessment_by_response = {
            str(row.get("application_response_id") or ""): row
            for row in assessments
            if str(row.get("application_response_id") or "")
        }

        for response in responses:
            data = normalise_form_response(form, response, title_to_key)
            event_key = _sync_key(form_id, data)
            if processed.get(event_key) == self.ruleset:
                counts["application_skipped"] += 1
                continue

            response_id = str(data.get("response_id") or "")
            prior_response = by_response.get(response_id)
            if (
                prior_response
                and str(prior_response.get("application_submitted_at") or "")
                == str(data.get("submitted_at") or "")
                and str(prior_response.get("ruleset") or "") == self.ruleset
            ):
                self.store.upsert(
                    "Sync State",
                    "key",
                    {
                        "key": event_key,
                        "value": self.ruleset,
                        "updated_at": now_iso(),
                    },
                    dry_run=dry_run,
                )
                processed[event_key] = self.ruleset
                counts["application_skipped"] += 1
                continue

            email = normalized_email(data.get("email"))
            prior = by_email.get(email, {}) if email else {}
            cid = str(prior.get("candidate_id") or candidate_id(response_id, email))

            initial = dict(self.application_rule(data, semantic=None))
            decision = initial
            if (
                _status(initial, "application_review") != "needs_completion"
                and self.application_judge
            ):
                try:
                    judged = dict(self.application_judge(data))
                    semantic = dict(judged.get("semantic") or judged)
                    decision = dict(self.application_rule(data, semantic=semantic))
                    decision["evidence"] = {
                        **dict(decision.get("evidence") or {}),
                        "judge": judged.get("evidence") or {},
                    }
                except Exception as exc:
                    self.warnings.append(f"{cid}: application judge: {exc}")

            proposed = _status(decision, "application_review")
            old_status = str(prior.get("status") or "")
            status = (
                old_status
                if old_status in _MANUAL_TERMINAL_STATUSES
                else proposed
            )

            candidate_row = dict(prior)
            candidate_row.update(
                {
                    "candidate_id": cid,
                    "normalized_email": email,
                    "application_response_id": response_id,
                    "application_submitted_at": data.get("submitted_at", ""),
                    **{field: data.get(field, "") for field in _CANDIDATE_FIELDS},
                    "total_score": decision.get("total_score", ""),
                    "confidence": decision.get("confidence", ""),
                    "status": status,
                    "decision_reason": _json(
                        {
                            "reasons": _reasons(decision),
                            "evidence": _evidence(decision),
                        }
                    ),
                    "ruleset": self.ruleset,
                    "updated_at": now_iso(),
                }
            )
            assessment = dict(assessment_by_response.get(response_id, {}))
            assessment.update(
                {
                    "candidate_id": cid,
                    "application_response_id": response_id,
                    "normalized_email": email,
                    "submitted_at": data.get("submitted_at", ""),
                    **{field: data.get(field, "") for field in _ASSESSMENT_FIELDS},
                    **{
                        dimension: _score(decision, dimension)
                        for dimension in SEMANTIC_DIMENSIONS
                    },
                    "total_score": decision.get("total_score", ""),
                    "confidence": decision.get("confidence", ""),
                    "status": status,
                    "review_reason": _json(
                        {
                            "reasons": _reasons(decision),
                            "evidence": _evidence(decision),
                        }
                    ),
                    "ruleset": self.ruleset,
                    "updated_at": now_iso(),
                }
            )
            # Append-only event and outbox precede the mutable projections.
            # If a Sheets call fails mid-event, the next run can reconstruct the
            # same IDs while the candidate still exposes the previous status.
            decision_id, decision_created = self._decision(
                candidate=cid,
                old=old_status,
                new=status,
                event_key=event_key,
                reasons=_reasons(decision),
                evidence=_evidence(decision),
                dry_run=dry_run,
            )
            if decision_created:
                counts["decisions"] += 1
            if self._enqueue(
                decision_id=decision_id,
                candidate=cid,
                recipient=email,
                template=_application_template(status, resources),
                dry_run=dry_run,
            ):
                counts["outbox"] += 1

            candidate_result = self.store.upsert(
                "Candidates",
                "candidate_id",
                candidate_row,
                dry_run=dry_run,
            )
            counts[
                "application_created"
                if candidate_result.created
                else "application_seen"
            ] += 1
            if candidate_result.changed:
                counts["application_changed"] += 1

            assessment_result = self.store.upsert(
                "Assessments",
                "application_response_id",
                assessment,
                dry_run=dry_run,
            )
            counts[
                "assessment_created"
                if assessment_result.created
                else "assessment_seen"
            ] += 1

            by_email[email] = candidate_row
            by_response[response_id] = candidate_row
            assessment_by_response[response_id] = assessment
            self.store.upsert(
                "Sync State",
                "key",
                {
                    "key": event_key,
                    "value": self.ruleset,
                    "updated_at": now_iso(),
                },
                dry_run=dry_run,
            )
            processed[event_key] = self.ruleset
        return counts

    def sync(self, *, dry_run: bool = False) -> dict[str, Any]:
        self.warnings = []
        resources = self._resources()
        counts = self.sync_applications(resources, dry_run=dry_run)
        return {
            "dry_run": dry_run,
            "counts": dict(counts),
            "warnings": list(self.warnings),
            "ruleset": self.ruleset,
        }


def status_summary(store: HiringSheet) -> dict[str, Any]:
    candidates = store.rows("Candidates")
    assessments = store.rows("Assessments")
    outbox = store.rows("Outbox")
    return {
        "candidates": len(candidates),
        "candidate_statuses": dict(
            Counter(str(row.get("status") or "empty") for row in candidates)
        ),
        "assessments": len(assessments),
        "assessment_statuses": dict(
            Counter(str(row.get("status") or "empty") for row in assessments)
        ),
        "outbox": dict(Counter(str(row.get("state") or "empty") for row in outbox)),
        "resources": store.config_values(),
    }
