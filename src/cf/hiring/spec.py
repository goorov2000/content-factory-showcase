"""Stable schema contract for the single hiring application form.

Google Forms answers are keyed by opaque question IDs.  The human-readable
titles below are therefore versioned runtime data: the adapter resolves each
question ID through the current form metadata before normalising a response.
"""

RULESET_VERSION = "hiring-v3-2026-07-31"

APPLICATION_FORM_TITLE = "Отклик на вакансию контент-продюсера — JE LA PECHE"

# Dict order is the canonical question order inside the Form.  Contact fields
# are stored in the private tracker but never sent to the semantic judge.
APPLICATION_TITLES = {
    "source": "Где вы увидели вакансию?",
    "name": "Имя",
    "telegram": "Telegram для связи",
    "evidence_project": "Один проект из вашего опыта",
    "additional_project": "Ещё один проект (необязательно)",
    "team_size_band": "Размер команды",
    "weekly_output_band": "Объём роликов за неделю",
    "ownership_case": "Случай из опыта",
    "evidence_case": "Ответ — задача 1",
    "priority_case": "Ответ — задача 2",
    "numeracy_case": "Ответ — задача 3",
    "integrity_case": "Ответ — задача 4",
    "learning_rule_application": "Ответ — задача 5.1",
    "learning_rule_transfer": "Ответ — задача 5.2",
    "income_expectation": "Ожидания по доходу в месяц",
    "start_date": "Когда готовы начать?",
    "ai_usage": "Использовали ли вы ИИ при заполнении?",
    "ai_verification": "Комментарий об использовании ИИ",
    "completion_time": "Сколько времени заняло заполнение? (необязательно)",
    "consent": "Согласие на обработку данных для отбора",
}

# Only the explicitly optional fields are absent.  Email is collected by the
# Form setting and checked separately by the deterministic rule.
APPLICATION_REQUIRED = tuple(
    key
    for key in APPLICATION_TITLES
    if key not in {"additional_project", "completion_time"}
)

SEMANTIC_DIMENSIONS = (
    "track_record",
    "ownership",
    "evidence_reasoning",
    "prioritization",
    "numeracy",
    "integrity_communication",
    "learning_transfer",
)

ACTIVE_TRACKER_TABS = (
    "Candidates",
    "Assessments",
    "Decisions",
    "Outbox",
    "Sync State",
    "Config",
    "Screening Rubric",
    "Assessment Rubric",
    "README",
)

# Cutover keeps the v1 sheets as immutable audit evidence.
ARCHIVED_TRACKER_TABS = (
    "ARCHIVE — Test Submissions v1",
    "ARCHIVE — Test Rubric v1",
)

TRACKER_TABS = ACTIVE_TRACKER_TABS + ARCHIVED_TRACKER_TABS
