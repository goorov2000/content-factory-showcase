"""Очереди конвейера и ворота: единый предикат готовности темы (queues.py).

Главное, что здесь доказывается, — состояние «файл промпта есть, а активной
версии нет» больше не невидимо. До появления queues.py дашборд смотрел только
файл, генератор — только строку prompt_versions, а метрика воронки — третьим
способом; тема в этом состоянии тихо выпадала из производства.
"""
import json

import pytest

from cf.dashboard.queues import (COVERAGE_FULL, COVERAGE_PARTIAL, PROMPT_DRAFT,
                                 PROMPT_FILE_ONLY, PROMPT_NONE, PROMPT_READY,
                                 PROMPT_UNCOVERED, approved_by_niche, brief_gate,
                                 build_gates, formula_prompt_state, gate_banner,
                                 gate_summary_rows, has_active_version,
                                 niches_awaiting_prompt_draft,
                                 niches_ready_for_briefs,
                                 niches_with_uncovered_recipes, prompt_coverage,
                                 prompt_drafts, prompt_gate, prompt_state,
                                 proposed_by_niche, recipe_gate)


def _texts(gates):
    """Тексты сводки состояния завода (queues.gate_summary_rows)."""
    return [row["text"] for row in gate_summary_rows(gates)]


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def _approved(root, entries):
    _write(root / "formulas" / "_approved" / "index.json", {"approved": entries})


def _entry(name, niche, version=1):
    return {"name": name, "niche": niche, "version": version,
            "path": f"formulas/_approved/{niche}/{name}-v{version}.json"}


def _prompt_file(root, niche):
    _write(root / "prompts" / "briefs" / niche / "reel.md", "# промпт\n")


def _draft(root, niche, status="proposed", date="2026-07-26"):
    _write(root / "proposals" / f"{date}-brief-{niche}-reel.md",
           f"---\nstatus: {status}\nprompt_id: brief-{niche}-reel\n---\n# черновик\n")


def _active(niche):
    return {"prompt_id": f"brief-{niche}-reel", "active": "TRUE"}


# ── prompt_state: четыре состояния ───────────────────────────────────────────

def test_prompt_state_none_when_nothing_written(tmp_path):
    assert prompt_state(tmp_path, "тема", []) == PROMPT_NONE


def test_prompt_state_draft_when_proposal_awaits(tmp_path):
    _draft(tmp_path, "тема")
    assert prompt_state(tmp_path, "тема", []) == PROMPT_DRAFT


def test_prompt_state_file_only_when_version_not_activated(tmp_path):
    # Ровно та дыра, ради которой модуль и написан: файл сохранён, а
    # log-prompt-version не выполнен — дашборд раньше считал тему готовой.
    _prompt_file(tmp_path, "тема")
    assert prompt_state(tmp_path, "тема", []) == PROMPT_FILE_ONLY


def test_prompt_state_ready_needs_both_file_and_active_version(tmp_path):
    _prompt_file(tmp_path, "тема")
    assert prompt_state(tmp_path, "тема", [_active("тема")]) == PROMPT_READY


def test_prompt_state_ignores_deactivated_version(tmp_path):
    _prompt_file(tmp_path, "тема")
    rows = [{"prompt_id": "brief-тема-reel", "active": "FALSE"}]
    assert prompt_state(tmp_path, "тема", rows) == PROMPT_FILE_ONLY


def test_prompt_state_unreadable_versions_is_not_ready(tmp_path):
    # Правило №2: лист не прочитан -> судить нельзя, но и «всё хорошо» молча
    # выдавать нельзя. Безопасная сторона — тема показывается ждущей.
    _prompt_file(tmp_path, "тема")
    assert prompt_state(tmp_path, "тема", None) == PROMPT_FILE_ONLY
    assert has_active_version(None, "тема") is None


def test_prompt_state_rejects_path_injection(tmp_path):
    assert prompt_state(tmp_path, "../../etc", []) == PROMPT_NONE


def test_prompt_drafts_skips_applied_and_rejected(tmp_path):
    _draft(tmp_path, "живая")
    _draft(tmp_path, "применённая", status="approved")
    _draft(tmp_path, "отклонённая", status="rejected")
    assert set(prompt_drafts(tmp_path)) == {"живая"}


def test_prompt_drafts_prefers_newest_by_date_prefix(tmp_path):
    _draft(tmp_path, "тема", date="2026-07-01")
    _draft(tmp_path, "тема", date="2026-07-26")
    assert prompt_drafts(tmp_path)["тема"].name == "2026-07-26-brief-тема-reel.md"


# ── счёт рецептов по темам ───────────────────────────────────────────────────

def test_approved_and_proposed_counted_per_niche(tmp_path):
    _approved(tmp_path, [_entry("a", "альфа"), _entry("b", "альфа"),
                         _entry("c", "бета")])
    _write(tmp_path / "formulas" / "альфа" / "d.json",
           {"name": "d", "niche": "альфа", "status": "proposed"})
    _write(tmp_path / "formulas" / "гамма" / "e.json",
           {"name": "e", "niche": "гамма", "status": "proposed"})
    _write(tmp_path / "formulas" / "гамма" / "f.json",
           {"name": "f", "niche": "гамма", "status": "rejected"})
    assert approved_by_niche(tmp_path) == {"альфа": 2, "бета": 1}
    assert proposed_by_niche(tmp_path) == {"альфа": 1, "гамма": 1}


def test_proposed_ignores_approved_snapshot_dir(tmp_path):
    # Снапшоты в _approved иммутабельны и в очередь одобрения не попадают.
    _approved(tmp_path, [])
    _write(tmp_path / "formulas" / "_approved" / "тема" / "s-v1.json",
           {"name": "s", "niche": "тема", "status": "proposed"})
    assert proposed_by_niche(tmp_path) == {}


# ── очереди воркеров ─────────────────────────────────────────────────────────

def test_prompt_draft_queue_needs_approved_recipe(tmp_path):
    # Тема с одними черновиками рецептов промпт не получает: выводить его не из чего.
    _approved(tmp_path, [_entry("a", "сготовая")])
    _write(tmp_path / "formulas" / "сырая" / "b.json",
           {"name": "b", "niche": "сырая", "status": "proposed"})
    assert niches_awaiting_prompt_draft(tmp_path, [], exclude=set()) == ["сготовая"]


def test_prompt_draft_queue_skips_theme_with_pending_draft(tmp_path):
    # Иначе агент писал бы новый черновик по той же теме каждый прогон.
    _approved(tmp_path, [_entry("a", "тема")])
    _draft(tmp_path, "тема")
    assert niches_awaiting_prompt_draft(tmp_path, [], exclude=set()) == []


def test_prompt_draft_queue_skips_excluded_theme(tmp_path):
    _approved(tmp_path, [_entry("a", "нецелевая")])
    assert niches_awaiting_prompt_draft(tmp_path, [], exclude={"нецелевая"}) == []


def test_briefs_queue_only_ready_themes(tmp_path):
    _approved(tmp_path, [_entry("a", "готовая"), _entry("b", "сфайлом"),
                         _entry("c", "пустая")])
    _prompt_file(tmp_path, "готовая")
    _prompt_file(tmp_path, "сфайлом")          # файл есть, версия не включена
    versions = [_active("готовая")]
    assert niches_ready_for_briefs(tmp_path, versions, exclude=set()) == ["готовая"]


def test_briefs_queue_empty_is_the_signal_to_skip_paid_call(tmp_path):
    _approved(tmp_path, [_entry("a", "тема")])
    assert niches_ready_for_briefs(tmp_path, [], exclude=set()) == []


# ── покрытие рецептов промптом темы ──────────────────────────────────────────
# Тот же молчаливый скип, что породил ворота, только этажом ниже: промпт темы
# включён, а рецепты, которых он не называет, генератор пропускает. Признак
# эвристический (упоминание имени в тексте) и НИЧЕГО не блокирует — тесты ниже
# фиксируют обе половины: и что состояние стало видимым, и что производство от
# него не гаснет.

def _prompt_naming(root, niche, names):
    """Промпт темы, называющий именно эти рецепты (как заголовок боевого файла)."""
    body = "# промпт\n" + "".join(f"Ты генерируешь бриф по формуле `{n}`.\n"
                                  for n in names)
    _write(root / "prompts" / "briefs" / niche / "reel.md", body)


_МУЖСКИЕ_ОБРАЗЫ = ["short-styling-idea-reel", "grwm-interactive-frame",
                   "reference-recreation-fit", "style-manifesto-statement"]


def _uncovered_case(root):
    """Разобранный случай 26.07: 4 утверждённых рецепта, промпт про один."""
    _approved(root, [_entry(n, "мужские-образы") for n in _МУЖСКИЕ_ОБРАЗЫ])
    _prompt_naming(root, "мужские-образы", _МУЖСКИЕ_ОБРАЗЫ[:1])
    return [_active("мужские-образы")]


def test_coverage_names_recipes_the_prompt_does_not_cover(tmp_path):
    # Отчёт brief-generator 2026-07-26T16:13:54: «grwm-interactive-frame,
    # reference-recreation-fit, style-manifesto-statement — активный промпт
    # brief-мужские-образы-reel v2 написан только под short-styling-idea-reel».
    versions = _uncovered_case(tmp_path)
    coverage = prompt_coverage(tmp_path, "мужские-образы")
    assert coverage["state"] == COVERAGE_PARTIAL
    assert coverage["approved"] == 4
    assert coverage["covered"] == _МУЖСКИЕ_ОБРАЗЫ[:1]
    assert coverage["uncovered"] == _МУЖСКИЕ_ОБРАЗЫ[1:]
    assert coverage["state_label"] == ("промпт включён, но называет 1 рецепт из 4"
                                       " — по остальным производство может не идти")


def test_coverage_does_not_stop_production(tmp_path):
    # Ворота не судят о качестве (решение разбора): тема как производила, так и
    # производит, ворота открыты, красной полосы нет.
    versions = _uncovered_case(tmp_path)
    assert prompt_state(tmp_path, "мужские-образы", versions) == PROMPT_READY
    assert niches_ready_for_briefs(tmp_path, versions,
                                   exclude=set()) == ["мужские-образы"]
    gate = prompt_gate(tmp_path, versions, exclude=set())
    assert gate["blocking"] is False and gate["rows"] == []
    assert gate_banner(build_gates(tmp_path, versions, metrics={},
                                   exclude=set())) == ""


def test_uncovered_recipes_are_visible_in_gate_and_summary(tmp_path):
    # Молчаливый зелёный кончился: и ворота, и сводка «ждут вас» называют счёт.
    versions = _uncovered_case(tmp_path)
    gate = prompt_gate(tmp_path, versions, exclude=set())
    assert [c["niche"] for c in gate["coverage"]] == ["мужские-образы"]
    assert gate["coverage"][0]["uncovered"] == _МУЖСКИЕ_ОБРАЗЫ[1:]
    gates = build_gates(tmp_path, versions, metrics={}, exclude=set())
    assert "3 рецепта не названы правилами своей темы" in _texts(gates)


def test_formula_prompt_state_answers_per_recipe(tmp_path):
    versions = _uncovered_case(tmp_path)
    assert formula_prompt_state(tmp_path, "мужские-образы",
                                "short-styling-idea-reel", versions) == PROMPT_READY
    assert formula_prompt_state(tmp_path, "мужские-образы",
                                "reference-recreation-fit",
                                versions) == PROMPT_UNCOVERED


def test_formula_prompt_state_falls_back_to_theme_state(tmp_path):
    # Пока тема стоит на воротах, про покрытие говорить нечего: её состояние —
    # состояние темы, и решать оператору именно его.
    _approved(tmp_path, [_entry("a", "тема")])
    _prompt_file(tmp_path, "тема")
    assert formula_prompt_state(tmp_path, "тема", "a", []) == PROMPT_FILE_ONLY
    assert formula_prompt_state(tmp_path, "друг", "a", []) == PROMPT_NONE


def test_coverage_full_when_prompt_names_every_recipe(tmp_path):
    # Черновик v3 называет все четыре рецепта — покрытие закрывается.
    _approved(tmp_path, [_entry(n, "мужские-образы") for n in _МУЖСКИЕ_ОБРАЗЫ])
    _prompt_naming(tmp_path, "мужские-образы", _МУЖСКИЕ_ОБРАЗЫ)
    versions = [_active("мужские-образы")]
    assert prompt_coverage(tmp_path, "мужские-образы")["state"] == COVERAGE_FULL
    assert niches_with_uncovered_recipes(tmp_path, versions, exclude=set()) == []
    assert prompt_gate(tmp_path, versions, exclude=set())["coverage"] == []


def test_coverage_counts_name_inside_snapshot_path(tmp_path):
    # Имя рецепта чаще всего приходит ссылкой на снапшот — это упоминание.
    _approved(tmp_path, [_entry("grwm-interactive-frame", "тема")])
    _write(tmp_path / "prompts" / "briefs" / "тема" / "reel.md",
           "Снапшот: formulas/_approved/тема/grwm-interactive-frame-v2.json\n")
    assert prompt_coverage(tmp_path, "тема")["state"] == COVERAGE_FULL


def test_coverage_silent_about_theme_still_at_the_gate(tmp_path):
    # Тема без включённой версии видна СТРОКОЙ ворот; второе сообщение о ней
    # заставляло бы решать, какое из двух главное.
    _approved(tmp_path, [_entry("a", "сфайлом")])
    _prompt_naming(tmp_path, "сфайлом", [])
    assert niches_with_uncovered_recipes(tmp_path, [], exclude=set()) == []


def test_coverage_skips_excluded_theme(tmp_path):
    _approved(tmp_path, [_entry("a", "нецелевая")])
    _prompt_naming(tmp_path, "нецелевая", [])
    versions = [_active("нецелевая")]
    assert niches_with_uncovered_recipes(tmp_path, versions,
                                         exclude={"нецелевая"}) == []


def test_coverage_is_none_when_there_is_nothing_to_judge_by(tmp_path):
    # Правило №2: нет файла промпта или нет названных рецептов — «не знаю», а не
    # «ничего не покрыто». None и пустой uncovered — разные ответы.
    _approved(tmp_path, [_entry("a", "тема")])
    assert prompt_coverage(tmp_path, "тема") is None      # файла промпта нет
    _prompt_naming(tmp_path, "тема", [])
    assert prompt_coverage(tmp_path, "тема", formulas=[]) is None
    assert prompt_coverage(tmp_path, "тема", formulas=[""]) is None


# ── ворота ───────────────────────────────────────────────────────────────────

def test_recipe_gate_blocks_only_theme_without_any_approved(tmp_path):
    # Решение оператора 2026-07-26: ворота держат ПО ТЕМЕ. Черновик по уже
    # работающей теме не гасит производство — иначе один неразобранный рецепт
    # каждый день останавливал бы единственную живую тему.
    _approved(tmp_path, [_entry("a", "работающая")])
    _write(tmp_path / "formulas" / "работающая" / "new.json",
           {"name": "new", "niche": "работающая", "status": "proposed"})
    _write(tmp_path / "formulas" / "новая" / "first.json",
           {"name": "first", "niche": "новая", "status": "proposed"})
    gate = recipe_gate(tmp_path, exclude=set())
    assert gate["blocking"] is True
    assert [r["niche"] for r in gate["rows"]] == ["новая"]
    assert [r["niche"] for r in gate["informational"]] == ["работающая"]


def test_recipe_gate_open_when_no_drafts(tmp_path):
    _approved(tmp_path, [_entry("a", "тема")])
    assert recipe_gate(tmp_path, exclude=set())["blocking"] is False


def test_prompt_gate_lists_every_blocking_state(tmp_path):
    _approved(tmp_path, [_entry("a", "нетничего"), _entry("b", "счерновиком"),
                         _entry("c", "сфайлом"), _entry("d", "готовая")])
    _draft(tmp_path, "счерновиком")
    _prompt_file(tmp_path, "сфайлом")
    _prompt_file(tmp_path, "готовая")
    gate = prompt_gate(tmp_path, [_active("готовая")], exclude=set())
    assert gate["blocking"] is True
    assert {r["niche"]: r["state"] for r in gate["rows"]} == {
        "нетничего": PROMPT_NONE,
        "счерновиком": PROMPT_DRAFT,
        "сфайлом": PROMPT_FILE_ONLY,
    }
    draft_row = next(r for r in gate["rows"] if r["niche"] == "счерновиком")
    assert draft_row["draft_name"] == "2026-07-26-brief-счерновиком-reel.md"


def test_prompt_gate_ignores_theme_without_approved_recipe(tmp_path):
    _approved(tmp_path, [])
    _write(tmp_path / "formulas" / "сырая" / "b.json",
           {"name": "b", "niche": "сырая", "status": "proposed"})
    assert prompt_gate(tmp_path, [], exclude=set())["blocking"] is False


def test_brief_gate_never_blocks_the_machine():
    # Продукт цикла уже произведён: эти ворота держат съёмку, не автоматику.
    gate = brief_gate({"briefs_attention": 7})
    assert gate["blocking"] is False and gate["count"] == 7


# ── подпись актора ворот следует политике, а не константе ────────────────────
# 2026-07-28, элемент 17в. До правки здесь в трёх местах стояло жёсткое «вы»,
# хотя ворота рецептов и правил сценариев закрывает машина с вечера 26.07: лента
# подписывала человеком то, чего он не делает.

def _policy(root, **policy):
    _write(root / "cf.config.json", {"gates": {"policy": policy}})


def test_auto_gates_are_signed_by_the_machine(tmp_path):
    _policy(tmp_path, recipes="auto", prompt="auto")
    gates = build_gates(tmp_path, [], metrics={}, exclude=set())
    assert gates["recipes"]["actor"] == "CLAUDE CODE"
    assert gates["prompt"]["actor"] == "CLAUDE CODE"


def test_manual_policy_returns_the_signature_to_the_human(tmp_path):
    # Рычаг возврата в ручной режим стоит одной строки конфига — и подпись
    # обязана поехать вместе с ним, без правки кода.
    _policy(tmp_path, recipes="manual", prompt="manual")
    gates = build_gates(tmp_path, [], metrics={}, exclude=set())
    assert gates["recipes"]["actor"] == "вы"
    assert gates["prompt"]["actor"] == "вы"


def test_policy_is_read_per_gate_not_globally(tmp_path):
    _policy(tmp_path, recipes="manual", prompt="auto")
    gates = build_gates(tmp_path, [], metrics={}, exclude=set())
    assert gates["recipes"]["actor"] == "вы"
    assert gates["prompt"]["actor"] == "CLAUDE CODE"


def test_brief_gate_stays_human_whatever_the_policy_says(tmp_path):
    # Сценарии остаются за человеком по решению владельца: дальше идёт съёмка,
    # то есть его время и деньги. Ключа политики у этих ворот нет вовсе.
    _policy(tmp_path, recipes="auto", prompt="auto", briefs="auto")
    gates = build_gates(tmp_path, [], metrics={"briefs_attention": 3}, exclude=set())
    assert gates["briefs"]["actor"] == "вы"


# ── сводка для главной ───────────────────────────────────────────────────────

def test_banner_names_the_first_holding_gate(tmp_path):
    _approved(tmp_path, [_entry("a", "бренды-магазины")])
    gates = build_gates(tmp_path, [], metrics={}, exclude=set())
    banner = gate_banner(gates)
    assert "Включение правил сценариев темы" in banner and "бренды-магазины" in banner


def test_banner_empty_when_conveyor_moves(tmp_path):
    _approved(tmp_path, [_entry("a", "тема")])
    _prompt_file(tmp_path, "тема")
    gates = build_gates(tmp_path, [_active("тема")], metrics={}, exclude=set())
    assert gate_banner(gates) == ""


def test_recipe_gate_wins_the_banner_over_prompt_gate(tmp_path):
    # Порядок ленты: рецепты выше промпта. Оператору называют ПЕРВЫЕ ворота,
    # иначе он закроет вторые и упрётся в первые следующим прогоном.
    _write(tmp_path / "formulas" / "новая" / "f.json",
           {"name": "f", "niche": "новая", "status": "proposed"})
    _approved(tmp_path, [_entry("a", "другая")])
    gates = build_gates(tmp_path, [], metrics={}, exclude=set())
    assert "Одобрение рецептов" in gate_banner(gates)


def test_summary_counts_every_gate(tmp_path):
    _write(tmp_path / "formulas" / "новая" / "f.json",
           {"name": "f", "niche": "новая", "status": "proposed"})
    _approved(tmp_path, [_entry("a", "безпромпта")])
    gates = build_gates(tmp_path, [], metrics={"briefs_attention": 3},
                        exclude=set())
    summary = _texts(gates)
    assert summary == ["1 тема без утверждённого рецепта",
                       "1 тема без включённых правил сценариев",
                       "3 сценариев ждут решения"]


@pytest.mark.parametrize("count,word", [(1, "тема"), (2, "темы"), (5, "тем"),
                                        (11, "тем"), (21, "тема")])
def test_theme_plural_is_declined(tmp_path, count, word):
    entries = [_entry(f"f{i}", f"тема{i}") for i in range(count)]
    _approved(tmp_path, entries)
    gates = build_gates(tmp_path, [], metrics={}, exclude=set())
    assert f"{count} {word} без включённых правил сценариев" in _texts(gates)


# --- сценарии под площадку без активного аккаунта (2026-08-04) ----------------------

def test_briefs_for_inactive_platforms_counts_by_platform():
    """Аудит 2026-08-04: 113 из 114 сценариев написаны под TikTok, а все три
    tiktok-слота в cf.config.json — PLACEHOLDER/active:false. Снять было некуда, и
    об этом не говорил ни один экран."""
    from cf.dashboard.queues import briefs_for_inactive_platforms
    accounts = [{"slug": "tiktok-1", "platform": "tiktok", "active": False},
                {"slug": "instagram-1", "platform": "instagram", "active": True}]
    briefs = ([{"platform": "tiktok"}] * 3) + [{"platform": "instagram"}]

    assert briefs_for_inactive_platforms(briefs, accounts) == {"tiktok": 3}


def test_no_accounts_configured_gives_no_warning():
    # Правило №2: судить по НЕзаданному нельзя — пустой реестр молчит, а не кричит.
    from cf.dashboard.queues import briefs_for_inactive_platforms
    assert briefs_for_inactive_platforms([{"platform": "tiktok"}], []) == {}
    assert briefs_for_inactive_platforms([{"platform": "tiktok"}], None) == {}


def test_all_platforms_live_gives_no_warning():
    from cf.dashboard.queues import briefs_for_inactive_platforms
    accounts = [{"slug": "tiktok-1", "platform": "tiktok", "active": True}]
    assert briefs_for_inactive_platforms([{"platform": "tiktok"}], accounts) == {}


def test_brief_without_platform_is_not_counted():
    # пустая платформа — не «неактивная», а неизвестная; в предупреждение не идёт
    from cf.dashboard.queues import briefs_for_inactive_platforms
    accounts = [{"slug": "instagram-1", "platform": "instagram", "active": True}]
    assert briefs_for_inactive_platforms([{"platform": ""}, {}], accounts) == {}


def test_inactive_platform_warning_text():
    from cf.dashboard.queues import inactive_platform_warning
    assert inactive_platform_warning({}) == ""
    text = inactive_platform_warning({"tiktok": 105})
    assert "tiktok: 105 сценариев" in text and "cf.config.json" in text
