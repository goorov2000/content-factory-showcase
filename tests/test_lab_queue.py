"""Очередь Лаборатории: proposed-формулы и предложения ниш в контексте lab_context."""
import json

from cf.dashboard.sections import lab_context


class FakeCache:
    stale = False

    def rows(self, tab):
        return []


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def _approved_index(root, entries=None):
    _write(root / "formulas" / "_approved" / "index.json",
           {"approved": entries or []})


def test_lab_context_lists_proposed_formulas_and_niches(tmp_path):
    f = tmp_path / "formulas" / "стритвир" / "grid.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"name": "grid", "niche": "стритвир", "version": 1,
                             "status": "proposed", "hook_structure": "5 дней — 5 образов",
                             "evidence": {"source_urls": ["u1", "u2", "u3"],
                                          "avg_views": 100000, "avg_er": 0.04}}),
                 encoding="utf-8")
    (tmp_path / "formulas" / "_approved").mkdir()
    (tmp_path / "formulas" / "_approved" / "index.json").write_text(
        json.dumps({"approved": []}), encoding="utf-8")
    np = tmp_path / "agent-runtime" / "niche-proposals"
    np.mkdir(parents=True)
    (np / "2026-07-15-tall.json").write_text(json.dumps(
        {"name": "одежда-для-высоких", "title": "Для высоких", "cluster_size": 37,
         "examples": [], "status": "proposed"}), encoding="utf-8")
    ctx = lab_context(FakeCache(), root=tmp_path)
    assert [q["name"] for q in ctx["formula_queue"]] == ["grid"]
    assert ctx["formula_queue"][0]["evidence_urls"] == ["u1", "u2", "u3"]
    assert [n["name"] for n in ctx["niche_queue"]] == ["одежда-для-высоких"]


def test_lab_context_paused_formula_has_reason_and_excluded_from_queue(tmp_path):
    root = tmp_path
    _approved_index(root)
    f = root / "formulas" / "стиль" / "paused-one.json"
    _write(f, {"name": "paused-one", "niche": "стиль", "version": 1,
               "status": "paused", "status_reason": "3 из 5 reject",
               "hook_structure": "интро", "evidence": {}})
    ctx = lab_context(FakeCache(), root=root)
    assert ctx["formula_queue"] == []
    assert len(ctx["paused_formulas"]) == 1
    assert ctx["paused_formulas"][0]["name"] == "paused-one"
    assert ctx["paused_formulas"][0]["status_reason"] == "3 из 5 reject"


def test_lab_context_excludes_approved_and_rejected_and_approved_dir(tmp_path):
    root = tmp_path
    _approved_index(root, [{"name": "f-one", "niche": "стиль", "version": 1,
                            "path": "formulas/стиль/f-one.json",
                            "approved_at": "2026-07-14T17:00:00+00:00"}])
    _write(root / "formulas" / "стиль" / "f-one.json",
           {"name": "f-one", "niche": "стиль", "version": 1, "status": "approved"})
    _write(root / "formulas" / "стиль" / "rejected-one.json",
           {"name": "rejected-one", "niche": "стиль", "version": 1, "status": "rejected"})
    # файл прямо в _approved (например, случайно скопирован) должен игнорироваться сканом
    _write(root / "formulas" / "_approved" / "stray.json",
           {"name": "stray", "niche": "стиль", "version": 1, "status": "proposed"})
    ctx = lab_context(FakeCache(), root=root)
    assert ctx["formula_queue"] == []
    assert ctx["paused_formulas"] == []


def test_lab_context_caps_long_hook_structure(tmp_path):
    root = tmp_path
    _approved_index(root)
    _write(root / "formulas" / "стиль" / "long.json",
           {"name": "long", "niche": "стиль", "version": 1, "status": "proposed",
            "hook_structure": "х" * 300,
            "evidence": {"source_urls": "не-список"}})  # заодно защита от не-списка
    ctx = lab_context(FakeCache(), root=root)
    assert len(ctx["formula_queue"][0]["hook_structure"]) == 200
    assert ctx["formula_queue"][0]["evidence_urls"] == []


def test_formula_queue_shows_full_descriptive_fields(tmp_path):
    # очередь одобрения — go/no-go решение: проблема/формат/условия не режутся
    # (иначе гипотеза обрывается на полуслове и читается как «бред в конце»)
    _approved_index(tmp_path)
    problem = ("Аудитория пролистывает прямую рекламу магазинов (нижняя квартиль "
               "ниши — каталожные ролики с ценой и адресом, ER < 0.03); удержать "
               "её может только развлекательный контент, в котором магазин и товар "
               "— часть сюжета, а не объект продажи.")
    conditions = "Ниша бренды-магазины; " + "у" * 320  # длиннее прежнего лимита 300
    _write(tmp_path / "formulas" / "бренды-магазины" / "skit.json",
           {"name": "skit", "niche": "бренды-магазины", "version": 1,
            "status": "proposed", "problem_definition": problem,
            "solution_structure": {"format": "х" * 260},  # длиннее прежнего 220
            "conditions": conditions,
            "evidence": {"source_urls": ["u1"]}})
    q = lab_context(FakeCache(), root=tmp_path)["formula_queue"][0]
    assert q["problem"] == problem                 # целиком, с «объект продажи» в конце
    assert q["problem"].endswith("объект продажи.")
    assert "…" not in q["problem"]
    assert len(q["solution_format"]) == 260 and "…" not in q["solution_format"]
    assert q["conditions"] == conditions and "…" not in q["conditions"]


def test_formula_queue_flags_niche_without_brief_prompt(tmp_path):
    # P5.2: у формулы, чья ниша не имеет prompts/briefs/{niche}/reel.md, флаг False
    root = tmp_path
    _approved_index(root)
    # ниша С промптом
    (root / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (root / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    _write(root / "formulas" / "мужские-образы" / "has.json",
           {"name": "has", "niche": "мужские-образы", "version": 1,
            "status": "proposed", "hook_structure": "хук",
            "evidence": {"source_urls": []}})
    # ниша БЕЗ промпта
    _write(root / "formulas" / "барбершоп" / "no.json",
           {"name": "no", "niche": "барбершоп", "version": 1, "status": "proposed",
            "hook_structure": "хук", "evidence": {"source_urls": []}})
    ctx = lab_context(FakeCache(), root=root)
    by_name = {q["name"]: q for q in ctx["formula_queue"]}
    assert by_name["has"]["niche_has_prompt"] is True
    assert by_name["no"]["niche_has_prompt"] is False


def test_approved_formula_flags_niche_without_brief_prompt(tmp_path):
    # P5.2: флаг niche_has_prompt переживает approve — есть и у approved-формул
    root = tmp_path
    _approved_index(root, [
        {"name": "with", "niche": "мужские-образы", "version": 1,
         "path": "formulas/мужские-образы/with.json",
         "approved_at": "2026-07-14T17:00:00+00:00"},
        {"name": "without", "niche": "мужской-стиль", "version": 1,
         "path": "formulas/мужской-стиль/without.json",
         "approved_at": "2026-07-15T17:00:00+00:00"},
    ])
    _write(root / "formulas" / "мужские-образы" / "with.json",
           {"name": "with", "niche": "мужские-образы", "version": 1, "status": "approved"})
    _write(root / "formulas" / "мужской-стиль" / "without.json",
           {"name": "without", "niche": "мужской-стиль", "version": 1, "status": "approved"})
    # brief-промпт есть только у мужские-образы
    (root / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (root / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    ctx = lab_context(FakeCache(), root=root)
    by_name = {f["name"]: f for f in ctx["formulas"]}
    assert by_name["with"]["niche_has_prompt"] is True
    assert by_name["without"]["niche_has_prompt"] is False


def test_paused_formula_flags_niche_without_brief_prompt(tmp_path):
    # P5.2: флаг niche_has_prompt проставлен и у paused-формул
    root = tmp_path
    _approved_index(root)
    _write(root / "formulas" / "барбершоп" / "p.json",
           {"name": "p", "niche": "барбершоп", "version": 1, "status": "paused",
            "status_reason": "пауза", "hook_structure": "х", "evidence": {}})
    ctx = lab_context(FakeCache(), root=root)
    assert ctx["paused_formulas"][0]["niche_has_prompt"] is False


def test_lab_context_broken_json_skipped_neighbors_kept(tmp_path):
    root = tmp_path
    _approved_index(root)
    _write(root / "formulas" / "стиль" / "broken.json", "{оборвано")
    _write(root / "formulas" / "стиль" / "ok.json",
           {"name": "ok", "niche": "стиль", "version": 1, "status": "proposed",
            "hook_structure": "хук", "evidence": {"source_urls": []}})
    np = root / "agent-runtime" / "niche-proposals"
    _write(np / "broken.json", "{оборвано")
    _write(np / "good.json", {"name": "good-niche", "title": "Хорошая",
                               "cluster_size": 10, "examples": [], "status": "proposed"})
    ctx = lab_context(FakeCache(), root=root)
    assert [q["name"] for q in ctx["formula_queue"]] == ["ok"]
    assert [n["name"] for n in ctx["niche_queue"]] == ["good-niche"]


def _config_exclude(root, niches):
    _write(root / "cf.config.json",
           {"dashboard": {"fanout": {"exclude_niches": niches}}})


def test_fanout_exclude_niches_reads_config(tmp_path):
    from cf.dashboard.sections import fanout_exclude_niches
    assert fanout_exclude_niches(tmp_path) == set()          # нет файла → пусто
    _config_exclude(tmp_path, ["женская-мода", " знаменитости-мода ", ""])
    assert fanout_exclude_niches(tmp_path) == {"женская-мода", "знаменитости-мода"}


def test_formula_queue_flags_excluded_niche(tmp_path):
    _approved_index(tmp_path)
    _config_exclude(tmp_path, ["женская-мода"])
    _write(tmp_path / "formulas" / "женская-мода" / "concept.json",
           {"name": "concept", "niche": "женская-мода", "version": 1,
            "status": "proposed", "evidence": {"source_urls": ["u1"]}})
    _write(tmp_path / "formulas" / "мужские-образы" / "styling.json",
           {"name": "styling", "niche": "мужские-образы", "version": 1,
            "status": "proposed", "evidence": {"source_urls": ["u1"]}})
    q = {f["name"]: f for f in lab_context(FakeCache(), root=tmp_path)["formula_queue"]}
    assert q["concept"]["niche_excluded"] is True
    assert q["styling"]["niche_excluded"] is False


def test_formula_queue_no_exclude_flag_without_config(tmp_path):
    _approved_index(tmp_path)
    _write(tmp_path / "formulas" / "женская-мода" / "concept.json",
           {"name": "concept", "niche": "женская-мода", "version": 1,
            "status": "proposed", "evidence": {"source_urls": ["u1"]}})
    q = lab_context(FakeCache(), root=tmp_path)["formula_queue"][0]
    assert q["niche_excluded"] is False                       # нет конфига → не помечена
