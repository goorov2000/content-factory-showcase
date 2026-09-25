"""Google hiring adapters: local boundary fakes, no credentials or network."""

import pytest

from cf.hiring.google import (
    FormsClient,
    HiringSheet,
    normalise_form_response,
    question_titles,
    validate_question_contract,
)


class FakeResponse:
    def __init__(self, payload, *, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self.responses.pop(0)


def test_forms_client_collects_all_response_pages():
    session = FakeSession(
        FakeResponse({
            "responses": [{"responseId": "r-1"}, {"responseId": "r-2"}],
            "nextPageToken": "page-2",
        }),
        FakeResponse({"responses": [{"responseId": "r-3"}]}),
    )

    rows = FormsClient(session).list_responses("screening-form")

    assert rows == [
        {"responseId": "r-1"},
        {"responseId": "r-2"},
        {"responseId": "r-3"},
    ]
    assert session.calls == [
        {
            "url": (
                "https://forms.googleapis.com/v1/forms/"
                "screening-form/responses"
            ),
            "params": {"pageSize": 5000},
        },
        {
            "url": (
                "https://forms.googleapis.com/v1/forms/"
                "screening-form/responses"
            ),
            "params": {"pageSize": 5000, "pageToken": "page-2"},
        },
    ]


def test_question_titles_and_response_normalisation_use_form_metadata():
    form = {
        "items": [
            {
                "title": " Имя ",
                "questionItem": {"question": {"questionId": "q-name"}},
            },
            {
                "title": "Опыт",
                "questionItem": {"question": {"questionId": "q-experience"}},
            },
            {"title": "Заголовок раздела"},
            {
                "title": "",
                "questionItem": {"question": {"questionId": "q-untitled"}},
            },
        ]
    }
    response = {
        "responseId": "response-17",
        "createTime": "2026-07-30T08:00:00Z",
        "lastSubmittedTime": "2026-07-30T08:05:00Z",
        "respondentEmail": " producer@example.com ",
        "answers": {
            "q-name": {
                "textAnswers": {"answers": [{"value": " Анна "}]}
            },
            "q-experience": {
                "textAnswers": {
                    "answers": [
                        {"value": "Три криэйтера"},
                        {"value": "25 роликов в неделю"},
                    ]
                }
            },
            "q-unmapped": {
                "textAnswers": {"answers": [{"value": "не переносить"}]}
            },
        },
    }

    assert question_titles(form) == {
        "q-name": "Имя",
        "q-experience": "Опыт",
    }
    assert normalise_form_response(
        form,
        response,
        {"Имя": "name", "Опыт": "management"},
    ) == {
        "response_id": "response-17",
        "submitted_at": "2026-07-30T08:05:00Z",
        "email": "producer@example.com",
        "name": "Анна",
        "management": "Три криэйтера\n25 роликов в неделю",
    }


def test_question_contract_ignores_sections_but_requires_exact_question_order():
    form = {
        "items": [
            {"title": "Раздел", "pageBreakItem": {}},
            {
                "title": "Первый",
                "questionItem": {"question": {"questionId": "q-1"}},
            },
            {
                "title": "Второй",
                "questionItem": {"question": {"questionId": "q-2"}},
            },
        ]
    }

    validate_question_contract(form, ["Первый", "Второй"])

    with pytest.raises(RuntimeError, match="question contract mismatch"):
        validate_question_contract(form, ["Второй", "Первый"])


def test_question_contract_rejects_duplicate_or_missing_title():
    form = {
        "items": [
            {
                "title": "Одинаковый",
                "questionItem": {"question": {"questionId": "q-1"}},
            },
            {
                "title": "Одинаковый",
                "questionItem": {"question": {"questionId": "q-2"}},
            },
        ]
    }

    with pytest.raises(RuntimeError, match="question contract mismatch"):
        validate_question_contract(form, ["Одинаковый", "Другой"])


@pytest.mark.parametrize("operation", ["get_form", "list_responses"])
def test_forms_client_surfaces_google_http_error(operation):
    session = FakeSession(
        FakeResponse(
            {"error": {"message": "permission denied"}},
            status_code=403,
        )
    )
    client = FormsClient(session)

    with pytest.raises(
        RuntimeError,
        match=r"Google API HTTP 403: permission denied",
    ):
        getattr(client, operation)("form-1")


class FakeWorksheet:
    def __init__(self, headers, records=None):
        self.headers = list(headers)
        self.records = [dict(row) for row in (records or [])]
        self.append_calls = []
        self.update_calls = []

    def row_values(self, row_number):
        assert row_number == 1
        return list(self.headers)

    def get_all_records(self, **kwargs):
        assert kwargs == {
            "default_blank": "",
            "numericise_ignore": ["all"],
        }
        return [dict(row) for row in self.records]

    def append_row(self, values, *, value_input_option):
        values = list(values)
        self.append_calls.append({
            "values": values,
            "value_input_option": value_input_option,
        })
        self.records.append(dict(zip(self.headers, values)))

    def update(self, *, range_name, values, value_input_option):
        matrix = [list(row) for row in values]
        self.update_calls.append({
            "range_name": range_name,
            "values": matrix,
            "value_input_option": value_input_option,
        })
        row_number = int(range_name[1:])
        self.records[row_number - 2] = dict(zip(self.headers, matrix[0]))


class FakeBook:
    def __init__(self, **worksheets):
        self.worksheets = worksheets

    def worksheet(self, title):
        return self.worksheets[title]


def _sheet(records=None):
    worksheet = FakeWorksheet(
        ["candidate_id", "status", "name"],
        records=records,
    )
    return HiringSheet(FakeBook(Candidates=worksheet)), worksheet


def test_hiring_sheet_upsert_creates_row_in_header_order():
    sheet, worksheet = _sheet()

    result = sheet.upsert(
        "Candidates",
        "candidate_id",
        {
            "name": "Анна",
            "candidate_id": "candidate-1",
            "status": "application_review",
        },
    )

    assert result.created is True
    assert result.changed is True
    assert result.row_number == 2
    assert worksheet.append_calls == [{
        "values": ["candidate-1", "application_review", "Анна"],
        "value_input_option": "RAW",
    }]
    assert worksheet.update_calls == []


def test_hiring_sheet_upsert_updates_existing_row_in_header_order():
    sheet, worksheet = _sheet([{
        "candidate_id": "candidate-1",
        "status": "application_review",
        "name": "Анна",
    }])

    result = sheet.upsert(
        "Candidates",
        "candidate_id",
        {
            "status": "defense_invite",
            "name": "Анна",
            "candidate_id": "candidate-1",
        },
    )

    assert result.created is False
    assert result.changed is True
    assert result.row_number == 2
    assert worksheet.update_calls == [{
        "range_name": "A2",
        "values": [["candidate-1", "defense_invite", "Анна"]],
        "value_input_option": "RAW",
    }]
    assert worksheet.append_calls == []


def test_hiring_sheet_upsert_leaves_identical_row_untouched():
    row = {
        "candidate_id": "candidate-1",
        "status": "application_review",
        "name": "Анна",
    }
    sheet, worksheet = _sheet([row])

    result = sheet.upsert(
        "Candidates",
        "candidate_id",
        dict(row),
    )

    assert result.created is False
    assert result.changed is False
    assert result.row_number == 2
    assert worksheet.append_calls == []
    assert worksheet.update_calls == []


def test_hiring_sheet_append_once_appends_each_key_only_once():
    sheet, worksheet = _sheet()
    row = {
        "candidate_id": "candidate-1",
        "status": "application_review",
        "name": "Анна",
    }

    assert sheet.append_once(
        "Candidates", "candidate_id", row
    ) is True
    assert sheet.append_once(
        "Candidates", "candidate_id", row
    ) is False
    assert len(worksheet.append_calls) == 1
    assert worksheet.update_calls == []
