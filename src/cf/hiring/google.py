"""Small Google Forms/Sheets adapters for the hiring workflow.

This module deliberately does not widen ``cf.sheets.SCOPES``. Hiring resources
live in a separate, human-owned spreadsheet and are accessed through their own
credentials and scopes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials

from cf.config import resolve_path

FORMS_BASE = "https://forms.googleapis.com/v1"
FORMS_SCOPES = (
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
)
SHEETS_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


def service_credentials(config: Mapping[str, Any], scopes: Iterable[str]) -> Credentials:
    return Credentials.from_service_account_file(
        resolve_path(config["service_account_file"]), scopes=list(scopes)
    )


def _response_error(response) -> RuntimeError:
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message") or str(payload)
    except Exception:
        message = (getattr(response, "text", "") or "")[:500]
    return RuntimeError(f"Google API HTTP {response.status_code}: {message}")


def _answer_text(answer: Mapping[str, Any]) -> str:
    values = (
        answer.get("textAnswers", {}).get("answers", [])
        if isinstance(answer, Mapping)
        else []
    )
    return "\n".join(
        str(value.get("value", "")).strip()
        for value in values
        if str(value.get("value", "")).strip()
    )


def question_titles(form: Mapping[str, Any]) -> dict[str, str]:
    """Return ``questionId -> item title`` for all simple form questions."""
    titles: dict[str, str] = {}
    for item in form.get("items", []) or []:
        title = str(item.get("title") or "").strip()
        question = item.get("questionItem", {}).get("question", {})
        question_id = str(question.get("questionId") or "").strip()
        if question_id and title:
            titles[question_id] = title
    return titles


def validate_question_contract(
    form: Mapping[str, Any], expected_titles: Iterable[str]
) -> None:
    """Fail loudly when the live Form no longer matches the versioned schema."""
    actual = list(question_titles(form).values())
    expected = [str(title).strip() for title in expected_titles]
    if actual != expected:
        missing = [title for title in expected if title not in actual]
        unexpected = [title for title in actual if title not in expected]
        raise RuntimeError(
            "Google Form question contract mismatch: "
            f"missing={missing}, unexpected={unexpected}, "
            f"order_matches={actual == expected}"
        )


def normalise_form_response(
    form: Mapping[str, Any],
    response: Mapping[str, Any],
    title_to_key: Mapping[str, str],
) -> dict[str, str]:
    """Flatten a Forms response using the current form metadata."""
    by_id = question_titles(form)
    out: dict[str, str] = {
        "response_id": str(response.get("responseId") or ""),
        "submitted_at": str(
            response.get("lastSubmittedTime") or response.get("createTime") or ""
        ),
        "email": str(response.get("respondentEmail") or "").strip(),
    }
    for question_id, answer in (response.get("answers") or {}).items():
        title = by_id.get(str(question_id), "")
        key = title_to_key.get(title)
        if key:
            out[key] = _answer_text(answer)
    return out


class FormsClient:
    def __init__(self, session: AuthorizedSession):
        self.session = session

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "FormsClient":
        creds = service_credentials(config, FORMS_SCOPES)
        return cls(AuthorizedSession(creds))

    def get_form(self, form_id: str) -> dict[str, Any]:
        response = self.session.get(f"{FORMS_BASE}/forms/{form_id}")
        if not response.ok:
            raise _response_error(response)
        return response.json()

    def list_responses(self, form_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        token = ""
        while True:
            params = {"pageSize": 5000}
            if token:
                params["pageToken"] = token
            response = self.session.get(
                f"{FORMS_BASE}/forms/{form_id}/responses", params=params
            )
            if not response.ok:
                raise _response_error(response)
            payload = response.json()
            rows.extend(payload.get("responses", []) or [])
            token = str(payload.get("nextPageToken") or "")
            if not token:
                return rows


@dataclass
class UpsertResult:
    created: bool
    changed: bool
    row_number: int


class HiringSheet:
    """Header-driven, idempotent access to the dedicated hiring spreadsheet."""

    def __init__(self, book):
        self.book = book

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "HiringSheet":
        hiring = config.get("hiring") or {}
        spreadsheet_id = str(hiring.get("spreadsheet_id") or "").strip()
        if not spreadsheet_id:
            raise ValueError("cf.config.json: hiring.spreadsheet_id не заполнен")
        creds = service_credentials(config, SHEETS_SCOPES)
        return cls(gspread.authorize(creds).open_by_key(spreadsheet_id))

    def _worksheet(self, title: str):
        return self.book.worksheet(title)

    def rows(self, title: str) -> list[dict[str, Any]]:
        return self._worksheet(title).get_all_records(
            default_blank="", numericise_ignore=["all"]
        )

    def config_values(self) -> dict[str, str]:
        return {
            str(row.get("key") or "").strip(): str(row.get("value") or "").strip()
            for row in self.rows("Config")
            if str(row.get("key") or "").strip()
        }

    def upsert(
        self, title: str, key: str, row: Mapping[str, Any], *, dry_run: bool = False
    ) -> UpsertResult:
        ws = self._worksheet(title)
        headers = [str(value).strip() for value in ws.row_values(1)]
        if key not in headers:
            raise ValueError(f"{title}: нет ключевой колонки {key!r}")
        unknown = sorted(set(row) - set(headers))
        if unknown:
            raise ValueError(f"{title}: поля вне схемы: {unknown}")
        records = ws.get_all_records(default_blank="", numericise_ignore=["all"])
        target = str(row.get(key) or "")
        existing_index = next(
            (
                index
                for index, record in enumerate(records, start=2)
                if str(record.get(key) or "") == target
            ),
            None,
        )
        values = [row.get(header, "") for header in headers]
        if existing_index is None:
            if not dry_run:
                ws.append_row(values, value_input_option="RAW")
            return UpsertResult(created=True, changed=True, row_number=len(records) + 2)

        existing = records[existing_index - 2]
        changed = any(str(existing.get(h, "")) != str(row.get(h, "")) for h in headers)
        if changed and not dry_run:
            ws.update(
                range_name=f"A{existing_index}",
                values=[values],
                value_input_option="RAW",
            )
        return UpsertResult(created=False, changed=changed, row_number=existing_index)

    def append_once(
        self, title: str, key: str, row: Mapping[str, Any], *, dry_run: bool = False
    ) -> bool:
        ws = self._worksheet(title)
        headers = [str(value).strip() for value in ws.row_values(1)]
        if key not in headers:
            raise ValueError(f"{title}: нет ключевой колонки {key!r}")
        unknown = sorted(set(row) - set(headers))
        if unknown:
            raise ValueError(f"{title}: поля вне схемы: {unknown}")
        target = str(row.get(key) or "")
        records = ws.get_all_records(default_blank="", numericise_ignore=["all"])
        if any(str(record.get(key) or "") == target for record in records):
            return False
        if not dry_run:
            ws.append_row(
                [row.get(header, "") for header in headers],
                value_input_option="RAW",
            )
        return True
