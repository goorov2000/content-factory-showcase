"""UI-тесты: карточки очереди формул/ниш в /lab, бейдж «авто» на /briefs."""
import json

from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from tests.fakes import FakeSheets


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _empty_index(root):
    _write(root / "formulas" / "_approved" / "index.json", {"approved": []})


def _write_formula(root, niche, name, status="proposed", status_reason="", version=1):
    path = root / "formulas" / niche / f"{name}.json"
    _write(path, {
        "name": name, "niche": niche, "version": version, "status": status,
        "status_reason": status_reason, "hook_structure": "5 дней — 5 образов",
        "evidence": {"source_urls": ["https://tiktok.com/v/1", "https://tiktok.com/v/2"],
                     "avg_views": 100000, "avg_er": 0.04},
    })
    return path


def _write_niche_proposal(root, filename, name, cluster_size=37):
    path = root / "agent-runtime" / "niche-proposals" / filename
    _write(path, {
        "name": name, "title": "Для высоких", "description": "Одежда для высоких людей",
        "cluster_size": cluster_size, "status": "proposed",
        "examples": [{"source_url": "https://tiktok.com/v/9", "caption": "рост 190"}],
    })
    return path


def _client(tmp_path, tables=None):
    sheets = FakeSheets(tables or {"run_log": [], "briefs": []})
    app = create_app(sheets=sheets, lab_root=tmp_path)
    return TestClient(app)


def test_lab_renders_formula_queue_cards(tmp_path):
    _empty_index(tmp_path)
    _write_formula(tmp_path, "стритвир", "grid")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "grid" in html
    assert "Утвердить" in html
    assert "В паузу" in html
    assert "Отклонить" in html
    assert 'action="/formulas/decision"' in html
    assert 'value="formulas/стритвир/grid.json"' in html


def _write_full_formula(root, niche, name, solution, **extra):
    """Формула с произвольной формой solution_structure — их в репозитории 9 разных."""
    data = {"name": name, "niche": niche, "version": 1, "status": "proposed",
            "problem_definition": "Зритель не знает, что выбрать",
            "solution_structure": solution,
            "evidence": {"source_urls": ["https://tiktok.com/v/1",
                                         "https://tiktok.com/v/2",
                                         "https://tiktok.com/v/3"],
                         "avg_views": 100000, "avg_er": 0.04}}
    data.update(extra)
    _write(root / "formulas" / niche / f"{name}.json", data)


def test_lab_shows_video_structure_from_steps_without_format(tmp_path):
    # Три черновика очереди (contrarian-verdict, specific-picks-list, star-peak-moment)
    # держат структуру в `steps` без ключа `format`: карточка читала только format,
    # поэтому у них пропадала строка «Формат», а шаги не показывались ни у кого.
    _empty_index(tmp_path)
    _write_full_formula(tmp_path, "обувь", "countdown",
                        {"steps": ["1. Вердикт против массовой вещи (0-3 сек)",
                                   "2. Аргумент: силуэт и посадка"],
                         "duration_sec": "8-60"})
    html = _client(tmp_path).get("/lab").text

    assert "Структура ролика" in html
    assert "Вердикт против массовой вещи (0-3 сек)" in html
    assert "8-60 сек" in html                       # длительность спасает строку «Формат»
    # ручную нумерацию снимаем — нумерует разметка, иначе выходит «1. 1. Вердикт»
    assert "1. Вердикт против" not in html


def test_lab_shows_video_structure_from_beats(tmp_path):
    # вторая форма того же поля: format + beats (мужские-образы, уход-грумминг)
    _empty_index(tmp_path)
    _write_full_formula(tmp_path, "стритвир", "frame",
                        {"format": "разговорный разбор образа",
                         "beats": ["0-5с: рамка словами", "финал: вопрос зрителю"]})
    html = _client(tmp_path).get("/lab").text

    assert "разговорный разбор образа" in html
    assert "0-5с: рамка словами" in html
    assert "финал: вопрос зрителю" in html


def test_lab_shows_visual_cta_and_leftover_solution_keys(tmp_path):
    # visual_requirements, cta_type и «хвост» solution_structure не доходили до
    # карточки вообще — теперь под катом «Съёмка», а не в небытии
    _empty_index(tmp_path)
    _write_full_formula(tmp_path, "обувь", "picks",
                        {"format": "список", "steps": ["показ моделей"],
                         "voiceover": "войсовер с конкретикой брендов"},
                        visual_requirements=["вещь на человеке, не на вешалке"],
                        cta_type="comment")
    html = _client(tmp_path).get("/lab").text

    assert "Съёмка" in html
    assert "вещь на человеке, не на вешалке" in html
    assert "войсовер" in html                        # незнакомый ключ не теряется
    assert "comment" in html


def test_lab_queue_card_shows_status_reason(tmp_path):
    # Заметка аналитика жила только в секции пауз: предупреждение «human_review
    # сохраняется — промо в 3 из 5 evidence» до оператора не доходило
    _empty_index(tmp_path)
    _write_full_formula(tmp_path, "обувь", "note", {"format": "список"},
                        status_reason="human_review сохраняется: промо в 3 из 5 evidence")
    html = _client(tmp_path).get("/lab").text

    assert "Заметка аналитика" in html
    assert "промо в 3 из 5 evidence" in html


def test_lab_renders_niche_card(tmp_path):
    _empty_index(tmp_path)
    _write_niche_proposal(tmp_path, "tall.json", "одежда-для-высоких")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "НОВАЯ ТЕМА" in html          # словарь ДС §7: ниша → тема
    assert "37" in html
    assert "Принять тему" in html
    assert 'action="/niches/decision"' in html
    assert 'value="tall.json"' in html


def test_lab_renders_paused_with_reason(tmp_path):
    _empty_index(tmp_path)
    _write_formula(tmp_path, "стиль", "paused-one", status="paused",
                   status_reason="3 из 5 reject")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "paused-one" in html
    assert "3 из 5 reject" in html
    assert "Вернуть в работу" in html


def test_lab_empty_queue_state(tmp_path):
    _empty_index(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "empty-state" in html
    assert 'action="/formulas/decision"' not in html
    assert 'action="/niches/decision"' not in html


def test_lab_queue_card_has_no_prompt_notice(tmp_path):
    # Пометка «нет промпта» убрана из очереди одобрения: промпт ниши пишется ПОСЛЕ
    # утверждения рецепта, до решения она ничего не меняла и висела на каждой карточке
    _empty_index(tmp_path)
    _write_formula(tmp_path, "барбершоп", "grid")   # prompts/briefs/барбершоп нет
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "grid" in html                            # сам рецепт в очереди
    assert "тема ждёт правил сценариев" not in html
    assert "Включение правил сценариев темы" not in html    # утверждённых рецептов нет
    assert "/prompts/scaffold" not in html           # скаффолд удалён целиком


def test_lab_no_badge_when_niche_has_prompt(tmp_path):
    _empty_index(tmp_path)
    _write_formula(tmp_path, "мужские-образы", "grid")
    (tmp_path / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (tmp_path / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "тема ждёт правил сценариев" not in resp.text
    assert "Включение правил сценариев темы" not in resp.text


def test_lab_approved_formula_shows_no_prompt_badge(tmp_path):
    # Бейдж живёт там, где он означает работающую задачу: у УТВЕРЖДЁННОГО рецепта
    # темы без промпта. Плюс карточка ворот «Включение правил сценариев темы».
    _write(tmp_path / "formulas" / "_approved" / "index.json",
           {"approved": [{"name": "sys", "niche": "мужской-стиль", "version": 1,
                          "path": "formulas/мужской-стиль/sys.json",
                          "approved_at": "2026-07-15T10:00:00+00:00"}]})
    _write(tmp_path / "formulas" / "мужской-стиль" / "sys.json",
           {"name": "sys", "niche": "мужской-стиль", "version": 1,
            "status": "approved", "confidence": "high"})
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "sys" in html                             # approved-формула отрисована
    assert "тема ждёт правил сценариев" in html
    assert "Включение правил сценариев темы" in html
    # ▶ рисуется только с раннером (как у ритуалов) — здесь его нет
    assert 'action="/prompts/write"' not in html


def test_lab_approved_formula_no_badge_when_prompt_exists(tmp_path):
    _write(tmp_path / "formulas" / "_approved" / "index.json",
           {"approved": [{"name": "sys", "niche": "мужские-образы", "version": 1,
                          "path": "formulas/мужские-образы/sys.json",
                          "approved_at": "2026-07-15T10:00:00+00:00"}]})
    _write(tmp_path / "formulas" / "мужские-образы" / "sys.json",
           {"name": "sys", "niche": "мужские-образы", "version": 1,
            "status": "approved", "confidence": "high"})
    (tmp_path / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (tmp_path / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "ниша без brief-промпта" not in resp.text


def test_lab_approved_formula_shows_own_performance(tmp_path):
    # P5.11: own_performance из индекса рендерится рядом с формулой (свои замеры)
    _write(tmp_path / "formulas" / "_approved" / "index.json",
           {"approved": [{"name": "sys", "niche": "мужские-образы", "version": 1,
                          "path": "formulas/мужские-образы/sys.json",
                          "approved_at": "2026-07-15T10:00:00+00:00",
                          "own_performance": {"reels": 4, "median_views": 120000,
                                              "avg_er": 0.037,
                                              "last_measured_at": "2026-07-18"}}]})
    _write(tmp_path / "formulas" / "мужские-образы" / "sys.json",
           {"name": "sys", "niche": "мужские-образы", "version": 1, "status": "approved"})
    (tmp_path / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (tmp_path / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "Свои замеры" in html
    assert "4 ролика" in html          # склонение, а не «4 роликов»
    assert "120 000" in html                    # медиана: единый fmt_views
    assert "3,7%" in html                            # вовлечённость: единый fmt_pct
    assert "2026-07-18" in html


def test_lab_approved_formula_without_own_performance_shows_no_data(tmp_path):
    # P5.11: формула без own_performance в индексе -> «нет данных»
    _write(tmp_path / "formulas" / "_approved" / "index.json",
           {"approved": [{"name": "sys", "niche": "мужские-образы", "version": 1,
                          "path": "formulas/мужские-образы/sys.json",
                          "approved_at": "2026-07-15T10:00:00+00:00"}]})
    _write(tmp_path / "formulas" / "мужские-образы" / "sys.json",
           {"name": "sys", "niche": "мужские-образы", "version": 1, "status": "approved"})
    (tmp_path / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (tmp_path / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "Свои замеры: нет данных" in resp.text


def test_lab_own_performance_er_none_shows_no_data(tmp_path):
    # P5.11: avg_er None (er не замерялся) -> в блоке своих замеров ER «нет данных»
    _write(tmp_path / "formulas" / "_approved" / "index.json",
           {"approved": [{"name": "sys", "niche": "мужские-образы", "version": 1,
                          "path": "formulas/мужские-образы/sys.json",
                          "approved_at": "2026-07-15T10:00:00+00:00",
                          "own_performance": {"reels": 2, "median_views": 5000,
                                              "avg_er": None,
                                              "last_measured_at": "2026-07-18"}}]})
    _write(tmp_path / "formulas" / "мужские-образы" / "sys.json",
           {"name": "sys", "niche": "мужские-образы", "version": 1, "status": "approved"})
    (tmp_path / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (tmp_path / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "2 ролика" in html
    assert "вовлечённость нет данных" in html


def test_lab_broken_own_performance_falls_back_to_no_data(tmp_path):
    # P5.11: битое own_performance в индексе (строка вместо dict / без median_views)
    # не роняет весь /lab в 500 — нормализуется в «нет данных»
    _write(tmp_path / "formulas" / "_approved" / "index.json",
           {"approved": [
               {"name": "s1", "niche": "мужские-образы", "version": 1,
                "path": "formulas/мужские-образы/s1.json",
                "approved_at": "2026-07-15T10:00:00+00:00",
                "own_performance": "мусор"},                      # строка вместо dict
               {"name": "s2", "niche": "мужские-образы", "version": 1,
                "path": "formulas/мужские-образы/s2.json",
                "approved_at": "2026-07-15T10:00:00+00:00",
                "own_performance": {"reels": "x"}},               # без median_views/нечисло
           ]})
    for nm in ("s1", "s2"):
        _write(tmp_path / "formulas" / "мужские-образы" / f"{nm}.json",
               {"name": nm, "niche": "мужские-образы", "version": 1, "status": "approved"})
    (tmp_path / "prompts" / "briefs" / "мужские-образы").mkdir(parents=True)
    (tmp_path / "prompts" / "briefs" / "мужские-образы" / "reel.md").write_text(
        "промпт", encoding="utf-8")
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200                        # не 500
    assert resp.text.count("Свои замеры: нет данных") >= 2


def test_lab_paused_formula_has_no_prompt_notice(tmp_path):
    # Рецепт на паузе сценариев не производит, а ворота считают
    # только утверждённые — пометка вела бы в карточку, которой для темы нет
    _empty_index(tmp_path)
    _write_formula(tmp_path, "барбершоп", "old", status="paused",
                   status_reason="на паузе")        # prompts/briefs/барбершоп нет
    client = _client(tmp_path)

    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "old" in html
    assert "тема ждёт правил сценариев" not in html
    assert "Включение правил сценариев темы" not in html


BRIEF_TABLES_AUTO = {
    "briefs": [
        {"brief_id": "B1", "hook": "3 образа на осень", "niche": "мужские-образы",
         "review_status": "approved", "created_at": "2026-07-12",
         "script": "Кадр 1 — поло...", "references": "https://tiktok.com/v/1",
         "formula_id": "F-07", "rejection_reason": "",
         "reviewer_notes": "авто-одобрен: recommend + формула approved"},
        {"brief_id": "B2", "hook": "Уход за трикотажем", "niche": "лайфстайл-мотивация",
         "review_status": "approved", "created_at": "2026-07-10",
         "script": "Кадр 1 — стирка...", "references": "https://tiktok.com/v/2",
         "formula_id": "F-03", "rejection_reason": "", "reviewer_notes": "ревьюер: ок"},
    ],
    "run_log": [],
}


def test_brief_auto_badge(tmp_path):
    client = _client(tmp_path, BRIEF_TABLES_AUTO)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    html = resp.text
    assert 'без проверки' in html
    # B1 авто-одобрен и выбран по умолчанию — бейдж в строке списка и в карточке решения;
    # B2 с обычной заметкой бейдж нигде не получает
    assert html.count('без проверки') == 2   # строка списка + карточка решения
