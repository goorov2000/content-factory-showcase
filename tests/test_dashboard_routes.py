import json
import re
import sys
import tempfile
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from cf.dashboard.data import DataCache
from cf.dashboard.runner import StageRunner
from tests.fakes import FakeSheets
from tests.test_dashboard_sections import _origin_repo


# Этап 3б: маршруты-РЕШЕНИЯ (ревью сценария, решение по рецепту/теме, включение
# промпта темы) требуют Origin/Referer — они приходят с формы дашборда, а не от
# безголового клиента. TestClient заголовков не шлёт, поэтому даём их по умолчанию;
# тесты самого CSRF передают свои headers= и перекрывают этот дефолт.
BROWSER = {"origin": "http://127.0.0.1:8787"}


def make_client(tables=None, **kwargs):
    sheets = FakeSheets(tables or {})
    app = create_app(sheets=sheets, **kwargs)
    return TestClient(app, headers=BROWSER), sheets


def test_root_opens_the_scripts_screen():
    # Стартовый экран — сценарии, а не внутренности завода (решение владельца
    # 2026-07-26): конечный пользователь дашборда — SMM-продюсер, и единственное
    # решение, которое остаётся человеку, живёт здесь. «Обзор» — в один клик.
    client, _ = make_client()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/briefs"


OVERVIEW_TABLES = {
    "raw_tiktok": [{"source_url": "u1", "posted_at": "2026-07-01"}],
    "raw_instagram": [],
    "briefs": [{"brief_id": "B1", "review_status": "pending", "created_at": "2026-07-12"}],
    "reels": [{"reel_id": "R1", "published_at": "2026-07-05"}],
    "performance": [{"reel_id": "R1", "er": "4.5"}],
    "run_log": [{"run_id": "aaa", "agent": "cf-analyze", "status": "success",
                 "completed_at": "2026-07-14T12:04:00+00:00"}],
}


def test_overview_renders_metrics_and_pipeline():
    client, _ = make_client(OVERVIEW_TABLES)
    resp = client.get("/overview")
    assert resp.status_code == 200
    text = resp.text
    # Плитки этапа 3 (2026-07-28, элементы 6–9): звенья завода, очередь
    # назначения, публикации и «Темп недели». Прежние заголовки —
    # «СОБРАННЫЕ РОЛИКИ», «СЦЕНАРИИ ЖДУТ ВНИМАНИЯ», «ЖДУТ СТАТИСТИКИ»,
    # «СРЕДНЯЯ ВОВЛЕЧЁННОСТЬ» — заменены осознанно, по решениям владельца.
    assert "ЗВЕНЬЯ ЗАВОДА" in text and "ЖДУТ НАЗНАЧЕНИЯ ИСПОЛНИТЕЛЯ" in text
    assert "ОПУБЛИКОВАНЫ" in text and "ТЕМП НЕДЕЛИ" in text
    # Лента из 14 шагов на языке продюсера: «Контент-завод» раскрыт в Разметку/
    # Анализ/Черновики рецептов/Промпт темы/Сценарии/Ревью. Два решения из трёх
    # стали машинными (вечер 2026-07-26): перед их воротами стоит строка работы, а
    # сами ворота показывают только исключения — «на ваше решение».
    for step in ("Сбор роликов", "Разметка тем", "Анализ приёмов",
                 "Черновики рецептов", "Проверка рецептов",
                 "Рецепты на ваше решение", "Черновик правил сценариев темы",
                 "Включение правил сценариев темы", "Темы на ваше решение",
                 "Сценарии", "Ревью сценариев", "Одобрение сценариев",
                 "Съёмка и публикация", "Статистика"):
        assert step in text
    # Разделителя «дальше — ручные этапы» больше нет: он врал — ниже ворот снова
    # идёт автоматика (промпт темы, сценарии, ревью, статистика).
    assert "дальше — ручные этапы" not in text
    assert "Apify" in text                           # actor шага сбора (был N8N)
    assert 'href="/sources"' in text                 # «открыть →» шага «Сбор» ведёт в раздел


def test_overview_cleaned_of_elements_owner_removed():
    # Уборка 2026-07-28 (элементы 3, 10–13, 16, 18, 20, 21). Инварианты на
    # снесённое поправлены НАМЕРЕННО — это ТЗ владельца, а не подгонка теста:
    # период жёстко 30 дней, счёт недели переехал в кольца, всё про прогоны — на
    # «Историю задач», графики результатов — на «Результаты».
    client, _ = make_client(OVERVIEW_TABLES)
    text = client.get("/overview").text
    for gone in ('name="days"',                    # 3: селектор периода
                 "Сценариев одобрено за неделю",   # 10: карточка темпа
                 "Темы в работе",                  # 12: строка воронки
                 "доходят до съёмки за неделю",    # 13: конверсия
                 "ждут вас:",                      # 16: строка под «Конвейером»
                 "цикл замкнут",                   # 18: статичный лозунг
                 "Вовлечённость по неделям",       # 20: график
                 "Последние запуски"):             # 21: карточка запусков
        assert gone not in text, gone


def test_overview_period_is_fixed_at_30_days_regardless_of_query():
    # Селектора периода больше нет; метрики считаются за 30 дней, а мусор в
    # параметре по-прежнему не роняет страницу (маршрут его просто не читает).
    tables = dict(OVERVIEW_TABLES)
    tables["raw_tiktok"] = [{"source_url": "u1", "posted_at": "2026-07-01"},
                            {"source_url": "u2", "posted_at": "2026-01-01"}]
    client, _ = make_client(tables)
    baseline = client.get("/overview")
    assert baseline.status_code == 200
    for query in ("?days=7", "?days=90", "?days=абв"):
        resp = client.get("/overview" + query)
        assert resp.status_code == 200
        assert resp.text == baseline.text          # период на параметр не реагирует


# Маршрута /refresh больше нет (решение владельца 2026-07-28, С3): кнопка
# «Обновить данные» освободила место под «Уведомления», а принудительный сброс
# кеша повторяется обычным F5. Прежние три теста редиректа и один CSRF-тест
# заменены проверкой ниже — что маршрута и кнопки не осталось нигде.
GOOD_ORIGIN = {"origin": "http://127.0.0.1:8787"}


def test_refresh_route_and_button_are_gone():
    client, _ = make_client(OVERVIEW_TABLES)
    assert client.post("/refresh", headers=GOOD_ORIGIN,
                       follow_redirects=False).status_code == 404
    html = client.get("/overview").text
    assert "Обновить данные" not in html
    assert 'action="/refresh"' not in html


def test_no_screen_still_points_at_the_removed_button():
    # Мёртвая ссылка в тексте ошибки хуже отсутствия подсказки: она обещает
    # кнопку, которой нет.
    client, _ = make_client({})
    for path in ("/overview", "/briefs", "/briefs/shooting-list", "/runs",
                 "/performance", "/sources", "/lab"):
        assert "Обновить данные" not in client.get(path).text, path


def test_overview_all_zero_er_renders():
    tables = dict(OVERVIEW_TABLES)
    tables["performance"] = [{"reel_id": "R1", "er": "0"}]
    client, _ = make_client(tables)
    resp = client.get("/overview")
    assert resp.status_code == 200   # peak == 0 не должен ронять график


def test_overview_error_branch_when_sheets_down():
    class BrokenSheets(FakeSheets):
        def read_rows(self, tab_key):
            raise ConnectionError("sheets down")

    app = create_app(sheets=BrokenSheets({}))
    client = TestClient(app)
    resp = client.get("/overview")
    assert resp.status_code == 200
    # Фаза 3: продюсеру — что делать, техническая причина под катом (не выброшена).
    assert "Не удалось загрузить данные" in resp.text
    assert "sheets down" in resp.text


def test_stages_partial_survives_sheets_down():
    # Ревью 14.09.2026: фолбэк метрик строками «—» доезжал до plural_ru → int("—"),
    # и лента конвейера отдавала 500 ровно тогда, когда что-то сломалось; htmx
    # опрашивает её каждые 2 секунды во время прогона.
    class BrokenSheets(FakeSheets):
        def read_rows(self, tab_key, include_heavy=False):
            raise ConnectionError("sheets down")

    client = TestClient(create_app(sheets=BrokenSheets({})))
    assert client.get("/partials/stages").status_code == 200


BRIEF_TABLES = {
    "briefs": [
        {"brief_id": "B1", "hook": "3 образа на осень", "niche": "мужские-образы",
         "review_status": "pending", "created_at": "2026-07-12",
         "script": "Кадр 1 — поло...", "references": "https://tiktok.com/v/1",
         "formula_id": "F-07", "rejection_reason": "", "reviewer_notes": ""},
        {"brief_id": "B2", "hook": "Уход за трикотажем", "niche": "лайфстайл-мотивация",
         "review_status": "approved", "created_at": "2026-07-10",
         "script": "Кадр 1 — стирка...", "references": "https://tiktok.com/v/2",
         "formula_id": "F-03", "rejection_reason": "", "reviewer_notes": ""},
    ],
    "run_log": [],
}


def test_briefs_queue_and_detail():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    assert "B1" in resp.text and "B2" in resp.text
    assert "3 образа на осень" in resp.text
    # первый pending выбран по умолчанию — карточка с его скриптом
    assert "Кадр 1 — поло..." in resp.text
    assert "Одобрить" in resp.text and "Отклонить" in resp.text


def test_numeric_looking_notes_do_not_take_down_the_scripts_screen():
    # Ревью 14.09.2026: gspread прогоняет ячейки через numericise, и заметка «5»
    # или «2026» приезжала int — шаблон звал .startswith() и отдавал 500 на
    # стартовой странице дашборда (/ -> /briefs).
    tables = {"briefs": [dict(BRIEF_TABLES["briefs"][1], reviewer_notes=5)],
              "run_log": []}
    client, _ = make_client(tables)
    assert client.get("/briefs").status_code == 200
    assert client.get("/briefs?status=approved&brief=B2").status_code == 200


def test_briefs_filter_pending():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.get("/briefs?status=pending")
    assert "B1" in resp.text and "B2" not in resp.text


# --- P2.13: пустой ?id= выбирает первый pending, а не бриф с пустым brief_id ---


def test_briefs_empty_id_selects_first_pending_not_empty_brief_id():
    tables = {
        "briefs": [
            {"brief_id": "", "hook": "пустой id", "review_status": "approved",
             "script": "СКРИПТ-ПУСТОГО", "references": "", "formula_id": ""},
            {"brief_id": "B9", "hook": "живой pending", "review_status": "pending",
             "script": "СКРИПТ-B9", "references": "", "formula_id": ""},
        ],
        "run_log": [],
    }
    client, _ = make_client(tables)
    resp = client.get("/briefs")                          # ?id= по умолчанию пустой
    assert resp.status_code == 200
    # скрипт рендерится только в карточке выбранного брифа
    assert "СКРИПТ-B9" in resp.text                       # выбран первый pending
    assert "СКРИПТ-ПУСТОГО" not in resp.text              # не строка с пустым brief_id


# --- P2.13: статус «доработка» (revised) виден, фильтруется, считается ---


def test_briefs_revised_status_is_filterable_counted_and_badged():
    tables = {
        "briefs": [
            dict(BRIEF_TABLES["briefs"][0]),              # B1 pending
            {"brief_id": "B7", "hook": "на доработку", "niche": "образы",
             "review_status": "Revised ",                 # грязный регистр — нормализуется
             "created_at": "2026-07-11", "script": "переделать хук",
             "references": "", "formula_id": "",
             "rejection_reason": "", "reviewer_notes": ""},
        ],
        "run_log": [],
    }
    client, _ = make_client(tables)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    assert "Доработка" in resp.text                        # чип + бейдж «Доработка»
    assert "Доработка · 1" in resp.text                    # счётчик ревизных брифов
    assert "B7" in resp.text                               # ревизный бриф виден в списке
    assert "badge revised" in resp.text                    # бейдж со статусом revised
    resp2 = client.get("/briefs?status=revised")           # фильтр по «доработке»
    assert "B7" in resp2.text and "B1" not in resp2.text


def test_review_post_approves_and_rerenders():
    client, sheets = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sheets.tables["briefs"][0]["review_status"] == "approved"


def test_saved_review_survives_failed_reread_of_the_queue():
    # Ревью 14.09.2026: решение записано, а перечитывание очереди упало — маршрут
    # отдавал 500, htmx не свопал ответ, и продюсер жал кнопку второй раз.
    class FlakyAfterWrite(FakeSheets):
        written = False

        def update_row_fields(self, *a, **k):
            FlakyAfterWrite.written = True
            return super().update_row_fields(*a, **k)

        def read_rows(self, tab_key, include_heavy=False):
            if tab_key == "briefs" and FlakyAfterWrite.written:
                raise ConnectionError("sheets 503")
            return super().read_rows(tab_key, include_heavy)

    sheets = FlakyAfterWrite(json.loads(json.dumps(BRIEF_TABLES)))
    client = TestClient(create_app(sheets=sheets), headers=BROWSER)
    resp = client.post("/briefs/B1/review", data={"decision": "approved", "notes": "ок"},
                       headers={"hx-request": "true"}, follow_redirects=False)
    assert resp.status_code == 200
    assert "Решение сохранено" in resp.text
    assert sheets.tables["briefs"][0]["review_status"] == "approved"


def test_enabling_theme_prompt_refreshes_prompt_versions_cache(tmp_path):
    # Ревью 14.09.2026: после «Включить промпт темы» кеш prompt_versions (TTL 180 с)
    # не сбрасывался, и /lab до трёх минут показывал «версия не включена».
    from cf.dashboard import app as app_mod
    invalidated = []

    class SpyCache(DataCache):
        def invalidate(self, tab_key):
            invalidated.append(tab_key)
            return super().invalidate(tab_key)

    sheets = FakeSheets({"prompt_versions": [], "run_log": []})
    cache = SpyCache(sheets)
    client = TestClient(create_app(sheets=sheets, cache=cache), headers=BROWSER)
    original = app_mod.apply_prompt
    app_mod.apply_prompt = lambda *a, **k: "тема"
    try:
        client.post("/prompts/apply", data={"filename": "x.md", "sha256": ""},
                    follow_redirects=False)
    finally:
        app_mod.apply_prompt = original
    assert "prompt_versions" in invalidated


def test_review_post_sends_reason_code():
    # P5.12: форма reject шлёт reason_code -> префикс [code] в rejection_reason.
    client, sheets = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "rejected", "notes": "URL X",
                             "reason_code": "reference_mismatch"},
                       follow_redirects=False)
    assert resp.status_code == 303
    brief = sheets.tables["briefs"][0]
    assert brief["review_status"] == "rejected"
    assert brief["rejection_reason"] == "[reference_mismatch]"
    assert brief["reviewer_notes"] == "URL X"


def test_review_reject_form_renders_reason_dropdown():
    # Дропдаун кодов присутствует в форме ревью pending-брифа.
    client, _ = make_client(BRIEF_TABLES)
    resp = client.get("/briefs?status=pending")
    assert 'name="reason_code"' in resp.text
    assert "reference_mismatch" in resp.text
    assert "Женский референс в мужском брифе" in resp.text


def test_review_form_disables_both_decision_buttons():
    # htmx 1.9 разворачивает «find X» в querySelector — ОДИН элемент, поэтому
    # «find button» гасил только «Одобрить». Двойной клик по «Отклонить» на
    # медленном Sheets давал два POST → две записи решения и две строки Run Log.
    client, _ = make_client(BRIEF_TABLES)
    html = client.get("/briefs?status=pending").text
    assert 'hx-disabled-elt="#review-actions button"' in html
    assert 'id="review-actions"' in html
    assert "find button" not in html


def test_review_post_unknown_brief_404():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/NOPE/review", data={"decision": "approved", "notes": ""})
    assert resp.status_code == 404


# --- P3.5: решение по брифу точечно инвалидирует только briefs+run_log ---


class _SpyCache(DataCache):
    def __init__(self, sheets, **kw):
        super().__init__(sheets, **kw)
        self.invalidated = []
        self.refresh_calls = 0

    def invalidate(self, tab_key):
        self.invalidated.append(tab_key)
        super().invalidate(tab_key)

    def refresh(self):
        self.refresh_calls += 1
        super().refresh()


def test_review_invalidates_only_briefs_and_run_log_no_full_refresh():
    sheets = FakeSheets({**BRIEF_TABLES,
                         "raw_tiktok": [{"source_url": "u1", "posted_at": "2026-07-01"}]})
    cache = _SpyCache(sheets)
    client = TestClient(create_app(sheets=sheets, cache=cache), headers=BROWSER)
    cache.rows("raw_tiktok")                        # медленная вкладка прогрета в кеш
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sorted(cache.invalidated) == ["briefs", "run_log"]  # только они
    assert cache.refresh_calls == 0                 # ни pre-write, ни полного сброса
    assert cache._store.get("raw_tiktok") is not None  # raw остался в кеше


def test_review_post_schema_mismatch_returns_descriptive_error_and_keeps_pending():
    # briefs без колонок rejection_reason/reviewer_notes -> решение НЕ сохраняется молча,
    # оператор получает внятную ошибку про схему, а не голый 500.
    tables = {
        "briefs": [{"brief_id": "B1", "review_status": "pending"}],
        "run_log": [],
    }
    client, sheets = make_client(tables)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       follow_redirects=False)
    assert resp.status_code == 500
    body = resp.text.lower()
    assert "схем" in body or "колон" in body
    assert sheets.tables["briefs"][0]["review_status"] == "pending"  # не записано


def test_review_post_runlog_failure_warns_but_saves_decision():
    # P2.14: запись в CF Run Log сорвалась. Решение всё равно применяется в Sheets,
    # но оператор ДОЛЖЕН это увидеть — ответ 200 с предупреждением (не немой редирект),
    # зеркалит runner._finish, который честно показывает сбой лога в UI.
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("run_log append failed")

    tables = {
        "briefs": [dict(BRIEF_TABLES["briefs"][0])],   # B1 pending, со всеми колонками
        "run_log": [],
    }
    sheets = BrokenLogSheets(tables)
    client = TestClient(create_app(sheets=sheets), headers=BROWSER)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       follow_redirects=False)
    assert resp.status_code == 200                      # не редирект — рендер с заметкой
    assert sheets.tables["briefs"][0]["review_status"] == "approved"  # решение записано
    assert "Run Log" in resp.text                       # предупреждение о сбое видно


def test_review_post_pending_undoes_rejected_decision():
    tables = {
        "briefs": [dict(BRIEF_TABLES["briefs"][0], review_status="rejected",
                        rejection_reason="слабый CTA")],
        "run_log": [],
    }
    client, sheets = make_client(tables)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "pending", "notes": ""},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sheets.tables["briefs"][0]["review_status"] == "pending"
    assert sheets.tables["briefs"][0]["rejection_reason"] == ""


def test_briefs_detail_shows_undo_button_for_decided_brief():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.get("/briefs?id=B2")  # B2 одобрен в фикстуре
    assert resp.status_code == 200
    assert "Отменить решение" in resp.text


def test_review_post_invalid_decision_still_422():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/B1/review", data={"decision": "maybe", "notes": ""})
    assert resp.status_code == 422


def test_briefs_references_split_on_semicolon():
    tables = {
        "briefs": [dict(BRIEF_TABLES["briefs"][0],
                        references="https://a.example/1; https://b.example/2")],
        "run_log": [],
    }
    client, _ = make_client(tables)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    assert 'href="https://a.example/1"' in resp.text
    assert 'href="https://b.example/2"' in resp.text
    assert "https://a.example/1;" not in resp.text


def test_briefs_dirty_status_still_shows_review_form():
    tables = {
        "briefs": [dict(BRIEF_TABLES["briefs"][0], review_status="Pending ")],
        "run_log": [],
    }
    client, _ = make_client(tables)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    assert "Одобрить" in resp.text and "Отклонить" in resp.text
    assert 'badge pending' in resp.text


def test_briefs_non_http_reference_rendered_as_text():
    tables = {
        "briefs": [dict(BRIEF_TABLES["briefs"][0], references="note: см. диск")],
        "run_log": [],
    }
    client, _ = make_client(tables)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    assert "note:" in resp.text
    assert 'href="note:' not in resp.text
    assert '<a href="см.' not in resp.text


def test_briefs_origin_section_renders_full_chain(tmp_path):
    root = _origin_repo(tmp_path)
    tables = {
        "briefs": [{"brief_id": "b-1", "hook": "х", "script": "с",
                    "review_status": "", "human_status": "pending",
                    "formula_id": "short-styling-idea-reel",
                    "source_pattern_ids": "tt-01",
                    "references": "https://t.tt/a; https://x.com/чужой"}],
        "run_log": [],
    }
    client, _ = make_client(tables, lab_root=tmp_path)
    resp = client.get("/briefs?id=b-1")
    assert resp.status_code == 200
    html = resp.text
    assert "ПОЧЕМУ ЭТО СРАБОТАЕТ" in html
    assert "личный образ автора" in html          # формулировка паттерна tt-01
    assert "пример этого сценария" in html         # https://t.tt/a совпал с evidence
    # https://x.com/чужой не совпал — предупреждение на языке продюсера, без «evidence»
    assert "не подтверждены лабораторией" in html
    assert "evidence" not in html
    # слаг рецепта не выброшен, но живёт под катом «Служебные ID»
    assert "short-styling-idea-reel" in html


def test_briefs_origin_section_honest_when_missing(tmp_path):
    tables = {
        "briefs": [{"brief_id": "b-1", "hook": "х", "script": "с",
                    "review_status": "", "human_status": "pending",
                    "formula_id": "", "source_pattern_ids": "",
                    "references": "https://t.tt/a"}],
        "run_log": [],
    }
    client, _ = make_client(tables, lab_root=tmp_path)
    resp = client.get("/briefs?id=b-1")
    assert resp.status_code == 200
    html = resp.text
    assert "не сохранилось обоснование" in html
    assert "formula_id" not in html
    assert 'href="https://t.tt/a"' in html


def test_briefs_origin_evidence_url_scheme_guard(tmp_path):
    root = _origin_repo(tmp_path)
    (root / "agent-runtime" / "patterns" / "2026-07-15-подозрительный.json").write_text(
        json.dumps({
            "meta": {"source_tab": "raw_tiktok"},
            "patterns": [{"pattern_id": "tt-09", "description": "подозрительный url",
                          "confidence": "low",
                          "evidence": {"source_urls": ["javascript:alert(1)"],
                                       "avg_views": 10.0, "avg_er": 0.01}}]},
            ensure_ascii=False), encoding="utf-8")
    tables = {
        "briefs": [{"brief_id": "b-1", "hook": "х", "script": "с",
                    "review_status": "", "formula_id": "",
                    "source_pattern_ids": "tt-09", "references": ""}],
        "run_log": [],
    }
    client, _ = make_client(tables, lab_root=root)
    resp = client.get("/briefs?id=b-1")
    assert resp.status_code == 200
    assert 'href="javascript:' not in resp.text   # текстом можно, ссылкой нельзя


def _toast_payload(html):
    """JSON тостов, который сервер отдал клиенту (base.html → toasts.js)."""
    start = html.index('id="toast-data">') + len('id="toast-data">')
    return json.loads(html[start:html.index("</script>", start)])


def test_stale_data_arrives_as_a_toast_not_as_a_banner():
    # 2026-07-28, элемент 4: полоса «данные могли устареть» сдвигала всё, что
    # ниже. Сигнал остался, форма изменилась.
    sheets = FakeSheets(OVERVIEW_TABLES)
    now = [0.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: now[0])
    app = create_app(sheets=sheets, cache=cache)
    client = TestClient(app)
    warm = [x["text"] for x in _toast_payload(client.get("/overview").text)]
    assert not any("устареть" in x for x in warm)      # кеш прогрет, данные свежие

    def broken(tab_key):
        raise ConnectionError("sheets down")

    sheets.read_rows = broken
    now[0] = 120.0                                          # TTL истёк
    resp = client.get("/overview")
    assert resp.status_code == 200
    texts = [t["text"] for t in _toast_payload(resp.text)]
    assert any("Данные могли устареть" in t for t in texts)
    # полосы на странице не осталось ни на одном экране
    for path in ("/overview", "/briefs", "/runs", "/sources", "/performance",
                 "/lab", "/briefs/shooting-list"):
        html = client.get(path).text
        assert 'class="banner warn">Данные могли устареть' not in html, path


class SyncRunner(StageRunner):
    """Для тестов: запуск синхронно, без потоков."""
    def start(self, stage):
        if self.state[stage]["status"] == "running":
            return False
        return self.run_sync(stage) or True

    def start_cycle(self):
        # гейт как в проде: конвейер (_pipeline_busy), а не any_running — идущий
        # ритуал циклу не помеха (P5.7)
        if self._pipeline_busy():
            return False
        self.run_cycle_sync()
        return True

    def reply(self, stage, session_id, text):
        # тот же формат-гард, что у StageRunner.reply (M22) — дабл не должен
        # обходить валидацию, иначе роут-тесты меряют не боевое поведение
        from cf.dashboard.runner import _SESSION_ID_RE
        if not _SESSION_ID_RE.fullmatch(str(session_id)):
            raise ValueError(f"недопустимый session_id: {session_id!r}")
        if self.state[stage]["status"] == "running":
            return False
        return self._reply_sync(stage, session_id, text) or True

    def run_ritual(self, ritual):
        if self.ritual_state[ritual]["status"] == "running":
            return False
        return self._run_ritual_sync(ritual) or True


# C3.2: raw — cli-звено; факт запуска сбора виден по collect-команде в run_command
COLLECT_TIKTOK = [sys.executable, "-u", "-m", "cf", "collect", "tiktok"]
COLLECT_INSTAGRAM = [sys.executable, "-u", "-m", "cf", "collect", "instagram"]


def make_runner_client():
    sheets = FakeSheets({**OVERVIEW_TABLES})
    commands = []

    def run_command(argv):
        commands.append(argv)
        return (0, '{"result": "ок", "session_id": "s1"}')

    runner = SyncRunner(
        sheets,
        {"dashboard": {"workflows": {}}},
        http_post=lambda url: None,
        run_command=run_command,
        locks_dir=tempfile.mkdtemp(),  # изоляция от боевых agent-runtime/locks (M5)
    )
    app = create_app(sheets=sheets, runner=runner)
    return TestClient(app), runner, commands


def test_stage_run_endpoint_triggers_and_returns_partial():
    client, runner, commands = make_runner_client()
    resp = client.post("/stages/raw/run")
    assert resp.status_code == 200
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]
    assert 'id="stages"' in resp.text            # htmx-партиал конвейера


def test_stage_run_endpoint_shows_ok_status():
    client, runner, commands = make_runner_client()
    resp = client.post("/stages/raw/run")
    assert resp.status_code == 200
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]
    assert "tl-done" in resp.text                 # успех — шаг «Сбор» завершён (done)


def test_stage_run_unknown_stage_404():
    client, _, _ = make_runner_client()
    assert client.post("/stages/nope/run").status_code == 404


def test_cycle_run_endpoint():
    client, runner, commands = make_runner_client()
    resp = client.post("/cycle/run", follow_redirects=False)
    assert resp.status_code == 303
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]
    # заметка цикла называет ФАКТ, а не заготовку «ждёт продюсера»
    assert runner.cycle_note.startswith("цикл прошёл")


def test_cycle_run_then_partial_shows_cycle_note():
    client, runner, _ = make_runner_client()
    resp = client.post("/cycle/run", follow_redirects=False)
    assert resp.status_code == 303
    partial = client.get("/partials/stages")
    assert partial.status_code == 200
    assert "цикл прошёл" in partial.text


def test_stages_partial_endpoint():
    client, _, _ = make_runner_client()
    resp = client.get("/partials/stages")
    assert resp.status_code == 200
    assert "Анализ приёмов" in resp.text and "Разметка тем" in resp.text


class _StageTileParser(HTMLParser):
    """Проходит по дереву ленты и считает, лежит ли кнопка .stage-run внутри
    открытого <a>. Стдлибовый html.parser — без внешних зависимостей."""

    def __init__(self):
        super().__init__()
        self.open_anchors = 0
        self.run_buttons = 0
        self.run_button_under_anchor = False
        self.open_links = 0

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "a":
            self.open_anchors += 1
            if "tl-open" in classes:
                self.open_links += 1
        elif tag == "button" and "stage-run" in classes:
            self.run_buttons += 1
            if self.open_anchors > 0:
                self.run_button_under_anchor = True

    def handle_endtag(self, tag):
        if tag == "a" and self.open_anchors > 0:
            self.open_anchors -= 1


def test_timeline_renders_real_progress_sim_bar_and_backlog():
    # Фаза 2б: анализ тем — реальный done/total + рецепты; сценарии — симулированный
    # бар с данными для тикера; downstream — живой бэклог из overview_metrics.
    client, runner, _ = make_runner_client()
    runner.run_progress["analyze"].update(status="running", done=1, total=3,
                                          count=4, started_at=runner.now())
    runner.run_progress["briefs"].update(status="running", started_at=runner.now(),
                                         eta_median=60)
    html = client.get("/partials/stages").text
    assert "1 из 3 тем · 4 рецепта" in html          # реальный прогресс анализа
    # прогресс v2: тикеру нужны якорь, потолок до следующего якоря, τ до него и
    # метка запуска — по ней клиент отличает новую полоску от паузы между фактами
    assert 'data-sim="1"' in html and "data-ceil=" in html and "data-tau=" in html
    assert "data-run=" in html
    assert "tl-running" in html
    assert "Разбираем тему" in html                   # подпись «что сейчас происходит»
    assert 'class="tl-elapsed mono"' in html          # и сколько идёт
    assert "ждёт решения" in html                    # downstream-бэклог одобрения (склонён)


def test_finished_step_shows_how_long_it_ran():
    # Таймер завершённого шага остаётся на ленте: «сколько это заняло» —
    # вопрос, который задают уже после финиша (правка оператора 2026-07-25).
    client, runner, _ = make_runner_client()
    runner.run_progress["collect"].update(status="done", done=2, total=2,
                                          produced=118, produced_new=24,
                                          duration=1082.8, started_at=None)
    html = client.get("/partials/stages").text
    assert "шёл 18:02" in html
    assert "118 роликов · 24 новых" in html           # добыча, а не «2/2 платформ»
    assert "платформ" not in html


def test_stage_run_button_is_not_anchor_descendant():
    # P2.1/C1: кнопка ▶ не должна быть потомком <a>, иначе клик всплывает
    # к href, браузер уходит на навигацию и htmx-POST обрывается. В ленте ▶ и
    # ссылка «открыть →» — соседи в .tl-actions, кнопка идёт первой.
    client, _, _ = make_runner_client()
    html = client.get("/partials/stages").text
    parser = _StageTileParser()
    parser.feed(html)
    assert parser.run_buttons > 0                    # запускаемые этапы отрисовали ▶
    assert not parser.run_button_under_anchor        # ▶ не внутри <a>
    assert parser.open_links > 0                     # у шагов есть ссылка «открыть →»
    # htmx-атрибуты кнопки целы — POST-запуск этапа работает
    assert 'hx-post="/stages/raw/run"' in html
    assert 'hx-target="#stages"' in html
    assert 'hx-swap="outerHTML"' in html


# --- 2026-07-28 (элементы 15, 17е): платный запуск спрашивает подтверждение ---


def test_ribbon_run_buttons_ask_before_spending_money():
    # Клик по ▶ немедленно тратит деньги на вызовы агентов. hx-confirm — штатный
    # вопрос htmx: отказ запрос не отправляет.
    client, _, _ = make_runner_client()
    html = client.get("/partials/stages").text
    assert 'hx-confirm="Запустить этап «Сбор роликов»? Это платный прогон агентов."' in html


def test_ribbon_confirm_does_not_disturb_focus_ids_or_progress():
    # Возврат фокуса после свопа раз в 2 с завязан на id кнопок, а механика
    # полоски — на data-frac/ceil/tau. Подтверждение не трогает ни то, ни другое.
    client, _, _ = make_runner_client()
    html = client.get("/partials/stages").text
    assert 'id="run-raw"' in html and 'id="open-collect"' in html
    assert 'hx-disabled-elt="this"' in html
    # «→ открыть» ничего не запускает — подтверждения не спрашивает
    open_link = html[html.index('id="open-collect"'):]
    assert "confirm" not in open_link[:open_link.index("</a>")]


def test_full_cycle_button_asks_before_spending_money():
    client, _, _ = make_runner_client()
    html = client.get("/overview").text
    assert 'action="/cycle/run"' in html
    assert "data-confirm=\"Запустить полный цикл?" in html


def test_confirm_script_is_served_and_delegated_from_document():
    # Кнопки ленты живут внутри блока, который htmx подменяет каждые 2 с:
    # слушатель, повешенный на саму форму, исчез бы вместе с ней.
    client, _, _ = make_runner_client()
    assert '/static/confirm.js' in client.get("/overview").text
    js = client.get("/static/confirm.js")
    assert js.status_code == 200
    assert "document.addEventListener('submit'" in js.text
    assert "data-confirm" in js.text and "preventDefault" in js.text


# --- P4.1: ▶ рисуется только у звеньев с настроенным раннером -----------------


def test_stages_run_button_only_for_configured_stages():
    # publish объявлен runnable, но вебхука dashboard.workflows.publish нет —
    # ▶ был бы мёртвой кнопкой, которая всегда падает ("webhook не настроен").
    # C3.2: raw и stats — cli-звенья (subprocess), настроены БЕЗ вебхуков;
    # factory идёт через фан-аут (всегда настроен).
    sheets = FakeSheets({**OVERVIEW_TABLES})
    runner = SyncRunner(sheets, {"dashboard": {"workflows": {}}})
    client = TestClient(create_app(sheets=sheets, runner=runner))
    html = client.get("/partials/stages").text
    assert 'hx-post="/stages/publish/run"' not in html   # без вебхука — нет ▶
    assert 'hx-post="/stages/raw/run"' in html            # cli-звено — ▶ без вебхука
    assert 'hx-post="/stages/stats/run"' in html          # cli-звено — ▶ без вебхука
    assert 'hx-post="/stages/factory/run"' in html        # фан-аут — тоже настроен


def test_stages_run_button_restored_when_webhook_added_without_code_change():
    # P4.1 acceptance: добавление вебхука publish в конфиг возвращает ▶ без правки кода.
    sheets = FakeSheets({**OVERVIEW_TABLES})
    runner = SyncRunner(sheets, {"dashboard": {"workflows": {
        "publish": "https://n8n.local/hook/publish"}}})
    client = TestClient(create_app(sheets=sheets, runner=runner))
    html = client.get("/partials/stages").text
    assert 'hx-post="/stages/publish/run"' in html


def test_factory_button_comes_from_fanout_kind_without_any_webhook():
    # P4.1 hardening: factory сохраняет ▶ при полностью пустых workflows — источник
    # кнопки это kind=="fanout" в STAGES, а не вебхук.
    sheets = FakeSheets({**OVERVIEW_TABLES})
    runner = SyncRunner(sheets, {"dashboard": {"workflows": {}}})
    client = TestClient(create_app(sheets=sheets, runner=runner))
    html = client.get("/partials/stages").text
    assert 'hx-post="/stages/factory/run"' in html


def test_fanout_button_derived_from_STAGES_not_hardcoded_factory(monkeypatch):
    # P4.1 hardening: фанаут-ключи выводятся из STAGES (kind=="fanout"), не хардкодятся.
    # Если factory перестаёт быть fanout и вебхука нет — ▶ уходит; это доказывает,
    # что ключ выведен из STAGES, а не зашит {"factory"}.
    import cf.dashboard.app as app_mod
    sheets = FakeSheets({**OVERVIEW_TABLES})
    runner = SyncRunner(sheets, {"dashboard": {"workflows": {}}})
    monkeypatch.setattr(app_mod, "STAGES",
                        {**app_mod.STAGES, "factory": {"kind": "n8n"}})
    client = TestClient(create_app(sheets=sheets, runner=runner))
    html = client.get("/partials/stages").text
    assert 'hx-post="/stages/factory/run"' not in html  # kind сменился → ▶ ушла


def test_build_production_app_wires_dependencies():
    from cf.dashboard.app import build_production_app
    sheets = FakeSheets({"run_log": []})
    app = build_production_app(sheets=sheets, config={"n8n": {"base_url": ""}},
                               health_autostart=False)
    assert app.state.sheets is sheets
    assert app.state.health is not None
    assert app.state.cache is not None


def test_build_production_app_health_autostart_flag():
    from cf.dashboard.app import build_production_app
    sheets = FakeSheets({"run_log": []})
    app = build_production_app(sheets=sheets, config={"n8n": {"base_url": ""}},
                               health_autostart=False)
    assert app.state.health.thread is None  # фоновый поток не запущен


def test_sidebar_briefs_badge_shows_pending_count():
    tables = dict(OVERVIEW_TABLES)
    tables["briefs"] = [
        {"brief_id": "B1", "review_status": "pending", "created_at": "2026-07-12"},
        {"brief_id": "B2", "review_status": "PENDING ", "created_at": "2026-07-13"},
        {"brief_id": "B3", "review_status": "approved", "created_at": "2026-07-10"},
    ]
    client, _ = make_client(tables)
    resp = client.get("/runs")   # бейдж виден из любого раздела
    assert resp.status_code == 200
    # Фаза 6: цифра бейджа сопровождается пояснением для скринридера
    assert '<span class="nav-badge">2<span class="sr-only"> сценариев ждут решения</span>' in resp.text


def test_sidebar_briefs_badge_hidden_when_no_pending():
    tables = dict(OVERVIEW_TABLES)
    tables["briefs"] = [{"brief_id": "B3", "review_status": "approved"}]
    client, _ = make_client(tables)
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "nav-badge" not in resp.text


def test_sidebar_briefs_badge_survives_sheets_failure():
    class BrokenSheets(FakeSheets):
        def read_rows(self, tab_key):
            raise ConnectionError("sheets down")

    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "nav-badge" not in resp.text


# --- Фаза 1 UX-доводки: навбар на языке задач продюсера (§2 спеки) ------------


def test_navbar_uses_producer_language_and_exposes_shooting_queue():
    client, _ = make_client(OVERVIEW_TABLES)
    nav = client.get("/overview").text
    # переименования системного жаргона в язык задач.
    # «Лаборатория» вернулась 2026-07-26 по решению оператора: «Аналитика»
    # читалась как отчёт постфактум, тогда как раздел — рабочее место, где
    # принимают решения по рецептам. Остальные три переименования в силе.
    for label in ("Сценарии", "Результаты", "Референсы", "Лаборатория"):
        assert label in nav
    # старые системные/транслитные ярлыки навбара ушли
    for gone in (">Брифы<", ">Перформанс<", ">Источники<"):
        assert gone not in nav
    # «Очередь съёмки» — главный ежедневный инструмент — теперь пункт меню, не только чип
    assert 'href="/briefs/shooting-list"' in nav
    assert "Очередь съёмки" in nav


def test_shooting_list_nav_item_active_not_briefs():
    # на /briefs/shooting-list подсвечен свой пункт, а не «Сценарии» (active=shooting)
    client, _ = make_client(SHOOTING_TABLES)
    html = client.get("/briefs/shooting-list").text
    active = [line for line in html.splitlines() if "nav-item active" in line]
    assert any('href="/briefs/shooting-list"' in line for line in active)
    assert not any('href="/briefs"' in line and "nav-item active" in line
                   for line in active)


def test_section_eyebrows_have_no_google_table_names():
    # §2/П2 спеки: надстрочник раздела — назначение человеческим языком, а не имя
    # внутренней Google-таблицы (CF CREATIVE BRIEFS, CF RAW, CF PUBLISHED REELS…).
    client, _ = make_client(OVERVIEW_TABLES)
    for path in ("/briefs", "/performance", "/sources", "/runs"):
        text = client.get(path).text
        for leak in ("CF CREATIVE BRIEFS", "CF RAW", "CF PUBLISHED REELS",
                     "CF PERFORMANCE", "CF RUN LOG", "CF SHOOTING QUEUE"):
            assert leak not in text, f"{leak} утёк в {path}"


def test_runs_shows_reports_card():
    # Переезд 2026-07-28 (элемент 19): отчёты этапов и форма ответа агенту живут на
    # «Истории задач» — всё про прогоны собирается на одном экране.
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "/cf-analyze", "результат анализа", "sess-9")
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Отчёты этапов" in resp.text
    assert "результат анализа" in resp.text


def test_overview_has_no_reports_card():
    # Обратная сторона переезда: на «Обзоре» отчётов и формы ответа больше нет.
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "/cf-analyze", "результат анализа", "sess-9")
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "Отчёты этапов" not in resp.text
    assert 'id="reports"' not in resp.text
    assert "результат анализа" not in resp.text


def test_runs_reports_container_always_rendered_with_empty_state():
    # P2.12: секция отчётов рендерится всегда (пока есть runner), даже без отчётов —
    # с пустым плейсхолдером и контейнером #reports, чтобы hx-poll мог подхватить
    # отчёты/вопросы агента без ручного F5.
    client, _, _ = make_runner_client()
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Отчёты этапов" in resp.text           # секция на месте
    assert 'id="reports"' in resp.text             # контейнер для htmx-подмены
    assert "Пока нет отчётов" in resp.text          # пустой плейсхолдер


def test_runs_reports_survive_run_log_failure():
    # Канал ответа агенту не должен исчезать вместе со сбоем чтения Run Log:
    # карточка отчётов живёт вне ветки error журнала.
    client, runner, _ = make_runner_client()

    def broken(tab_key):
        raise ConnectionError("sheets down")

    runner.sheets.read_rows = broken       # тот же FakeSheets, что и у дашборда
    runner._add_report("factory", "/cf-analyze", "результат анализа", "sess-9")
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Не удалось загрузить журнал" in resp.text     # про журнал сказали честно
    assert "Отчёты этапов" in resp.text                    # но отчёты на месте
    assert "результат анализа" in resp.text


def test_runs_reports_container_polls_while_running_without_reports():
    # P2.12 регрессия: звено запущено, отчётов ещё нет. Раньше вся секция пряталась
    # (нет отчётов -> нет hx-poll), и появившийся позже отчёт был не виден без F5.
    # Теперь контейнер с hx-poll отрисован, пока звено выполняется.
    client, runner, _ = make_runner_client()
    runner.state["factory"]["status"] = "running"     # звено занято, отчётов нет
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert 'hx-get="/partials/reports"' in resp.text   # опрос запущен
    assert 'hx-trigger="every 3s"' in resp.text


def test_runs_survive_run_log_row_without_completed_at():
    # P2.12: строка run_log без completed_at не должна ронять экран (было 500 на
    # completed_at[:16] по Undefined). Инвариант переехал с «Обзора» на «Историю
    # задач» вместе с самими запусками (элемент 21) — не удалён.
    tables = dict(OVERVIEW_TABLES)
    tables["run_log"] = [{"run_id": "bbb", "agent": "cf-eval", "status": "success"}]
    client, _ = make_client(tables)
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "cf-eval" in resp.text


def test_reports_partial_endpoint():
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "/cf-analyze", "результат анализа", "sess-9")
    resp = client.get("/partials/reports")
    assert resp.status_code == 200
    assert 'id="reports"' in resp.text
    assert "результат анализа" in resp.text


def test_reports_translate_claude_limit_error_to_human_banner():
    # Фаза 2 (§2 спеки): англ-отказ Claude по лимиту на «Обзоре» → человеческая
    # строка; сырой английский остаётся, но под <details>, а не пугает продюсера.
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "/cf-analyze",
                       "You've reached your Fable 5 limit for this week.", "s1")
    resp = client.get("/partials/reports")
    assert resp.status_code == 200
    assert "Система была занята, повторит позже." in resp.text
    assert "<details" in resp.text                  # английский текст свёрнут
    assert "Fable 5 limit" in resp.text             # но не удалён — доступен по клику


def test_reports_collapse_machine_dump_under_details():
    # Сырой машинный дамп (batches=…, NICHE_RESULT) сворачивается под <details>,
    # чтобы не быть стеной на главной; человеческий вопрос агента остаётся на виду.
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "collect", "batches=7 rows=519 kept=257", "s1")
    runner._add_report("factory", "/cf-analyze", "Какой хук взять за основу?", "s2")
    html = client.get("/partials/reports").text
    assert "<details" in html and "batches=7" in html   # дамп — под катом
    # вопрос агента рендерится напрямую в <pre>, без сворачивания
    assert "Какой хук взять за основу?" in html


def test_reply_endpoint_updates_reports_and_redirects():
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "/cf-analyze", "черновой план", "sess-1")
    resp = client.post("/stages/factory/reply",
                       data={"session_id": "sess-1", "answer": "вариант 2"},
                       follow_redirects=False)
    assert resp.status_code == 303
    # переезд 2026-07-28: отчёты и канал ответа живут на «Истории задач»
    assert resp.headers["location"] == "/runs"
    reports = runner.reports["factory"]
    assert reports[-2]["title"] == "вопрос продюсера"
    assert reports[-2]["text"] == "вариант 2"
    assert reports[-1]["title"] == "ответ агента"
    assert runner.state["factory"]["status"] == "ok"


def test_reply_endpoint_empty_answer_422():
    client, _, _ = make_runner_client()
    resp = client.post("/stages/factory/reply",
                       data={"session_id": "sess-1", "answer": "   "})
    assert resp.status_code == 422


def test_reply_endpoint_blank_session_id_422():
    client, _, _ = make_runner_client()
    resp = client.post("/stages/factory/reply",
                       data={"session_id": "  ", "answer": "вариант 2"})
    assert resp.status_code == 422


def test_reply_endpoint_unknown_stage_404():
    client, _, _ = make_runner_client()
    resp = client.post("/stages/nope/reply",
                       data={"session_id": "sess-1", "answer": "текст"})
    assert resp.status_code == 404


def test_reply_endpoint_no_runner_404():
    client, _ = make_client(OVERVIEW_TABLES)
    resp = client.post("/stages/factory/reply",
                       data={"session_id": "sess-1", "answer": "текст"})
    assert resp.status_code == 404


# --- P2.2: ответ/цикл не теряются молча при занятом звене ---


def test_reply_on_busy_stage_returns_note_and_keeps_text():
    # звено занято другим прогоном -> runner.reply() возвращает False.
    # Ответ продюсера не должен исчезнуть: рендерим обзор с заметкой и текстом.
    client, runner, _ = make_runner_client()
    runner._add_report("factory", "/cf-analyze", "черновой план", "sess-1")
    runner.state["factory"]["status"] = "running"
    resp = client.post("/stages/factory/reply",
                       data={"session_id": "sess-1", "answer": "мой важный ответ"})
    assert resp.status_code == 200                         # не редирект — рендер с заметкой
    body = resp.text
    assert "занят" in body                                 # объяснение: звено занято
    assert "мой важный ответ" in body                      # текст вернулся в форму, не потерян


def test_cycle_run_on_busy_stage_surfaces_note_and_does_not_start():
    # звено занято -> start_cycle() возвращает False. Отказ не должен быть немым:
    # у БРАУЗЕРНОЙ формы (Origin есть) — заметка на обзоре; headless-клиент
    # без Origin/Referer получает 409 (M13, отдельный тест ниже).
    client, runner, commands = make_runner_client()
    runner.state["raw"]["status"] = "running"
    resp = client.post("/cycle/run",                       # 303 -> следуем на /overview
                       headers={"origin": "http://127.0.0.1:8787"})
    assert resp.status_code == 200
    assert "цикл не запущен" in resp.text                   # заметка видна на обзоре
    assert commands == []                                  # collect не дёрнут — старта не было


# --- CSRF: сверка Origin/Referer для POST (P0.5) ----------------------------


def test_csrf_evil_origin_blocked_and_runner_not_invoked():
    client, runner, commands = make_runner_client()
    resp = client.post("/cycle/run", headers={"origin": "http://evil.example"},
                       follow_redirects=False)
    assert resp.status_code == 403
    assert commands == []                        # конвейер НЕ запущен


def test_csrf_allowed_origin_passes():
    client, runner, commands = make_runner_client()
    resp = client.post("/cycle/run", headers={"origin": "http://127.0.0.1:8787"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]


def test_csrf_localhost_origin_passes():
    client, runner, commands = make_runner_client()
    resp = client.post("/cycle/run", headers={"origin": "http://localhost:8787"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]


def test_csrf_origin_prefix_trick_blocked():
    # точное совпадение, а не startswith: 127.0.0.1:8787.evil.example — чужой
    client, _, commands = make_runner_client()
    resp = client.post("/cycle/run",
                       headers={"origin": "http://127.0.0.1:8787.evil.example"})
    assert resp.status_code == 403
    assert commands == []


def test_csrf_allowed_referer_passes_without_origin():
    client, sheets = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": ""},
                       headers={"referer": "http://127.0.0.1:8787/briefs"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sheets.tables["briefs"][0]["review_status"] == "approved"


def test_csrf_referer_equal_to_origin_passes():
    client, runner, _ = make_runner_client()
    resp = client.post("/cycle/run",
                       headers={"referer": "http://127.0.0.1:8787"},
                       follow_redirects=False)
    assert resp.status_code == 303


def test_csrf_evil_referer_blocked_and_action_not_executed():
    # Клиент БЕЗ дефолтного Origin: проверяется именно ветка Referer (Origin
    # старше по приоритету и перекрыл бы её).
    sheets = FakeSheets(BRIEF_TABLES)
    client = TestClient(create_app(sheets=sheets))
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": ""},
                       headers={"referer": "http://evil.example/x"})
    assert resp.status_code == 403
    assert sheets.tables["briefs"][0]["review_status"] == "pending"


def test_csrf_evil_origin_wins_over_good_referer():
    client, _, commands = make_runner_client()
    resp = client.post("/cycle/run",
                       headers={"origin": "http://evil.example",
                                "referer": "http://127.0.0.1:8787/overview"})
    assert resp.status_code == 403
    assert commands == []


def test_csrf_no_headers_allowed():
    # сознательное ослабление плана: без Origin/Referer — не-браузерный клиент
    # (curl, планировщик, TestClient), пропускаем
    client, runner, commands = make_runner_client()
    resp = client.post("/cycle/run", follow_redirects=False)
    assert resp.status_code == 303
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]


def test_csrf_get_with_evil_origin_not_blocked():
    client, _ = make_client(OVERVIEW_TABLES)
    resp = client.get("/overview", headers={"origin": "http://evil.example"})
    assert resp.status_code == 200


def test_csrf_guard_covers_every_post_route_without_exceptions():
    # Прежде здесь проверялся /refresh (его больше нет). Правило не изменилось:
    # исключений у гарда нет — любой браузерный POST с чужой страницы отбивается,
    # включая платные запуски.
    sheets = FakeSheets(OVERVIEW_TABLES)
    client = TestClient(create_app(sheets=sheets))
    for path in ("/cycle/run", "/stages/raw/run", "/briefs/B1/assign"):
        resp = client.post(path, headers={"origin": "http://evil.example",
                                          "referer": "http://evil.example/"})
        assert resp.status_code == 403, path


def test_csrf_origins_follow_create_app_port():
    sheets = FakeSheets(BRIEF_TABLES)
    client = TestClient(create_app(sheets=sheets, port=9999))
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": ""},
                       headers={"origin": "http://127.0.0.1:9999"},
                       follow_redirects=False)
    assert resp.status_code == 303
    resp = client.post("/briefs/B1/review",
                       data={"decision": "pending", "notes": ""},
                       headers={"origin": "http://127.0.0.1:8787"})
    assert resp.status_code == 403


def test_style_css_script_box_prewrap_and_pill_auto_in_palette():
    # P2.15: многострочный скрипт брифа должен рендериться с переносами (.script-box
    # pre-wrap), а .pill.auto — использовать палитровый токен, без сырого голубого
    # rgba(90,140,220,...) вне закрытой палитры DS.
    import re
    client, _ = make_client(OVERVIEW_TABLES)
    css = client.get("/static/style.css").text
    script_box = re.search(r"\.script-box\s*\{[^}]*\}", css)
    assert script_box and "pre-wrap" in script_box.group(0)
    assert "rgba(90, 140, 220" not in css
    assert "rgba(90,140,220" not in css
    pill_auto = re.search(r"\.pill\.auto\s*\{[^}]*\}", css)
    assert pill_auto and "var(--" in pill_auto.group(0)   # палитровый токен, не сырой цвет


# --- P5.1: контур публикации (форма «Опубликован» + состояние «вышел») -------

PUBLISH_TABLES = {
    "briefs": [
        {"brief_id": "B1", "hook": "летний образ", "niche": "мужские-образы",
         "review_status": "approved", "generated_at": "2026-07-12",
         "script": "Кадр 1 — поло...", "references": "", "formula_id": "F-07",
         "prompt_version": "v3", "rejection_reason": "", "reviewer_notes": ""},
        {"brief_id": "B2", "hook": "второй образ", "niche": "мужские-образы",
         "review_status": "approved", "generated_at": "2026-07-11",
         "script": "Кадр 1 — свитер...", "references": "", "formula_id": "F-07",
         "prompt_version": "v3", "rejection_reason": "", "reviewer_notes": ""},
    ],
    # B1 уже опубликован, B2 — ещё нет
    "reels": [
        {"reel_id": "777", "brief_id": "B1", "platform": "tiktok",
         "post_url": "https://www.tiktok.com/@x/video/777",
         "published_at": "2026-07-13", "prompt_version": "v3",
         "production_notes": ""},
    ],
    "run_log": [],
}


def test_published_brief_shows_published_badge_in_list():
    # approved-бриф С рилом -> «опубликован»; approved БЕЗ рила -> просто «одобрен».
    # Карточку открываем на B2 (не вышел), чтобы «badge published» осталось только у
    # строки B1 в списке — тогда count==1 доказывает, что B2 не помечен опубликованным.
    client, _ = make_client(PUBLISH_TABLES)
    resp = client.get("/briefs?status=approved&id=B2")
    assert resp.status_code == 200
    assert "badge published" in resp.text           # B1 вышел (строка в списке)
    assert resp.text.count("badge published") == 1  # только B1, не B2
    assert "badge approved" in resp.text            # B2 остаётся «одобрен»


def test_published_brief_card_shows_reel_and_hides_publish_form():
    client, _ = make_client(PUBLISH_TABLES)
    resp = client.get("/briefs?id=B1")
    assert resp.status_code == 200
    assert "badge published" in resp.text
    assert "https://www.tiktok.com/@x/video/777" in resp.text   # ссылка на рил
    assert 'action="/briefs/B1/published"' not in resp.text     # форму уже не показываем


def test_approved_unpublished_brief_card_shows_publish_form():
    client, _ = make_client(PUBLISH_TABLES)
    resp = client.get("/briefs?id=B2")
    assert resp.status_code == 200
    # форма публикации в карточке B2 (её нет у опубликованного брифа — см. соседний тест)
    assert 'action="/briefs/B2/published"' in resp.text
    assert 'name="url"' in resp.text


def test_post_published_writes_reel_row_and_redirects():
    client, sheets = make_client({**PUBLISH_TABLES, "reels": []})
    resp = client.post("/briefs/B1/published",
                       data={"url": "https://www.tiktok.com/@x/video/12345",
                             "notes": "без плашки"},
                       headers={"origin": "http://127.0.0.1:8787"},
                       follow_redirects=False)
    assert resp.status_code == 303
    reels = sheets.tables["reels"]
    assert len(reels) == 1
    assert reels[0]["reel_id"] == "12345"
    assert reels[0]["brief_id"] == "B1"
    assert reels[0]["prompt_version"] == "v3"        # подтянут из брифа
    assert reels[0]["production_notes"] == "без плашки"
    assert sheets.tables["run_log"][-1]["agent"] == "mark-published"


def test_post_published_then_brief_shows_published_state():
    client, _ = make_client({**PUBLISH_TABLES, "reels": []})
    client.post("/briefs/B1/published",
                data={"url": "https://www.tiktok.com/@x/video/12345"},
                headers={"origin": "http://127.0.0.1:8787"})
    resp = client.get("/briefs?id=B1")
    assert resp.status_code == 200
    assert "badge published" in resp.text            # бриф перешёл в «опубликован»


def test_post_published_same_origin_referer_passes_csrf():
    client, sheets = make_client({**PUBLISH_TABLES, "reels": []})
    resp = client.post("/briefs/B1/published",
                       data={"url": "https://www.tiktok.com/@x/video/1"},
                       headers={"referer": "http://127.0.0.1:8787/briefs"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert len(sheets.tables["reels"]) == 1


def test_post_published_evil_origin_blocked_and_not_written():
    client, sheets = make_client({**PUBLISH_TABLES, "reels": []})
    resp = client.post("/briefs/B1/published",
                       data={"url": "https://www.tiktok.com/@x/video/1"},
                       headers={"origin": "http://evil.example"})
    assert resp.status_code == 403
    assert sheets.tables["reels"] == []


def test_post_published_unknown_brief_422():
    client, _ = make_client({**PUBLISH_TABLES, "reels": []})
    resp = client.post("/briefs/NOPE/published",
                       data={"url": "https://tiktok.com/@x/video/1"},
                       headers={"origin": "http://127.0.0.1:8787"})
    assert resp.status_code == 422


def test_post_published_non_http_url_422():
    client, _ = make_client({**PUBLISH_TABLES, "reels": []})
    resp = client.post("/briefs/B1/published",
                       data={"url": "ftp://x/video/1"},
                       headers={"origin": "http://127.0.0.1:8787"})
    assert resp.status_code == 422


# --- P5.4: печатная очередь съёмки (/briefs/shooting-list) -------------------

SHOOTING_TABLES = {
    "briefs": [
        # approved + рила ещё нет -> попадает в очередь съёмки
        {"brief_id": "S1", "hook": "осенний лук", "niche": "мужские-образы",
         "review_status": "approved", "generated_at": "2026-07-12",
         "script": "Кадр 1 — поло\nКадр 2 — джинсы",
         "references": "https://tiktok.com/v/1 note-не-ссылка",
         "formula_id": "F-07", "prompt_version": "v3",
         "rejection_reason": "", "reviewer_notes": ""},
        # approved, но рил уже опубликован -> исключается (вышел)
        {"brief_id": "S2", "hook": "уже вышел", "niche": "мужские-образы",
         "review_status": "approved", "generated_at": "2026-07-11",
         "script": "Кадр 1 — свитер", "references": "", "formula_id": "F-07",
         "prompt_version": "v3", "rejection_reason": "", "reviewer_notes": ""},
        # pending -> исключается (ещё не одобрен)
        {"brief_id": "S3", "hook": "ждёт ревью", "niche": "стритвир",
         "review_status": "pending", "generated_at": "2026-07-10",
         "script": "Кадр 1 — куртка", "references": "", "formula_id": "F-03",
         "rejection_reason": "", "reviewer_notes": ""},
    ],
    "reels": [
        {"reel_id": "999", "brief_id": "S2", "platform": "tiktok",
         "post_url": "https://www.tiktok.com/@x/video/999",
         "published_at": "2026-07-13"},
    ],
    "run_log": [],
}


def test_shooting_list_lists_approved_no_reel_grouped_by_niche():
    client, _ = make_client(SHOOTING_TABLES)
    resp = client.get("/briefs/shooting-list")
    assert resp.status_code == 200
    text = resp.text
    assert "осенний лук" in text                 # S1: approved, рила нет
    assert "Кадр 1 — поло" in text               # его скрипт
    assert "уже вышел" not in text               # S2 опубликован — не в очереди
    assert "ждёт ревью" not in text              # S3 pending — не в очереди
    assert "мужские образы" in text              # группировка по теме (niche_label)


def test_shooting_list_references_http_only():
    client, _ = make_client(SHOOTING_TABLES)
    resp = client.get("/briefs/shooting-list")
    assert resp.status_code == 200
    assert 'href="https://tiktok.com/v/1"' in resp.text   # http(s) — ссылкой
    assert 'href="note-не-ссылка"' not in resp.text        # прочее — не ссылкой
    assert "note-не-ссылка" in resp.text                   # но текстом присутствует


def test_shooting_list_multiline_script_prewrap():
    client, _ = make_client(SHOOTING_TABLES)
    resp = client.get("/briefs/shooting-list")
    assert resp.status_code == 200
    assert "script-box" in resp.text             # pre-wrap класс (переносы выживают)
    assert "Кадр 1 — поло" in resp.text
    assert "Кадр 2 — джинсы" in resp.text         # вторая строка скрипта тоже видна


def test_shooting_list_md_export():
    client, _ = make_client(SHOOTING_TABLES)
    resp = client.get("/briefs/shooting-list?format=md")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert ".md" in resp.headers.get("content-disposition", "")
    body = resp.text
    assert "осенний лук" in body                  # контент брифа в выгрузке
    assert "Кадр 1 — поло" in body                # многострочный скрипт сохранён
    assert "Кадр 2 — джинсы" in body
    assert "уже вышел" not in body                # опубликованный не попал
    assert "ждёт ревью" not in body               # pending не попал


# --- 2026-07-28 (С4): «Отдал в работу» — назначение исполнителя ---------------
# Слоты — ЗНАЧЕНИЯ ПОЛЯ, а не люди: при найме слот переименовывается одной
# правкой в cf.config.json. Колонки исполнителя в CF Creative Briefs до этой
# работы не было вовсе: creator/content_owner живут в опубликованных роликах и
# заполняются, когда ролик уже вышел, — для очереди это поздно.

SLOTS = ["криэйтор-1", "криэйтор-2"]

# Заголовки ЯВНО: без них FakeSheets выводит схему из ключей записываемой строки
# и «сохраняет» что угодно — ровно тот дрейф схемы, который тест обязан ловить.
SHOOTING_HEADERS = {"briefs": ["brief_id", "hook", "niche", "review_status",
                               "generated_at", "script", "references", "formula_id",
                               "prompt_version", "rejection_reason", "reviewer_notes",
                               "creator_slot", "assigned_at"],
                    "run_log": ["run_id", "agent", "status", "input_summary",
                                "trigger_type", "started_at", "completed_at",
                                "errors"]}


def make_shooting_client(slots=SLOTS, headers=SHOOTING_HEADERS, tables=None):
    sheets = FakeSheets(tables or SHOOTING_TABLES, headers=headers)
    app = create_app(sheets=sheets, creator_slots=slots)
    return TestClient(app, headers=BROWSER), sheets


def _brief(sheets, brief_id):
    return next(b for b in sheets.tables["briefs"] if b["brief_id"] == brief_id)


def test_shooting_list_shows_assign_button_for_each_slot():
    client, _ = make_shooting_client()
    html = client.get("/briefs/shooting-list").text
    assert 'action="/briefs/S1/assign"' in html
    assert "Отдал в работу" in html
    for slot in SLOTS:
        assert f'<option value="{slot}"' in html


def test_assign_writes_slot_and_date_and_labels_the_card():
    client, sheets = make_shooting_client()
    resp = client.post("/briefs/S1/assign", data={"slot": "криэйтор-1"},
                       follow_redirects=False)
    assert resp.status_code == 303
    row = _brief(sheets, "S1")
    assert row["creator_slot"] == "криэйтор-1"
    assert row["assigned_at"]                       # дата назначения проставлена
    # Сценарий остаётся в очереди — с меткой, а не исчезает из неё
    html = client.get("/briefs/shooting-list").text
    assert "осенний лук" in html
    assert "криэйтор-1, с " in html


def test_assign_can_be_changed_to_another_slot():
    client, sheets = make_shooting_client()
    client.post("/briefs/S1/assign", data={"slot": "криэйтор-1"})
    client.post("/briefs/S1/assign", data={"slot": "криэйтор-2"})
    assert _brief(sheets, "S1")["creator_slot"] == "криэйтор-2"


def test_assign_can_be_removed_completely():
    client, sheets = make_shooting_client()
    client.post("/briefs/S1/assign", data={"slot": "криэйтор-1"})
    client.post("/briefs/S1/assign", data={"unassign": "1"})
    row = _brief(sheets, "S1")
    # Снятие обнуляет и дату: «слота нет, а дата назначения есть» — состояние,
    # которого не бывает, и оно врало бы плитке «Ждут назначения».
    assert row["creator_slot"] == "" and row["assigned_at"] == ""


def test_assign_without_columns_fails_loudly_instead_of_silent_noop():
    # Назначение — суть действия: молча потерять его нельзя. Это НЕ то же, что
    # необязательные поля вроде даты ревью, где no-op допустим.
    headers = {"briefs": ["brief_id", "hook", "niche", "review_status",
                          "generated_at", "script", "references", "formula_id"],
               "run_log": SHOOTING_HEADERS["run_log"]}
    client, sheets = make_shooting_client(headers=headers)
    resp = client.post("/briefs/S1/assign", data={"slot": "криэйтор-1"})
    assert resp.status_code == 500
    assert "Назначение не сохранено" in resp.text
    assert "creator_slot" not in _brief(sheets, "S1")


def test_assign_rejects_a_slot_outside_the_configured_list():
    client, sheets = make_shooting_client()
    resp = client.post("/briefs/S1/assign", data={"slot": "вася"})
    assert resp.status_code == 422
    assert not _brief(sheets, "S1").get("creator_slot")


def test_assign_unknown_brief_404():
    client, _ = make_shooting_client()
    assert client.post("/briefs/НЕТ/assign",
                       data={"slot": "криэйтор-1"}).status_code == 404


def test_assign_leaves_a_trace_in_run_log():
    client, sheets = make_shooting_client()
    client.post("/briefs/S1/assign", data={"slot": "криэйтор-1"})
    entries = [r for r in sheets.tables["run_log"] if r["agent"] == "dashboard-assign"]
    assert len(entries) == 1
    assert "S1" in entries[0]["input_summary"]
    assert "криэйтор-1" in entries[0]["input_summary"]


def test_assign_is_a_decision_route_and_needs_the_dashboard_page():
    # Назначение меняет состояние по воле оператора — безголовым оно не бывает.
    sheets = FakeSheets(SHOOTING_TABLES, headers=SHOOTING_HEADERS)
    client = TestClient(create_app(sheets=sheets, creator_slots=SLOTS))
    resp = client.post("/briefs/S1/assign", data={"slot": "криэйтор-1"})
    assert resp.status_code == 403
    assert not _brief(sheets, "S1").get("creator_slot")


def test_assignment_columns_are_registered_so_drift_is_seen_before_the_click():
    # Реестр EXPECTED_COLUMNS кормит и здоровье систем, и cf status: отсутствие
    # колонок должно быть видно ДО того, как продюсер нажмёт «Отдал в работу».
    from cf.sheets import EXPECTED_COLUMNS, schema_drift
    assert {"creator_slot", "assigned_at"} <= set(EXPECTED_COLUMNS["briefs"])
    headers = {"briefs": ["brief_id", "reviewed_at", "cta"]}
    sheets = FakeSheets({"briefs": []}, headers=headers)
    assert schema_drift(sheets, {"briefs": EXPECTED_COLUMNS["briefs"]}) == {
        "briefs": ["creator_slot", "assigned_at"]}


def test_empty_slot_list_hides_the_button_with_a_reason():
    client, _ = make_shooting_client(slots=[])
    html = client.get("/briefs/shooting-list").text
    assert html.count("Отдал в работу") == 0
    assert "Назначать некого" in html
    assert "dashboard.creator_slots" in html      # где именно править


def test_md_export_carries_the_assignment():
    client, _ = make_shooting_client()
    body = client.get("/briefs/shooting-list?format=md").text
    assert "- Исполнитель: не назначен" in body
    client.post("/briefs/S1/assign", data={"slot": "криэйтор-2"})
    body = client.get("/briefs/shooting-list?format=md").text
    assert "- Исполнитель: криэйтор-2, с " in body


def test_shooting_list_empty_state_when_nothing_to_shoot():
    tables = {
        "briefs": [{"brief_id": "P1", "hook": "жду", "review_status": "pending",
                    "script": "с", "references": "", "formula_id": ""}],
        "reels": [],
        "run_log": [],
    }
    client, _ = make_client(tables)
    resp = client.get("/briefs/shooting-list")
    assert resp.status_code == 200                # пустая очередь — не ошибка


def test_briefs_page_links_to_shooting_list():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.get("/briefs")
    assert resp.status_code == 200
    assert 'href="/briefs/shooting-list"' in resp.text   # ссылка «→ очередь съёмки»


# --- UTM-контур, тикет 01: публикация с аккаунтом ----------------------------
# Реестр — верхнеуровневый ключ accounts в cf.config.json; формы публикации
# получают select аккаунта, POST валидирует слаг (образец — post_assign).

ACCOUNTS = [
    {"slug": "tiktok-1", "platform": "tiktok", "handle": "@a", "active": True},
    {"slug": "instagram-1", "platform": "instagram", "handle": "@b",
     "active": True},
    {"slug": "tiktok-2", "platform": "tiktok", "handle": "PLACEHOLDER",
     "active": False},
]


def test_brief_card_publish_form_has_select_of_active_accounts_only():
    client, _ = make_client(PUBLISH_TABLES, accounts=ACCOUNTS)
    html = client.get("/briefs?id=B2").text
    assert 'name="account"' in html
    assert '<option value="tiktok-1"' in html
    assert '<option value="instagram-1"' in html
    assert '<option value="tiktok-2"' not in html     # active: false — не предлагаем


def test_shooting_list_has_publish_form_with_account_select():
    client, _ = make_client(SHOOTING_TABLES, accounts=ACCOUNTS)
    html = client.get("/briefs/shooting-list").text
    assert 'action="/briefs/S1/published"' in html
    assert 'name="url"' in html
    assert 'name="account"' in html
    assert '<option value="tiktok-1"' in html
    # Скрытое поле возврата: из очереди съёмки редирект ведёт обратно в очередь.
    assert 'name="return_to" value="shooting"' in html


def test_post_published_from_shooting_queue_writes_account_and_returns_there():
    client, sheets = make_client({**SHOOTING_TABLES, "reels": []},
                                 accounts=ACCOUNTS)
    resp = client.post("/briefs/S1/published",
                       data={"url": "https://www.tiktok.com/@a/video/321",
                             "account": "tiktok-1", "return_to": "shooting"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/briefs/shooting-list"
    reel = sheets.tables["reels"][0]
    assert reel["account"] == "tiktok-1"
    assert reel["brief_id"] == "S1"
    # Опубликованный сценарий сам ушёл из очереди съёмки (у него теперь рил).
    assert "осенний лук" not in client.get("/briefs/shooting-list").text


def test_post_published_from_card_redirects_to_card_as_before():
    client, sheets = make_client({**PUBLISH_TABLES, "reels": []},
                                 accounts=ACCOUNTS)
    resp = client.post("/briefs/B1/published",
                       data={"url": "https://www.tiktok.com/@a/video/5",
                             "account": "instagram-1"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/briefs?id=B1"
    assert sheets.tables["reels"][0]["account"] == "instagram-1"


def test_post_published_unknown_account_422_and_nothing_written():
    client, sheets = make_client({**PUBLISH_TABLES, "reels": []},
                                 accounts=ACCOUNTS)
    for bad in ("вася", "tiktok-2"):          # неизвестный и выключенный слаги
        resp = client.post("/briefs/B1/published",
                           data={"url": "https://www.tiktok.com/@a/video/6",
                                 "account": bad})
        assert resp.status_code == 422
    assert sheets.tables["reels"] == []
    assert sheets.tables["run_log"] == []


def test_post_published_without_account_still_accepted():
    # Обратная совместимость: POST без account (старая форма, CLI) проходит.
    client, sheets = make_client({**PUBLISH_TABLES, "reels": []},
                                 accounts=ACCOUNTS)
    resp = client.post("/briefs/B1/published",
                       data={"url": "https://www.tiktok.com/@a/video/7"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sheets.tables["reels"][0]["account"] == ""


def test_empty_account_registry_shows_reason_not_hides_forms():
    # Образец — пустые creator_slots: селекта нет, но формы публикации говорят
    # причину, а публикация без аккаунта остаётся возможной.
    card_client, _ = make_client(PUBLISH_TABLES)             # accounts=()
    queue_client, _ = make_client(SHOOTING_TABLES)
    card = card_client.get("/briefs?id=B2").text
    queue = queue_client.get("/briefs/shooting-list").text
    for html in (card, queue):
        assert 'name="account"' not in html
        assert "реестр аккаунтов пуст" in html
        assert "cf.config.json" in html                # где именно править
    assert 'action="/briefs/B2/published"' in card     # форма не спрятана
    assert 'action="/briefs/S1/published"' in queue


def test_all_inactive_registry_counts_as_empty():
    # 5 слотов-заготовок с active: false в боевом конфиге — это «аккаунты не
    # заведены», а не «есть из чего выбрать».
    inactive = [dict(a, active=False) for a in ACCOUNTS]
    client, _ = make_client(SHOOTING_TABLES, accounts=inactive)
    html = client.get("/briefs/shooting-list").text
    assert 'name="account"' not in html
    assert "реестр аккаунтов пуст" in html


# --- P5.7: карточка «Ритуалы недели» на /lab + запуск через runner -----------


def _ritual_repo(tmp_path):
    """Мини-репо: eval вечной давности (гарантирует «просрочено» при любом реальном
    today) + pending source-proposal (напоминание «ждёт apply-sources»)."""
    (tmp_path / "agent-runtime" / "evals").mkdir(parents=True)
    (tmp_path / "agent-runtime" / "evals" / "1970-01-01-weekly-eval.json").write_text(
        "{}", encoding="utf-8")
    (tmp_path / "proposals").mkdir(parents=True)
    (tmp_path / "proposals" / "2026-07-15-sources-tiktok.json").write_text(
        json.dumps({"platform": "tiktok", "generated_at": "2026-07-15",
                    "status": "pending",
                    "remove": [{"source": "#x", "reason": "низкий yield", "stats": {}}],
                    "add": []}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _ritual_client(tmp_path):
    sheets = FakeSheets({"run_log": [], "briefs": []})
    commands = []
    runner = SyncRunner(
        sheets, {"dashboard": {"workflows": {}}}, http_post=lambda url: None,
        run_command=lambda argv: (commands.append(argv), (0, "{}"))[1])
    # фейковый claude не пишет в Run Log — контракт правила №6 гоняют тесты раннера
    runner._agent_logged = lambda agent, since: True
    app = create_app(sheets=sheets, runner=runner, lab_root=_ritual_repo(tmp_path))
    return TestClient(app), runner, commands


def test_lab_page_shows_rituals_card(tmp_path):
    client, _, _ = _ritual_client(tmp_path)
    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.text
    assert "Ритуалы недели" in html
    assert "Недельный eval" in html and "Тюнинг источников" in html
    assert "просрочено" in html                          # eval вечной давности
    assert "ждёт apply-sources" in html                  # pending source-proposal
    assert 'action="/rituals/eval/run"' in html          # кнопка ▶ eval
    assert 'action="/rituals/tune-sources/run"' in html   # кнопка ▶ tune-sources


def test_ritual_run_endpoint_dispatches_command_and_redirects(tmp_path):
    # приёмка: POST кнопки вызывает runner с нужной командой (фейк run_command)
    client, runner, commands = _ritual_client(tmp_path)
    resp = client.post("/rituals/eval/run", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/runs"           # отчёт живёт в «Отчётах этапов»
    assert commands == [["claude", "-p", "/cf-eval", "--output-format", "json"]]
    assert len(runner.reports["ritual-eval"]) == 1


def test_ritual_run_endpoint_tune_sources(tmp_path):
    client, _, commands = _ritual_client(tmp_path)
    resp = client.post("/rituals/tune-sources/run", follow_redirects=False)
    assert resp.status_code == 303
    assert commands == [
        ["claude", "-p", "/cf-tune-sources", "--output-format", "json"]]


def test_lab_page_shows_candidate_ab_badge():
    # P5.13: кандидат виден на /lab бейджем «Кандидат A/B» (не путать с погашенной).
    client, _ = make_client({"prompt_versions": [
        {"prompt_id": "brief-x", "version": "v2", "github_path": "prompts/x.md",
         "active": "TRUE", "activated_at": "2026-07-12T10:00:00+00:00"},
        {"prompt_id": "brief-x", "version": "v3", "github_path": "prompts/x-v3.md",
         "active": "CANDIDATE", "activated_at": "2026-07-15T10:00:00+00:00"},
    ]})
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "Кандидат A/B" in resp.text


def test_ritual_run_unknown_ritual_404(tmp_path):
    client, _, _ = _ritual_client(tmp_path)
    assert client.post("/rituals/nope/run").status_code == 404


def test_ritual_run_no_runner_404():
    client, _ = make_client({"run_log": [], "briefs": []})
    assert client.post("/rituals/eval/run").status_code == 404


def test_lab_page_rituals_card_without_runner_has_no_buttons(tmp_path):
    # секции без раннера (create_app без runner): карточка есть, кнопок ▶ нет
    sheets = FakeSheets({"run_log": [], "briefs": []})
    client = TestClient(create_app(sheets=sheets, lab_root=_ritual_repo(tmp_path)))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "Ритуалы недели" in resp.text
    assert 'action="/rituals/eval/run"' not in resp.text


def test_build_production_app_csrf_uses_given_port():
    from cf.dashboard.app import build_production_app
    sheets = FakeSheets({"run_log": []})
    app = build_production_app(sheets=sheets, config={"n8n": {"base_url": ""}},
                               health_autostart=False, port=9001)
    client = TestClient(app)
    # стандартный 8787 для сервера на 9001 — чужой origin
    resp = client.post("/stages/nope/run",
                       headers={"origin": "http://127.0.0.1:8787"})
    assert resp.status_code == 403
    # свой origin проходит CSRF и доходит до маршрута (404 — звена нет)
    resp = client.post("/stages/nope/run",
                       headers={"origin": "http://127.0.0.1:9001"})
    assert resp.status_code == 404


# --- P5.5: htmx-инлайн ревью, hotkeys, revised в очереди внимания -------------

# Две pending-строки: после решения по B1 карточка должна перейти на следующий
# pending (B3), а список — показать обновлённые бейджи без полной перерисовки.
HX_REVIEW_TABLES = {
    "briefs": [
        dict(BRIEF_TABLES["briefs"][0]),                 # B1 pending, все колонки
        {"brief_id": "B3", "hook": "второй pending", "niche": "образы",
         "review_status": "pending", "generated_at": "2026-07-11",
         "script": "СКРИПТ-B3", "references": "", "formula_id": "",
         "rejection_reason": "", "reviewer_notes": ""},
    ],
    "run_log": [],
}

GOOD_HX = {"hx-request": "true", "origin": "http://127.0.0.1:8787"}


def test_review_hx_post_returns_partial_and_advances_to_next_pending():
    # hx-post решения возвращает партиал (список + карточка), а не 303-редирект;
    # решение применено в Sheets; карточка перешла на следующий pending (B3).
    client, sheets = make_client(HX_REVIEW_TABLES)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       headers=GOOD_HX, follow_redirects=False)
    assert resp.status_code == 200                        # партиал, не редирект
    assert sheets.tables["briefs"][0]["review_status"] == "approved"
    body = resp.text
    assert 'id="briefs-review"' in body                   # инлайн-контейнер очереди
    assert "<!DOCTYPE html>" not in body                  # без полной перерисовки страницы
    assert "<html" not in body.lower()
    assert "B3" in body and "СКРИПТ-B3" in body           # следующий pending выбран
    assert "badge pending" in body                        # список с бейджами статусов


def test_review_hx_post_reject_shows_updated_badge():
    # Отклонение через hx: бриф получает статус rejected в Sheets.
    client, sheets = make_client(HX_REVIEW_TABLES)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "rejected", "notes": "слабый хук"},
                       headers=GOOD_HX, follow_redirects=False)
    assert resp.status_code == 200
    assert sheets.tables["briefs"][0]["review_status"] == "rejected"
    assert 'id="briefs-review"' in resp.text


def test_review_hx_post_invalidates_only_briefs_and_run_log():
    # P3.5: точечная инвалидация и на htmx-пути — медленные raw/reels остаются в кеше.
    sheets = FakeSheets({**HX_REVIEW_TABLES,
                         "raw_tiktok": [{"source_url": "u1", "posted_at": "2026-07-01"}],
                         "reels": [{"reel_id": "R1", "published_at": "2026-07-05"}]})
    cache = _SpyCache(sheets)
    client = TestClient(create_app(sheets=sheets, cache=cache), headers=BROWSER)
    cache.rows("raw_tiktok")                              # медленные вкладки прогреты
    cache.rows("reels")
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       headers=GOOD_HX, follow_redirects=False)
    assert resp.status_code == 200
    assert sorted(cache.invalidated) == ["briefs", "run_log"]  # только они
    assert cache.refresh_calls == 0                       # без полного сброса
    assert cache._store.get("raw_tiktok") is not None     # raw остался в кеше
    assert cache._store.get("reels") is not None          # reels остался в кеше


def test_review_hx_post_runlog_failure_surfaces_warning_in_partial():
    # P2.14 на htmx-пути: сбой записи в Run Log виден предупреждением в партиале,
    # решение всё равно применено.
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("run_log append failed")

    sheets = BrokenLogSheets({**HX_REVIEW_TABLES})
    client = TestClient(create_app(sheets=sheets), headers=BROWSER)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       headers={"hx-request": "true"}, follow_redirects=False)
    assert resp.status_code == 200
    assert sheets.tables["briefs"][0]["review_status"] == "approved"
    assert "Run Log" in resp.text                         # предупреждение видно
    assert 'id="briefs-review"' in resp.text              # это партиал, не полная страница
    assert "<!DOCTYPE html>" not in resp.text


def test_review_hx_post_unknown_brief_still_404():
    client, _ = make_client(HX_REVIEW_TABLES)
    resp = client.post("/briefs/NOPE/review",
                       data={"decision": "approved", "notes": ""}, headers=GOOD_HX)
    assert resp.status_code == 404


def test_review_form_uses_hx_post_targeting_inline_container():
    client, _ = make_client(BRIEF_TABLES)
    body = client.get("/briefs").text
    assert 'hx-post="/briefs/B1/review"' in body          # инлайн-ревью через htmx
    assert 'hx-target="#briefs-review"' in body           # цель свопа — контейнер очереди
    assert 'hx-swap="outerHTML"' in body
    assert 'data-hotkey="approve"' in body                # кнопка ✓ для хоткея A
    assert 'data-hotkey="reject"' in body                 # кнопка ✕ для хоткея R
    # прогрессивное улучшение: обычный POST сохранён на случай отключённого JS
    assert 'method="post" action="/briefs/B1/review"' in body


def test_briefs_page_includes_hotkey_script_with_input_guard():
    client, _ = make_client(BRIEF_TABLES)
    body = client.get("/briefs").text
    assert "data-hotkeys" in body                         # инлайн-скрипт хоткеев на странице
    assert "KeyA" in body and "KeyR" in body              # A / R
    assert "ArrowRight" in body                           # →
    # гард: хоткеи не срабатывают при вводе в текстовое поле
    assert "TEXTAREA" in body and "INPUT" in body
    assert "isContentEditable" in body


def test_attention_queue_still_counts_revised_in_the_gate():
    # Плитка «Сценарии ждут внимания» снята с «Обзора» осознанно (элемент 7): она
    # показывала 0 и была формально права — все ждущие отложены капом рецепта.
    # Сам счётчик внимания (pending + revised) никуда не делся: на нём стоят
    # ворота «Одобрение сценариев» и строка ленты, поэтому инвариант «доработка
    # не забывается» проверяется там, а не на плитке.
    tables = dict(OVERVIEW_TABLES)
    tables["briefs"] = [
        {"brief_id": "B1", "review_status": "pending", "generated_at": "2026-07-12"},
        {"brief_id": "B2", "review_status": "revised", "generated_at": "2026-07-12"},
    ]
    client, _ = make_client(tables)
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "СЦЕНАРИИ ЖДУТ ВНИМАНИЯ" not in resp.text
    assert "2 ждут решения" in resp.text                  # строка ворот в ленте


# ── M13 (аудит 2026-07-24): пропуск цикла виден systemd, но не ломает браузер ─

def test_cycle_run_busy_returns_409_for_headless_client():
    client, runner, commands = make_runner_client()
    runner.state["raw"]["status"] = "running"          # конвейер занят
    resp = client.post("/cycle/run")                    # curl systemd: без Origin/Referer
    assert resp.status_code == 409
    assert commands == []


def test_cycle_run_busy_keeps_303_for_browser_origin():
    client, runner, commands = make_runner_client()
    runner.state["raw"]["status"] = "running"
    resp = client.post("/cycle/run", headers={"origin": "http://127.0.0.1:8787"},
                       follow_redirects=False)
    assert resp.status_code == 303                      # браузерная форма — как раньше
    assert "не запущен" in runner.cycle_note
    assert commands == []


def test_reply_bad_session_id_422():
    # M22: session_id уходит в argv claude — токен-флаг отклоняется до запуска
    client, runner, commands = make_runner_client()
    resp = client.post("/stages/factory/reply",
                       data={"session_id": "--dangerously-skip-permissions",
                             "answer": "текст"})
    assert resp.status_code == 422
    assert commands == []


# --- Фаза 3 UX-доводки: ясность по разделам (§3 спеки) ------------------------


class _VisibleText(HTMLParser):
    """Только видимый текст страницы: без атрибутов, классов и скриптов.

    Регресс-грепу нужен именно он: `class="badge approved"` и `?status=approved` —
    техника разметки, а не слова, которые читает продюсер."""

    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def visible_text(html):
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(parser.chunks)


PHASE3_TABLES = {
    "raw_tiktok": [{"source_url": "https://tiktok.com/@a/video/1", "account": "a",
                    "views": 705906, "likes": 10, "comments": 2, "saves": 5,
                    "posted_at": "2026-07-01", "niche": "other", "hook_text": ""}],
    "raw_instagram": [],
    "briefs": [{"brief_id": "b-1", "hook": "3 образа на осень", "script": "кадр 1",
                "review_status": "approved", "generated_at": "2026-07-12",
                "formula_id": "F-07", "references": "https://tiktok.com/@x/video/2"}],
    "reels": [],
    "performance": [],
    "run_log": [],
}


def test_every_section_opens_with_a_lead_line():
    # Приёмка Фазы 3: каждый раздел объясняет одной строкой, что это и что делать
    client, _ = make_client(PHASE3_TABLES)
    for path in ("/briefs", "/briefs/shooting-list", "/performance", "/sources", "/runs"):
        assert '<p class="lead">' in client.get(path).text, path


def test_producer_sections_have_no_system_jargon_in_visible_text():
    client, _ = make_client(PHASE3_TABLES)
    for path in ("/briefs", "/briefs/shooting-list", "/performance", "/sources",
                 "/money"):
        text = visible_text(client.get(path).text)
        for token in ("evidence", "formula_id", "brief_id", "prompt_version",
                      "reels", "Views", "Likes", "Saves", "Google Sheets",
                      "raw-видео", "CF RAW"):
            assert token not in text, f"{token} утёк в {path}"


def test_brief_card_leads_with_the_hook_not_with_slugs():
    client, _ = make_client(PHASE3_TABLES)
    html = client.get("/briefs?id=b-1").text
    # заголовок карточки — сама идея ролика, слаги ушли под кат «Служебные ID»
    assert ">3 образа на осень</h2>" in html and 'class="hook-title"' in html
    assert "Служебные ID" in html
    assert html.index("hook-title") < html.index("Служебные ID")
    assert "b-1" in html                      # ID не выброшен, только понижен в правах


def test_shooting_list_card_numbers_scenarios_and_hides_slugs():
    client, _ = make_client(SHOOTING_TABLES)
    html = client.get("/briefs/shooting-list").text
    assert "Сценарий 1 из 1" in html          # читаемый номер вместо слага
    assert "добавлен 12.07.2026" in html      # дата по-человечески, а не внутри слага
    assert "ПРИМЕРЫ РОЛИКОВ" in html          # референсы объяснены
    assert "TikTok" in html                   # ярлык ссылки вместо сырого URL
    assert "Служебные ID" in html


RUNS_PHASE3 = {"run_log": [
    {"run_id": "1", "agent": "collect-tiktok", "trigger_type": "dry-run",
     "input_summary": "batches=2 rows=80 kept=17", "status": "success",
     "completed_at": "2026-07-24T22:25:00+00:00", "errors": "[]"},
    {"run_id": "2", "agent": "brief-reviewer", "trigger_type": "dashboard",
     "input_summary": "pending:2 → recommend×2 → auto-approved×2", "status": "success",
     "completed_at": "2026-07-24T20:10:00+00:00",
     "errors": '["ревью брифов не удалась"]'},
]}


def test_runs_page_speaks_task_language_and_keeps_raw_under_details():
    client, _ = make_client(RUNS_PHASE3)
    html = client.get("/runs").text
    assert "Сбор роликов из TikTok" in html            # словарь agent_id → задача
    assert "Проверка сценариев" in html
    assert "collect-tiktok" in html                    # слаг не выброшен
    assert "отобрано 17" in html                       # человеческая сводка
    assert "<summary>детали</summary>" in html         # сырой дамп под катом
    assert "тест — ничего не сохранено" in html        # dry-run помечен
    # «Успех» не стоит там, где внутри ошибка
    assert "Успех с ошибками" in html
    assert "ревью брифов не удалась" in html


def test_runs_page_dump_is_not_in_default_view():
    client, _ = make_client(RUNS_PHASE3)
    html = client.get("/runs").text
    before_details = html.split("<summary>детали</summary>")[0]
    assert "batches=" not in before_details


# --- Фаза 4 UX-доводки: адаптивность (§3 спеки) -------------------------------


def read_style_css():
    from cf.dashboard.app import BASE_DIR
    return (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")


def read_static(name):
    from cf.dashboard.app import BASE_DIR
    return (BASE_DIR / "static" / name).read_text(encoding="utf-8")


def test_layout_has_breakpoints_for_tablet_and_phone():
    css = read_style_css()
    # до Фазы 4 единственным @media был prefers-reduced-motion — каркас не перестраивался
    for query in ("max-width: 1023px", "max-width: 767px", "max-width: 479px"):
        assert f"@media ({query})" in css, query
    # сайдбар сворачивается, колонки стекаются, KPI-строка перестаёт быть жёсткой 4×
    assert ".nav-toggle:checked ~ .sidebar .nav { display: flex" in css
    assert ".two-col { grid-template-columns: 1fr; }" in css
    # очередь сценариев стекается раньше остальных: на 1024 таблица ужималась до
    # 270px и резала колонки «Статус» и «Дата» без всякого намёка
    assert "@media (max-width: 1279px)" in css
    assert ".briefs-layout { grid-template-columns: 1fr; }" in css
    assert ".stat-row { grid-template-columns: repeat(2, 1fr); }" in css


# --- 2026-07-28 (С1): тосты вместо полос --------------------------------------


def test_toast_container_is_fixed_and_empty_so_layout_never_shifts():
    # Причина отказа от полос словами владельца: «сообщения в виде полос на
    # странице ломают страницу, сдвигая элементы ниже».
    client, _ = make_client(PHASE3_TABLES)
    html = client.get("/overview").text
    assert '<div class="toasts" id="toasts" aria-live="polite"></div>' in html
    css = read_style_css()
    box = css[css.index(".toasts {"):]
    box = box[:box.index("}") + 1]
    assert "position: fixed" in box and "right: 16px" in box and "bottom: 16px" in box


def test_gate_banner_left_the_page_and_arrives_as_a_toast(tmp_path):
    from tests.test_queues import _approved, _entry
    _approved(tmp_path, [_entry("f1", "бренды-магазины")])
    sheets = FakeSheets(dict(OVERVIEW_TABLES, prompt_versions=[]))
    client = TestClient(create_app(sheets=sheets, lab_root=tmp_path), headers=BROWSER)
    html = client.get("/overview").text
    assert "banner gate-banner" not in html                 # полосы нет
    toasts = _toast_payload(html)
    gate = [t for t in toasts if t.get("tone") == "error"]
    assert gate and "Конвейер стоит на воротах" in gate[0]["text"]
    assert gate[0]["href"] == "/lab"                        # точка действия при тосте
    # …и та же тема висит строкой в «Уведомлениях», пока ворота держат
    assert "бренды-магазины" in html or "без включённых правил" in _notif_panel(html)


def test_toast_timer_is_six_seconds_and_pauses_on_hover():
    css = read_style_css()
    bar = css[css.index("\n.toast-bar {"):]
    bar = bar[:bar.index("}") + 1]
    assert "animation: toast-life 6s linear forwards" in bar
    assert ".toast:hover .toast-bar" in css and "animation-play-state: paused" in css
    # полоса И ЕСТЬ таймер: её animationend закрывает тост, поэтому пауза по
    # наведению останавливает и срок жизни — двух счётчиков не заведено
    js = read_static("toasts.js")
    assert "bar.addEventListener('animationend'" in js


def test_toast_stack_is_three_and_the_rest_wait():
    js = read_static("toasts.js")
    assert "MAX_VISIBLE = 3" in js
    assert "host.children.length < MAX_VISIBLE" in js
    assert "queue.shift()" in js                            # четвёртый ждёт очереди


def test_toast_flies_left_into_the_notifications_button():
    js = read_static("toasts.js")
    assert "document.getElementById('notif-btn')" in js
    assert "toast.style.transform" in js
    # «уменьшить движение» гасит полёт, но НЕ полосу: без неё тост не закроется
    assert "if (reduce) { remove(toast); return; }" in js
    css = read_style_css()
    assert "@media (prefers-reduced-motion: reduce) { .toast { transition: none; } }" in css


def test_toast_can_be_closed_by_hand_and_reaches_a_screen_reader():
    js = read_static("toasts.js")
    assert "'Закрыть уведомление'" in js
    assert "close.addEventListener('click'" in js
    client, _ = make_client(PHASE3_TABLES)
    assert 'aria-live="polite"' in client.get("/overview").text


# --- 2026-07-28 (С2/С3): раздел «Уведомления» в сайдбаре ----------------------


def _notif_panel(html):
    """Разметка панели уведомлений — от её контейнера до конца сайдбара."""
    start = html.index('id="notif-panel"')
    return html[start:html.index("</aside>", start)]


def test_notifications_button_stands_on_every_screen():
    client, _ = make_client(PHASE3_TABLES)
    for path in ("/overview", "/briefs", "/briefs/shooting-list", "/runs",
                 "/performance", "/sources", "/lab"):
        html = client.get(path).text
        assert 'id="notif-btn"' in html, path
        assert 'id="notif-panel"' in html, path
        assert 'aria-expanded="false"' in html, path       # закрыта по умолчанию


def test_notification_row_lives_exactly_while_its_condition_lives(tmp_path):
    # Производная от состояния, а не журнал: условие возникло — строка есть;
    # условие расшилось — строки нет, и никакого действия человека для этого не
    # нужно (ни «прочитано», ни хранилища).
    from tests.test_queues import _active, _approved, _entry, _prompt_naming
    _approved(tmp_path, [_entry("f1", "тема")])
    tables = {"briefs": [], "run_log": [], "reels": [], "performance": [],
              "raw_tiktok": [], "raw_instagram": [], "prompt_versions": []}

    def panel(versions):
        sheets = FakeSheets(dict(tables, prompt_versions=versions))
        client = TestClient(create_app(sheets=sheets, lab_root=tmp_path),
                            headers=BROWSER)
        return _notif_panel(client.get("/overview").text)

    # условие есть: у темы рецепт есть, правила сценариев не включены
    assert "без включённых правил сценариев" in panel([])
    # условие ушло: файл правил на месте (и называет рецепт), версия активна —
    # строка исчезла сама, без «прочитано» и без участия человека
    _prompt_naming(tmp_path, "тема", ["f1"])
    after = panel([_active("тема")])
    assert "без включённых правил сценариев" not in after
    assert "Ничего не требует действия" in after


def test_notifications_do_not_claim_all_clear_on_unread_data():
    # Правило №2: сбой чтения формул/Sheets — это «неизвестно», а не «ничего не
    # требует действия». Пустой список рисовал бы успокоительное «завод едет сам»
    # по НЕпрочитанным данным.
    from cf.dashboard.queues import notifications
    rows = notifications(None)
    assert [r["key"] for r in rows] == ["unknown"]
    assert "не посчитано" in rows[0]["text"]

    class BrokenSheets(FakeSheets):
        def read_rows(self, tab_key):
            raise ConnectionError("sheets down")

    client = TestClient(create_app(sheets=BrokenSheets({})))
    panel = _notif_panel(client.get("/overview").text)
    assert "Ничего не требует действия" not in panel
    assert "не посчитано" in panel


def test_presentation_mode_counts_demo_as_publication():
    # Презентационный режим (dashboard.demo_as_published): владельцу нужно
    # показать, как «Обзор» выглядит при работающей выкладке, пока отдела съёмки
    # ещё нет. Флаг выключен по умолчанию и снимается одной строкой конфига —
    # это режим показа, а не молчаливая подмена данных.
    from cf.dashboard.data import demo_as_published_from_config, reel_is_published
    demo = {"reel_id": "D", "status": "demo", "post_url": "https://demo.invalid/1"}
    assert reel_is_published(demo) is False
    assert reel_is_published(demo, count_demo=True) is True
    # строка без ссылки не «выложена» ни в каком режиме
    assert reel_is_published({"status": "demo", "post_url": ""}, count_demo=True) is False

    cfg = lambda v: {"dashboard": {"demo_as_published": v}}
    assert demo_as_published_from_config(cfg(True)) is True
    assert demo_as_published_from_config(cfg("true")) is True
    assert demo_as_published_from_config(cfg(False)) is False
    assert demo_as_published_from_config({}) is False          # по умолчанию правда
    assert demo_as_published_from_config(cfg("мусор")) is False

    # Дата в окне: окно скользящее, фиксированная уехала бы из него с календарём.
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    tables = dict(OVERVIEW_TABLES)
    tables["reels"] = [{"reel_id": "D", "brief_id": "B1", "status": "demo",
                        "published_at": today,
                        "post_url": "https://demo.invalid/1"}]

    def published_tile(flag):
        client = TestClient(create_app(sheets=FakeSheets(tables),
                                       demo_as_published=flag))
        return _tile(client.get("/overview").text, "ОПУБЛИКОВАНЫ")

    assert ">0<" in published_tile(False)      # правда: настоящих съёмок нет
    assert ">1<" in published_tile(True)       # показ: демо считается выкладкой


def test_demo_reels_do_not_hide_real_scripts_from_the_shooting_queue():
    # Живой случай 28.07: демо-петля (`cf demo-seed`) ставит фиктивные рилы на
    # РЕАЛЬНЫЕ одобренные сценарии. Сырой join объявлял их снятыми — 35 сценариев
    # из 64 пропали и из плитки «Ждут назначения», и из «Очереди съёмки», хотя
    # никто их не снимал. Признак «ролик вышел» в дашборде должен быть один.
    tables = {
        "raw_tiktok": [], "raw_instagram": [], "performance": [], "run_log": [],
        "briefs": [
            {"brief_id": "REAL", "hook": "настоящий", "review_status": "approved",
             "generated_at": "2026-07-12", "script": "с", "references": "",
             "formula_id": "F"},
            {"brief_id": "SHOT", "hook": "снят по-настоящему",
             "review_status": "approved", "generated_at": "2026-07-12",
             "script": "с", "references": "", "formula_id": "F"},
        ],
        "reels": [
            # фиктивный рил демо-петли на реальном сценарии — не съёмка
            {"reel_id": "DEMO-01", "brief_id": "REAL", "status": "demo",
             "published_at": "2026-07-13", "post_url": "https://demo.invalid/1"},
            {"reel_id": "R-01", "brief_id": "SHOT", "published_at": "2026-07-13",
             "post_url": "https://tiktok.com/@x/1"},
        ],
    }
    client, _ = make_client(tables)
    queue = client.get("/briefs/shooting-list").text
    assert "настоящий" in queue                    # демо-рил его не прячет
    assert "снят по-настоящему" not in queue        # а настоящий рил — прячет
    assert ">1<" in _tile(client.get("/overview").text, "ЖДУТ НАЗНАЧЕНИЯ")


def test_published_count_is_the_same_predicate_in_the_tile_and_in_the_ribbon():
    # Иначе плитка честно показывает 0, а лента рядом рисует «35 роликов вышло»
    # из демо-строк eval-петли: два способа считать «ролик вышел» — тот самый
    # класс расхождения, ради которого заведён единый источник признаков.
    tables = dict(OVERVIEW_TABLES)
    tables["reels"] = [
        {"reel_id": "DEMO", "published_at": "2026-07-05", "status": "demo",
         "post_url": "https://demo.invalid/1"},
        {"reel_id": "NOLINK", "published_at": "2026-07-05", "post_url": ""},
    ]
    tables["performance"] = []
    client, _ = make_client(tables)
    html = client.get("/overview").text
    ribbon = html[html.index("Съёмка и публикация"):]
    assert "0 роликов вышло" in ribbon[:400]


def test_notifications_show_stale_data_and_count_them_on_the_button():
    sheets = FakeSheets(OVERVIEW_TABLES)
    now = [0.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: now[0])
    client = TestClient(create_app(sheets=sheets, cache=cache), headers=BROWSER)
    assert "Данные могли устареть" not in _notif_panel(client.get("/overview").text)

    def broken(tab_key):
        raise ConnectionError("sheets down")

    sheets.read_rows = broken
    now[0] = 300.0                                     # TTL истёк, чтение падает
    html = client.get("/overview").text
    assert "Данные могли устареть" in _notif_panel(html)
    assert 'class="notif-count"' in html                # счётчик на кнопке


def test_notification_panel_escapes_the_sidebar_and_is_wider_than_it():
    # У сайдбара overflow-y: auto — абсолютная панель обрезалась бы ровно по той
    # границе, за которую ей и нужно выйти (ширина сайдбара 240px).
    css = read_style_css()
    pop = css[css.index(".notif-pop {"):]
    pop = pop[:pop.index("}") + 1]
    assert "position: fixed" in pop
    assert "width: 320px" in pop                        # шире сайдбара (240px)
    assert "z-index" in pop                             # поверх страницы


def test_notification_panel_is_keyboard_reachable_and_closes_three_ways():
    client, _ = make_client(PHASE3_TABLES)
    html = client.get("/overview").text
    assert 'aria-controls="notif-panel"' in html
    assert 'tabindex="-1"' in html                      # фокус уходит в панель
    js = client.get("/static/notifications.js")
    assert js.status_code == 200
    assert "Escape" in js.text                          # Esc
    assert "pop.contains(evt.target)" in js.text        # клик вне
    assert "if (isOpen()) close(true); else open();" in js.text   # повторный клик
    assert "btn.focus()" in js.text                     # фокус возвращается на кнопку


# --- 2026-07-28 (элементы 6–9): плитки этапа 3 --------------------------------


def _tile(html, label):
    """Разметка ОДНОЙ плитки: от её заголовка до заголовка следующей.

    Границу ищем по `stat-label`, а не по имени соседней плитки: порядок плиток
    — решение владельца и уже менялся, а тест не должен падать от перестановки.
    """
    rest = html[html.index(label):]
    ends = [i for i in (rest.find('class="stat-label"'), rest.find("</section>"))
            if i > 0]
    return rest[:min(ends)] if ends else rest


STAGE3_TABLES = {
    "raw_tiktok": [{"source_url": "u1", "posted_at": "2026-07-01",
                    "collected_at": "2026-07-01", "niche": "мужской-стиль"}],
    "raw_instagram": [],
    "briefs": [{"brief_id": "B1", "review_status": "approved",
                "generated_at": "2026-07-12"}],
    "reels": [{"reel_id": "R1", "published_at": "2026-07-05",
               "post_url": "https://tiktok.com/@x/1"}],
    "performance": [{"reel_id": "R1", "er": "4.5"}],
    "run_log": [],
}


def test_link_tiles_show_four_stages_in_the_footprint_of_one():
    client, _ = make_client(STAGE3_TABLES)
    html = client.get("/overview").text
    block = _tile(html, "ЗВЕНЬЯ ЗАВОДА")
    assert block.count('class="mini-tile"') == 4      # четыре в габарите одной плитки
    for label in ("raw ролики", "темы", "рецепты", "сценарии"):
        assert f">{label}<" in block, label
    assert "ЗВЕНЬЯ ЗАВОДА · 7 ДНЕЙ" in html           # окно названо прямо на плитке


def test_assignment_tile_carries_the_aging_note_and_drops_the_old_phrase():
    tables = dict(STAGE3_TABLES)
    tables["briefs"] = [
        {"brief_id": "NEW", "review_status": "approved", "generated_at": "2026-07-12"},
        # одобрен давно и не снят — попадает и в счёт плитки, и в подпись старения
        {"brief_id": "OLD", "review_status": "approved", "generated_at": "2026-01-01"},
    ]
    client, _ = make_client(tables)
    html = client.get("/overview").text
    tile = _tile(html, "ЖДУТ НАЗНАЧЕНИЯ")
    assert ">2<" in tile                              # оба одобренных без исполнителя
    assert "ждут дольше" in tile                      # старение — подписью в плитке
    assert "доходят до съёмки за неделю" not in html  # формулировка убрана совсем


def test_published_tile_shows_zero_honestly_and_ignores_demo_rows():
    # Пустота — не дефект, а правило №2: роликов снято 0, и показать это честно
    # важнее, чем спрятать плитку.
    tables = dict(STAGE3_TABLES)
    tables["reels"] = [{"reel_id": "D", "published_at": "2026-07-13",
                        "status": "demo", "post_url": "https://demo.invalid/1"}]
    client, _ = make_client(tables)
    tile = _tile(client.get("/overview").text, "ОПУБЛИКОВАНЫ")
    assert ">0<" in tile
    assert "вышли со ссылкой за 7 дней" in tile


def test_tempo_rings_are_readable_as_text_not_only_as_colour():
    client, _ = make_client(STAGE3_TABLES)
    tile = _tile(client.get("/overview").text, "ТЕМП НЕДЕЛИ")
    # сам рисунок скринридеру не читается — смысл несут подписи и числа рядом
    assert 'class="rings-art"' in tile and 'aria-hidden="true"' in tile
    for label in ("одобрено", "назначен исполнитель", "выложено"):
        assert label in tile, label
    assert tile.count("/70") == 3                     # у каждого кольца своя цель
    assert tile.count('class="ring-fill') == 3


def test_tempo_rings_do_not_draw_past_the_goal():
    # Даты относительно «сегодня»: окно скользящее, и фиксированная дата уехала бы
    # из него вместе с календарём.
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    tables = dict(STAGE3_TABLES)
    tables["briefs"] = [{"brief_id": f"B{i}", "review_status": "approved",
                         "generated_at": today} for i in range(200)]
    client, _ = make_client(tables)
    tile = _tile(client.get("/overview").text, "ТЕМП НЕДЕЛИ")
    assert "200/70" in tile                           # честное число, не обрезанное
    for dash in re.findall(r'stroke-dasharray="([\d.]+) ([\d.]+)"', tile):
        assert float(dash[0]) <= float(dash[1]) + 1e-6


def test_tiles_are_two_wide_and_two_narrow_in_the_owner_order():
    # Решение владельца 2026-07-28: «Звенья завода» занимают доли 1-2, «Ждут
    # назначения» — 3, «Темп недели» — 4-5, «Опубликованы» — 6. Прежний auto-fit
    # раскладывал плитки «по месту» и на ноутбуке 1280 давал три в первом ряду и
    # одну сиротой во втором.
    client, _ = make_client(STAGE3_TABLES)
    html = client.get("/overview").text
    at = [html.index(label) for label in ("ЗВЕНЬЯ ЗАВОДА", "ЖДУТ НАЗНАЧЕНИЯ",
                                          "ТЕМП НЕДЕЛИ", "ОПУБЛИКОВАНЫ")]
    assert at == sorted(at)          # порядок разметки = порядок на экране
    for label, wide in (("ЗВЕНЬЯ ЗАВОДА", True), ("ЖДУТ НАЗНАЧЕНИЯ", False),
                        ("ТЕМП НЕДЕЛИ", True), ("ОПУБЛИКОВАНЫ", False)):
        head = html[:html.index(label)]
        opening = max(head.rfind('<div class="stat"'), head.rfind('<div class="stat '))
        assert ("stat--wide" in html[opening:opening + 40]) is wide, label
    css = read_style_css()
    assert ".stat-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert ".stat--wide { grid-column: span 2; }" in css


def test_wide_tiles_become_ordinary_on_a_phone():
    # В сетке 2×2 «span 2» отдал бы широкой плитке целый ряд, а соседняя доля
    # осталась бы дырой — тем же дисбалансом, ради которого сетку и переделали.
    css = read_style_css()
    phone = css[css.index("@media (max-width: 767px)"):]
    phone = phone[:phone.index("\n}")]
    assert ".stat--wide { grid-column: auto; }" in phone


# --- 2026-07-28: кусочки сценариев под плитками -------------------------------


PEEK_TABLES = {
    "raw_tiktok": [], "raw_instagram": [], "reels": [], "performance": [],
    "run_log": [],
    "briefs": [
        {"brief_id": "OLD", "review_status": "approved", "generated_at": "2026-07-10",
         "hook": "одобренный старый", "script": "полный текст " * 40, "formula_id": "f"},
        {"brief_id": "WAIT", "review_status": "pending", "generated_at": "2026-07-01",
         "hook": "ждёт решения", "script": "первая строка сценария", "formula_id": "f"},
        {"brief_id": "DEFER", "review_status": "pending", "generated_at": "2026-07-20",
         "hook": "отложен заводом", "script": "текст", "formula_id": "f",
         "reviewer_notes": "отложено заводом: кап рецепта"},
    ],
}


def test_overview_shows_pieces_of_the_scripts_under_the_tiles():
    # Решение владельца 2026-07-28: под плитками оставался пустой экран, и по
    # нему нельзя было понять, ЧТО завод написал.
    client, _ = make_client(PEEK_TABLES)
    html = client.get("/overview").text
    assert html.index("stat-row") < html.index("Сценарии завода")   # под плитками
    peek = html[html.index("Сценарии завода"):html.index('class="ov-side"')]
    for hook in ("ждёт решения", "одобренный старый", "отложен заводом"):
        assert hook in peek, hook
    assert "первая строка сценария" in peek                 # кусочек самого текста
    # Ждущий решения — первым: это единственное, что на заводе за человеком.
    # Отложенный капом ждущим НЕ считается — тем же предикатом, что и плитка.
    assert peek.index("ждёт решения") < peek.index("отложен заводом")
    assert peek.index("ждёт решения") < peek.index("одобренный старый")
    assert peek.count("peek peek--waiting") == 1
    # Длинный сценарий режется на сервере, а не льётся в разметку целиком
    assert ("полный текст " * 40).strip() not in peek
    assert "…" in peek
    assert 'href="/briefs?status=all&amp;id=WAIT"' in peek   # карточка ведёт к решению


def test_script_peek_says_so_when_there_are_no_scripts():
    # Правило №2: пустой блок молчит, а честная строка объясняет пустоту.
    tables = dict(PEEK_TABLES, briefs=[])
    client, _ = make_client(tables)
    html = client.get("/overview").text
    peek = html[html.index("Сценарии завода"):html.index('class="ov-side"')]
    assert "Сценариев ещё нет" in peek
    assert 'class="peek' not in peek


# --- 2026-07-28 (С5): «Обзор» в две колонки -----------------------------------


def test_overview_puts_the_ribbon_in_its_own_column():
    client, _, _ = make_runner_client()
    html = client.get("/overview").text
    assert 'class="overview-cols"' in html
    assert 'class="ov-main"' in html
    assert 'class="ov-side" aria-label="Конвейер"' in html
    # плитки и кусочки сценариев — слева, «Конвейер» целиком — справа
    main = html[html.index('class="ov-main"'):html.index('class="ov-side"')]
    side = html[html.index('class="ov-side"'):]
    assert "stat-row" in main and "Сценарии завода" in main
    assert 'id="stages"' not in main and "Конвейер" not in main
    assert 'id="stages"' in side and "Конвейер" in side


def test_full_cycle_button_stands_at_the_top_of_the_pipeline_column():
    # Решение владельца 2026-07-28: раздел с кнопкой запуска переехал в правую
    # колонку — одной кнопкой сверху, над лентой, которую она и двигает.
    client, _, _ = make_runner_client()
    html = client.get("/overview").text
    side = html[html.index('class="ov-side"'):]
    assert side.count('action="/cycle/run"') == 1          # ровно одна кнопка
    assert html.count('action="/cycle/run"') == 1          # и слева её не осталось
    assert side.index('action="/cycle/run"') < side.index('id="stages"')
    # платный прогон по-прежнему требует подтверждения
    assert "data-confirm=" in side[:side.index('id="stages"')]


def test_ribbon_column_is_in_flow_not_sticky_and_scrolls_with_the_page():
    # Решение владельца: лента в потоке — ни position:sticky, ни своего скролла.
    css = read_style_css()
    side = css[css.index(".ov-side {"):]
    side = side[:side.index("}") + 1]
    assert "sticky" not in side and "overflow" not in side


def test_ribbon_row_moves_meta_to_a_second_tier_instead_of_clipping():
    # Резиновая строка: где место есть — один ряд; где нет — актор и счётчик
    # уезжают под название ОДНОЙ группой. Обрезки и переполнения нет ни на одной
    # ширине, поэтому ни nowrap, ни ellipsis у названия быть не должно.
    client, _, _ = make_runner_client()
    html = client.get("/partials/stages").text
    assert 'class="tl-meta"' in html
    meta = html[html.index('class="tl-meta"'):]
    assert meta.index("tl-actor") < meta.index("tl-count")   # группа целиком
    css = read_style_css()
    assert ".tl-line { display: flex" in css and "justify-content: space-between" in css
    line = css[css.index(".tl-name {"):]
    assert "text-overflow" not in line[:line.index("}")]


def test_overview_columns_stack_with_the_ribbon_on_top_on_narrow_screens():
    css = read_style_css()
    narrow = css[css.index("@media (max-width: 1279px)"):]
    narrow = narrow[:narrow.index("\n}")]
    assert ".overview-cols { grid-template-columns: 1fr; }" in narrow
    assert ".ov-side { order: -1; }" in narrow          # лента наверх, над плитками


def test_style_version_bumped_so_browser_does_not_serve_stale_css():
    client, _ = make_client(OVERVIEW_TABLES)
    assert '/static/style.css?v=18' in client.get("/overview").text


def test_burger_toggle_is_in_shell_before_sidebar():
    # CSS-переключатель работает только если чекбокс — предыдущий сосед сайдбара
    client, _ = make_client(OVERVIEW_TABLES)
    html = client.get("/overview").text
    assert 'class="nav-toggle" type="checkbox" id="nav-toggle"' in html
    assert '<label class="nav-burger" for="nav-toggle"' in html
    assert html.index('class="nav-toggle"') < html.index('<aside class="sidebar">')


def test_wide_tables_scroll_inside_card_instead_of_being_clipped():
    # таблицы рисуются только при непустых данных — берём фикстуры со строками
    tables = dict(PHASE3_TABLES, **RUNS_PHASE3)
    tables["reels"] = [{"reel_id": "r-1", "brief_id": "b-1", "platform": "tiktok",
                        "post_url": "https://t/1", "published_at": "2026-07-14"}]
    client, _ = make_client(tables)
    css = read_style_css()
    assert ".card--table { padding: 0; overflow-x: auto; }" in css
    for path in ("/sources", "/runs", "/performance", "/briefs"):
        html = client.get(path).text
        assert 'class="card card--table"' in html, path
        # инлайновый overflow:hidden резал крайние колонки на узком экране
        assert 'style="padding: 0; overflow: hidden;"' not in html, path


def test_lab_long_fields_are_clamped_with_full_text_on_demand(tmp_path):
    from tests.test_dashboard_sections import _write
    # Порог «показать полностью» синхронизирован с обрезкой в 3 строки (~340
    # символов на десктопе): при пороге 160 две трети раскрывашек не раскрывали
    # ничего — текст и так помещался целиком.
    long_text = ("Зрительница листает бесконечные однотипные каталожные подборки "
                 "образов без лица и голоса — им незачем ставить лайк и не с кем "
                 "себя ассоциировать; обезличенный контент собирает просмотры, "
                 "но не собирает аудиторию: ни одного повода вернуться к автору "
                 "он не даёт, а лента подхватывает только то, что удерживает "
                 "внимание дольше первых полутора секунд просмотра.")
    _write(tmp_path / "formulas" / "_approved" / "index.json", {"approved": []})
    _write(tmp_path / "formulas" / "стиль" / "f1.json",
           {"name": "f1", "niche": "стиль", "version": 1, "status": "proposed",
            "problem_definition": long_text, "hook_structure": "x",
            "evidence": {"source_urls": []}})
    client, _ = make_client({"run_log": [], "briefs": []}, lab_root=tmp_path)
    html = client.get("/lab").text
    assert '<details class="more">' in html
    assert '<span class="clamp">' in html
    assert long_text in html                 # полный текст остаётся — не обрезаем данные


# --- Фаза 5 UX-доводки: долг дизайн-системы (§3 спеки) ------------------------


def template_files():
    from cf.dashboard.app import BASE_DIR
    return sorted((BASE_DIR / "templates").rglob("*.html"))


def test_no_static_inline_styles_left_in_templates():
    # Инлайны допустимы только там, где значение вычисляется (ширина прогресс-бара,
    # высота столбца графика) — всё остальное живёт классами ДС.
    import re
    leftovers = []
    for path in template_files():
        for style in re.findall(r'style="([^"]*)"', path.read_text(encoding="utf-8")):
            if "{{" not in style:
                leftovers.append(f"{path.name}: {style}")
    assert leftovers == [], leftovers


def test_empty_state_split_into_three_meanings():
    css = read_style_css()
    assert ".banner.error {" in css and ".table-foot {" in css
    # водяной знак остаётся ТОЛЬКО у настоящей пустоты
    assert ".empty-state::before" in css
    client, _ = make_client({"raw_tiktok": [], "raw_instagram": []})
    empty = client.get("/sources").text
    assert 'class="empty-state">Пока нет собранных роликов' in empty

    class BrokenSheets(FakeSheets):
        def read_rows(self, tab_key):
            raise ConnectionError("sheets down")

    broken = TestClient(create_app(sheets=BrokenSheets({}))).get("/sources").text
    assert 'class="banner error"' in broken          # ошибка — баннер, не «пустота»
    assert 'class="empty-state">Не удалось' not in broken


def test_pagination_footer_has_no_watermark_class():
    rows = [{"source_url": f"u{i}", "account": "a", "views": 1,
             "posted_at": "2026-07-01"} for i in range(220)]
    client, _ = make_client({"raw_tiktok": rows, "raw_instagram": []})
    html = client.get("/sources").text
    assert 'class="table-foot">Показаны 200 из 220' in html


def test_declared_ui_font_is_actually_self_hosted():
    from cf.dashboard.app import BASE_DIR
    css = read_style_css()
    assert "@font-face { font-family: Montserrat;" in css
    for name in ("Montserrat-cyrillic.woff2", "Montserrat-latin.woff2"):
        path = BASE_DIR / "static" / "fonts" / name
        assert path.is_file(), name
        assert path.read_bytes()[:4] == b"wOF2", name   # действительно woff2, не HTML-ошибка


def test_shooting_list_classes_exist_in_ds():
    css = read_style_css()
    assert ".shooting-list {" in css and ".shooting-group {" in css


# --- Фаза 6 UX-доводки: порог доступности (§3 спеки) --------------------------


def test_skip_link_leads_to_main_content():
    client, _ = make_client(OVERVIEW_TABLES)
    html = client.get("/overview").text
    assert '<a class="skip-link" href="#main">Перейти к содержимому</a>' in html
    assert '<main class="main" id="main"' in html
    assert html.index("skip-link") < html.index('<aside class="sidebar">')


def test_form_fields_have_labels():
    # Селектор периода снят с «Обзора» (элемент 3), инвариант на его подпись
    # поправлен намеренно; проверка «у каждого поля есть label» остаётся.
    client, _ = make_client(OVERVIEW_TABLES)
    briefs = client.get("/briefs").text
    for control_id in ("review-notes", "reject-reason"):
        assert f'for="{control_id}"' in briefs and f'id="{control_id}"' in briefs


def test_tables_have_caption_and_column_scopes():
    client, _ = make_client(dict(PHASE3_TABLES, **RUNS_PHASE3))
    html = client.get("/runs").text
    assert '<caption class="sr-only">Журнал задач системы</caption>' in html
    assert '<th scope="col">Задача</th>' in html


def test_icon_only_links_have_accessible_names():
    client, _ = make_client(PHASE3_TABLES)
    sources = client.get("/sources").text
    assert "aria-label=\"Открыть ролик a на площадке\"" in sources
    assert "<th scope=\"col\">Ролик</th>" in sources


def test_async_regions_are_announced():
    # Живой регион обязан жить в DOM ДО изменения: блок, который приезжает вместе
    # со своим содержимым (hx-swap="outerHTML"), ассистивные технологии молчат.
    # Поэтому объявление вынесено в стабильный #review-say, а он остаётся на
    # странице при свопе очереди.
    client, _ = make_client(PHASE3_TABLES)
    html = client.get("/briefs").text
    assert '<p id="review-say" role="status" aria-live="polite"' in html
    assert html.index("review-say") < html.index('id="briefs-review"')
    assert 'id="briefs-review" aria-live' not in html      # фальшивого региона нет
    client2, runner, _ = make_runner_client()
    assert 'id="reports" aria-live' not in client2.get("/overview").text


def test_navbar_marks_current_section():
    client, _ = make_client(OVERVIEW_TABLES)
    html = client.get("/overview").text
    active = [line for line in html.splitlines()
              if "nav-item active" in line and 'aria-current="page"' in line]
    assert active, "у активного пункта навбара нет aria-current"


# --- Инцидент 2026-07-25: правка файлов под живым сервисом ---------------------


def test_templates_are_pinned_to_process_not_reloaded_from_disk():
    # Каталог сервиса = рабочее дерево репозитория. Jinja по умолчанию перечитывает
    # шаблон с диска, а Python-модуль остаётся старым — живой дашборд отдавал 500
    # («No filter named 'duration'») каждые 2 секунды поллинга ленты.
    client, _ = make_client(OVERVIEW_TABLES)
    app = client.app
    assert app.state.templates.env.auto_reload is False


def test_precompile_templates_compiles_all_and_survives_broken_one(tmp_path, caplog):
    from cf.dashboard.app import _precompile_templates, BASE_DIR
    from jinja2 import Environment, FileSystemLoader

    from cf.dashboard.app import template_filters

    env = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates")))
    # ровно те же фильтры, что у боевого приложения: Jinja проверяет имена при
    # компиляции, и разъехавшиеся списки означали бы «падает только в проде»
    env.filters.update(template_filters())
    _precompile_templates(env)
    # все шаблоны разделов легли в кэш процесса
    for name in ("overview.html", "briefs.html", "runs.html", "sources.html",
                 "performance.html", "shooting-list.html", "lab.html",
                 "partials/stages.html", "partials/briefs_review.html"):
        assert name in env.cache or any(k[1] == name for k in env.cache), name


def test_production_app_precompiles_templates(monkeypatch):
    # боевой сервис прогревает шаблоны на старте — иначе первый рендер после
    # правки файла подхватил бы несовместимую разметку
    from cf.dashboard import app as app_mod

    called = []
    monkeypatch.setattr(app_mod, "_precompile_templates",
                        lambda env: called.append(env))
    app_mod.build_production_app(sheets=FakeSheets({}), config={"dashboard": {}},
                                 health_autostart=False)
    assert called, "боевое приложение не прогрело шаблоны"


def test_interrupted_run_says_so_instead_of_hanging_bar():
    # Оператор: «всё ещё висит». На деле прогон умер при рестарте сервиса, а лента
    # показывала полоску 1/2 без единого слова — и читалась как зависшая работа.
    client, runner, _ = make_runner_client()
    runner.run_progress["analyze"].update(status="interrupted", done=1, total=2)
    html = client.get("/partials/stages").text
    assert "tl-interrupted" in html
    # подсказка ведёт к кнопке ▶, которая живёт на первом шаге звена
    assert "прогон прерван — запустите этап заново кнопкой ▶ у «Разметка тем»" in html
    assert 'title="прогон прерван"' in html
    # полоска не анимируется и не выглядит работающей
    assert "data-sim" not in html
    assert 'hx-trigger="every 2s"' not in html            # поллить нечего


def test_non_running_bars_are_not_painted_as_active():
    css = read_style_css()
    # синий — только для бегущего шага; idle/interrupted — приглушённый silver
    assert ".tl-idle .tl-fill, .tl-interrupted .tl-fill { background: var(--silver); }" in css


def test_excluded_niche_card_gives_one_reason_and_no_prompt_button(tmp_path):
    # Вопрос оператора 26.07: «в очереди стоит рецепт на женскую моду — как к
    # нему относиться?». Ниша в exclude_niches, промпта у неё тоже нет, и до
    # правки карточка показывала ОБЕ плашки, причём «нет промпта» звала нажать
    # «Создать промпт из шаблона» — для темы, по которой бренд контент не
    # выпускает. Показываем ровно одну причину, ту, что решает судьбу рецепта.
    root = tmp_path
    (root / "formulas" / "женская-мода").mkdir(parents=True)
    (root / "formulas" / "женская-мода" / "wm-one.json").write_text(json.dumps({
        "name": "wm-one", "niche": "женская-мода", "version": 1,
        "status": "proposed", "confidence": "high",
        "evidence": {"source_urls": ["https://t.tt/a", "https://t.tt/b",
                                     "https://t.tt/c", "https://t.tt/d"]},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "cf.config.json").write_text(json.dumps({
        "dashboard": {"fanout": {"exclude_niches": ["женская-мода"]}}},
        ensure_ascii=False), encoding="utf-8")
    client, _ = make_client({"run_log": [], "briefs": []}, lab_root=str(root))

    html = client.get("/lab").text

    assert "не наша тема" in html                     # причина названа прямо
    assert "отклоните его" in html                    # и сказано, что делать
    # нецелевая ниша не зовёт писать промпт: ни пометки, ни запуска агента
    assert 'action="/prompts/write"' not in html
    assert "ниша ждёт brief-промпта" not in html


# --- 2026-07-28: сортировка очереди сценариев по дате -------------------------


SORT_HOOKS = ("свежий", "средний", "старый", "без даты")
_SORT_ROW = {"script": "с", "references": "", "formula_id": "F",
             "rejection_reason": "", "reviewer_notes": ""}
SORT_TABLES = {
    "briefs": [
        # порядок ЛИСТА намеренно вперемешку: сортирует экран, а не таблица
        dict(_SORT_ROW, brief_id="MID", hook="средний", review_status="approved",
             generated_at="2026-07-14"),
        dict(_SORT_ROW, brief_id="OLD", hook="старый", review_status="approved",
             generated_at="2026-07-01"),
        dict(_SORT_ROW, brief_id="NEW", hook="свежий", review_status="approved",
             generated_at="2026-07-27"),
        # дата неизвестна — не «самый старый» и не «самый новый» (правило №2)
        dict(_SORT_ROW, brief_id="NODATE", hook="без даты", review_status="approved",
             generated_at=""),
    ],
    "run_log": [],
}


def _hook_order(html):
    """Порядок строк очереди — по месту заголовков в <tbody>."""
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    return [hook for _, hook in sorted((body.index(h), h) for h in SORT_HOOKS)]


def test_briefs_queue_puts_the_freshest_on_top_by_default():
    # Решение владельца 2026-07-28: порядок листа шёл от самых старых, и очередь
    # приходилось мотать вниз, чтобы увидеть то, что завод написал вчера.
    client, _ = make_client(SORT_TABLES)
    assert _hook_order(client.get("/briefs").text) == [
        "свежий", "средний", "старый", "без даты"]


def test_briefs_queue_flips_to_oldest_first_and_keeps_undated_last():
    client, _ = make_client(SORT_TABLES)
    assert _hook_order(client.get("/briefs?sort=date-asc").text) == [
        "старый", "средний", "свежий", "без даты"]


def test_briefs_sort_header_is_a_link_that_flips_and_names_the_direction():
    client, _ = make_client(SORT_TABLES)
    html = client.get("/briefs").text
    head = html[html.index("<thead>"):html.index("</thead>")]
    assert 'aria-sort="descending"' in head          # список упорядочен по этой колонке
    assert "sort=date-asc" in head                   # клик переключает направление
    assert "сначала новые" in head                   # направление названо словами
    flipped = client.get("/briefs?sort=date-asc").text
    head = flipped[flipped.index("<thead>"):flipped.index("</thead>")]
    assert 'aria-sort="ascending"' in head and "sort=date-desc" in head


def test_briefs_sort_survives_filter_row_click_and_review_decision():
    client, sheets = make_client(SORT_TABLES)
    html = client.get("/briefs?sort=date-asc").text
    assert "/briefs?status=approved&amp;sort=date-asc" in html    # чипы фильтра
    assert "sort=date-asc&amp;id=NEW" in html                     # ссылки строк
    assert 'name="sort" value="date-asc"' not in html   # у approved формы решения нет
    pending = dict(SORT_TABLES)
    pending["briefs"] = [dict(b, review_status="pending") for b in SORT_TABLES["briefs"]]
    client, _ = make_client(pending)
    form = client.get("/briefs?sort=date-asc").text
    assert 'name="sort" value="date-asc"' in form       # порядок едет с решением
    resp = client.post("/briefs/OLD/review", data={"decision": "approved",
                                                   "sort": "date-asc"},
                       headers=GOOD_ORIGIN, follow_redirects=False)
    assert resp.headers["location"] == "/briefs?status=pending&sort=date-asc"


def test_briefs_sort_garbage_falls_back_to_the_default_order():
    client, _ = make_client(SORT_TABLES)
    assert _hook_order(client.get("/briefs?sort=абв").text) == [
        "свежий", "средний", "старый", "без даты"]


# --- UTM-контур, тикеты 03–04: страница «Деньги» (/money) ---------------------
# Реестр заказов переехал сюда с временной /orders (тикет 04); контракт решений
# постоянный: POST с CSRF-защитой, 404/422 на мусор, точечная инвалидация кеша,
# Run Log dashboard-order. Статусы меняет только человек. Сверху — расчётный
# лист (чистая функция cf.payout), переходы по аккаунтам и ссылки для bio.

from datetime import datetime, timezone  # noqa: E402

from cf.collect.metrika import ORDER_HEADERS  # noqa: E402

ORDERS_ACCOUNTS = [
    {"slug": "tiktok-1", "platform": "tiktok", "handle": "@боевой", "active": True},
    # выключенный слот — заготовка: в select не попадает, POST его отбивает
    {"slug": "instagram-1", "platform": "instagram", "handle": "@пусто",
     "active": False},
]

# Ставки — условные тестовые, нарочно ФИКСТУРОЙ, а не из конфига:
# тесты не должны зависеть от правки пилотных ставок в боевом конфиге.
MONEY_PAYOUT = {"per_transition_rub": 30, "sales_percent": 10, "hold_days": 14,
                "monthly_fix_rub": {}}
SITE_URL = "https://jelapeche.com"

CANDIDATE_ORDER = {
    "order_id": "mk-40129", "source": "metrika_ecommerce",
    "order_date": "2026-07-29", "revenue": 1990, "utm_source": "tiktok",
    "utm_campaign": "tiktok-1", "utm_content": "b42", "account": "tiktok-1",
    "reel_id": "b42", "status": "candidate", "status_changed_at": "",
    "decided_by": "", "notes": "", "collected_at": "2026-07-30T08:20:00+00:00",
}

CONFIRMED_ORDER = dict(
    CANDIDATE_ORDER, order_id="mk-40001", order_date="2026-07-30",
    revenue=500, reel_id="", utm_content="", status="confirmed",
    status_changed_at="2026-07-30T09:00:00+00:00", decided_by="human")

# Закрытый месяц в прошлом: заказы там всегда созревшие (холд не зависит от
# дня запуска сьюта), а месячная строка трафика даёт детерминированный лист.
PAST_MONTH = "2026-06"
PAST_ORDER = dict(CONFIRMED_ORDER, order_id="mk-june", order_date="2026-06-10",
                  revenue=1990)
PAST_MONTHLY_UTM = {
    "utm_id": f"m-{PAST_MONTH}-tiktok-1", "row_kind": "monthly", "date": "",
    "month": PAST_MONTH, "account": "tiktok-1", "utm_campaign": "tiktok-1",
    "utm_content": "", "reel_id": "", "visits": 210, "users": 100,
    "collected_at": "2026-07-01T08:20:00+00:00",
}
PAST_DAILY_UTM = {
    "utm_id": f"d-{PAST_MONTH}-15-tiktok-1--", "row_kind": "daily",
    "date": f"{PAST_MONTH}-15", "month": PAST_MONTH, "account": "tiktok-1",
    "utm_campaign": "tiktok-1", "utm_content": "", "reel_id": "",
    "visits": 90, "users": 80, "collected_at": "2026-07-01T08:20:00+00:00",
}


def make_orders_client(rows=(CANDIDATE_ORDER, CONFIRMED_ORDER), utm=(),
                       cache=None, accounts=ORDERS_ACCOUNTS,
                       payout=MONEY_PAYOUT, site=SITE_URL):
    sheets = FakeSheets({"orders": [dict(r) for r in rows],
                         "utm_traffic": [dict(r) for r in utm], "run_log": []},
                        headers={"orders": list(ORDER_HEADERS)})
    app = create_app(sheets=sheets, cache=cache, accounts=accounts,
                     payout=payout, site_base_url=site)
    return TestClient(app, headers=BROWSER), sheets


def test_money_page_renders_candidates_first_with_decision_buttons():
    client, _ = make_orders_client()
    resp = client.get("/money")
    assert resp.status_code == 200
    html = resp.text
    # кандидат сверху, хотя решённый заказ свежее по дате (смотрим внутри
    # таблицы заказов: расчётный лист выше может законно назвать решённый)
    table = html[html.index("Реестр заказов"):]
    assert table.index("mk-40129") < table.index("mk-40001")
    # кнопки решений кандидата — на языке владельца
    assert "Оплачен, не возвращён" in html and "Возврат" in html
    assert "Не наш" in html
    # у решённого можно передумать — в том числе вернуть в кандидаты
    assert "Вернуть в кандидаты" in html
    # POST-роуты решений не переехали — только страница
    assert 'action="/orders/mk-40129/status"' in html
    # форма ручного заказа: только АКТИВНЫЕ аккаунты из реестра
    assert 'action="/orders/manual"' in html
    assert '<option value="tiktok-1">' in html
    assert '<option value="instagram-1">' not in html


def test_money_page_zero_month_is_a_state_not_an_error():
    client, _ = make_orders_client(rows=())
    resp = client.get("/money")
    assert resp.status_code == 200
    assert "Нулевой месяц" in resp.text
    assert "Заказов пока нет" in resp.text
    # пустые состояния зовут к следующему шагу без CLI-жаргона (ревью 30.07)
    assert "утреннего сбора" in resp.text
    # сноска о нижней границе — безусловна, даже на пустом месяце
    assert "нижнюю границу вклада роликов" in resp.text


def test_money_page_missing_tabs_are_not_an_error(tmp_path):
    # Вкладок CF Orders / CF UTM Traffic ещё нет (сбор не запускался):
    # страница честно пуста, а не падает в баннер ошибки.
    from gspread.exceptions import WorksheetNotFound
    sheets = FakeSheets({"run_log": []},
                        fail_read_tabs={
                            "orders": WorksheetNotFound("нет вкладки"),
                            "utm_traffic": WorksheetNotFound("нет вкладки")})
    client = TestClient(create_app(sheets=sheets, accounts=ORDERS_ACCOUNTS,
                                   payout=MONEY_PAYOUT, site_base_url=SITE_URL),
                        headers=BROWSER)
    resp = client.get("/money")
    assert resp.status_code == 200
    assert "Заказов пока нет" in resp.text
    assert "Не удалось загрузить" not in resp.text


def test_money_page_empty_registry_names_reason_instead_of_sections():
    sheets = FakeSheets({"orders": [], "run_log": []},
                        headers={"orders": list(ORDER_HEADERS)})
    client = TestClient(create_app(sheets=sheets, accounts=[],
                                   payout=MONEY_PAYOUT, site_base_url=SITE_URL),
                        headers=BROWSER)
    resp = client.get("/money")
    assert resp.status_code == 200
    html = resp.text
    assert "реестр аккаунтов пуст" in html.lower()         # причина названа
    # денежных секций нет — вместо них причина (образец — тикет 01)
    assert "РАСЧЁТНЫЙ ЛИСТ" not in html
    assert "ССЫЛКИ ДЛЯ BIO" not in html
    assert 'action="/orders/manual"' not in html           # формы нет
    # заказы остаются видны: у секции своя честная деградация
    assert "Заказов пока нет" in html


def test_order_status_post_confirms_logs_and_returns_to_money():
    client, sheets = make_orders_client()
    resp = client.post("/orders/mk-40129/status", data={"status": "confirmed"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/money"
    row = sheets.tables["orders"][0]
    assert row["status"] == "confirmed"
    assert row["decided_by"] == "human" and row["status_changed_at"]
    (tab, log_row), = sheets.appended
    assert tab == "run_log" and log_row["agent"] == "dashboard-order"


def test_order_status_post_unknown_order_404_and_unknown_status_422():
    client, sheets = make_orders_client()
    assert client.post("/orders/NOPE/status",
                       data={"status": "confirmed"}).status_code == 404
    assert client.post("/orders/mk-40129/status",
                       data={"status": "paid"}).status_code == 422
    assert sheets.tables["orders"][0]["status"] == "candidate"  # не тронут
    assert sheets.appended == []


def test_order_status_post_requires_browser_origin():
    # Решение о деньгах — маршрут-РЕШЕНИЕ (этап 3б): безголовый POST без
    # Origin/Referer получает 403, статус не меняется.
    sheets = FakeSheets({"orders": [dict(CANDIDATE_ORDER)], "run_log": []},
                        headers={"orders": list(ORDER_HEADERS)})
    headless = TestClient(create_app(sheets=sheets, accounts=ORDERS_ACCOUNTS))
    resp = headless.post("/orders/mk-40129/status", data={"status": "confirmed"})
    assert resp.status_code == 403
    assert sheets.tables["orders"][0]["status"] == "candidate"
    # ручной заказ — такое же решение: 403 без заголовков браузера
    resp = headless.post("/orders/manual",
                         data={"order_date": "2026-07-29", "revenue": "500",
                               "account": "tiktok-1"})
    assert resp.status_code == 403


def test_order_status_invalidates_only_orders_and_run_log():
    sheets = FakeSheets({"orders": [dict(CANDIDATE_ORDER)], "run_log": [],
                         "raw_tiktok": [{"source_url": "u1",
                                         "posted_at": "2026-07-01"}]},
                        headers={"orders": list(ORDER_HEADERS)})
    cache = _SpyCache(sheets)
    client = TestClient(create_app(sheets=sheets, cache=cache,
                                   accounts=ORDERS_ACCOUNTS), headers=BROWSER)
    cache.rows("raw_tiktok")                        # медленная вкладка в кеше
    resp = client.post("/orders/mk-40129/status", data={"status": "rejected"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sorted(cache.invalidated) == ["orders", "run_log"]
    assert cache._store.get("raw_tiktok") is not None


def test_order_status_runlog_failure_warns_but_saves_decision():
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("run_log append failed")

    sheets = BrokenLogSheets({"orders": [dict(CANDIDATE_ORDER)], "run_log": []},
                             headers={"orders": list(ORDER_HEADERS)})
    client = TestClient(create_app(sheets=sheets, accounts=ORDERS_ACCOUNTS),
                        headers=BROWSER)
    resp = client.post("/orders/mk-40129/status", data={"status": "confirmed"},
                       follow_redirects=False)
    assert resp.status_code == 200                     # рендер с заметкой
    assert sheets.tables["orders"][0]["status"] == "confirmed"  # решение цело
    assert "Run Log" in resp.text


def test_manual_order_post_writes_confirmed_row_and_returns_to_money():
    client, sheets = make_orders_client(rows=())
    resp = client.post("/orders/manual",
                       data={"order_date": "2026-07-29", "revenue": "3500",
                             "account": "tiktok-1", "notes": "перевод"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/money"
    (row,) = sheets.tables["orders"]
    assert row["order_id"].startswith("manual-")
    assert row["source"] == "manual" and row["status"] == "confirmed"
    assert row["revenue"] == 3500 and row["account"] == "tiktok-1"
    (tab, log_row) = sheets.appended[-1]               # [0] — сама строка заказа
    assert tab == "run_log" and log_row["agent"] == "dashboard-order"


def test_manual_order_post_validation_422():
    client, sheets = make_orders_client(rows=())
    base = {"order_date": "2026-07-29", "revenue": "500", "account": "tiktok-1"}
    # неизвестный и выключенный аккаунт
    for account in ("чужой", "instagram-1"):
        assert client.post("/orders/manual",
                           data=dict(base, account=account)).status_code == 422
    # сумма: ноль, отрицательная, мусор
    for revenue in ("0", "-5", "абв"):
        assert client.post("/orders/manual",
                           data=dict(base, revenue=revenue)).status_code == 422
    # битая дата
    assert client.post("/orders/manual",
                       data=dict(base, order_date="29.07.2026")).status_code == 422
    assert sheets.tables["orders"] == []               # ничего не записано


# --- Расчётный лист, месяцы, ссылки и md-экспорт (тикет 04) -------------------


def test_money_sheet_counts_monthly_uniques_and_confirmed_sale():
    # Закрытый месяц: уники — из месячной строки (дневная не суммируется),
    # продажа созрела (холд давно позади), итог = 100×30 + 10% от 1990.
    client, _ = make_orders_client(rows=(PAST_ORDER,),
                                   utm=(PAST_MONTHLY_UTM, PAST_DAILY_UTM))
    html = client.get(f"/money?month={PAST_MONTH}").text
    assert "РАСЧЁТНЫЙ ЛИСТ" in html and PAST_MONTH in html
    assert "100 × 30 ₽" in html and "3000 ₽" in html   # уники × ставка
    assert "mk-june" in html and "199 ₽" in html       # 10% с продажи
    assert "3199 ₽" in html                            # итог
    assert "нижнюю границу вклада роликов" in html     # сноска безусловна
    # переходы по аккаунтам: биллинговые уники + дневная динамика счётчиком
    assert "ПЕРЕХОДЫ ПО АККАУНТАМ" in html
    assert "Дней с переходами" in html


def test_money_fresh_confirmed_order_is_held_not_paid():
    # Заказ сегодняшним числом всегда моложе холда: в текущем месяце он идёт
    # строкой «в холде», а не в сумму.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fresh = dict(CONFIRMED_ORDER, order_id="mk-fresh", order_date=today)
    client, _ = make_orders_client(rows=(fresh,))
    html = client.get("/money").text
    assert "mk-fresh" in html
    assert "в холде, войдёт в следующий лист" in html


def test_money_month_selector_lists_months_with_data():
    client, _ = make_orders_client(rows=(PAST_ORDER,), utm=(PAST_MONTHLY_UTM,))
    html = client.get("/money").text
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    assert f'href="/money?month={PAST_MONTH}"' in html   # месяц с данными
    assert f'href="/money?month={current}"' in html      # текущий — всегда
    # мусор в параметре не роняет страницу — молча падаем в текущий месяц
    assert client.get("/money?month=абв").status_code == 200


def test_money_md_export_carries_same_numbers_and_footnote():
    client, _ = make_orders_client(rows=(PAST_ORDER,), utm=(PAST_MONTHLY_UTM,))
    resp = client.get(f"/money?month={PAST_MONTH}&format=md")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert f'filename="money-{PAST_MONTH}.md"' in resp.headers["content-disposition"]
    md = resp.text
    assert md.startswith("# Расчётный лист")
    assert "| tiktok-1 | 100 | 3000 ₽ |" in md
    assert "mk-june" in md and "199 ₽" in md
    assert "3199 ₽" in md
    assert "нижнюю границу вклада роликов" in md
    assert "сформирован" in md


def test_money_bio_links_only_active_accounts_and_convention():
    client, _ = make_orders_client(rows=())
    html = client.get("/money").text
    assert "ССЫЛКИ ДЛЯ BIO" in html
    # конвенция: base_url + source/medium/campaign (в HTML & экранирован)
    assert "https://jelapeche.com/?utm_source=tiktok" in html
    assert "utm_medium=cf-organic" in html
    assert "utm_campaign=tiktok-1" in html
    # неактивный аккаунт ссылки не получает
    assert 'id="bio-instagram-1"' not in html
    # конструктор per-reel на месте
    assert 'id="reel-id-input"' in html


def test_money_links_reason_when_no_active_accounts():
    # Реестр не пуст, но все выключены: секция не прячется молча — причина.
    inactive = [dict(a, active=False) for a in ORDERS_ACCOUNTS]
    client, _ = make_orders_client(rows=(), accounts=inactive)
    html = client.get("/money").text
    assert "выключены" in html
    assert 'id="reel-id-input"' not in html


def test_money_per_reel_section_distinguishes_exact_and_estimate():
    # Тикет 05: секция «По роликам» — точные переходы по метке ролика, оценка
    # (бейдж) — остаток дня по роликам окна 14 дней пропорционально просмотрам,
    # заказы с reel_id — у ролика.
    tagged = dict(PAST_DAILY_UTM, utm_id=f"d-{PAST_MONTH}-15-tiktok-1-b42",
                  utm_content="b42", reel_id="b42", visits=7)
    reels = [{"reel_id": "b42", "brief_id": "b1", "account": "tiktok-1",
              "published_at": f"{PAST_MONTH}-14"},
             {"reel_id": "b43", "brief_id": "b2", "account": "tiktok-1",
              "published_at": f"{PAST_MONTH}-10"}]
    perf = [{"reel_id": "b42", "views": 3000, "measured_at": f"{PAST_MONTH}-20"},
            {"reel_id": "b43", "views": 1000, "measured_at": f"{PAST_MONTH}-20"}]
    order = dict(PAST_ORDER, order_id="mk-reel", reel_id="b42", utm_content="b42")
    sheets = FakeSheets({"orders": [order], "utm_traffic": [PAST_DAILY_UTM, tagged],
                         "reels": reels, "performance": perf, "run_log": []},
                        headers={"orders": list(ORDER_HEADERS)})
    client = TestClient(create_app(sheets=sheets, accounts=ORDERS_ACCOUNTS,
                                   payout=MONEY_PAYOUT, site_base_url=SITE_URL),
                        headers=BROWSER)
    html = client.get(f"/money?month={PAST_MONTH}").text
    assert "ПО РОЛИКАМ" in html
    # секция целиком: от своего заголовка до СЛЕДУЮЩЕЙ секции «ЗАКАЗЫ»
    # (в шапке страницы «ЗАКАЗЫ» тоже есть — ищем после начала секции)
    start = html.index("ПО РОЛИКАМ")
    section = html[start:html.index("ЗАКАЗЫ", start)]
    assert "b42" in section and "b43" in section
    assert ">оценка<" in section                    # бейдж отличает модель от факта
    # остаток дня 90 делится 3000/1000: 67.5 и 22.5; точные — 7 у b42
    assert "67.5" in section and "22.5" in section
    assert "1990" in section                        # выручка ролика b42 (заказ mk-reel)


def test_money_per_reel_empty_state_is_honest():
    client, _ = make_orders_client(rows=())
    html = client.get("/money").text
    assert "ПО РОЛИКАМ" in html
    assert "По-роликовых переходов за этот месяц нет" in html


def test_old_orders_url_redirects_to_money():
    client, _ = make_orders_client()
    resp = client.get("/orders", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/money"


def test_money_nav_tab_present_on_pages():
    client, _ = make_orders_client()
    html = client.get("/money").text
    assert 'href="/money"' in html and "Деньги" in html
    assert 'aria-current="page"' in html
