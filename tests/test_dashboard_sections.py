import datetime
import json

from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from cf.dashboard.data import DataCache
from cf.dashboard.sections import (_ritual_file_date, brief_origin, lab_context,
                                   performance_context, read_frontmatter,
                                   rituals_context, runs_context, sources_context)
from tests.fakes import FakeSheets


def make_cache(tables):
    return DataCache(FakeSheets(tables))


def make_client(tables=None, **kwargs):
    sheets = FakeSheets(tables or {})
    return TestClient(create_app(sheets=sheets, **kwargs))


class BrokenSheets(FakeSheets):
    def read_rows(self, tab_key):
        raise ConnectionError("sheets down")


RAW_TABLES = {
    "raw_tiktok": [
        {"source_url": "https://t/1", "account": "a1", "views": 100, "likes": 1,
         "comments": 0, "saves": 2, "posted_at": "2026-07-01", "niche": "pets",
         "hook_text": "хук раз"},
        {"source_url": "https://t/2", "account": "a2", "views": 900, "likes": 9,
         "comments": 3, "saves": 5, "posted_at": "2026-07-10", "niche": "стиль",
         "hook_text": "хук два"},
    ],
    "raw_instagram": [
        {"source_url": "https://i/1", "account": "b1", "views": 50, "likes": 1,
         "comments": 0, "saves": 0, "posted_at": "2026-07-05", "niche": "стиль",
         "hook_text": "иг хук"},
    ],
}


def test_sources_context_sorts_desc_and_counts():
    ctx = sources_context(make_cache(RAW_TABLES), "tiktok")
    assert ctx["tab"] == "tiktok"
    assert ctx["counts"] == {"tiktok": 2, "instagram": 1}
    assert [r["source_url"] for r in ctx["rows"]] == ["https://t/2", "https://t/1"]
    assert ctx["total"] == 2


def test_sources_context_instagram_and_bad_tab():
    cache = make_cache(RAW_TABLES)
    assert sources_context(cache, "instagram")["rows"][0]["account"] == "b1"
    assert sources_context(cache, "чушь")["tab"] == "tiktok"


def test_sources_context_caps_rows():
    rows = [{"source_url": f"u{i}", "posted_at": f"2026-06-{i % 28 + 1:02d}"}
            for i in range(250)]
    ctx = sources_context(make_cache({"raw_tiktok": rows, "raw_instagram": []}), "tiktok")
    assert len(ctx["rows"]) == 200
    assert ctx["total"] == 250


PERF_TABLES = {
    "reels": [
        {"reel_id": "R1", "brief_id": "B1", "platform": "tiktok",
         "post_url": "https://t/p1", "published_at": "2026-07-01",
         "production_notes": ""},
        {"reel_id": "R2", "brief_id": "B2", "platform": "instagram",
         "post_url": "https://i/p2", "published_at": "2026-07-08",
         "production_notes": "без плашки"},
    ],
    "performance": [
        {"reel_id": "R1", "brief_id": "B1", "prompt_version": "v1",
         "views": "5000", "er": "4.2", "measured_at": "2026-07-05"},
    ],
}


def test_performance_context_reels_default_sorted():
    ctx = performance_context(make_cache(PERF_TABLES), "reels")
    assert ctx["tab"] == "reels"
    assert ctx["counts"] == {"reels": 2, "metrics": 1}
    assert [r["reel_id"] for r in ctx["rows"]] == ["R2", "R1"]


def test_performance_context_metrics_tab_and_bad_tab():
    cache = make_cache(PERF_TABLES)
    assert performance_context(cache, "metrics")["rows"][0]["er"] == "4.2"
    assert performance_context(cache, "nope")["tab"] == "reels"


def test_sources_page_renders_table_and_tabs():
    client = make_client(RAW_TABLES)
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "РЕФЕРЕНСЫ" in resp.text                       # §2: раздел «Референсы» (был «Источники»)
    assert "хук два" in resp.text                        # строки TikTok по умолчанию
    assert 'href="https://t/2"' in resp.text             # ссылка на источник
    assert 'href="/sources?tab=instagram"' in resp.text  # чип второй вкладки


def test_sources_page_instagram_tab():
    client = make_client(RAW_TABLES)
    resp = client.get("/sources?tab=instagram")
    assert resp.status_code == 200
    assert "иг хук" in resp.text and "хук два" not in resp.text


def test_sources_page_empty_state():
    client = make_client({"raw_tiktok": [], "raw_instagram": []})
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "Пока нет собранных роликов" in resp.text


def test_sources_page_error_branch():
    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "Не удалось загрузить референсы" in resp.text
    assert "sheets down" in resp.text       # причина под катом, не выброшена


def test_performance_page_reels_tab_default():
    client = make_client(PERF_TABLES)
    resp = client.get("/performance")
    assert resp.status_code == 200
    assert "РЕЗУЛЬТАТЫ" in resp.text                      # §2: раздел «Результаты» (был «Перформанс»)
    assert "R2" in resp.text and "без плашки" in resp.text
    assert 'href="https://i/p2"' in resp.text
    assert 'href="/performance?tab=metrics"' in resp.text


def test_performance_page_metrics_tab():
    client = make_client(PERF_TABLES)
    resp = client.get("/performance?tab=metrics")
    assert resp.status_code == 200
    # ER выводится процентом, а не сырой долей; version-id промпта убран из вида
    assert "4,2%" in resp.text and "v1" not in resp.text
    assert "без плашки" not in resp.text


def test_performance_page_empty_state():
    client = make_client({"reels": [], "performance": []})
    resp = client.get("/performance")
    assert resp.status_code == 200
    # Фаза 3: пустое состояние приглашает к действию и даёт маршрут к сценариям
    assert "Здесь появятся вышедшие ролики" in resp.text
    assert 'href="/briefs"' in resp.text
    resp = client.get("/performance?tab=metrics")
    assert "Здесь появятся вышедшие ролики" in resp.text


def test_performance_page_error_branch():
    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/performance")
    assert resp.status_code == 200
    assert "Не удалось загрузить результаты" in resp.text
    assert "sheets down" in resp.text


def test_runs_context_sorts_parses_errors_and_badges():
    runs = [
        {"run_id": "1", "agent": "cf-analyze", "status": "success",
         "completed_at": "2026-07-14T10:00:00+00:00", "errors": "[]"},
        {"run_id": "2", "agent": "cf-eval", "status": "failed",
         "completed_at": "2026-07-14T12:00:00+00:00",
         "errors": '["sheets timeout"]'},
        {"run_id": "3", "agent": "cf-formula", "status": "insufficient_data",
         "completed_at": "2026-07-14T11:00:00+00:00", "errors": ""},
    ]
    ctx = runs_context(make_cache({"run_log": runs}))
    assert [r["run_id"] for r in ctx["runs"]] == ["2", "3", "1"]
    assert ctx["runs"][0]["errors_list"] == ["sheets timeout"]
    assert ctx["runs"][2]["errors_list"] == []
    assert ctx["runs"][0]["badge_class"] == "failed"
    assert ctx["runs"][0]["badge_label"] == "Ошибка"
    assert ctx["runs"][1]["badge_class"] == "insufficient"
    assert ctx["runs"][2]["badge_class"] == "success"
    assert ctx["total"] == 3


def test_runs_context_errors_not_json_kept_as_text():
    runs = [{"run_id": "1", "agent": "a", "status": "failed",
             "completed_at": "2026-07-14", "errors": "просто текст"}]
    ctx = runs_context(make_cache({"run_log": runs}))
    assert ctx["runs"][0]["errors_list"] == ["просто текст"]


def test_runs_context_unknown_status_shown_as_is():
    runs = [{"run_id": "1", "agent": "a", "status": "weird",
             "completed_at": "2026-07-14", "errors": ""}]
    ctx = runs_context(make_cache({"run_log": runs}))
    assert ctx["runs"][0]["badge_class"] == "insufficient"
    assert ctx["runs"][0]["badge_label"] == "weird"


def test_runs_context_caps_at_100():
    runs = [{"run_id": str(i), "agent": "a", "status": "success",
             "completed_at": f"2026-07-{i % 28 + 1:02d}T00:00:00", "errors": "[]"}
            for i in range(120)]
    ctx = runs_context(make_cache({"run_log": runs}))
    assert len(ctx["runs"]) == 100
    assert ctx["total"] == 120


RUNS_TABLES = {"run_log": [
    {"run_id": "1", "agent": "cf-analyze", "trigger_type": "manual",
     "input_summary": "/cf-analyze батч 07-14", "status": "success",
     "started_at": "2026-07-14T09:59:00+00:00",
     "completed_at": "2026-07-14T10:00:00+00:00", "errors": "[]"},
    {"run_id": "2", "agent": "cf-eval", "trigger_type": "scheduled",
     "input_summary": "weekly eval", "status": "failed",
     "started_at": "2026-07-14T11:59:00+00:00",
     "completed_at": "2026-07-14T12:00:00+00:00",
     "errors": '["sheets timeout"]'},
]}


def test_runs_page_renders_journal():
    client = make_client(RUNS_TABLES)
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "ИСТОРИЯ ЗАДАЧ" in resp.text
    assert "cf-analyze" in resp.text and "cf-eval" in resp.text
    assert "Успех" in resp.text and "Ошибка" in resp.text
    assert "sheets timeout" in resp.text
    assert "14.07.2026 12:00" in resp.text


def test_runs_page_empty_state():
    client = make_client({"run_log": []})
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Задачи ещё не запускались" in resp.text


def test_runs_page_error_branch():
    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Не удалось загрузить журнал" in resp.text
    assert "sheets down" in resp.text


def test_read_frontmatter_parses_simple_yaml():
    text = ("---\nstatus: proposed\nprompt_id: brief-x\ncreated: 2026-07-14\n---\n"
            "# Заголовок\nтело")
    assert read_frontmatter(text) == {
        "status": "proposed", "prompt_id": "brief-x", "created": "2026-07-14"}


def test_read_frontmatter_without_header_or_empty():
    assert read_frontmatter("# Просто файл") == {}
    assert read_frontmatter("") == {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def make_lab_root(tmp_path):
    _write(tmp_path / "formulas" / "_approved" / "index.json", {
        "approved": [{"name": "f-one", "niche": "стиль", "version": 2,
                      "path": "formulas/стиль/f-one.json",
                      "approved_at": "2026-07-14T17:00:00+00:00"}]})
    _write(tmp_path / "formulas" / "стиль" / "f-one.json",
           {"name": "f-one", "confidence": "medium", "conditions": "shorts до 30 сек"})
    _write(tmp_path / "agent-runtime" / "patterns" / "2026-07-14-стиль-patterns.json", {
        "meta": {"niche": "стиль", "source_tab": "raw_tiktok",
                 "generated_at": "2026-07-14T16:00:00+00:00"},
        "patterns": [{"pattern_id": "p-1", "description": "хук с идеей",
                      "confidence": "medium",
                      "evidence": {"source_urls": ["u1", "u2"],
                                   "avg_views": 1000.0, "avg_er": 0.1}}]})
    _write(tmp_path / "proposals" / "2026-07-10-brief-x.md",
           "---\nstatus: approved\nprompt_id: brief-x\ncreated: 2026-07-10\n---\n"
           "# Правка visual_direction\n")
    return tmp_path


LAB_VERSIONS = {"prompt_versions": [
    {"prompt_id": "brief-x", "version": "v1", "github_path": "prompts/x.md",
     "active": "FALSE", "activated_at": "2026-07-01T10:00:00+00:00", "changelog": ""},
    {"prompt_id": "brief-x", "version": "v2", "github_path": "prompts/x.md",
     "active": "TRUE", "activated_at": "2026-07-12T10:00:00+00:00", "changelog": "гейты"},
]}


def test_lab_reads_each_approved_formula_json_once(tmp_path, monkeypatch):
    # P3.6: утверждённая формула лежит в niche-каталоге, поэтому и список formulas,
    # и _formula_queues натыкаются на её файл. Читать его дважды за один /lab не нужно.
    import cf.dashboard.sections as sec
    root = make_lab_root(tmp_path)          # index.json approved -> formulas/стиль/f-one.json
    counts = {}
    orig = sec._load_json

    def spy(path):
        counts[path.name] = counts.get(path.name, 0) + 1
        return orig(path)

    monkeypatch.setattr(sec, "_load_json", spy)
    lab_context(make_cache(LAB_VERSIONS), root=root)
    assert counts["f-one.json"] == 1        # прочитан один раз, не дважды


def test_lab_context_reads_versions_files_and_patterns(tmp_path):
    ctx = lab_context(make_cache(LAB_VERSIONS), root=make_lab_root(tmp_path))
    assert ctx["versions"][0]["version"] == "v2"          # активная — первой
    assert ctx["versions"][0]["is_active"] is True
    assert ctx["versions"][1]["is_active"] is False


def test_lab_context_marks_candidate_version(tmp_path):
    # P5.13: кандидат A/B отличим от погашенной версии (is_candidate), стоит после active.
    tables = {"prompt_versions": [
        {"prompt_id": "brief-x", "version": "v2", "github_path": "prompts/x.md",
         "active": "TRUE", "activated_at": "2026-07-12T10:00:00+00:00"},
        {"prompt_id": "brief-x", "version": "v3", "github_path": "prompts/x-v3.md",
         "active": "CANDIDATE", "activated_at": "2026-07-15T10:00:00+00:00"},
        {"prompt_id": "brief-x", "version": "v1", "github_path": "prompts/x.md",
         "active": "FALSE", "activated_at": "2026-07-01T10:00:00+00:00"},
    ]}
    ctx = lab_context(make_cache(tables), root=make_lab_root(tmp_path))
    by_v = {v["version"]: v for v in ctx["versions"]}
    assert by_v["v2"]["is_active"] and not by_v["v2"]["is_candidate"]
    assert by_v["v3"]["is_candidate"] and not by_v["v3"]["is_active"]
    assert not by_v["v1"]["is_active"] and not by_v["v1"]["is_candidate"]
    assert ctx["versions"][0]["version"] == "v2"          # active первой
    assert ctx["versions"][1]["version"] == "v3"          # candidate до погашенной
    assert ctx["formulas"][0]["name"] == "f-one"
    assert ctx["formulas"][0]["confidence"] == "medium"    # из файла формулы
    batch = ctx["patterns"][0]
    assert batch["niche"] == "стиль" and batch["source_tab"] == "raw_tiktok"
    assert batch["items"][0]["urls"] == 2
    assert batch["items"][0]["avg_views"] == 1000.0
    assert ctx["proposals"][0]["title"] == "Правка visual_direction"
    assert ctx["eval_report"] is None


def test_lab_context_orders_proposed_first(tmp_path):
    root = make_lab_root(tmp_path)
    _write(root / "proposals" / "2026-07-14-brief-y.md",
           "---\nstatus: proposed\nprompt_id: brief-y\ncreated: 2026-07-14\n---\n"
           "# Новый proposal\n")
    ctx = lab_context(make_cache(LAB_VERSIONS), root=root)
    assert [p["status"] for p in ctx["proposals"]] == ["proposed", "approved"]


def test_lab_context_reads_latest_eval(tmp_path):
    root = make_lab_root(tmp_path)
    _write(root / "agent-runtime" / "evals" / "2026-07-07-weekly-eval.json",
           {"insights": ["старый"]})
    _write(root / "agent-runtime" / "evals" / "2026-07-14-weekly-eval.json", {
        "period": {"since": "2026-07-07", "until": "2026-07-14"},
        "by_prompt_version": {"v1": {"reels": 2, "avg_views": 100.0, "avg_er": 0.05}},
        "success_criteria": {"reviewer_pass_rate": 1.0},
        "insights": ["r-1 дал x2 views против медианы v1"]})
    ctx = lab_context(make_cache(LAB_VERSIONS), root=root)
    assert ctx["eval_report"]["file"] == "2026-07-14-weekly-eval.json"
    assert ctx["eval_report"]["insights"] == ["r-1 дал x2 views против медианы v1"]
    assert "v1" in ctx["eval_report"]["by_prompt_version"]


def test_lab_context_skips_broken_json(tmp_path):
    root = make_lab_root(tmp_path)
    _write(root / "agent-runtime" / "patterns" / "broken.json", "{оборвано")
    ctx = lab_context(make_cache(LAB_VERSIONS), root=root)
    assert len(ctx["patterns"]) == 1


def test_lab_context_sheets_down_keeps_files(tmp_path):
    ctx = lab_context(DataCache(BrokenSheets({})), root=make_lab_root(tmp_path))
    assert ctx["versions"] is None                       # карточка покажет ошибку
    assert ctx["formulas"]                               # файлы прочитаны


def test_lab_context_empty_root(tmp_path):
    ctx = lab_context(make_cache(LAB_VERSIONS), root=tmp_path)
    assert ctx["formulas"] == [] and ctx["patterns"] == []
    assert ctx["proposals"] == [] and ctx["eval_report"] is None


def test_lab_page_renders_all_cards(tmp_path):
    sheets = FakeSheets(LAB_VERSIONS)
    client = TestClient(create_app(sheets=sheets, lab_root=make_lab_root(tmp_path)))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "ЛАБОРАТОРИЯ" in resp.text   # §2: раздел «Лаборатория» (имя вернул оператор 26.07 —
                                    # «аналитика» звучала отчётом, а это рабочее место)
    assert "РЕШЕНИЯ КОММИТЯТСЯ В GIT" in resp.text        # честный тег: есть approve-кнопки
    assert "brief-x" in resp.text and "v2" in resp.text   # версии промптов
    assert "f-one" in resp.text                           # формулы
    assert "p-1" in resp.text and "хук с идеей" in resp.text  # паттерны с evidence
    assert "Правка visual_direction" in resp.text         # proposals
    assert "Eval ещё не запускался" in resp.text          # empty-state eval


def test_lab_page_sheets_down_still_shows_files(tmp_path):
    client = TestClient(create_app(sheets=BrokenSheets({}),
                                   lab_root=make_lab_root(tmp_path)))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "Версии промптов не прочитаны" in resp.text   # баннер, не «пустота» с логотипом
    assert "f-one" in resp.text


def _origin_repo(tmp_path):
    """Мини-репо: формула + два файла паттернов (старый и свежий дубль)."""
    fdir = tmp_path / "formulas" / "_approved"
    fdir.mkdir(parents=True)
    (fdir / "index.json").write_text(json.dumps({"approved": [{
        "name": "short-styling-idea-reel", "niche": "мужские-образы",
        "path": "formulas/мужские-образы/short-styling-idea-reel.json",
        "version": 2, "approved_at": "2026-07-14T17:07:59+00:00"}]},
        ensure_ascii=False), encoding="utf-8")
    ffile = tmp_path / "formulas" / "мужские-образы"
    ffile.mkdir(parents=True)
    (ffile / "short-styling-idea-reel.json").write_text(json.dumps({
        "problem_definition": "Мега-вирусные форматы проигрывают личному контенту.",
        "confidence": "medium"}, ensure_ascii=False), encoding="utf-8")
    pdir = tmp_path / "agent-runtime" / "patterns"
    pdir.mkdir(parents=True)
    (pdir / "2026-07-10-старый.json").write_text(json.dumps({
        "meta": {"source_tab": "raw_tiktok"},
        "patterns": [{"pattern_id": "tt-01", "description": "УСТАРЕВШАЯ формулировка",
                      "confidence": "low",
                      "evidence": {"source_urls": ["https://t.tt/old"],
                                   "avg_views": 1.0, "avg_er": 0.01}}]},
        ensure_ascii=False), encoding="utf-8")
    (pdir / "2026-07-14-свежий.json").write_text(json.dumps({
        "meta": {"source_tab": "raw_tiktok"},
        "patterns": [{"pattern_id": "tt-01", "description": "личный образ автора",
                      "confidence": "medium",
                      "evidence": {"source_urls": ["https://t.tt/a/", "https://t.tt/b"],
                                   "avg_views": 108118.0, "avg_er": 0.16}}]},
        ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_brief_origin_full_chain(tmp_path):
    root = _origin_repo(tmp_path)
    selected = {"formula_id": "short-styling-idea-reel",
                "source_pattern_ids": "tt-01, ig-99"}
    origin = brief_origin(selected, ["https://t.tt/a", "https://x.com/чужой"], root=root)
    f = origin["formula"]
    assert f["name"] == "short-styling-idea-reel" and f["missing"] is False
    assert f["version"] == 2 and f["approved_at"] == "2026-07-14"
    assert f["problem"].startswith("Мега-вирусные")
    (p,) = origin["patterns"]
    assert p["description"] == "личный образ автора"      # свежий файл победил
    assert p["platform"] == "TikTok"
    assert p["avg_views"] == 108118.0
    # матчинг терпит трейлинг-слэш с обеих сторон
    assert p["evidence"] == [{"url": "https://t.tt/a/", "is_reference": True},
                             {"url": "https://t.tt/b", "is_reference": False}]
    assert origin["missing_pattern_ids"] == ["ig-99"]
    assert origin["unmatched_refs"] == ["https://x.com/чужой"]


def test_brief_origin_none_when_agent_wrote_nothing(tmp_path):
    assert brief_origin({"formula_id": "", "source_pattern_ids": " "},
                        [], root=tmp_path) is None
    assert brief_origin(None, [], root=tmp_path) is None


def test_brief_origin_missing_formula_file(tmp_path):
    origin = brief_origin({"formula_id": "нет-такой", "source_pattern_ids": ""},
                          [], root=tmp_path)
    assert origin["formula"] == {"name": "нет-такой", "missing": True}
    assert origin["patterns"] == [] and origin["missing_pattern_ids"] == []


def test_brief_origin_dedupes_duplicate_pattern_ids(tmp_path):
    root = _origin_repo(tmp_path)
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-01, tt-01"},
                          [], root=root)
    assert len(origin["patterns"]) == 1
    assert origin["patterns"][0]["pattern_id"] == "tt-01"


def test_brief_origin_preserves_pattern_order(tmp_path):
    root = _origin_repo(tmp_path)
    (root / "agent-runtime" / "patterns" / "2026-07-13-второй.json").write_text(
        json.dumps({
            "meta": {"source_tab": "raw_instagram"},
            "patterns": [{"pattern_id": "tt-02", "description": "второй паттерн",
                          "confidence": "low",
                          "evidence": {"source_urls": ["https://i.ig/x"],
                                       "avg_views": 10.0, "avg_er": 0.02}}]},
            ensure_ascii=False), encoding="utf-8")
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-02, tt-01"},
                          [], root=root)
    # порядок как у агента в брифе, а не порядок файлов
    assert [p["pattern_id"] for p in origin["patterns"]] == ["tt-02", "tt-01"]


def test_brief_origin_coerces_stringy_metrics(tmp_path):
    root = _origin_repo(tmp_path)
    (root / "agent-runtime" / "patterns" / "2026-07-15-строковые-метрики.json").write_text(
        json.dumps({
            "meta": {"source_tab": "raw_tiktok"},
            "patterns": [{"pattern_id": "tt-03", "description": "строковые метрики",
                          "confidence": "low",
                          "evidence": {"source_urls": ["https://t.tt/s"],
                                       "avg_views": "108118", "avg_er": "N/A"}}]},
            ensure_ascii=False), encoding="utf-8")
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-03"},
                          [], root=root)
    (p,) = origin["patterns"]
    assert p["avg_views"] == 108118.0    # строка из агентского JSON → число
    assert p["avg_er"] is None           # «N/A» честно превращается в None
    # рендер страницы брифов не падает на строковой метрике
    tables = {"briefs": [{"brief_id": "b-1", "hook": "х", "script": "с",
                          "review_status": "", "formula_id": "",
                          "source_pattern_ids": "tt-03", "references": ""}],
              "run_log": []}
    client = make_client(tables, lab_root=root)
    resp = client.get("/briefs?id=b-1")
    assert resp.status_code == 200
    assert "в среднем просмотров 108 118" in resp.text


def test_brief_origin_broken_pattern_json_does_not_crash(tmp_path):
    root = _origin_repo(tmp_path)
    (root / "agent-runtime" / "patterns" / "2026-07-15-битый.json").write_text(
        "{оборванный", encoding="utf-8")
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-01"},
                          [], root=root)
    assert origin["patterns"][0]["pattern_id"] == "tt-01"


# --- P3.6: ранний выход при чтении файлов паттернов --------------------------


def test_origin_patterns_stops_after_all_ids_found(tmp_path, monkeypatch):
    # P3.6: как только все pattern_ids найдены в свежем файле, старые файлы НЕ читаются.
    import cf.dashboard.sections as sec
    root = _origin_repo(tmp_path)          # свежий 2026-07-14 (tt-01) + старый 2026-07-10 (tt-01)
    opened = []
    orig = sec._load_json

    def spy(path):
        opened.append(path.name)
        return orig(path)

    monkeypatch.setattr(sec, "_load_json", spy)
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-01"}, [], root=root)
    assert origin["patterns"][0]["pattern_id"] == "tt-01"
    assert "2026-07-14-свежий.json" in opened        # свежий прочитан (id найден)
    assert "2026-07-10-старый.json" not in opened     # старый уже не читаем — ранний выход


def test_origin_patterns_reads_all_files_when_id_unresolved(tmp_path, monkeypatch):
    # Регрессия: пока не все id найдены, обход продолжается (ищем недостающие).
    import cf.dashboard.sections as sec
    root = _origin_repo(tmp_path)
    opened = []
    orig = sec._load_json
    monkeypatch.setattr(sec, "_load_json",
                        lambda p: (opened.append(p.name), orig(p))[1])
    # ig-99 нет ни в одном файле -> ранний выход не срабатывает, читаем все
    brief_origin({"formula_id": "", "source_pattern_ids": "tt-01, ig-99"}, [], root=root)
    assert "2026-07-10-старый.json" in opened


# --- P2.4: /lab устойчив к грязному JSON агентов -----------------------------

def _dirty_lab_root(tmp_path):
    """Мини-репо с грязными типами агентского JSON: строковые метрики, строковый
    cluster_size, source_urls строкой вместо списка, javascript:-URL в референсах."""
    _write(tmp_path / "formulas" / "_approved" / "index.json", {"approved": []})
    _write(tmp_path / "formulas" / "стиль" / "dirty.json", {
        "name": "dirty-formula", "niche": "стиль", "version": 1,
        "status": "proposed", "hook_structure": "хук",
        "evidence": {"source_urls": ["javascript:alert(1)", "https://ok.example/1"],
                     "avg_views": "105830", "avg_er": "N/A"}})
    _write(tmp_path / "agent-runtime" / "niche-proposals" / "n.json", {
        "name": "грязная-ниша", "title": "Грязная", "cluster_size": "7",
        "status": "proposed",
        "examples": [{"caption": "пример", "source_url": "javascript:alert(2)"}]})
    _write(tmp_path / "agent-runtime" / "patterns"
           / "2026-07-15-стиль-raw_tiktok-patterns.json", {
        "meta": {"niche": "стиль", "generated_at": "2026-07-15T00:00:00+00:00"},
        "patterns": [{"pattern_id": "p-dirty", "description": "строковый source_urls",
                      "confidence": "low",
                      "evidence": {"source_urls": "https://one.example/x",
                                   "avg_views": "999", "avg_er": "0.1"}}]})
    return tmp_path


def test_formula_queue_coerces_string_metrics(tmp_path):
    ctx = lab_context(make_cache({}), root=_dirty_lab_root(tmp_path))
    item = ctx["formula_queue"][0]
    assert item["avg_views"] == 105830.0   # строка агента → число
    assert item["avg_er"] is None          # «N/A» честно превращается в None
    assert "javascript:alert(1)" in item["evidence_urls"]  # хранится, но не линкуется


def test_niche_queue_coerces_string_cluster_size(tmp_path):
    root = _dirty_lab_root(tmp_path)
    _write(root / "agent-runtime" / "niche-proposals" / "big.json", {
        "name": "большая", "title": "Большая", "cluster_size": 20,
        "status": "proposed", "examples": []})
    ctx = lab_context(make_cache({}), root=root)
    assert [n["name"] for n in ctx["niche_queue"]] == ["большая", "грязная-ниша"]
    assert ctx["niche_queue"][1]["cluster_size"] == 7     # строка "7" → int 7


def test_lab_patterns_string_source_urls_not_char_iterated(tmp_path):
    ctx = lab_context(make_cache({}), root=_dirty_lab_root(tmp_path))
    item = ctx["patterns"][0]["items"][0]
    assert item["urls"] == 1               # строка = один URL, не len(строки)
    assert item["avg_views"] == 999.0      # строковая метрика коэрсится


def test_origin_patterns_string_source_urls_not_char_iterated(tmp_path):
    root = _dirty_lab_root(tmp_path)
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "p-dirty"},
                          [], root=root)
    (p,) = origin["patterns"]
    assert [e["url"] for e in p["evidence"]] == ["https://one.example/x"]


def test_lab_page_200_on_dirty_json_and_no_js_links(tmp_path):
    sheets = FakeSheets({"run_log": [], "briefs": []})
    client = TestClient(create_app(sheets=sheets, lab_root=_dirty_lab_root(tmp_path)))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "105 830" in resp.text                      # строковый avg_views отформатирован
    assert 'href="javascript:' not in resp.text        # javascript: не становится ссылкой
    assert 'href="https://ok.example/1"' in resp.text  # http(s) остаётся ссылкой


def test_lab_page_error_branch_returns_200(monkeypatch, tmp_path):
    """Единственный ранее без try/except маршрут: сбой контекста → error-state, не 500."""
    import cf.dashboard.app as app_module

    def boom(*args, **kwargs):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(app_module, "lab_context", boom)
    client = TestClient(create_app(
        sheets=FakeSheets({"run_log": [], "briefs": []}), lab_root=tmp_path))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "Не удалось собрать «Лабораторию»" in resp.text
    assert "сломалось" in resp.text


# --- P2.16: контракт meta.source_tab ----------------------------------------

def test_source_tab_read_from_meta(tmp_path):
    root = tmp_path
    _write(root / "formulas" / "_approved" / "index.json", {"approved": []})
    _write(root / "agent-runtime" / "patterns" / "2026-07-15-x-нечто.json", {
        "meta": {"niche": "x", "source_tab": "raw_instagram",
                 "generated_at": "2026-07-15T00:00:00+00:00"},
        "patterns": [{"pattern_id": "q-1", "description": "d", "confidence": "low",
                      "evidence": {"source_urls": ["https://a"],
                                   "avg_views": 1, "avg_er": 0.1}}]})
    ctx = lab_context(make_cache({}), root=root)
    assert ctx["patterns"][0]["source_tab"] == "raw_instagram"


def test_source_tab_fallback_from_filename(tmp_path):
    """Фан-аут/старый файл: tab только в имени, meta.source_tab отсутствует."""
    root = tmp_path
    _write(root / "formulas" / "_approved" / "index.json", {"approved": []})
    _write(root / "agent-runtime" / "patterns"
           / "2026-07-15-бренды-raw_tiktok-patterns.json", {
        "meta": {"niche": "бренды", "generated_at": "2026-07-15T00:00:00+00:00"},
        "patterns": [{"pattern_id": "tt-9", "description": "d", "confidence": "low",
                      "evidence": {"source_urls": ["https://a"],
                                   "avg_views": 1, "avg_er": 0.1}}]})
    ctx = lab_context(make_cache({}), root=root)
    assert ctx["patterns"][0]["source_tab"] == "raw_tiktok"      # выведено из имени
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-9"},
                          [], root=root)
    assert origin["patterns"][0]["platform"] == "TikTok"         # платформа тоже из имени


def test_source_tab_fallback_instagram_short_form(tmp_path):
    root = tmp_path
    _write(root / "formulas" / "_approved" / "index.json", {"approved": []})
    _write(root / "agent-runtime" / "patterns"
           / "2026-07-14-ниша-instagram-patterns.json", {
        "meta": {"niche": "ниша", "generated_at": "2026-07-14T00:00:00+00:00"},
        "patterns": [{"pattern_id": "ig-1", "description": "d", "confidence": "low",
                      "evidence": {"source_urls": ["https://a"],
                                   "avg_views": 1, "avg_er": 0.1}}]})
    ctx = lab_context(make_cache({}), root=root)
    assert ctx["patterns"][0]["source_tab"] == "raw_instagram"


# --- P5.7: карточка «Ритуалы недели» -----------------------------------------

TODAY = datetime.date(2026, 7, 21)


def _eval_file(root, date):
    _write(root / "agent-runtime" / "evals" / f"{date}-weekly-eval.json",
           {"insights": []})


def _source_stats_file(root, date, tab="raw_tiktok"):
    _write(root / "agent-runtime" / "source-stats"
           / f"{date}-{tab}-source-stats.json", {"sources": []})


def _source_proposal(root, date, platform, status, remove=1, add=1, applied_at=None):
    data = {
        "platform": platform, "generated_at": date, "status": status,
        "remove": [{"source": f"#x{i}", "reason": "низкий yield", "stats": {}}
                   for i in range(remove)],
        "add": [{"source": f"#y{i}", "kind": "hashtag", "evidence": "count>=3"}
                for i in range(add)]}
    if applied_at:
        data["applied_at"] = applied_at
    _write(root / "proposals" / f"{date}-sources-{platform}.json", data)


def _ritual(ctx, key):
    return next(r for r in ctx["rituals"] if r["key"] == key)


def test_rituals_context_overdue_when_eval_8_days_old(tmp_path):
    # приёмка: eval 8-дневной давности → бейдж «просрочено»
    _eval_file(tmp_path, "2026-07-13")            # 8 дней до 2026-07-21
    ctx = rituals_context(tmp_path, today=TODAY)
    ev = _ritual(ctx, "eval")
    assert ev["last_date"] == "2026-07-13"
    assert ev["days_ago"] == 8
    assert ev["overdue"] is True
    assert ev["badge"] == "просрочено"


def test_rituals_context_fresh_no_badge(tmp_path):
    # краевой: оба ритуала свежие (<= 7 дней) → без бейджей
    _eval_file(tmp_path, "2026-07-20")            # 1 день
    _source_stats_file(tmp_path, "2026-07-14")    # ровно 7 дней — ещё не просрочка
    ctx = rituals_context(tmp_path, today=TODAY)
    assert _ritual(ctx, "eval")["overdue"] is False
    assert _ritual(ctx, "eval")["badge"] is None
    assert _ritual(ctx, "tune-sources")["days_ago"] == 7
    assert _ritual(ctx, "tune-sources")["overdue"] is False


def test_rituals_context_empty_agent_runtime(tmp_path):
    # краевой: пустой agent-runtime (свежий клон) → «ещё не запускался» + «нет данных»
    ctx = rituals_context(tmp_path, today=TODAY)
    for key in ("eval", "tune-sources"):
        r = _ritual(ctx, key)
        assert r["last_date"] is None
        assert r["overdue"] is True
        assert r["badge"] == "нет данных"
    assert ctx["pending_source_proposals"] == []


def test_rituals_context_picks_latest_source_stats(tmp_path):
    # несколько source-stats (по вкладке + по неделям) → берём самый свежий по дате
    _source_stats_file(tmp_path, "2026-07-08", "raw_tiktok")
    _source_stats_file(tmp_path, "2026-07-15", "raw_tiktok")
    _source_stats_file(tmp_path, "2026-07-15", "raw_instagram")
    ts = _ritual(rituals_context(tmp_path, today=TODAY), "tune-sources")
    assert ts["last_date"] == "2026-07-15"
    assert ts["days_ago"] == 6 and ts["overdue"] is False


def test_rituals_context_pending_source_proposal_listed(tmp_path):
    # приёмка: pending source-proposal показан как «ждёт apply-sources»
    _source_proposal(tmp_path, "2026-07-15", "tiktok", "pending", remove=2, add=3)
    ctx = rituals_context(tmp_path, today=TODAY)
    (p,) = ctx["pending_source_proposals"]
    assert p["file"] == "2026-07-15-sources-tiktok.json"
    assert p["platform"] == "tiktok"
    assert p["remove"] == 2 and p["add"] == 3


def test_rituals_context_approved_without_applied_at_still_awaits(tmp_path):
    # расширение ревью: approved, но НЕ применён (нет applied_at) — буквально ждёт
    # cf apply-sources, значит должен оставаться в карточке
    _source_proposal(tmp_path, "2026-07-15", "tiktok", "approved")
    (p,) = rituals_context(tmp_path, today=TODAY)["pending_source_proposals"]
    assert p["platform"] == "tiktok"


def test_rituals_context_applied_or_rejected_proposal_ignored(tmp_path):
    # применённый (approved + applied_at) и отклонённый уже закрыты — не напоминаем
    _source_proposal(tmp_path, "2026-07-15", "tiktok", "approved",
                     applied_at="2026-07-16T10:00:00+00:00")
    _source_proposal(tmp_path, "2026-07-16", "instagram", "rejected")
    assert rituals_context(tmp_path, today=TODAY)["pending_source_proposals"] == []


def test_ritual_file_date_prefers_name_over_mtime(tmp_path):
    # имя с датой — первичный источник (mtime Я.Диск может испортить)
    path = tmp_path / "2026-07-13-weekly-eval.json"
    path.write_text("{}", encoding="utf-8")
    assert _ritual_file_date(path) == datetime.date(2026, 7, 13)


def test_ritual_file_date_falls_back_to_mtime_without_date_in_name(tmp_path):
    # имя без даты → fallback на mtime
    import os
    path = tmp_path / "weekly-eval.json"
    path.write_text("{}", encoding="utf-8")
    ts = datetime.datetime(2026, 7, 3, 12, 0).timestamp()
    os.utime(path, (ts, ts))
    assert _ritual_file_date(path) == datetime.date(2026, 7, 3)


def test_ritual_file_date_garbage_date_falls_back_to_mtime(tmp_path):
    # мусорная дата (совпала с regex, но невалидна) → mtime-ветка, не падение
    import os
    path = tmp_path / "2026-99-99-weekly-eval.json"
    path.write_text("{}", encoding="utf-8")
    ts = datetime.datetime(2026, 7, 2, 12, 0).timestamp()
    os.utime(path, (ts, ts))
    assert _ritual_file_date(path) == datetime.date(2026, 7, 2)


def test_rituals_context_future_date_clamped_to_zero(tmp_path):
    # будущая дата в имени (клок-скью/опечатка) → 0 дн., без отрицательных и просрочки
    _eval_file(tmp_path, "2026-08-01")            # позже TODAY 2026-07-21
    ev = _ritual(rituals_context(tmp_path, today=TODAY), "eval")
    assert ev["days_ago"] == 0
    assert ev["overdue"] is False and ev["badge"] is None


def test_ritual_specs_match_runner_rituals():
    # drift-гвоздь: наборы ключей и command/label в sections.RITUAL_SPECS и
    # runner.RITUALS должны совпадать (иначе кнопка/маршрут разъедутся с карточкой)
    from cf.dashboard.runner import RITUALS
    from cf.dashboard.sections import RITUAL_SPECS
    specs = {s["key"]: s for s in RITUAL_SPECS}
    assert set(specs) == set(RITUALS)
    for key, meta in RITUALS.items():
        assert specs[key]["command"] == meta["command"]
        assert specs[key]["label"] == meta["label"]


def test_lab_context_includes_rituals(tmp_path):
    # rituals прокидываются в общий контекст /lab (today передаётся насквозь)
    _eval_file(tmp_path, "2026-07-13")
    _source_proposal(tmp_path, "2026-07-15", "tiktok", "pending")
    ctx = lab_context(make_cache({}), root=tmp_path, today=TODAY)
    assert _ritual(ctx, "eval")["badge"] == "просрочено"
    assert len(ctx["pending_source_proposals"]) == 1


# --- Фаза 3 UX-доводки: ясность по разделам (§3 спеки) ------------------------


def test_sources_context_sorts_by_views_and_saves_from_url():
    # Главная задача продюсера здесь — найти, что взлетело: сортировка живёт в URL
    cache = make_cache(RAW_TABLES)
    assert [r["source_url"] for r in sources_context(cache, "tiktok", "views")["rows"]] \
        == ["https://t/2", "https://t/1"]
    assert sources_context(cache, "tiktok", "views")["sort"] == "views"
    assert sources_context(cache, "tiktok", "мусор")["sort"] == "date"   # дефолт


def test_sources_context_numeric_sort_puts_garbage_last():
    rows = [{"source_url": "u1", "views": "", "posted_at": "2026-07-01"},
            {"source_url": "u2", "views": "700", "posted_at": "2026-07-02"}]
    ctx = sources_context(make_cache({"raw_tiktok": rows, "raw_instagram": []}),
                          "tiktok", "views")
    assert [r["source_url"] for r in ctx["rows"]] == ["u2", "u1"]


def test_sources_hook_column_hidden_while_hooks_are_not_collected():
    # Колонка из 200 прочерков читается как поломка, а не как «данных нет»
    rows = [{"source_url": "u1", "account": "a", "views": 10,
             "posted_at": "2026-07-01", "hook_text": ""}]
    tables = {"raw_tiktok": rows, "raw_instagram": []}
    assert sources_context(make_cache(tables), "tiktok")["has_hooks"] is False
    assert "Заход" not in make_client(tables).get("/sources").text
    assert sources_context(make_cache(RAW_TABLES), "tiktok")["has_hooks"] is True
    assert "Заход" in make_client(RAW_TABLES).get("/sources").text


def test_sources_page_speaks_producer_language():
    html = make_client(RAW_TABLES).get("/sources").text
    assert "Ролики конкурентов и лидеров тем" in html      # лид «что это»
    for eng in (">Views<", ">Likes<", ">Comm<", ">Saves<"):
        assert eng not in html
    for ru in ("Просмотры", "Лайки", "Комментарии", "Сохранения"):
        assert ru in html
    big = {"raw_tiktok": [{"source_url": "u", "account": "a", "views": 705906,
                          "posted_at": "2026-07-01"}], "raw_instagram": []}
    assert "705\u00a0906" in make_client(big).get("/sources").text   # разряды
    assert 'href="/sources?tab=tiktok&sort=views"' in html  # пресет «сначала популярные»


def test_performance_metrics_row_gets_human_caption():
    # ID остаётся, но строку опознаём по хуку сценария + площадке и дате
    tables = dict(PERF_TABLES, briefs=[{"brief_id": "B1", "hook": "3 образа на осень"}])
    ctx = performance_context(make_cache(tables), "metrics")
    row = ctx["rows"][0]
    assert row["brief_hook"] == "3 образа на осень"
    assert row["platform"] == "tiktok"
    html = make_client(tables).get("/performance?tab=metrics").text
    assert "3 образа на осень" in html
    assert "Промпт" not in html and "v1" not in html       # version-id убран из вида


def test_performance_reels_row_gets_hook_caption():
    tables = dict(PERF_TABLES, briefs=[{"brief_id": "B2", "hook": "лук недели"}])
    html = make_client(tables).get("/performance").text
    assert "лук недели" in html
    assert "Дата публикации" in html                       # «Опубликован» → явная дата


# --- Ширина доказательной базы рецепта (аудит 2026-07-26) -------------------

def test_evidence_level_thresholds():
    from cf.dashboard.sections import evidence_level
    # 3 URL — нижняя граница валидности паттерна: один отвалившийся референс
    # делает приём невалидным, поэтому «на минимуме», а не «достаточно».
    assert evidence_level(0) == "thin"
    assert evidence_level(3) == "thin"
    assert evidence_level(4) == "ok"
    assert evidence_level(5) == "ok"
    assert evidence_level(6) == "wide"     # удвоенный минимум
    assert evidence_level(16) == "wide"
    # мусор не роняет рендер очереди одобрения
    assert evidence_level(None) == "thin"
    assert evidence_level("пять") == "thin"


def test_lab_marks_evidence_width_by_url_count(tmp_path):
    # Метка считается по ЧИСЛУ референсов, а не по confidence: confidence ставит
    # агент суждением, а оператору нужен сравнимый между рецептами признак.
    root = make_lab_root(tmp_path)
    _write(root / "formulas" / "стиль" / "wide-one.json",
           {"name": "wide-one", "niche": "стиль", "version": 1,
            "status": "proposed", "confidence": "medium",
            "evidence": {"source_urls": [f"https://t.tt/{i}" for i in range(7)]}})
    _write(root / "formulas" / "стиль" / "thin-one.json",
           {"name": "thin-one", "niche": "стиль", "version": 1,
            "status": "proposed", "confidence": "high",
            "evidence": {"source_urls": ["https://t.tt/a", "https://t.tt/b",
                                         "https://t.tt/c"]}})
    ctx = lab_context(make_cache(LAB_VERSIONS), root=str(root))
    by = {f["name"]: f for f in ctx["formula_queue"]}
    assert by["wide-one"]["evidence_level"] == "wide"   # 7 URL при medium
    assert by["thin-one"]["evidence_level"] == "thin"   # 3 URL при high


def test_evidence_labels_are_words_not_only_colour():
    # Цвет не единственный носитель смысла (требование доступности кита):
    # рядом всегда стоит подпись словами.
    from cf.dashboard.labels import evidence_hint, evidence_label
    assert evidence_label("wide") == "широкая база"
    assert evidence_label("ok") == "база достаточна"
    assert evidence_label("thin") == "база на минимуме"
    assert evidence_label("мусор") == ""
    assert "6" in evidence_hint("wide")
    assert evidence_hint("мусор") == ""


# ── Страница «Деньги»: money_context (UTM-контур, тикет 04) ──────────────────

MONEY_ACCOUNTS = [
    {"slug": "tiktok-1", "platform": "tiktok", "handle": "@x", "active": True},
]
MONEY_PAYOUT = {"per_transition_rub": 30, "sales_percent": 10, "hold_days": 14,
                "monthly_fix_rub": {}}
MONEY_NOW = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.timezone.utc)


def _utm(utm_id, kind, month, **extra):
    row = {"utm_id": utm_id, "row_kind": kind, "date": "", "month": month,
           "account": "tiktok-1", "utm_campaign": "tiktok-1", "utm_content": "",
           "reel_id": "", "visits": 0, "users": 0, "collected_at": ""}
    row.update(extra)
    return row


def test_money_context_months_selector_and_daily_counter():
    from cf.dashboard.sections import money_context
    utm = [_utm("m-2026-06-tiktok-1", "monthly", "2026-06", users=10, visits=12),
           # два дневных ряда ОДНОГО дня (разные метки роликов) — день один
           _utm("d1", "daily", "2026-06", date="2026-06-01", users=3),
           _utm("d2", "daily", "2026-06", date="2026-06-01", users=2,
                utm_content="b42", reel_id="b42"),
           _utm("d3", "daily", "2026-06", date="2026-06-05", users=1)]
    orders = [{"order_id": "o1", "order_date": "2026-05-02", "revenue": 100,
               "status": "confirmed", "account": "tiktok-1"}]
    cache = make_cache({"utm_traffic": utm, "orders": orders, "run_log": []})
    ctx = money_context(cache, MONEY_ACCOUNTS, MONEY_PAYOUT,
                        "https://jelapeche.com", "2026-06", MONEY_NOW)
    # месяцы: где есть данные, плюс текущий; свежие первыми
    assert ctx["months"] == ["2026-07", "2026-06", "2026-05"]
    assert ctx["month"] == "2026-06"
    (t,) = ctx["transitions"]
    assert t["users"] == 10           # биллинговая цифра месячной строки
    assert t["days_active"] == 2      # дни уникальны, а не число строк
    (link,) = ctx["links"]
    assert link["url"].endswith("utm_campaign=tiktok-1")


def test_money_context_garbage_month_falls_back_to_current():
    from cf.dashboard.sections import money_context
    cache = make_cache({"utm_traffic": [], "orders": [], "run_log": []})
    ctx = money_context(cache, MONEY_ACCOUNTS, MONEY_PAYOUT, "", "мусор",
                        MONEY_NOW)
    assert ctx["month"] == "2026-07"
    assert ctx["months"] == ["2026-07"]
    # base_url пуст: ссылок нет, но причина названа, а не молча пропала секция
    assert ctx["links"] == [] and "base_url" in ctx["links_reason"]


def test_money_context_missing_tabs_are_zero_month_not_error():
    from gspread.exceptions import WorksheetNotFound
    from cf.dashboard.sections import money_context
    sheets = FakeSheets({"run_log": []}, fail_read_tabs={
        "utm_traffic": WorksheetNotFound("нет"),
        "orders": WorksheetNotFound("нет")})
    ctx = money_context(DataCache(sheets), MONEY_ACCOUNTS, MONEY_PAYOUT,
                        "https://jelapeche.com", "", MONEY_NOW)
    assert ctx["sheet"]["total"] == 0
    assert ctx["orders"] == [] and ctx["transitions"] == []
    assert ctx["per_reel"]["rows"] == []                  # секция «По роликам» пуста


# ── Секция «По роликам» (UTM-контур, тикет 05) ────────────────────────────────

def test_money_context_per_reel_exact_estimate_and_orders():
    from cf.dashboard.sections import money_context
    utm = [_utm("d-t", "daily", "2026-06", date="2026-06-15",
                utm_content="r1", reel_id="r1", visits=7),
           _utm("d-b", "daily", "2026-06", date="2026-06-15", visits=90)]
    reels = [{"reel_id": "r1", "brief_id": "b1", "account": "tiktok-1",
              "published_at": "2026-06-14"},
             {"reel_id": "r2", "brief_id": "b2", "account": "tiktok-1",
              "published_at": "2026-06-10"}]
    perf = [{"reel_id": "r1", "views": 3000, "measured_at": "2026-06-20"},
            {"reel_id": "r2", "views": 1000, "measured_at": "2026-06-20"}]
    orders = [{"order_id": "o1", "order_date": "2026-06-16", "revenue": 1990,
               "status": "confirmed", "account": "tiktok-1", "reel_id": "r1"}]
    cache = make_cache({"utm_traffic": utm, "orders": orders, "reels": reels,
                        "performance": perf, "run_log": []})
    ctx = money_context(cache, MONEY_ACCOUNTS, MONEY_PAYOUT,
                        "https://jelapeche.com", "2026-06", MONEY_NOW)
    rows = {r["reel_id"]: r for r in ctx["per_reel"]["rows"]}
    assert rows["r1"]["clicks_exact"] == 7
    assert rows["r1"]["clicks_estimated"] == 67.5         # 90 × 3000/4000
    assert rows["r2"]["clicks_estimated"] == 22.5
    assert rows["r1"]["estimated"] is True
    assert rows["r1"]["orders"] == 1 and rows["r1"]["revenue"] == 1990
    assert ctx["per_reel"]["unattributed"] == 0
    # сортировка: больше переходов — выше
    assert [r["reel_id"] for r in ctx["per_reel"]["rows"]] == ["r1", "r2"]


def test_money_context_per_reel_unattributed_when_no_window_reels():
    from cf.dashboard.sections import money_context
    utm = [_utm("d-b", "daily", "2026-06", date="2026-06-15", visits=40)]
    cache = make_cache({"utm_traffic": utm, "orders": [], "run_log": []})
    ctx = money_context(cache, MONEY_ACCOUNTS, MONEY_PAYOUT,
                        "https://jelapeche.com", "2026-06", MONEY_NOW)
    assert ctx["per_reel"]["rows"] == []
    assert ctx["per_reel"]["unattributed"] == 40
    assert ctx["per_reel"]["window_days"] == 14
