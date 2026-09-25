import re

REQUIRED_SECTIONS = (
    "## Current version",
    "## Proposed changes",
    "## Evidence",
    "## Confidence",
    "## Risks",
)


def validate_proposal_text(text):
    errors = []
    stripped = text.lstrip()
    parts = stripped.split("---")
    if not stripped.startswith("---"):
        errors.append("нет YAML frontmatter (--- в начале файла)")
    elif len(parts) < 3:
        # только открывающий --- без закрывающего: блок «съедает» весь файл.
        errors.append("YAML frontmatter не закрыт (нужен закрывающий ---)")
    elif "status: proposed" not in parts[1]:
        errors.append("frontmatter должен содержать status: proposed")
    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"нет секции {section}")
    if "## Evidence" in text:
        evidence = text.split("## Evidence", 1)[1].split("\n## ", 1)[0]
        if not re.search(r"\d", evidence):
            errors.append("Evidence без цифр — метрики обязательны")
        if not any(m in evidence for m in ("http", "agent-runtime/", "formulas/")):
            errors.append("Evidence без ссылок на источники (url или путь к артефакту)")
    return errors
