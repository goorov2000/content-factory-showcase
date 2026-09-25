"""Provision and archive the human-owned hiring Google Form.

The service account cannot create a Drive-owned Form, but it can rebuild an
already shared Form.  Rebuilding replaces question IDs, so it is guarded by an
explicit zero-response check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cf.hiring.google import FORMS_BASE, _response_error
from cf.hiring.spec import APPLICATION_FORM_TITLE, APPLICATION_TITLES


def text_question(
    title: str,
    *,
    paragraph: bool = False,
    required: bool = True,
    description: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "questionItem": {
            "question": {
                "required": required,
                "textQuestion": {"paragraph": paragraph},
            }
        },
    }
    if description:
        item["description"] = description
    return item


def choice_question(
    title: str,
    options: list[str] | tuple[str, ...],
    *,
    kind: str = "DROP_DOWN",
    required: bool = True,
    description: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": title,
        "questionItem": {
            "question": {
                "required": required,
                "choiceQuestion": {
                    "type": kind,
                    "options": [{"value": value} for value in options],
                    "shuffle": False,
                },
            }
        },
    }
    if description:
        item["description"] = description
    return item


def section_item(title: str, description: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"title": title, "pageBreakItem": {}}
    if description:
        item["description"] = description
    return item


def application_items() -> list[dict[str, Any]]:
    """Return the canonical eight-section, single-submission application.

    Витринная копия: условия пяти отборочных задач и вопросы к ним заменены
    плейсхолдерами — это данные найма; структура секций и контракт формы сохранены.
    """
    q = APPLICATION_TITLES
    return [
        section_item(
            "1/8. О вас",
            "Сначала несколько коротких вопросов.",
        ),
        choice_question(
            q["source"],
            ("HH.ru", "Telegram", "Рекомендация", "Другое"),
        ),
        text_question(q["name"]),
        text_question(q["telegram"], description="Например, @username"),
        section_item(
            "2/8. Ваш опыт",
            "Ответьте на основе реальных примеров. Если часть данных закрыта "
            "NDA, можно обезличить названия и детали.",
        ),
        text_question(
            q["evidence_project"],
            paragraph=True,
            description=(
                "Расскажите об одном проекте, в котором вы участвовали лично. "
                "Что происходило, что именно делали вы и чем всё закончилось? "
                "Если результат можно посмотреть публично, добавьте ссылку. "
                "Если проект под NDA, опишите его без названий и закрытых данных."
            ),
        ),
        text_question(
            q["additional_project"],
            paragraph=True,
            required=False,
            description="Заполните, только если этот пример показывает другую сторону вашего опыта.",
        ),
        choice_question(
            q["team_size_band"],
            ("0", "1–2", "3–5", "6–8", "9+"),
            description="За работу скольких людей вы отвечали одновременно?",
        ),
        choice_question(
            q["weekly_output_band"],
            ("Меньше 10", "10–19", "20–39", "40–69", "70+"),
            description=(
                "Какой максимальный объём уникальных готовых роликов ваша "
                "команда стабильно выпускала за неделю? Один и тот же файл на "
                "двух площадках считается одним роликом."
            ),
        ),
        text_question(
            q["ownership_case"],
            paragraph=True,
            description=(
                "Опишите случай, когда принятое вами решение не дало ожидаемого "
                "результата. Что произошло дальше и чем всё закончилось? Можно "
                "взять пример из работы, учёбы или личного проекта."
            ),
        ),
        section_item(
            "3/8. Задача 1",
            "<условие задачи 1: в витринной копии не публикуется>",
        ),
        text_question(
            q["evidence_case"],
            paragraph=True,
            description="<вопрос к задаче 1: в витринной копии не публикуется>",
        ),
        section_item(
            "4/8. Задача 2",
            "<условие задачи 2: в витринной копии не публикуется>",
        ),
        text_question(
            q["priority_case"],
            paragraph=True,
            description="<вопрос к задаче 2: в витринной копии не публикуется>",
        ),
        section_item(
            "5/8. Задача 3",
            "<условие задачи 3: в витринной копии не публикуется>",
        ),
        text_question(
            q["numeracy_case"],
            paragraph=True,
            description="<вопрос к задаче 3: в витринной копии не публикуется>",
        ),
        section_item(
            "6/8. Задача 4",
            "<условие задачи 4: в витринной копии не публикуется>",
        ),
        text_question(
            q["integrity_case"],
            paragraph=True,
            description="<вопрос к задаче 4: в витринной копии не публикуется>",
        ),
        section_item(
            "7/8. Задача 5",
            "<условие задачи 5: в витринной копии не публикуется>",
        ),
        text_question(
            q["learning_rule_application"],
            paragraph=True,
            description="<вопрос к задаче 5: в витринной копии не публикуется> (часть 1)",
        ),
        text_question(
            q["learning_rule_transfer"],
            paragraph=True,
            description="<вопрос к задаче 5: в витринной копии не публикуется> (часть 2)",
        ),
        section_item(
            "8/8. Завершение",
            "Осталось несколько коротких вопросов.",
        ),
        text_question(q["income_expectation"]),
        text_question(q["start_date"]),
        choice_question(
            q["ai_usage"],
            (
                "Не использовал(а)",
                "Использовал(а) для структуры или редактуры",
                "Использовал(а) для части рассуждений или расчётов",
            ),
            kind="RADIO",
        ),
        text_question(
            q["ai_verification"],
            paragraph=True,
            description=(
                "Если использовали ИИ, кратко укажите, для каких вопросов или "
                "задач. Если не использовали — напишите «нет»."
            ),
        ),
        text_question(q["completion_time"], required=False),
        choice_question(
            q["consent"],
            (
                "Согласен(на): ответы используются только для отбора на эту "
                "вакансию и хранятся не дольше 6 месяцев после её закрытия.",
            ),
            kind="CHECKBOX",
        ),
    ]


APPLICATION_DESCRIPTION = (
    "В форме четыре части: контакты, опыт, несколько ситуаций и условия "
    "работы.\n\n"
    "Обычно заполнение занимает 20–25 минут. Отвечайте своими словами; можно "
    "использовать списки и короткие пункты. ИИ использовать можно — в конце "
    "мы попросим указать, как именно.\n\n"
    "На следующем этапе мы можем попросить коротко обсудить один из ответов."
)


class FormProvisioner:
    def __init__(self, session):
        self.session = session

    def get(self, form_id: str) -> dict[str, Any]:
        response = self.session.get(f"{FORMS_BASE}/forms/{form_id}")
        if not response.ok:
            raise _response_error(response)
        return response.json()

    def assert_no_responses(self, form_id: str) -> None:
        response = self.session.get(
            f"{FORMS_BASE}/forms/{form_id}/responses",
            params={"pageSize": 1},
        )
        if not response.ok:
            raise _response_error(response)
        if response.json().get("responses"):
            raise RuntimeError(
                "Google Form уже содержит ответы; destructive rebuild запрещён"
            )

    def _publish(
        self, form_id: str, *, published: bool, accepting: bool
    ) -> None:
        response = self.session.post(
            f"{FORMS_BASE}/forms/{form_id}:setPublishSettings",
            json={
                "publishSettings": {
                    "publishState": {
                        "isPublished": published,
                        "isAcceptingResponses": accepting,
                    }
                }
            },
        )
        if not response.ok:
            raise _response_error(response)

    def rebuild(
        self,
        form_id: str,
        *,
        title: str,
        description: str,
        items: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        # Close the form before the destructive zero-response check so a
        # response cannot race with replacement of question IDs.
        self._publish(form_id, published=True, accepting=False)
        self.assert_no_responses(form_id)
        current = self.get(form_id)
        requests: list[dict[str, Any]] = [
            {
                "updateFormInfo": {
                    "info": {"title": title, "description": description},
                    "updateMask": "title,description",
                }
            },
            {
                "updateSettings": {
                    "settings": {"emailCollectionType": "RESPONDER_INPUT"},
                    "updateMask": "emailCollectionType",
                }
            },
        ]
        for index in reversed(range(len(current.get("items", []) or []))):
            requests.append({"deleteItem": {"location": {"index": index}}})
        for index, item in enumerate(items):
            requests.append(
                {
                    "createItem": {
                        "item": dict(item),
                        "location": {"index": index},
                    }
                }
            )
        payload: dict[str, Any] = {
            "includeFormInResponse": True,
            "requests": requests,
        }
        revision_id = current.get("revisionId")
        if revision_id:
            payload["writeControl"] = {"requiredRevisionId": revision_id}
        response = self.session.post(
            f"{FORMS_BASE}/forms/{form_id}:batchUpdate",
            json=payload,
        )
        if not response.ok:
            raise _response_error(response)

        self._publish(form_id, published=True, accepting=True)
        final = self.get(form_id)
        self._verify_active_form(final, items)
        return final

    @staticmethod
    def _verify_active_form(
        form: Mapping[str, Any], expected_items: list[Mapping[str, Any]]
    ) -> None:
        actual_items = list(form.get("items", []) or [])
        actual_titles = [str(item.get("title") or "") for item in actual_items]
        expected_titles = [str(item.get("title") or "") for item in expected_items]
        if actual_titles != expected_titles:
            raise RuntimeError("Google Form verification failed: item order differs")

        for actual, expected in zip(actual_items, expected_items, strict=True):
            if str(actual.get("description") or "") != str(
                expected.get("description") or ""
            ):
                raise RuntimeError(
                    "Google Form verification failed: item description differs"
                )
            expected_question = expected.get("questionItem", {}).get("question")
            if expected_question is None:
                if "pageBreakItem" not in actual:
                    raise RuntimeError(
                        "Google Form verification failed: section type differs"
                    )
                continue
            actual_question = actual.get("questionItem", {}).get("question", {})
            # Google omits the field for an optional question instead of
            # returning ``required: false``.
            if bool(actual_question.get("required", False)) != bool(
                expected_question.get("required", False)
            ):
                raise RuntimeError(
                    "Google Form verification failed: required flag differs"
                )
            expected_kind = (
                "textQuestion" if "textQuestion" in expected_question else "choiceQuestion"
            )
            if expected_kind not in actual_question:
                raise RuntimeError(
                    "Google Form verification failed: question type differs"
                )
            if expected_kind == "textQuestion":
                expected_paragraph = bool(
                    expected_question["textQuestion"].get("paragraph", False)
                )
                actual_paragraph = bool(
                    actual_question["textQuestion"].get("paragraph", False)
                )
                if actual_paragraph != expected_paragraph:
                    raise RuntimeError(
                        "Google Form verification failed: text format differs"
                    )
            else:
                expected_choice = expected_question["choiceQuestion"]
                actual_choice = actual_question["choiceQuestion"]
                expected_values = [
                    str(option.get("value") or "")
                    for option in expected_choice.get("options", [])
                ]
                actual_values = [
                    str(option.get("value") or "")
                    for option in actual_choice.get("options", [])
                ]
                if (
                    actual_choice.get("type") != expected_choice.get("type")
                    or actual_values != expected_values
                ):
                    raise RuntimeError(
                        "Google Form verification failed: choices differ"
                    )

        mode = (form.get("settings") or {}).get("emailCollectionType")
        if mode != "RESPONDER_INPUT":
            raise RuntimeError(
                f"Google Form verification failed: emailCollectionType={mode!r}"
            )
        state = ((form.get("publishSettings") or {}).get("publishState") or {})
        if not state.get("isPublished") or not state.get("isAcceptingResponses"):
            raise RuntimeError("Google Form verification failed: form is not active")

    def archive(
        self,
        form_id: str,
        *,
        title: str,
        description: str,
    ) -> dict[str, Any]:
        """Retain all questions while making the old responder link inert."""
        response = self.session.post(
            f"{FORMS_BASE}/forms/{form_id}:batchUpdate",
            json={
                "includeFormInResponse": True,
                "requests": [
                    {
                        "updateFormInfo": {
                            "info": {"title": title, "description": description},
                            "updateMask": "title,description",
                        }
                    }
                ],
            },
        )
        if not response.ok:
            raise _response_error(response)
        self._publish(form_id, published=False, accepting=False)
        final = self.get(form_id)
        state = ((final.get("publishSettings") or {}).get("publishState") or {})
        if state.get("isPublished") or state.get("isAcceptingResponses"):
            raise RuntimeError("Google Form archive verification failed")
        return final


def provision_hiring_form(
    session, *, application_form_id: str
) -> dict[str, str]:
    client = FormProvisioner(session)
    application = client.rebuild(
        application_form_id,
        title=APPLICATION_FORM_TITLE,
        description=APPLICATION_DESCRIPTION,
        items=application_items(),
    )
    return {
        "APPLICATION_FORM_ID": application_form_id,
        "APPLICATION_FORM_URL": str(application.get("responderUri") or ""),
    }
