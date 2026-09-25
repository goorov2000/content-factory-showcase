import argparse
import json
import sys
from pathlib import Path

from cf.cli import (apply_filters, build_parser, cmd_read, cmd_status,
                    dedupe_rows, force_utf8_stdio, main, resolve_dashboard_port)

from tests.fakes import FakeSheets

REPO_ROOT = Path(__file__).resolve().parents[1]


def ns(**kw):
    return argparse.Namespace(**kw)


def test_apply_filters_niche_and_since():
    rows = [
        {"niche": "pets", "posted_at": "2026-07-01"},
        {"niche": "Pets", "posted_at": "2026-06-01"},
        {"niche": "food", "posted_at": "2026-07-05"},
    ]
    assert apply_filters(rows, niche="pets", since="2026-06-15") == [
        {"niche": "pets", "posted_at": "2026-07-01"}
    ]


def test_cmd_read_prints_json(capsys):
    sheets = FakeSheets({"raw_tiktok": [{"source_url": "https://a", "niche": "pets"}]})
    assert cmd_read(sheets, ns(tab="raw_tiktok", niche=None, since=None, out=None)) == 0
    assert json.loads(capsys.readouterr().out) == [{"source_url": "https://a", "niche": "pets"}]


def test_cmd_read_writes_file(tmp_path, capsys):
    sheets = FakeSheets({"raw_tiktok": [{"a": 1}]})
    out = tmp_path / "batch.json"
    assert cmd_read(sheets, ns(tab="raw_tiktok", niche=None, since=None, out=str(out))) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == [{"a": 1}]


def test_dedupe_rows_keeps_freshest_per_source_url():
    rows = [
        {"source_url": "https://a", "views": 10, "collected_at": "2026-07-01"},
        {"source_url": "https://b", "views": 5},
        {"source_url": "https://a", "views": 90, "collected_at": "2026-07-10"},
    ]
    assert dedupe_rows(rows) == [
        {"source_url": "https://a", "views": 90, "collected_at": "2026-07-10"},
        {"source_url": "https://b", "views": 5},
    ]


def test_dedupe_rows_keeps_rows_without_source_url():
    rows = [{"source_url": ""}, {"views": 1}, {"source_url": "  "}]
    assert dedupe_rows(rows) == rows


def test_cmd_read_dedupes_by_default(capsys):
    sheets = FakeSheets({"raw_tiktok": [
        {"source_url": "https://a", "views": 10},
        {"source_url": "https://a", "views": 90},
    ]})
    assert cmd_read(sheets, ns(tab="raw_tiktok", niche=None, since=None, out=None)) == 0
    assert json.loads(capsys.readouterr().out) == [{"source_url": "https://a", "views": 90}]


def test_cmd_read_no_dedup_keeps_duplicates(capsys):
    sheets = FakeSheets({"raw_tiktok": [
        {"source_url": "https://a", "views": 10},
        {"source_url": "https://a", "views": 90},
    ]})
    assert cmd_read(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                               out=None, dedup=False)) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_parser_read_no_dedup_flag():
    args = build_parser().parse_args(["read", "raw_tiktok", "--no-dedup"])
    assert args.dedup is False
    assert build_parser().parse_args(["read", "raw_tiktok"]).dedup is True


class _FakeStream:
    def __init__(self, encoding):
        self.encoding = encoding
        self.reconfigured = []

    def reconfigure(self, **kw):
        self.reconfigured.append(kw)


def test_force_utf8_stdio_reconfigures_non_utf8_streams(monkeypatch):
    out, err = _FakeStream("cp1251"), _FakeStream("cp866")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    force_utf8_stdio()
    assert out.reconfigured == [{"encoding": "utf-8"}]
    assert err.reconfigured == [{"encoding": "utf-8"}]


def test_force_utf8_stdio_leaves_utf8_and_plain_streams_alone(monkeypatch):
    out = _FakeStream("utf-8")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", object())  # без reconfigure — не должен падать
    force_utf8_stdio()
    assert out.reconfigured == []


def test_main_forces_utf8_before_command(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("cf.cli.force_utf8_stdio", lambda: calls.append(True))
    monkeypatch.setattr("cf.cli.Sheets", lambda: FakeSheets({"raw_tiktok": []}))
    assert main(["read", "raw_tiktok"]) == 0
    assert calls == [True]


def test_cmd_status_prints_recent_runs_newest_first(tmp_path, capsys):
    runs = [{"run_id": f"r{i}", "agent": "profiler", "status": "success",
             "completed_at": f"2026-07-0{i}"} for i in range(1, 6)]
    sheets = FakeSheets({"run_log": runs})
    assert cmd_status(sheets, ns(limit=3, proposals_dir=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "r5" in out and "r3" in out and "r2" not in out
    assert out.index("r5") < out.index("r3")


def test_cmd_status_full_sections(tmp_path, capsys):
    (tmp_path / "p1.md").write_text("---\nstatus: proposed\n---\n# P1", encoding="utf-8")
    (tmp_path / "p2.md").write_text("---\nstatus: rejected\n---\n# P2", encoding="utf-8")
    sheets = FakeSheets({
        "run_log": [{"run_id": "r1", "agent": "eval-agent", "status": "success",
                     "completed_at": "2026-07-08"}],
        "briefs": [{"brief_id": "b-9", "review_status": "pending"},
                   {"brief_id": "b-8", "review_status": "approved"}],
        "prompt_versions": [{"prompt_id": "brief-pets", "version": "v2",
                             "github_path": "prompts/briefs/pets/x.md", "active": "TRUE"}],
    })
    assert cmd_status(sheets, ns(limit=5, proposals_dir=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "Pending briefs: 1 (b-9)" in out
    assert "1 proposed" in out and "1 rejected" in out
    assert "brief-pets v2" in out


def test_cmd_status_excludes_briefs_deferred_by_factory(tmp_path, capsys):
    # (разбор 2026-07-27) Кап формулы откладывает сценарий: решать по нему человеку
    # нечего, завод вернётся сам (cf retry-deferred). Считая отложенных «ожидающими»,
    # cf status показывал 8 против 5 на дашборде — оператор шёл искать три решения,
    # которых нет. Предикат — общий с дашбордом, четвёртой копии признака не заводим.
    from cf.dashboard.data import DEFERRED_MARKER
    sheets = FakeSheets({
        "run_log": [],
        "briefs": [
            {"brief_id": "b-1", "review_status": "pending", "reviewer_notes": ""},
            {"brief_id": "b-2", "review_status": "pending",
             "reviewer_notes": f"{DEFERRED_MARKER}: кап формулы alpha исчерпан"},
        ],
    })

    assert cmd_status(sheets, ns(limit=0, proposals_dir=str(tmp_path))) == 0

    out = capsys.readouterr().out
    assert "Pending briefs: 1 (b-1)" in out
    assert "b-2" not in out                       # это не очередь человека
    assert "отложено заводом: 1" in out           # но и не спрятан молча


def test_parser_wires_subcommands():
    args = build_parser().parse_args(["read", "raw_tiktok", "--niche", "pets"])
    assert args.tab == "raw_tiktok" and args.niche == "pets" and args.func is cmd_read


def test_cmd_read_niche_empty_filter_runs_after_dedup(capsys):
    # P1.13.1: устаревшая строка без ниши и свежая классифицированная делят source_url.
    # После дедупа побеждает свежая (с нишей) — с --niche-empty её source_url НЕ должен
    # попасть в вывод (иначе агент-классификатор снова возьмёт уже размеченное видео).
    sheets = FakeSheets({"raw_tiktok": [
        {"source_url": "https://a", "niche": ""},           # stale, без ниши
        {"source_url": "https://a", "niche": "стритвир"},    # fresh, классифицирована
        {"source_url": "https://b", "niche": ""},            # реально без ниши
    ]})
    assert cmd_read(sheets, ns(tab="raw_tiktok", niche=None, since=None, out=None,
                               niche_empty=True)) == 0
    urls = [r["source_url"] for r in json.loads(capsys.readouterr().out)]
    assert "https://a" not in urls
    assert urls == ["https://b"]


def test_cmd_status_limit_zero_prints_no_runs_and_limit_two_prints_two(tmp_path, capsys):
    # P1.13.2: --limit 0 из-за runs[-0:] печатал ВСЕ запуски; должен печатать ноль.
    runs = [{"run_id": f"r{i}", "agent": "profiler", "status": "success",
             "completed_at": f"2026-07-0{i}"} for i in range(1, 6)]
    sheets = FakeSheets({"run_log": runs})

    assert cmd_status(sheets, ns(limit=0, proposals_dir=str(tmp_path))) == 0
    out0 = capsys.readouterr().out
    assert "Recent runs (0):" in out0
    for i in range(1, 6):
        assert f"r{i}" not in out0

    assert cmd_status(sheets, ns(limit=2, proposals_dir=str(tmp_path))) == 0
    out2 = capsys.readouterr().out
    assert "Recent runs (2):" in out2
    assert "r5" in out2 and "r4" in out2 and "r3" not in out2


# --- P4.2: порт дашборда берётся из конфига (dashboard.port больше не мёртвый ключ) ---


def test_resolve_dashboard_port_honors_config_when_no_flag():
    assert resolve_dashboard_port(None, {"dashboard": {"port": 9001}}) == 9001


def test_resolve_dashboard_port_defaults_to_8787_when_absent():
    assert resolve_dashboard_port(None, {}) == 8787
    assert resolve_dashboard_port(None, {"dashboard": {}}) == 8787


def test_resolve_dashboard_port_explicit_flag_wins_over_config():
    assert resolve_dashboard_port(9002, {"dashboard": {"port": 9001}}) == 9002


def test_parser_dashboard_port_defaults_none_for_config_fallback():
    # default=None, чтобы cmd_dashboard отличил «флаг не задан» и взял порт из конфига
    assert build_parser().parse_args(["dashboard"]).port is None
    assert build_parser().parse_args(["dashboard", "--port", "9003"]).port == 9003


def test_cmd_dashboard_serves_config_port(monkeypatch):
    import cf.config
    import cf.dashboard.app as dash_app
    import uvicorn
    from cf.cli import cmd_dashboard

    monkeypatch.setattr(cf.config, "load_config",
                        lambda *a, **k: {"dashboard": {"port": 9001}, "n8n": {}})
    captured = {}
    monkeypatch.setattr(dash_app, "build_production_app",
                        lambda **kw: (captured.update(kw), "APP")[1])
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, **kw: captured.update(run=kw))
    assert cmd_dashboard(FakeSheets({"run_log": []}), ns(port=None)) == 0
    assert captured["port"] == 9001            # build_production_app получил порт из конфига
    assert captured["run"]["port"] == 9001     # uvicorn слушает тот же порт


# --- P4.2: мёртвый ключ niches удалён из cf.config.json -----------------------


def test_cf_config_has_no_legacy_niches_key():
    from conftest import repo_config_path   # витринная копия: example-конфиг
    cfg = json.loads(repo_config_path().read_text(encoding="utf-8"))
    assert "niches" not in cfg                 # дублирующий таксономию ключ удалён
    assert "target_niches" in cfg              # его читает cmd_source_stats — не трогаем


def test_no_src_code_reads_config_niches_key():
    # источник истины по нишам — prompts/agents/niche-taxonomy.json, не config["niches"]
    needles = ('config["niches"]', "config['niches']",
               'config.get("niches"', "config.get('niches'")
    hits = []
    for py in (REPO_ROOT / "src").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        hits += [f"{py.name}: {n}" for n in needles if n in text]
    assert hits == []


def test_cmd_status_proposals_count_uses_frontmatter_not_body(tmp_path, capsys):
    # P1.13.3: статус proposal берётся из YAML-фронтматтера, а не из подстроки в теле.
    (tmp_path / "p1.md").write_text(
        "---\nstatus: proposed\n---\n# План\nРанее обсуждали status: approved.",
        encoding="utf-8")
    (tmp_path / "p2.md").write_text(
        "---\nstatus: approved\n---\n# План\nИзначально было status: proposed.",
        encoding="utf-8")
    sheets = FakeSheets({"run_log": [], "briefs": [], "prompt_versions": []})
    assert cmd_status(sheets, ns(limit=5, proposals_dir=str(tmp_path))) == 0
    assert "1 proposed, 1 approved, 0 rejected" in capsys.readouterr().out


def test_cmd_status_unknown_proposal_status_is_loud(tmp_path, capsys):
    # Gap#8: proposals/2026-07-26-sources-instagram.md со `status: pending` (слово
    # легально в schemas/source-proposal.schema.json, но это ДРУГОЙ артефакт) не
    # попадал ни в один счётчик и исчезал из сводки молча — заблокированный тюнинг
    # источников оператор просто не видел. Нераспознанный статус обязан быть громким.
    (tmp_path / "p1.md").write_text("---\nstatus: proposed\n---\n# P1", encoding="utf-8")
    (tmp_path / "p2.md").write_text("---\nstatus: pending\n---\n# P2", encoding="utf-8")
    (tmp_path / "p3.md").write_text("# без frontmatter", encoding="utf-8")
    sheets = FakeSheets({"run_log": [], "briefs": [], "prompt_versions": []})

    assert cmd_status(sheets, ns(limit=5, proposals_dir=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "1 proposed, 0 approved, 0 rejected, 2 с нераспознанным статусом" in out
    assert "файлов всего: 3" in out
    assert "ВНИМАНИЕ: proposal p2.md не попал в сводку (status: pending)" in out
    assert "ВНИМАНИЕ: proposal p3.md не попал в сводку (нет status во frontmatter)" in out


def test_cmd_status_no_noise_when_all_statuses_known(tmp_path, capsys):
    # Обратная сторона громкости: при чистом каталоге строка сводки прежняя, без хвоста.
    (tmp_path / "p1.md").write_text("---\nstatus: approved\n---\n# P1", encoding="utf-8")
    sheets = FakeSheets({"run_log": [], "briefs": [], "prompt_versions": []})
    assert cmd_status(sheets, ns(limit=5, proposals_dir=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "Proposals: 0 proposed, 1 approved, 0 rejected\n" in out
    assert "нераспознанным" not in out and "ВНИМАНИЕ: proposal" not in out
