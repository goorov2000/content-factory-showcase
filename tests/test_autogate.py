"""Машинные ворота: чек-лист рецепта, чек-лист промпта темы, журнал решений.

Ключевой инвариант всех тестов ниже — ворота НЕ пропускают по недостатку данных
(правило №2): нечитаемая вкладка, отсутствующая таксономия и непрочитанные
prompt_versions дают ok=None и уводят решение к человеку, а не к «зелёному».
"""
import argparse
import json

import pytest

from cf.cli import cmd_auto_approve_formula
from cf.dashboard.autogate import (formula_checks, formula_verdict, gate_policy,
                                   human_rejections, proposed_formula_drafts,
                                   prompt_draft_verdict, raw_index, verdict)

from tests.fakes import FakeSheets


def ns(**kw):
    kw.setdefault("run_started_at", "2026-07-26T00:00:00+00:00")
    return argparse.Namespace(**kw)


def _formula(**over):
    formula = {
        "name": "color-upgrade-ladder", "niche": "мужской-стиль",
        "status": "proposed", "version": 1,
        "hook_structure": {"timing": "0-3 сек", "elements": ["первая пара сразу"]},
        "problem_definition": "Зритель застревает на сочетании, которое «вроде норм».",
        "solution_structure": {"steps": ["1. пара", "2. сдвиг"]},
        "visual_requirements": ["обе версии на человеке"],
        "cta_type": "вопрос-выбор",
        "prohibitions": ["оценка зрителя"],
        "evidence": {"source_urls": ["https://t.tk/1", "https://t.tk/2",
                                     "https://t.tk/3"],
                     "avg_views": 32000, "avg_er": 0.05},
        "confidence": "medium",
        "conditions": "Ниша мужской-стиль, съёмка одним креатором",
        "source_pattern_ids": ["p-01"],
        "updated_at": "2026-07-26",
    }
    formula.update(over)
    return formula


def _rows(niche="мужской-стиль", authors=("a", "b", "c"), transcripts=3):
    rows = []
    for i, author in enumerate(authors, start=1):
        rows.append({"source_url": f"https://t.tk/{i}", "niche": niche,
                     "author": author, "views": 50000,
                     "transcript_text": "текст" if i <= transcripts else ""})
    return rows


def _repo(tmp_path, exclude=(), taxonomy=("мужской-стиль", "бренды-магазины")):
    """Минимальное дерево репозитория: конфиг, таксономия, пустой индекс."""
    (tmp_path / "prompts" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "agents" / "niche-taxonomy.json").write_text(
        json.dumps({"niches": list(taxonomy)}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "cf.config.json").write_text(
        json.dumps({"dashboard": {"fanout": {"exclude_niches": list(exclude)}}},
                   ensure_ascii=False), encoding="utf-8")
    idx = tmp_path / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": []}), encoding="utf-8")
    # схемы читаются из корня репозитория проекта — копируем реальную
    schemas = tmp_path / "schemas"
    schemas.mkdir(exist_ok=True)
    src = __import__("pathlib").Path("schemas/formula.schema.json")
    (schemas / "formula.schema.json").write_text(src.read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    return tmp_path


def _by_key(checks):
    return {c["key"]: c for c in checks}


# ── чек-лист рецепта ─────────────────────────────────────────────────────────

def test_clean_draft_is_green(tmp_path):
    root = _repo(tmp_path)
    result = formula_verdict(root, _formula(), raw_index(_rows()))
    assert result["green"], result["reasons"]


def test_excluded_niche_is_red(tmp_path):
    # 7 отказов оператора из 8 были именно этого класса.
    root = _repo(tmp_path, exclude=("женская-мода",),
                 taxonomy=("мужской-стиль", "женская-мода"))
    result = formula_verdict(root, _formula(niche="женская-мода"),
                             raw_index(_rows(niche="женская-мода")))
    assert not result["green"]
    assert _by_key(result["checks"])["niche"]["ok"] is False


def test_two_authors_is_red(tmp_path):
    # star-peak-moment (отказ оператора) держался на двух аккаунтах.
    root = _repo(tmp_path)
    rows = _rows(authors=("a", "a", "b"))
    result = formula_verdict(root, _formula(), raw_index(rows))
    assert _by_key(result["checks"])["authors"]["ok"] is False


def test_invented_evidence_is_red(tmp_path):
    # Правило №1: source_urls обязаны находиться в собранных данных.
    root = _repo(tmp_path)
    result = formula_verdict(root, _formula(), raw_index([]))
    assert _by_key(result["checks"])["evidence_resolves"]["ok"] is False


def test_foreign_niche_references_are_red(tmp_path):
    # «все 3 референса — женский контент» — дословная причина отката 14.07.
    root = _repo(tmp_path)
    result = formula_verdict(root, _formula(), raw_index(_rows(niche="женская-мода")))
    assert _by_key(result["checks"])["evidence_niche"]["ok"] is False


def test_no_transcripts_goes_to_human(tmp_path):
    root = _repo(tmp_path)
    result = formula_verdict(root, _formula(), raw_index(_rows(transcripts=0)))
    assert _by_key(result["checks"])["transcript"]["ok"] is False


def test_unread_raw_is_not_a_yes(tmp_path):
    # Правило №2: недоступная вкладка — «не знаю», а не «evidence чистый».
    root = _repo(tmp_path)
    checks = _by_key(formula_checks(root, _formula(), None))
    assert checks["evidence_resolves"]["ok"] is None
    assert not verdict(list(checks.values()))["green"]


def test_broken_schema_is_red(tmp_path):
    root = _repo(tmp_path)
    broken = _formula()
    del broken["cta_type"]
    result = formula_verdict(root, broken, raw_index(_rows()))
    assert _by_key(result["checks"])["schema"]["ok"] is False


def test_already_approved_version_is_not_reapproved(tmp_path):
    root = _repo(tmp_path)
    (root / "formulas" / "_approved" / "index.json").write_text(json.dumps(
        {"approved": [{"name": "color-upgrade-ladder", "niche": "мужской-стиль",
                       "version": 1, "path": "formulas/_approved/x.json"}]}),
        encoding="utf-8")
    result = formula_verdict(root, _formula(), raw_index(_rows()))
    assert _by_key(result["checks"])["duplicate"]["ok"] is False


def test_human_rejection_is_remembered(tmp_path):
    # Следующий прогон фан-аута перезаписывает файл рецепта черновиком v+1 со
    # статусом proposed — «нет» оператора живёт только в журнале решений.
    root = _repo(tmp_path)
    ledger = root / "formulas" / "_decisions.jsonl"
    ledger.write_text(json.dumps(
        {"niche": "мужской-стиль", "name": "color-upgrade-ladder",
         "decision": "rejected", "actor": "human"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert ("мужской-стиль", "color-upgrade-ladder") in human_rejections(root)
    result = formula_verdict(root, _formula(version=2), raw_index(_rows()))
    assert _by_key(result["checks"])["human_veto"]["ok"] is False


def test_human_rejection_survives_unicode_line_separator_in_reason(tmp_path):
    # Причину отказа человек вставляет из Telegram/Word, а там бывает U+2028.
    # json.dumps(ensure_ascii=False) оставляет его в строке; splitlines() резал
    # запись надвое, обе половины молча отбрасывались как «битые» — и вето
    # человека пропадало, машина могла вернуть отклонённый рецепт (14.09.2026).
    root = _repo(tmp_path)
    (root / "formulas" / "_decisions.jsonl").write_text(json.dumps(
        {"niche": "мужской-стиль", "name": "color-upgrade-ladder",
         "decision": "rejected", "actor": "human",
         "reason": "не наш бренд\u2028вторая строка"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert ("мужской-стиль", "color-upgrade-ladder") in human_rejections(root)


def test_auto_rejection_does_not_veto(tmp_path):
    # Вето — только человеческое: иначе машина запирала бы себя сама.
    root = _repo(tmp_path)
    (root / "formulas" / "_decisions.jsonl").write_text(json.dumps(
        {"niche": "мужской-стиль", "name": "color-upgrade-ladder",
         "decision": "rejected", "actor": "auto"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert human_rejections(root) == set()


def test_reapproval_lifts_the_veto(tmp_path):
    root = _repo(tmp_path)
    lines = [{"niche": "мужской-стиль", "name": "color-upgrade-ladder",
              "decision": "rejected", "actor": "human"},
             {"niche": "мужской-стиль", "name": "color-upgrade-ladder",
              "decision": "approved", "actor": "human"}]
    (root / "formulas" / "_decisions.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8")
    assert human_rejections(root) == set()


# ── политика ворот ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("config,expected", [
    ({}, "auto"),
    ({"gates": {"policy": {"recipes": "manual"}}}, "manual"),
    ({"gates": {"policy": {"recipes": "мусор"}}}, "auto"),
    (None, "auto"),
])
def test_gate_policy_defaults_to_auto(config, expected):
    assert gate_policy(config, "recipes") == expected


# ── очередь черновиков ───────────────────────────────────────────────────────

def test_proposed_drafts_skip_snapshots_and_excluded(tmp_path):
    root = _repo(tmp_path, exclude=("женская-мода",))
    for niche, name, status in (("мужской-стиль", "a", "proposed"),
                                ("мужской-стиль", "b", "approved"),
                                ("женская-мода", "c", "proposed")):
        d = root / "formulas" / niche
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(
            json.dumps({"name": name, "niche": niche, "status": status}),
            encoding="utf-8")
    snap = root / "formulas" / "_approved" / "мужской-стиль"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "a-v1.json").write_text(
        json.dumps({"name": "a", "niche": "мужской-стиль", "status": "proposed"}),
        encoding="utf-8")
    assert [p.stem for _n, p in proposed_formula_drafts(root)] == ["a"]


# ── чек-лист промпта темы ────────────────────────────────────────────────────

def _approved_index(root, niche, names):
    idx = root / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": [
        {"name": n, "niche": niche, "version": 1,
         "path": f"formulas/_approved/{niche}/{n}-v1.json"} for n in names]},
        ensure_ascii=False), encoding="utf-8")


def test_prompt_covering_all_recipes_is_green(tmp_path):
    root = _repo(tmp_path)
    _approved_index(root, "мужской-стиль", ["alpha", "beta"])
    text = "Рецепты темы: alpha и beta."
    assert prompt_draft_verdict(root, "мужской-стиль", text, versions=[])["green"]


def test_prompt_missing_a_recipe_is_red(tmp_path):
    # Дословный дефект «мужских-образов»: промпт называл 1 рецепт из 4, тема
    # давала ноль сценариев, а ворота были зелёными.
    root = _repo(tmp_path)
    _approved_index(root, "мужской-стиль", ["alpha", "beta", "gamma", "delta"])
    result = prompt_draft_verdict(root, "мужской-стиль", "Только alpha.", versions=[])
    coverage = _by_key(result["checks"])["coverage"]
    assert coverage["ok"] is False and "1 из 4" in coverage["detail"]


def test_prompt_pointing_at_stale_snapshot_is_red(tmp_path):
    # Черновик 26.07 протух за час: ссылался на вытесненные версии снапшотов.
    root = _repo(tmp_path)
    idx = root / "formulas" / "_approved" / "index.json"
    idx.write_text(json.dumps({"approved": [
        {"name": "alpha", "niche": "мужской-стиль", "version": 2,
         "path": "formulas/_approved/мужской-стиль/alpha-v2.json"}]},
        ensure_ascii=False), encoding="utf-8")
    text = "Читай formulas/_approved/мужской-стиль/alpha-v1.json — рецепт alpha."
    result = prompt_draft_verdict(root, "мужской-стиль", text, versions=[])
    assert _by_key(result["checks"])["snapshots_fresh"]["ok"] is False


def test_unread_versions_are_not_a_yes(tmp_path):
    root = _repo(tmp_path)
    _approved_index(root, "мужской-стиль", ["alpha"])
    result = prompt_draft_verdict(root, "мужской-стиль", "alpha", versions=None)
    assert _by_key(result["checks"])["not_active"]["ok"] is None
    assert not result["green"]


# ── команда авто-одобрения ───────────────────────────────────────────────────

def _write_draft(root, formula):
    path = root / "formulas" / formula["niche"] / f"{formula['name']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(formula, ensure_ascii=False), encoding="utf-8")
    return path


def _sheets(rows=None, config=None):
    # rows=[] — это «вкладка пуста», а не «параметр не задан»: `rows or _rows()`
    # подставил бы дефолтные строки и тест пустого evidence проверял бы обратное.
    rows = _rows() if rows is None else rows
    return FakeSheets(tables={"raw_tiktok": list(rows),
                              "raw_instagram": [], "run_log": []},
                      config=config or {"tabs": {"raw_tiktok": "raw_tiktok",
                                                 "raw_instagram": "raw_instagram",
                                                 "run_log": "run_log"}})


def test_command_approves_green_draft_and_logs(tmp_path):
    root = _repo(tmp_path)
    path = _write_draft(root, _formula())
    sheets = _sheets()
    code = cmd_auto_approve_formula(sheets, ns(
        path=str(path), index=str(root / "formulas" / "_approved" / "index.json"),
        review="", no_judge=True, check=False))
    assert code == 0
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "approved"
    # правило №6: прогон оставил след
    assert any(r.get("agent") == "auto-approve-formula" and r.get("status") == "success"
               for r in sheets.tables["run_log"])
    # решение попало в журнал с пометкой «машина»
    ledger = [json.loads(l) for l in
              (root / "formulas" / "_decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ledger[-1]["actor"] == "auto" and ledger[-1]["decision"] == "approved"


def test_command_refuses_red_draft_without_touching_it(tmp_path):
    root = _repo(tmp_path)
    path = _write_draft(root, _formula())
    sheets = _sheets(rows=[])          # evidence не резолвится
    code = cmd_auto_approve_formula(sheets, ns(
        path=str(path), index=str(root / "formulas" / "_approved" / "index.json"),
        review="", no_judge=True, check=False))
    assert code == 0                    # отказ — не сбой пайплайна
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "proposed"
    assert any(r.get("status") == "insufficient_data"
               for r in sheets.tables["run_log"])


def test_command_requires_judge_verdict_by_default(tmp_path):
    root = _repo(tmp_path)
    path = _write_draft(root, _formula())
    sheets = _sheets()
    cmd_auto_approve_formula(sheets, ns(
        path=str(path), index=str(root / "formulas" / "_approved" / "index.json"),
        review="", no_judge=False, check=False))
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "proposed"


def test_command_honours_judge_reject(tmp_path):
    # Единственный отказ оператора в целевой теме был именно такого класса:
    # машинно рецепт чист, а снять его бренд не может.
    root = _repo(tmp_path)
    path = _write_draft(root, _formula())
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"name": "color-upgrade-ladder", "verdict": "reject",
                                  "reason": "съёмка в чужом магазине"},
                                 ensure_ascii=False), encoding="utf-8")
    sheets = _sheets()
    cmd_auto_approve_formula(sheets, ns(
        path=str(path), index=str(root / "formulas" / "_approved" / "index.json"),
        review=str(review), no_judge=False, check=False))
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "proposed"


def test_manual_policy_stops_auto_approval(tmp_path):
    root = _repo(tmp_path)
    path = _write_draft(root, _formula())
    sheets = _sheets(config={"tabs": {"raw_tiktok": "raw_tiktok",
                                      "raw_instagram": "raw_instagram",
                                      "run_log": "run_log"},
                             "gates": {"policy": {"recipes": "manual"}}})
    cmd_auto_approve_formula(sheets, ns(
        path=str(path), index=str(root / "formulas" / "_approved" / "index.json"),
        review="", no_judge=True, check=False))
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "proposed"


def test_calibration_names_the_dangerous_class(tmp_path):
    """`cf gate-calibration` отличает «машина строже» от ПРОПУСКА.

    Эти два расхождения стоят разного: строгость — одно решение человека,
    пропуск — рецепт в производстве, которого человек бы не пустил. Сводка,
    которая мешает их в одну «точность», обосновывать автономию не может."""
    from cf.cli import cmd_gate_calibration
    root = _repo(tmp_path)
    # человек одобрил, а чек-лист красный (ни одной расшифровки) → «машина строже».
    # Ровно случай numbered-outfit-looks на боевых данных.
    silent = {"source_urls": [f"https://t.tk/1{i}" for i in range(3)],
              "avg_views": 1000, "avg_er": 0.01}
    _write_draft(root, _formula(name="stricter", status="approved", evidence=silent))
    # человек отклонил, а чек-лист зелёный → ПРОПУСК (случай shopper-pov-store-find)
    _write_draft(root, _formula(name="miss", status="rejected"))
    rows = _rows() + [{"source_url": f"https://t.tk/1{i}", "niche": "мужской-стиль",
                       "author": f"z{i}", "views": 50000, "transcript_text": ""}
                      for i in range(3)]
    sheets = _sheets(rows=rows)

    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cmd_gate_calibration(sheets, ns(
            root=str(root), index=str(root / "formulas" / "_approved" / "index.json")))
    text = out.getvalue()

    assert code == 0
    assert "ПРОПУСК" in text and "miss" in text
    assert "машина строже" in text and "stricter" in text
    assert "машина строже: 1" in text and "пропусков: 1" in text


def test_check_mode_changes_nothing(tmp_path):
    root = _repo(tmp_path)
    path = _write_draft(root, _formula())
    sheets = _sheets()
    cmd_auto_approve_formula(sheets, ns(
        path=str(path), index=str(root / "formulas" / "_approved" / "index.json"),
        review="", no_judge=True, check=True))
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "proposed"
    assert sheets.tables["run_log"] == []


# ── Целостность текста промпта (прогон 27.07 01:15) ──────────────────────────
# Кандидат reel-v3.md прошёл зелёным чек-листом и оказался обрублен посреди
# «Задачи»: покрытие рецептов, свежесть снапшотов и ссылки были в порядке, а текст
# потерял «Запреты» и «Выход». Генератор отказался по нему работать, и тема сутки
# не производила сценариев.

_ACTIVE = """# Промпт темы

## Вход
Снапшот рецепта.
Поля hook_structure и prohibitions.

## Задача
Сгенерируй бриф-JSON.
Поля hook, script, cta.

## Запреты (нарушение = reject ревьюером)
Каталожное перечисление товаров.
Хэштеги вместо подписи.

## Выход
Строка в CF Creative Briefs.
payload_json целиком.
"""

_TRUNCATED = """# Промпт темы

## Вход
Снапшот рецепта.
Поля hook_structure и prohibitions.

## Задача
Сгенерируй бриф-JSON по schemas/brief.schema.json:
"""


def test_truncated_prompt_is_caught(tmp_path):
    from cf.dashboard.autogate import prompt_integrity_checks
    checks = _by_key(prompt_integrity_checks(_TRUNCATED, _ACTIVE))
    assert checks["not_truncated"]["ok"] is False
    assert checks["sections_present"]["ok"] is False
    assert "Запреты" in checks["sections_present"]["detail"]
    assert checks["sections_filled"]["ok"] is False


def test_whole_prompt_passes_integrity():
    from cf.dashboard.autogate import prompt_integrity_checks
    assert all(c["ok"] for c in prompt_integrity_checks(_ACTIVE, _ACTIVE))


def test_first_enable_requires_canonical_sections():
    # Эталона нет — сверяем с каноническим набором: Вход, Задача, Запреты, Выход.
    from cf.dashboard.autogate import prompt_integrity_checks
    checks = _by_key(prompt_integrity_checks(_TRUNCATED))
    assert checks["sections_present"]["ok"] is False
    assert "Выход" in checks["sections_present"]["detail"]


def test_candidate_losing_a_section_is_red(tmp_path):
    # Кандидат не имеет права потерять секцию действующей версии: она уже
    # производит сценарии, потеря — регрессия, а не стилистика.
    from cf.dashboard.autogate import prompt_candidate_checks
    root = _repo(tmp_path)
    _approved_index(root, "мужской-стиль", ["alpha"])
    live = root / "prompts" / "briefs" / "мужской-стиль" / "reel.md"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(_ACTIVE, encoding="utf-8")
    without_ban = _ACTIVE.replace(
        "## Запреты (нарушение = reject ревьюером)\nКаталожное перечисление товаров.\n"
        "Хэштеги вместо подписи.\n\n", "")
    checks = _by_key(prompt_candidate_checks(
        root, "мужской-стиль", without_ban + "\nalpha\n", 3, versions=[]))
    assert checks["sections_present"]["ok"] is False
    assert "Запреты" in checks["sections_present"]["detail"]


# ── Протухание УЖЕ включённого промпта (27.07) ───────────────────────────────

def _live_prompt(root, niche, text):
    path = root / "prompts" / "briefs" / niche / "reel.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_live_prompt_pointing_at_superseded_snapshot_is_flagged(tmp_path):
    """Ворота проверяют свежесть при применении, а индекс живёт дальше.

    27.07: машина одобрила reference-recreation-fit v2 через час после применения
    промпта, и текст молча стал указывать на вытесненный v1. Ничего не падает —
    снапшоты иммутабельны, — поэтому и заметить это может только проверка."""
    from cf.dashboard.autogate import stale_prompt_snapshots
    root = _repo(tmp_path)
    _approved_index(root, "мужской-стиль", ["alpha"])
    (root / "formulas" / "_approved" / "index.json").write_text(json.dumps(
        {"approved": [{"name": "alpha", "niche": "мужской-стиль", "version": 2,
                       "path": "formulas/_approved/мужской-стиль/alpha-v2.json"}]},
        ensure_ascii=False), encoding="utf-8")
    _live_prompt(root, "мужской-стиль",
                 "Снапшот: `formulas/_approved/мужской-стиль/alpha-v1.json`.")

    stale = stale_prompt_snapshots(root)

    assert len(stale) == 1
    assert stale[0]["niche"] == "мужской-стиль"
    assert stale[0]["stale"] == ["alpha v1 → сейчас v2"]


def test_live_prompt_on_current_snapshot_is_quiet(tmp_path):
    from cf.dashboard.autogate import stale_prompt_snapshots
    root = _repo(tmp_path)
    _approved_index(root, "мужской-стиль", ["alpha"])
    _live_prompt(root, "мужской-стиль",
                 "Снапшот: `formulas/_approved/мужской-стиль/alpha-v1.json`.")
    assert stale_prompt_snapshots(root) == []


def test_gate_summary_names_stale_prompt_links(tmp_path):
    from cf.dashboard.queues import build_gates, gate_summary_rows
    _texts = lambda g: [r["text"] for r in gate_summary_rows(g)]
    root = _repo(tmp_path)
    (root / "formulas" / "_approved" / "index.json").write_text(json.dumps(
        {"approved": [{"name": "alpha", "niche": "мужской-стиль", "version": 2,
                       "path": "formulas/_approved/мужской-стиль/alpha-v2.json"}]},
        ensure_ascii=False), encoding="utf-8")
    _live_prompt(root, "мужской-стиль",
                 "alpha — `formulas/_approved/мужской-стиль/alpha-v1.json`.")
    # github_path обязателен: без него строка версии не привязана к файлу, и
    # проверять протухание не по чему.
    versions = [{"prompt_id": "brief-мужской-стиль-reel", "active": "TRUE",
                 "github_path": "prompts/briefs/мужской-стиль/reel.md"}]

    summary = _texts(build_gates(root, versions, metrics={}))

    assert any("устаревшие версии" in s for s in summary), summary


def test_retired_prompt_file_is_not_warned_about(tmp_path):
    # Погашенная версия лежит на диске, но сценариев не даёт. Предупреждать о ней
    # значит учить оператора игнорировать предупреждения.
    from cf.dashboard.autogate import stale_prompt_snapshots
    root = _repo(tmp_path)
    (root / "formulas" / "_approved" / "index.json").write_text(json.dumps(
        {"approved": [{"name": "alpha", "niche": "мужской-стиль", "version": 2,
                       "path": "formulas/_approved/мужской-стиль/alpha-v2.json"}]},
        ensure_ascii=False), encoding="utf-8")
    live = _live_prompt(root, "мужской-стиль",
                        "`formulas/_approved/мужской-стиль/alpha-v2.json`")
    retired = live.with_name("reel-v3.md")
    retired.write_text("`formulas/_approved/мужской-стиль/alpha-v1.json`",
                       encoding="utf-8")
    versions = [{"prompt_id": "brief-мужской-стиль-reel", "active": "TRUE",
                 "github_path": "prompts/briefs/мужской-стиль/reel.md"},
                {"prompt_id": "brief-мужской-стиль-reel", "active": "FALSE",
                 "github_path": "prompts/briefs/мужской-стиль/reel-v3.md"}]

    assert stale_prompt_snapshots(root, versions=versions) == []
    # без фильтра по производству протухший файл виден — это разные вопросы
    assert [s["path"] for s in stale_prompt_snapshots(root)] == [
        "prompts/briefs/мужской-стиль/reel-v3.md"]


def test_candidate_in_production_is_still_checked(tmp_path):
    from cf.dashboard.autogate import stale_prompt_snapshots
    root = _repo(tmp_path)
    (root / "formulas" / "_approved" / "index.json").write_text(json.dumps(
        {"approved": [{"name": "alpha", "niche": "мужской-стиль", "version": 2,
                       "path": "formulas/_approved/мужской-стиль/alpha-v2.json"}]},
        ensure_ascii=False), encoding="utf-8")
    live = _live_prompt(root, "мужской-стиль", "актуально alpha-v2")
    cand = live.with_name("reel-v4.md")
    cand.write_text("`formulas/_approved/мужской-стиль/alpha-v1.json`", encoding="utf-8")
    versions = [{"prompt_id": "brief-мужской-стиль-reel", "active": "CANDIDATE",
                 "github_path": "prompts/briefs/мужской-стиль/reel-v4.md"}]

    stale = stale_prompt_snapshots(root, versions=versions)
    assert [s["path"] for s in stale] == ["prompts/briefs/мужской-стиль/reel-v4.md"]


# ── Тикет 08 визуального контура: проверка visual_refs_resolve ───────────────
# Таблица случаев: резолвится / URL не в индексе / разбор не done / кадр за
# пределами манифеста / Raw не прочитаны → ok=None («непрочитанный Raw — не да»).


def _visual_rows(frame_count=10, visual_status="done"):
    rows = _rows()
    for r in rows:
        r["visual_status"] = visual_status
        r["media_manifest"] = json.dumps(
            {"dir": "tiktok/x", "cover": "cover.jpg", "frames_dir": "frames",
             "frame_count": frame_count, "raw_deleted": True})
    return rows


def _formula_with_refs(refs):
    f = _formula()
    f["evidence"] = dict(f["evidence"], visual_refs=refs)
    return f


def test_visual_refs_absent_check_present_not_blocking(tmp_path):
    # Старый чисто текстовый рецепт: проверка видна в чек-листе с честной
    # деталью, но не блокирует — ворота проходят как раньше.
    root = _repo(tmp_path)
    checks = _by_key(formula_checks(root, _formula(), raw_index(_rows())))
    assert checks["visual_refs_resolve"]["ok"] is True
    assert "не применяется" in checks["visual_refs_resolve"]["detail"]
    assert verdict(formula_checks(root, _formula(), raw_index(_rows())))["green"]


def test_visual_refs_resolve_green(tmp_path):
    root = _repo(tmp_path)
    refs = [{"source_url": "https://t.tk/1", "media": "cover"},
            {"source_url": "https://t.tk/2", "media": "frame", "frame_index": 10}]
    checks = _by_key(formula_checks(root, _formula_with_refs(refs),
                                    raw_index(_visual_rows(frame_count=10))))
    assert checks["visual_refs_resolve"]["ok"] is True
    assert "2 резолвятся" in checks["visual_refs_resolve"]["detail"]


def test_visual_ref_url_not_in_index_red(tmp_path):
    root = _repo(tmp_path)
    refs = [{"source_url": "https://t.tk/999", "media": "cover"}]
    checks = _by_key(formula_checks(root, _formula_with_refs(refs),
                                    raw_index(_visual_rows())))
    assert checks["visual_refs_resolve"]["ok"] is False
    assert "нет в raw" in checks["visual_refs_resolve"]["detail"]


def test_visual_ref_row_not_done_red(tmp_path):
    # Медиа скачано, но разбора не было: ссылаться не на что.
    root = _repo(tmp_path)
    refs = [{"source_url": "https://t.tk/1", "media": "cover"}]
    checks = _by_key(formula_checks(
        root, _formula_with_refs(refs),
        raw_index(_visual_rows(visual_status=""))))
    assert checks["visual_refs_resolve"]["ok"] is False
    assert "не разобрано" in checks["visual_refs_resolve"]["detail"]


def test_visual_ref_frame_beyond_manifest_red(tmp_path):
    root = _repo(tmp_path)
    refs = [{"source_url": "https://t.tk/1", "media": "frame", "frame_index": 11}]
    checks = _by_key(formula_checks(root, _formula_with_refs(refs),
                                    raw_index(_visual_rows(frame_count=10))))
    assert checks["visual_refs_resolve"]["ok"] is False
    assert "вне манифеста" in checks["visual_refs_resolve"]["detail"]


def test_visual_refs_raw_unread_is_none_not_yes(tmp_path):
    # Правило №2: непрочитанные Raw-вкладки — ok=None, рецепт уходит к человеку.
    root = _repo(tmp_path)
    refs = [{"source_url": "https://t.tk/1", "media": "cover"}]
    checks = _by_key(formula_checks(root, _formula_with_refs(refs), None))
    assert checks["visual_refs_resolve"]["ok"] is None
    v = verdict(formula_checks(root, _formula_with_refs(refs), None))
    assert not v["green"]
