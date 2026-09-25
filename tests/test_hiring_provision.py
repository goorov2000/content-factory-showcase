from copy import deepcopy

import pytest

from cf.hiring.provision import (
    APPLICATION_DESCRIPTION,
    FormProvisioner,
    application_items,
    provision_hiring_form,
)
from cf.hiring.spec import APPLICATION_FORM_TITLE, APPLICATION_TITLES


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = ""

    def json(self):
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(("get", url, deepcopy(params)))
        return self.responses.pop(0)

    def post(self, url, json):
        self.calls.append(("post", url, deepcopy(json)))
        return self.responses.pop(0)


def _google_items(items):
    result = []
    for item in items:
        copy = deepcopy(item)
        question = copy.get("questionItem", {}).get("question")
        if question is not None:
            question["questionId"] = "q-" + str(len(result))
        result.append(copy)
    return result


def test_single_form_has_eight_sections_and_twenty_questions():
    items = application_items()
    questions = [item for item in items if "questionItem" in item]
    sections = [item for item in items if "pageBreakItem" in item]

    assert len(items) == 28
    assert len(sections) == 8
    assert [item["title"] for item in questions] == list(
        APPLICATION_TITLES.values()
    )
    assert len({item["title"] for item in questions}) == len(questions)
    required = {
        item["title"]: item["questionItem"]["question"]["required"]
        for item in questions
    }
    assert required[APPLICATION_TITLES["additional_project"]] is False
    assert required[APPLICATION_TITLES["completion_time"]] is False
    assert all(
        value
        for title, value in required.items()
        if title
        not in {
            APPLICATION_TITLES["additional_project"],
            APPLICATION_TITLES["completion_time"],
        }
    )


def test_candidate_copy_is_neutral_and_tasks_are_formatted():
    items = application_items()
    visible_titles = [APPLICATION_FORM_TITLE, *[item["title"] for item in items]]
    forbidden_title_terms = (
        "evidence",
        "ownership",
        "numeracy",
        "приоритет",
        "дефицит",
        "причинност",
        "обратим",
        "перенос",
    )
    assert not any(
        term in title.lower()
        for title in visible_titles
        for term in forbidden_title_terms
    )

    visible_copy = "\n".join(
        [APPLICATION_DESCRIPTION]
        + [
            str(item.get("description") or "")
            for item in items
        ]
    ).lower()
    forbidden_prompts = (
        "что мы точно знаем",
        "какие три проверки",
        "обратимое решение",
        "назовите один риск",
        "безопасную формулировку",
        "что проверили самостоятельно",
        "в пяти пунктах",
    )
    assert not any(prompt in visible_copy for prompt in forbidden_prompts)

    task_two = next(item for item in items if item["title"] == "4/8. Задача 2")
    description = task_two["description"]
    # Витринная копия: условия задач заменены плейсхолдерами (см. README-SHOWCASE.md);
    # здесь проверяется только, что у секции задачи есть непустое описание.
    assert description.strip()


def test_rebuild_requires_zero_responses_then_replaces_and_publishes():
    items = application_items()[:3]
    final = {
        "items": _google_items(items),
        "settings": {"emailCollectionType": "RESPONDER_INPUT"},
        "publishSettings": {
            "publishState": {
                "isPublished": True,
                "isAcceptingResponses": True,
            }
        },
        "responderUri": "https://forms.example/respond",
    }
    session = FakeSession(
        [
            FakeResponse({}),
            FakeResponse({}),
            FakeResponse(
                {
                    "revisionId": "revision-1",
                    "items": [{"title": "old-1"}, {"title": "old-2"}],
                }
            ),
            FakeResponse({}),
            FakeResponse({}),
            FakeResponse(final),
        ]
    )

    result = FormProvisioner(session).rebuild(
        "form-1", title="Title", description="Description", items=items
    )

    assert session.calls[0][0] == "post"
    assert session.calls[0][1].endswith(":setPublishSettings")
    assert session.calls[0][2]["publishSettings"]["publishState"] == {
        "isPublished": True,
        "isAcceptingResponses": False,
    }
    assert session.calls[1] == (
        "get",
        "https://forms.googleapis.com/v1/forms/form-1/responses",
        {"pageSize": 1},
    )
    batch_payload = session.calls[3][2]
    assert batch_payload["writeControl"] == {
        "requiredRevisionId": "revision-1"
    }
    batch = batch_payload["requests"]
    assert [request["deleteItem"]["location"]["index"] for request in batch[2:4]] == [
        1,
        0,
    ]
    assert [
        request["createItem"]["location"]["index"] for request in batch[4:]
    ] == [0, 1, 2]
    assert session.calls[4][1].endswith(":setPublishSettings")
    assert session.calls[4][2]["publishSettings"]["publishState"] == {
        "isPublished": True,
        "isAcceptingResponses": True,
    }
    assert result["responderUri"] == "https://forms.example/respond"


def test_rebuild_refuses_to_replace_question_ids_after_any_response():
    session = FakeSession(
        [
            FakeResponse({}),
            FakeResponse({"responses": [{"responseId": "existing"}]}),
        ]
    )

    with pytest.raises(RuntimeError, match="destructive rebuild запрещён"):
        FormProvisioner(session).rebuild(
            "form-1",
            title="Title",
            description="Description",
            items=application_items(),
        )

    assert len(session.calls) == 2
    assert session.calls[0][2]["publishSettings"]["publishState"] == {
        "isPublished": True,
        "isAcceptingResponses": False,
    }


def test_rebuild_fails_if_google_returns_different_question_order():
    items = application_items()[:3]
    final = {
        "items": list(reversed(_google_items(items))),
        "settings": {"emailCollectionType": "RESPONDER_INPUT"},
        "publishSettings": {
            "publishState": {
                "isPublished": True,
                "isAcceptingResponses": True,
            }
        },
    }
    session = FakeSession(
        [
            FakeResponse({}),
            FakeResponse({}),
            FakeResponse({"items": []}),
            FakeResponse({}),
            FakeResponse({}),
            FakeResponse(final),
        ]
    )

    with pytest.raises(RuntimeError, match="item order differs"):
        FormProvisioner(session).rebuild(
            "form-1", title="Title", description="Description", items=items
        )


def test_verify_accepts_google_omitting_required_for_optional_question():
    item = next(
        item
        for item in application_items()
        if item["title"] == APPLICATION_TITLES["additional_project"]
    )
    final_item = _google_items([item])[0]
    del final_item["questionItem"]["question"]["required"]
    form = {
        "items": [final_item],
        "settings": {"emailCollectionType": "RESPONDER_INPUT"},
        "publishSettings": {
            "publishState": {
                "isPublished": True,
                "isAcceptingResponses": True,
            }
        },
    }

    FormProvisioner._verify_active_form(form, [item])


def test_archive_keeps_items_but_unpublishes_and_stops_responses():
    preserved = [{"title": "old question", "questionItem": {"question": {}}}]
    final = {
        "items": preserved,
        "publishSettings": {
            "publishState": {
                "isPublished": False,
                "isAcceptingResponses": False,
            }
        },
    }
    session = FakeSession(
        [FakeResponse({}), FakeResponse({}), FakeResponse(final)]
    )

    result = FormProvisioner(session).archive(
        "old-form",
        title="АРХИВ — test v1",
        description="Не использовать",
    )

    requests = session.calls[0][2]["requests"]
    assert requests == [
        {
            "updateFormInfo": {
                "info": {
                    "title": "АРХИВ — test v1",
                    "description": "Не использовать",
                },
                "updateMask": "title,description",
            }
        }
    ]
    assert session.calls[1][2]["publishSettings"]["publishState"] == {
        "isPublished": False,
        "isAcceptingResponses": False,
    }
    assert result["items"] == preserved


def test_provision_returns_only_one_active_form_resource(monkeypatch):
    expected = {
        "responderUri": "https://forms.example/application",
    }
    monkeypatch.setattr(
        FormProvisioner,
        "rebuild",
        lambda self, form_id, **kwargs: expected,
    )

    result = provision_hiring_form(object(), application_form_id="application-1")

    assert result == {
        "APPLICATION_FORM_ID": "application-1",
        "APPLICATION_FORM_URL": "https://forms.example/application",
    }
