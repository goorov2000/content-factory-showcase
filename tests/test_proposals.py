from cf.proposals import validate_proposal_text

VALID = """---
status: proposed
prompt_id: brief-pets
created: 2026-07-09
---
# Proposal: усилить требования к хуку

## Current version
Версия v1, файл `prompts/briefs/pets/talking-head.md`.

## Proposed changes
Заменить требование к хуку на явное: вопрос в первых 2 секундах.

## Evidence
- "слабый референс" — 4 случая (agent-runtime/reviews/rejection_history.json).
- v1 avg_views=20000 при формуле с avg_views=120000 (https://tiktok.com/@a/video/1).

## Confidence
medium — evidence из одного eval-периода.

## Risks
Хук-вопрос может выгореть в нише; заметим по падению avg_er в следующем eval.
"""


def test_valid_proposal_passes():
    assert validate_proposal_text(VALID) == []


def test_missing_section_fails():
    text = VALID.replace("## Risks", "## Прочее")
    assert any("Risks" in e for e in validate_proposal_text(text))


def test_evidence_without_numbers_fails():
    text = (VALID.split("## Evidence")[0]
            + "## Evidence\nпросто мнение без цифр и ссылок\n\n"
            + "## Confidence\nmedium\n\n## Risks\nнет\n")
    errors = validate_proposal_text(text)
    assert any("цифр" in e or "ссыл" in e for e in errors)


def test_wrong_frontmatter_status_fails():
    text = VALID.replace("status: proposed", "status: draft")
    assert any("status: proposed" in e for e in validate_proposal_text(text))


def test_unclosed_frontmatter_fails():
    # открывающий --- есть, закрывающего нет: status: proposed внутри есть,
    # но без закрытия блок захватывает весь файл — должно считаться невалидным.
    text = VALID.replace("created: 2026-07-09\n---\n", "created: 2026-07-09\n", 1)
    errors = validate_proposal_text(text)
    assert errors, "непокрытый frontmatter должен давать ошибку"
    assert any("закр" in e or "frontmatter" in e.lower() for e in errors)
