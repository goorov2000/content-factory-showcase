from pathlib import Path

# Тесты контрактов промптов/команд (P1.14 б и разделитель references в а):
# документ обязан ссылаться только на канонические значения, иначе агент
# сгенерирует ответ/действие, которое CLI отклонит или неверно распарсит.


def test_source_tuner_uses_canonical_insufficient_status():
    # runlog.VALID_STATUSES = {success, failed, insufficient_data}; argparse-choices
    # log-run отвергнут бы insufficient_evidence. В промпте должен быть канон-статус.
    text = Path("prompts/agents/source-tuner.md").read_text(encoding="utf-8")
    assert "insufficient_evidence" not in text
    assert "insufficient_data" in text


def test_review_brief_reads_from_payload_json():
    # Основной путь — payload_json (полный уже-валидный бриф): не зависит от того,
    # добавил ли оператор колонку cta в живой лист.
    text = Path(".claude/commands/cf-review-brief.md").read_text(encoding="utf-8")
    assert "payload_json" in text


def test_review_brief_fallback_splits_references_by_semicolon():
    # Запасной путь (payload_json пуст): references склеены через "; "; разбивка
    # «через запятую» собрала бы один склеенный элемент и сломала пересечение source_urls.
    text = Path(".claude/commands/cf-review-brief.md").read_text(encoding="utf-8")
    assert '"; "' in text


def test_brief_generator_uses_ab_plan_command():
    # P5.13: генератор берёт версии из `cf ab-plan` (кодовый детерминированный план
    # interleaving) — команда должна существовать в CLI (см. cmd_brief_version).
    from cf.cli import cmd_brief_version  # noqa: F401 — контракт: команда есть
    text = Path("prompts/agents/brief-generator.md").read_text(encoding="utf-8")
    assert "ab-plan" in text
    assert "candidate" in text


def test_generate_briefs_wrapper_requires_ab_plan():
    # P5.13: обёртка не должна противоречить brief-generator.md — ab-plan обязательный
    # вход per-формула (генерация строго по версии из плана, candidate → честный A/B).
    # Старый порядок «генерируй по активной версии» без ab-plan ломал headless-фан-аут.
    text = Path(".claude/commands/cf-generate-briefs.md").read_text(encoding="utf-8")
    assert "ab-plan" in text


def test_eval_agent_references_parallel_cohorts():
    # P5.13: eval сравнивает только параллельные когорты — правило и поле датасета
    # должны быть названы каноническим ключом cohorts_by_formula.
    text = Path("prompts/agents/eval-agent.md").read_text(encoding="utf-8")
    assert "cohorts_by_formula" in text
    assert "параллельн" in text.lower()


def test_propose_update_approve_launches_ab_not_before_after():
    # P5.13: approve = запуск A/B через --candidate в ОТДЕЛЬНЫЙ файл (не правка того же
    # файла = before/after), активация победителя — после вердикта weekly-eval.
    text = Path(".claude/commands/cf-propose-update.md").read_text(encoding="utf-8")
    assert "--candidate" in text
    assert "ОТДЕЛЬНЫЙ файл" in text
    assert "После вердикта weekly-eval" in text
