"""Включение правил сценариев темы по черновику — ворота №2 (prompt_apply.py).

Кнопка заменяет четыре ручных шага, поэтому цена ошибки выше ручного пути:
здесь доказывается, что каждый из семи отказов действительно срабатывает ДО
любой записи в боевые файлы.
"""
import json

import pytest

from cf.dashboard.prompt_apply import (PromptApplyError, apply_prompt,
                                       read_draft, reject_draft)


class FakeSheets:
    def __init__(self):
        self.versions = []
        self.runs = []

    def read_rows(self, tab):
        return self.versions if tab == "prompt_versions" else []

    def append_row(self, tab, row):
        (self.versions if tab == "prompt_versions" else self.runs).append(row)

    def update_rows_where(self, *a, **kw):
        return 0


def _git_spy(calls):
    def _git(message, paths):
        calls.append((message, list(paths)))
        return None
    return _git


PROMPT = """# Промпт генерации брифов — {niche} / reel

Ты генерируешь бриф по утверждённому рецепту
`formulas/_approved/{niche}/recipe-v1.json`.

## Выход
Строка в CF Creative Briefs."""


def _draft(root, niche="бренды", status="proposed", prompt=None, blocks=1,
           date="2026-07-26"):
    body = (prompt if prompt is not None else PROMPT.format(niche=niche))
    block = "```markdown\n" + body + "\n```"
    section = "\n\n".join([block] * blocks) if blocks else "нет блока"
    text = (f"---\nstatus: {status}\nprompt_id: brief-{niche}-reel\n---\n"
            f"# Proposal\n\n## Current version\nПромпта нет.\n\n"
            f"## Proposed changes\n{section}\n\n"
            f"## Evidence\n5 URL\n\n## Confidence\nmedium\n\n## Risks\nнет\n")
    p = root / "proposals" / f"{date}-brief-{niche}-reel.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _taxonomy(root, niches):
    p = root / "prompts" / "agents" / "niche-taxonomy.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"niches": niches}, ensure_ascii=False),
                 encoding="utf-8")


def _config(root, exclude=()):
    (root / "cf.config.json").write_text(
        json.dumps({"dashboard": {"fanout": {"exclude_niches": list(exclude)}}},
                   ensure_ascii=False), encoding="utf-8")


# ── счастливый путь ──────────────────────────────────────────────────────────

def test_apply_writes_prompt_activates_version_and_commits(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    sheets, calls = FakeSheets(), []
    assert apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                        git=_git_spy(calls), sheets=sheets) == "бренды"

    target = tmp_path / "prompts" / "briefs" / "бренды" / "reel.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("# Промпт генерации")
    # версия активирована тем же кодом, что у CLI
    assert any(str(r.get("prompt_id")) == "brief-бренды-reel"
               for r in sheets.versions)
    # черновик закрыт, чтобы воркер не писал новый каждый прогон
    draft_text = (tmp_path / "proposals"
                  / "2026-07-26-brief-бренды-reel.md").read_text(encoding="utf-8")
    assert "status: approved" in draft_text and "applied:" in draft_text
    # ровно один коммит, локальный, со следом решения
    assert len(calls) == 1
    assert "включён промпт темы" in calls[0][0]
    assert ".claude/memory/decisions/" in calls[0][1]
    assert list((tmp_path / ".claude" / "memory" / "decisions").glob("*.md"))
    # правило №6: след в Run Log
    assert sheets.runs and sheets.runs[-1]["agent"] == "prompt-apply"


def test_sheets_failure_while_activating_version_is_retryable(tmp_path, monkeypatch):
    # Ревью 14.09.2026: файл промпта писался ДО строки версии в Sheets. Сбой Sheets
    # оставлял «файл есть, версии нет», а повтор упирался в отказ №7 «уже существует»
    # — тема не расшивалась ни кнопкой, ни авто-воротами (cf apply-brief-prompt --auto).
    import cf.cli
    _draft(tmp_path)
    _config(tmp_path)
    sheets, calls = FakeSheets(), []

    def boom(*a, **k):
        raise ConnectionError("sheets 503")

    monkeypatch.setattr(cf.cli, "log_prompt_version", boom)
    with pytest.raises(PromptApplyError) as err:
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                     git=_git_spy(calls), sheets=sheets)
    assert "повтор" in str(err.value)
    target = tmp_path / "prompts" / "briefs" / "бренды" / "reel.md"
    assert not target.exists() and calls == []
    monkeypatch.undo()
    assert apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                        git=_git_spy(calls), sheets=sheets) == "бренды"


def test_read_draft_exposes_prompt_text_and_hash(tmp_path):
    _draft(tmp_path)
    info = read_draft(tmp_path, "2026-07-26-brief-бренды-reel.md")
    assert info["niche"] == "бренды" and info["status"] == "proposed"
    assert info["prompt_text"].startswith("# Промпт генерации")
    assert len(info["sha256"]) == 64


# ── семь отказов ─────────────────────────────────────────────────────────────

def test_refuses_path_traversal(tmp_path):
    _config(tmp_path)
    with pytest.raises(PromptApplyError, match="небезопасное имя"):
        apply_prompt(tmp_path, "../../etc/passwd", git=_git_spy([]))


def test_refuses_foreign_filename(tmp_path):
    _config(tmp_path)
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals" / "2026-07-26-formula-writer.md").write_text("x")
    with pytest.raises(PromptApplyError, match="не похоже на черновик"):
        apply_prompt(tmp_path, "2026-07-26-formula-writer.md", git=_git_spy([]))


def test_refuses_missing_draft(tmp_path):
    _config(tmp_path)
    (tmp_path / "proposals").mkdir()
    with pytest.raises(PromptApplyError) as exc:
        apply_prompt(tmp_path, "2026-07-26-brief-нет-reel.md", git=_git_spy([]))
    assert exc.value.status == 404


def test_refuses_already_applied_draft(tmp_path):
    _draft(tmp_path, status="approved")
    _config(tmp_path)
    with pytest.raises(PromptApplyError, match="уже в статусе"):
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md", git=_git_spy([]))


def test_refuses_excluded_niche(tmp_path):
    _draft(tmp_path, niche="стритвир")
    _config(tmp_path, exclude=["стритвир"])
    with pytest.raises(PromptApplyError, match="вне производства"):
        apply_prompt(tmp_path, "2026-07-26-brief-стритвир-reel.md", git=_git_spy([]))


def test_refuses_niche_outside_taxonomy(tmp_path):
    _draft(tmp_path, niche="выдуманная")
    _config(tmp_path)
    _taxonomy(tmp_path, ["бренды", "мужской-стиль"])
    with pytest.raises(PromptApplyError, match="нет в prompts/agents/niche-taxonomy"):
        apply_prompt(tmp_path, "2026-07-26-brief-выдуманная-reel.md",
                     git=_git_spy([]))


def test_refuses_on_sha_mismatch_with_409(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    with pytest.raises(PromptApplyError) as exc:
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                     sha256="0" * 64, git=_git_spy([]))
    assert exc.value.status == 409 and "перечитайте" in str(exc.value)


def test_accepts_matching_sha(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    info = read_draft(tmp_path, "2026-07-26-brief-бренды-reel.md")
    assert apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                        sha256=info["sha256"], git=_git_spy([])) == "бренды"


def test_refuses_when_section_has_two_blocks(tmp_path):
    # «Взять первый» здесь означало бы записать в боевой файл не то, что прочитал
    # человек, — поэтому отказ, а не эвристика.
    _draft(tmp_path, blocks=2)
    _config(tmp_path)
    with pytest.raises(PromptApplyError, match="2 блоков"):
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md", git=_git_spy([]))


def test_refuses_when_section_has_no_block(tmp_path):
    _draft(tmp_path, blocks=0)
    _config(tmp_path)
    with pytest.raises(PromptApplyError, match="нет блока"):
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md", git=_git_spy([]))


def test_refuses_prompt_pointing_at_mutable_formula_draft(tmp_path):
    # Защита evidence-цепочки: черновик рецепта меняется следующим прогоном, и
    # активная версия промпта начала бы означать другой текст правил.
    _draft(tmp_path, prompt="# Промпт\n\nБерёшь formulas/бренды/recipe.json и пишешь.")
    _config(tmp_path)
    with pytest.raises(PromptApplyError, match="изменяемый черновик рецепта"):
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md", git=_git_spy([]))


def test_allows_prompt_that_forbids_reading_the_draft(tmp_path):
    # Легальная форма упоминания: «formulas/{тема}/ не читаешь» — так написан
    # реальный промпт «бренды-магазины».
    _draft(tmp_path, prompt=("# Промпт\n\nСнапшот `formulas/_approved/бренды/r-v1.json`.\n"
                             "`formulas/бренды/` не читаешь — он может быть новее."))
    _config(tmp_path)
    assert apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                        git=_git_spy([])) == "бренды"


def test_refuses_when_prompt_file_already_exists(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    target = tmp_path / "prompts" / "briefs" / "бренды" / "reel.md"
    target.parent.mkdir(parents=True)
    target.write_text("старый промпт", encoding="utf-8")
    with pytest.raises(PromptApplyError, match="уже существует"):
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md", git=_git_spy([]))
    assert target.read_text(encoding="utf-8") == "старый промпт"


def test_no_write_happens_when_a_check_fails(tmp_path):
    # Порядок необратимости: все проверки ДО единой записи в боевые файлы.
    _draft(tmp_path, blocks=2)
    _config(tmp_path)
    calls, sheets = [], FakeSheets()
    with pytest.raises(PromptApplyError):
        apply_prompt(tmp_path, "2026-07-26-brief-бренды-reel.md",
                     git=_git_spy(calls), sheets=sheets)
    assert not (tmp_path / "prompts" / "briefs" / "бренды").exists()
    assert sheets.versions == [] and calls == []


# ── отклонение черновика ─────────────────────────────────────────────────────

def test_reject_marks_draft_and_commits(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    calls, sheets = [], FakeSheets()
    assert reject_draft(tmp_path, "2026-07-26-brief-бренды-reel.md",
                        reason="рано", git=_git_spy(calls), sheets=sheets) == "бренды"
    text = (tmp_path / "proposals"
            / "2026-07-26-brief-бренды-reel.md").read_text(encoding="utf-8")
    assert "status: rejected" in text
    assert not (tmp_path / "prompts" / "briefs").exists()   # промпт не записан
    assert "рано" in calls[0][0]
    assert sheets.runs[-1]["status"] == "skipped"


def test_reject_refuses_twice(tmp_path):
    _draft(tmp_path, status="rejected")
    _config(tmp_path)
    with pytest.raises(PromptApplyError, match="уже в статусе"):
        reject_draft(tmp_path, "2026-07-26-brief-бренды-reel.md", git=_git_spy([]))


# ── маршруты дашборда ────────────────────────────────────────────────────────

from fastapi.testclient import TestClient                       # noqa: E402

from cf.dashboard.app import create_app                         # noqa: E402
from tests.fakes import FakeSheets as SheetsFake                 # noqa: E402

BROWSER = {"origin": "http://127.0.0.1:8787"}


def _client(tmp_path, headers=BROWSER):
    app = create_app(sheets=SheetsFake({"run_log": [], "briefs": [],
                                        "prompt_versions": []}),
                     lab_root=tmp_path)
    return TestClient(app, headers=headers)


def test_route_apply_turns_the_theme_on(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    resp = _client(tmp_path).post(
        "/prompts/apply", data={"filename": "2026-07-26-brief-бренды-reel.md"},
        follow_redirects=False)
    assert resp.status_code == 303
    assert (tmp_path / "prompts" / "briefs" / "бренды" / "reel.md").is_file()


def test_route_apply_returns_409_on_stale_text(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    resp = _client(tmp_path).post(
        "/prompts/apply", data={"filename": "2026-07-26-brief-бренды-reel.md",
                                "sha256": "0" * 64})
    assert resp.status_code == 409
    assert not (tmp_path / "prompts" / "briefs").exists()


def test_route_apply_404_for_unknown_draft(tmp_path):
    _config(tmp_path)
    (tmp_path / "proposals").mkdir()
    resp = _client(tmp_path).post(
        "/prompts/apply", data={"filename": "2026-07-26-brief-нет-reel.md"})
    assert resp.status_code == 404


def test_route_apply_refuses_headless_client(tmp_path):
    # Этап 3б: включение промпта — решение оператора, безголовым оно быть не может.
    # Послабление «нет Origin и Referer -> пропускаем» осталось только у маршрутов
    # запуска, которыми пользуется cf-cycle.timer через curl.
    _draft(tmp_path)
    _config(tmp_path)
    resp = _client(tmp_path, headers={}).post(
        "/prompts/apply", data={"filename": "2026-07-26-brief-бренды-reel.md"})
    assert resp.status_code == 403
    assert not (tmp_path / "prompts" / "briefs").exists()


def test_route_reject_marks_draft(tmp_path):
    _draft(tmp_path)
    _config(tmp_path)
    resp = _client(tmp_path).post(
        "/prompts/reject", data={"filename": "2026-07-26-brief-бренды-reel.md",
                                 "reason": "рано"}, follow_redirects=False)
    assert resp.status_code == 303
    text = (tmp_path / "proposals"
            / "2026-07-26-brief-бренды-reel.md").read_text(encoding="utf-8")
    assert "status: rejected" in text


def test_lab_shows_full_prompt_text_for_review(tmp_path):
    # Ревью на месте: без полного текста кнопка «Включить» была бы слепым кликом.
    _draft(tmp_path)
    _config(tmp_path)
    _write_approved(tmp_path, "бренды")
    html = _client(tmp_path).get("/lab").text
    assert "Ворота · Включение правил сценариев темы" in html
    assert "Промпт генерации брифов" in html          # сам текст промпта
    assert 'action="/prompts/apply"' in html
    assert 'action="/prompts/reject"' in html


def _write_approved(root, niche):
    p = root / "formulas" / "_approved" / "index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"approved": [
        {"name": "r", "niche": niche, "version": 1,
         "path": f"formulas/_approved/{niche}/r-v1.json"}]}, ensure_ascii=False),
        encoding="utf-8")
