"""Звено «brief-промпт ниши»: агент пишет промпт по утверждённым рецептам темы.

Пришло на смену кнопке-скаффолду (P5.2), которая копировала промпт ниши-донора
«мужские-образы» с его таймингами, запретами и ссылкой на несуществующую формулу
целевой ниши, активировала версию и коммитила — то есть гасила честный скип
brief-generator и обходила правило №3 (промпты только через ревью оператора).

Реального claude ни один тест не зовёт: run_command подменён.
"""
import json

import pytest
from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from cf.dashboard.runner import PROMPT_WRITER, PROMPT_WRITER_CHANNEL, StageRunner
from cf.dashboard.sections import lab_context
from tests.fakes import FakeSheets


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _approved(root, niche, name="sys"):
    """Утверждённый рецепт ниши: индекс + сам файл формулы."""
    _write(root / "formulas" / "_approved" / "index.json",
           {"approved": [{"name": name, "niche": niche, "version": 1,
                          "path": f"formulas/{niche}/{name}.json",
                          "approved_at": "2026-07-15T10:00:00+00:00"}]})
    _write(root / "formulas" / niche / f"{name}.json",
           {"name": name, "niche": niche, "version": 1, "status": "approved",
            "confidence": "high"})


def _install_command(root):
    """Слэш-команда на месте (в бою — после применения proposal оператором)."""
    path = root.joinpath(*PROMPT_WRITER["command_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("команда", encoding="utf-8")
    return path


def _runner(root, calls=None, tmp_path=None):
    def run_command(argv):
        if calls is not None:
            calls.append(list(argv))
        return 0, json.dumps({"result": "готово", "session_id": "s1"}), ""

    return StageRunner(FakeSheets({"run_log": []}), {"dashboard": {"workflows": {}}},
                       http_post=lambda url: None, run_command=run_command,
                       root=root, locks_dir=str(tmp_path or root))


# --- запуск звена ----------------------------------------------------------

def test_run_prompt_writer_calls_claude_with_niche(tmp_path):
    _install_command(tmp_path)
    calls = []
    runner = _runner(tmp_path, calls)

    assert runner.run_prompt_writer("обувь") is True
    runner._run_prompt_writer_sync("обувь")  # синхронно, без гонки с потоком

    prompts = [a for c in calls for a in c if str(a).startswith("/cf-")]
    assert "/cf-write-brief-prompt обувь" in prompts


def test_run_prompt_writer_refuses_unsafe_niche(tmp_path):
    # имя уходит в argv claude — та же защита, что у session_id звеньев (M22)
    runner = _runner(tmp_path)
    with pytest.raises(ValueError):
        runner.run_prompt_writer("../../etc/passwd")
    with pytest.raises(ValueError):
        runner.run_prompt_writer("")


def test_prompt_writer_without_command_reports_instead_of_running(tmp_path):
    # Пока proposal не применён, файла команды нет. claude получил бы
    # «/cf-write-brief-prompt обувь» как обычный текст — отказ должен быть громким.
    calls = []
    runner = _runner(tmp_path, calls)

    assert runner._run_prompt_writer_sync("обувь") is False

    assert calls == []                                   # claude не вызывался
    reports = runner.reports[PROMPT_WRITER_CHANNEL]
    assert len(reports) == 1
    assert "Команда не установлена" in reports[0]["text"]
    assert "brief-prompt-writer" in reports[0]["text"]


def test_prompt_writer_second_run_for_same_niche_refused(tmp_path):
    _install_command(tmp_path)
    runner = _runner(tmp_path)
    runner.prompt_writer_state["обувь"] = "running"

    assert runner.run_prompt_writer("обувь") is False
    assert runner.run_prompt_writer("стритвир") is True  # другая ниша не блокирована


def test_prompt_writer_state_cleared_after_run(tmp_path):
    # иначе повторный запуск ниши навсегда заблокирован, а поллинг «Отчётов» не встанет
    _install_command(tmp_path)
    runner = _runner(tmp_path)
    runner.prompt_writer_state["обувь"] = "running"

    runner._run_prompt_writer_sync("обувь")

    assert "обувь" not in runner.prompt_writer_state
    assert runner.any_running() is False


def test_prompt_writer_running_keeps_polling_alive(tmp_path):
    runner = _runner(tmp_path)
    runner.prompt_writer_state["обувь"] = "running"
    assert runner.any_running() is True


# --- ворота «Включение правил сценариев темы» ---------------------------------------
# Предикат очереди переехал в queues.prompt_gate (единый источник для ленты,
# ворот и раннера) — полное покрытие в tests/test_queues.py. Здесь остаётся
# проверка, что /lab действительно отдаёт ворота в контекст шаблона.


def test_lab_route_exposes_prompt_gate_rows(tmp_path):
    _approved(tmp_path, "обувь")
    resp = _client(tmp_path).get("/lab")
    assert resp.status_code == 200
    assert "Включение правил сценариев темы" in resp.text
    assert "обувь" in resp.text


# --- маршрут ---------------------------------------------------------------

class _Cache:
    """Минимальный кеш для lab_context: Sheets-части /lab тут не проверяем."""

    stale = False

    def rows(self, _tab):
        raise RuntimeError("нет данных")


def _client(tmp_path, runner=None):
    app = create_app(sheets=FakeSheets({"run_log": [], "briefs": []}),
                     lab_root=tmp_path, runner=runner)
    return TestClient(app)


def test_lab_renders_run_button_with_runner(tmp_path):
    _approved(tmp_path, "обувь")
    html = _client(tmp_path, _runner(tmp_path)).get("/lab").text

    # ▶ остаётся только у темы без черновика: «написать сейчас, не дожидаясь
    # прогона». Тема с готовым черновиком показывает вместо неё текст и кнопку
    # включения — точка действия одна.
    assert "Включение правил сценариев темы" in html
    assert 'action="/prompts/write"' in html
    assert 'value="обувь"' in html
    assert "не дожидаясь прогона" in html


def test_route_prompts_write_starts_agent(tmp_path):
    _install_command(tmp_path)
    runner = _runner(tmp_path)
    started = []
    runner.run_prompt_writer = lambda niche: started.append(niche) or True

    resp = _client(tmp_path, runner).post("/prompts/write", data={"niche": "обувь"},
                                          follow_redirects=False)

    assert resp.status_code == 303
    # отчёт живёт на /runs (переезд «Отчётов этапов» 2026-07-28) — туда и ведём,
    # чтобы оператор увидел его живым
    assert resp.headers["location"] == "/runs"
    assert started == ["обувь"]


def test_route_prompts_write_rejects_unsafe_niche(tmp_path):
    resp = _client(tmp_path, _runner(tmp_path)).post(
        "/prompts/write", data={"niche": "../../etc"}, follow_redirects=False)
    assert resp.status_code == 422


def test_route_prompts_write_without_runner_404(tmp_path):
    resp = _client(tmp_path).post("/prompts/write", data={"niche": "обувь"},
                                  follow_redirects=False)
    assert resp.status_code == 404


def test_scaffold_route_and_api_gone(tmp_path):
    from cf.dashboard import decisions

    assert not hasattr(decisions, "scaffold_prompt")
    resp = _client(tmp_path).post("/prompts/scaffold", data={"niche": "обувь"},
                                  follow_redirects=False)
    assert resp.status_code == 404
