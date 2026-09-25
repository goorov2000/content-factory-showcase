# CF Dashboard — этап 2 (Перформанс, Запуски, Лаборатория, Источники)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (merge d3362a6, 2026-07-14). Чекбоксы в ходе исполнения не велись.

**Goal:** Заменить четыре заглушки дашборда (`/performance`, `/runs`, `/lab`, `/sources`) на живые разделы по спеке `docs/superpowers/specs/2026-07-14-cf-dashboard-design.md`.

**Architecture:** Новый модуль `sections.py` с чистыми функциями-контекстами поверх существующего `DataCache` (Sheets) и файлов репозитория (Лаборатория читает `formulas/`, `agent-runtime/patterns/`, `proposals/`, `agent-runtime/evals/`). Четыре Jinja-шаблона из уже существующих паттернов (таблица `.queue`, чипы, теги, бейджи, моно-цифры) — отдельных макетов по спеке не нужно. Лаборатория — строго read-only.

**Tech Stack:** FastAPI + Jinja2, pytest + `tests/fakes.py` (FakeSheets), существующий `static/style.css` на токенах DESIGN.md.

**Вне охвата** (осталось в бэклоге этапа 2 из финального ревью MVP, делается отдельно): бейдж-счётчик «Брифы» в сайдбаре, чистка `config` у `create_app`, объединение `_status`/`_brief_status`, no-autostart у HealthMonitor, чат с Клодом (этап 3).

## Данные (проверено по коду и cf.config.json)

| Раздел | Источник | Поля |
|---|---|---|
| Источники | вкладки `raw_tiktok`, `raw_instagram` | `source_url, account, views, likes, comments, saves, posted_at, niche, hook_text` |
| Перформанс·Рилсы | вкладка `reels` | `reel_id, brief_id, platform, post_url, published_at, production_notes` |
| Перформанс·Метрики | вкладка `performance` | `reel_id, brief_id, prompt_version, views, er, measured_at` |
| Запуски | вкладка `run_log` | `run_id, agent, trigger_type, started_at, completed_at, status, input_summary, errors` (errors — JSON-список строк; статусы `success/failed/insufficient_data` из `cf/runlog.py`) |
| Лаборатория | вкладка `prompt_versions` | `prompt_id, version, github_path, active (TRUE/FALSE), activated_at, changelog` |
| Лаборатория | `formulas/_approved/index.json` → файлы формул | index: `name, niche, path, version, approved_at`; формула: `confidence, conditions` |
| Лаборатория | `agent-runtime/patterns/*.json` | `meta{niche, source_tab, generated_at}`, `patterns[]{pattern_id, description, confidence, evidence{source_urls, avg_views, avg_er}}` |
| Лаборатория | `proposals/*.md` | YAML-шапка `status/prompt_id/created` + первый `# заголовок` |
| Лаборатория | `agent-runtime/evals/*-weekly-eval.json` (свежайший) | `period, by_prompt_version, success_criteria, insights` |

## File Structure

- Create: `src/cf/dashboard/sections.py` — контексты четырёх разделов (чистые функции, файловый I/O только для Лаборатории, корень инъецируется для тестов)
- Create: `src/cf/dashboard/templates/sources.html`, `performance.html`, `runs.html`, `lab.html`
- Modify: `src/cf/dashboard/app.py` — реальные роуты вместо `STUB_SECTIONS`; параметр `lab_root` у `create_app`
- Modify: `src/cf/dashboard/static/style.css` — `.num` (правое выравнивание цифр), `.badge.success/.failed/.insufficient`, `.run-error`
- Delete (в финальной задаче): `src/cf/dashboard/templates/stub.html`
- Test: `tests/test_dashboard_sections.py` (новый); правки `tests/test_dashboard_routes.py::test_stub_pages_render` по мере замены заглушек

Общие соглашения (как в MVP): роут ловит `Exception` от Sheets → рендер страницы с `error`; плашка `stale` из кеша; пустые данные → честный empty-state; сортировка ISO-дат строкой (как в `app.py` для run_log).

---

### Task 1: sections.py — sources_context и performance_context

**Files:**
- Create: `src/cf/dashboard/sections.py`
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Создать `tests/test_dashboard_sections.py`:

```python
from cf.dashboard.data import DataCache
from cf.dashboard.sections import performance_context, sources_context
from tests.fakes import FakeSheets


def make_cache(tables):
    return DataCache(FakeSheets(tables))


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cf.dashboard.sections'`

- [ ] **Step 3: Write minimal implementation**

Создать `src/cf/dashboard/sections.py`:

```python
"""Контексты разделов этапа 2: Источники, Перформанс, Запуски, Лаборатория.

Чистые функции поверх DataCache; Лаборатория дополнительно читает файлы
репозитория (read-only: промпты и формулы меняются только через git-ревью).
"""

SOURCES_LIMIT = 200
RUNS_LIMIT = 100


def _sort_desc(rows, field):
    return sorted(rows, key=lambda r: str(r.get(field, "")), reverse=True)


def sources_context(cache, tab):
    if tab not in ("tiktok", "instagram"):
        tab = "tiktok"
    counts = {"tiktok": len(cache.rows("raw_tiktok")),
              "instagram": len(cache.rows("raw_instagram"))}
    rows = _sort_desc(cache.rows(f"raw_{tab}"), "posted_at")
    return {"tab": tab, "counts": counts,
            "rows": rows[:SOURCES_LIMIT], "total": counts[tab]}


def performance_context(cache, tab):
    if tab not in ("reels", "metrics"):
        tab = "reels"
    reels = cache.rows("reels")
    perf = cache.rows("performance")
    if tab == "reels":
        rows = _sort_desc(reels, "published_at")
    else:
        rows = _sort_desc(perf, "measured_at")
    return {"tab": tab, "counts": {"reels": len(reels), "metrics": len(perf)},
            "rows": rows}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/sections.py tests/test_dashboard_sections.py
git commit -m "feat(dashboard): контексты Источников и Перформанса (sections.py)"
```

---

### Task 2: роут и шаблон «Источники»

**Files:**
- Create: `src/cf/dashboard/templates/sources.html`
- Modify: `src/cf/dashboard/app.py` (роут `/sources`, убрать `"sources"` из `STUB_SECTIONS`)
- Modify: `src/cf/dashboard/static/style.css` (класс `.num`)
- Modify: `tests/test_dashboard_routes.py:22-27` (`test_stub_pages_render` — убрать `/sources`)
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py` (импорты — в шапку файла):

```python
from fastapi.testclient import TestClient

from cf.dashboard.app import create_app


def make_client(tables=None, **kwargs):
    sheets = FakeSheets(tables or {})
    return TestClient(create_app(sheets=sheets, **kwargs))


class BrokenSheets(FakeSheets):
    def read_rows(self, tab_key):
        raise ConnectionError("sheets down")


def test_sources_page_renders_table_and_tabs():
    client = make_client(RAW_TABLES)
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "ИСТОЧНИКИ" in resp.text
    assert "хук два" in resp.text                      # строки TikTok по умолчанию
    assert 'href="https://t/2"' in resp.text            # ссылка на источник
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
    assert "пока нет raw-видео" in resp.text


def test_sources_page_error_branch():
    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "Не удалось прочитать Google Sheets" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — на `/sources` рендерится stub.html («этап 2»), «ИСТОЧНИКИ» в нём нет

- [ ] **Step 3: Implement route + template + CSS**

В `src/cf/dashboard/app.py`: добавить импорт после строки `from cf.dashboard.data import ...`:

```python
from cf.dashboard.sections import performance_context, sources_context
```

Убрать `"sources": "Источники",` из `STUB_SECTIONS` и добавить роут (рядом с роутом `/briefs`):

```python
    @app.get("/sources")
    def sources(request: Request, tab: str = "tiktok"):
        try:
            ctx, error = sources_context(cache, tab), None
        except Exception as exc:
            ctx, error = {"tab": "tiktok", "counts": {}, "rows": [], "total": 0}, str(exc)
        return templates.TemplateResponse(request, "sources.html", {
            "title": "Источники", "active": "sources", "stale": cache.stale,
            "error": error, "health": health, **ctx})
```

Создать `src/cf/dashboard/templates/sources.html`:

```html
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div>
    <div class="eyebrow">CF RAW · РЕФЕРЕНСЫ И МЕТРИКИ</div>
    <h1>ИСТОЧНИКИ</h1>
  </div>
  <nav class="chips">
    {% for key, label in [("tiktok","TikTok"),("instagram","Instagram")] %}
    <a class="chip {{ 'active' if tab == key }}" href="/sources?tab={{ key }}">
      {{ label }}{% if counts.get(key) is not none %} · {{ counts[key] }}{% endif %}</a>
    {% endfor %}
  </nav>
</header>

{% if stale %}<div class="banner warn">Sheets недоступен — показаны данные из кеша.</div>{% endif %}
{% if error %}<div class="empty-state">Не удалось прочитать Google Sheets: {{ error }}</div>
{% elif not rows %}<div class="empty-state">В этой ленте пока нет raw-видео.</div>
{% else %}
<div class="card" style="padding: 0; overflow: hidden;">
  <table class="queue">
    <thead><tr><th>Аккаунт</th><th>Хук</th><th>Ниша</th>
      <th class="num">Views</th><th class="num">Likes</th><th class="num">Comm</th><th class="num">Saves</th>
      <th>Дата</th><th></th></tr></thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td class="mono">{{ r.account }}</td>
        <td>{{ (r.hook_text or "—") | truncate(80) }}</td>
        <td><span class="tag">{{ r.niche or "—" }}</span></td>
        <td class="mono num">{{ r.views }}</td>
        <td class="mono num">{{ r.likes }}</td>
        <td class="mono num">{{ r.comments }}</td>
        <td class="mono num">{{ r.saves }}</td>
        <td class="mono">{{ (r.posted_at or "")[:10] }}</td>
        <td>{% if (r.source_url or "").startswith("http") %}<a href="{{ r.source_url }}" target="_blank" rel="noopener">↗</a>{% endif %}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% if total > rows | length %}<div class="empty-state">Показаны последние {{ rows | length }} из {{ total }}.</div>{% endif %}
{% endif %}
{% endblock %}
```

В `src/cf/dashboard/static/style.css` добавить в конец:

```css
/* Разделы этапа 2 */
.queue th.num, .queue td.num { text-align: right; }
```

В `tests/test_dashboard_routes.py` обновить `test_stub_pages_render`:

```python
def test_stub_pages_render():
    client, _ = make_client()
    for path in ("/performance", "/runs", "/lab"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "этап 2" in resp.text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py tests/test_dashboard_routes.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add -A src/cf/dashboard tests
git commit -m "feat(dashboard): раздел «Источники» — raw-ленты TikTok/Instagram"
```

---

### Task 3: роут и шаблон «Перформанс»

**Files:**
- Create: `src/cf/dashboard/templates/performance.html`
- Modify: `src/cf/dashboard/app.py` (роут `/performance`, убрать из `STUB_SECTIONS`)
- Modify: `tests/test_dashboard_routes.py` (`test_stub_pages_render` — убрать `/performance`)
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py`:

```python
def test_performance_page_reels_tab_default():
    client = make_client(PERF_TABLES)
    resp = client.get("/performance")
    assert resp.status_code == 200
    assert "ПЕРФОРМАНС" in resp.text
    assert "R2" in resp.text and "без плашки" in resp.text
    assert 'href="https://i/p2"' in resp.text
    assert 'href="/performance?tab=metrics"' in resp.text


def test_performance_page_metrics_tab():
    client = make_client(PERF_TABLES)
    resp = client.get("/performance?tab=metrics")
    assert resp.status_code == 200
    assert "4.2" in resp.text and "v1" in resp.text
    assert "без плашки" not in resp.text


def test_performance_page_empty_state():
    client = make_client({"reels": [], "performance": []})
    resp = client.get("/performance")
    assert resp.status_code == 200
    assert "рилсы ещё не публиковались" in resp.text
    resp = client.get("/performance?tab=metrics")
    assert "метрики ещё не собирались" in resp.text


def test_performance_page_error_branch():
    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/performance")
    assert resp.status_code == 200
    assert "Не удалось прочитать Google Sheets" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — на `/performance` рендерится stub

- [ ] **Step 3: Implement route + template**

В `app.py`: убрать `"performance": "Перформанс",` из `STUB_SECTIONS`, добавить роут:

```python
    @app.get("/performance")
    def performance(request: Request, tab: str = "reels"):
        try:
            ctx, error = performance_context(cache, tab), None
        except Exception as exc:
            ctx, error = {"tab": "reels", "counts": {}, "rows": []}, str(exc)
        return templates.TemplateResponse(request, "performance.html", {
            "title": "Перформанс", "active": "performance", "stale": cache.stale,
            "error": error, "health": health, **ctx})
```

Создать `src/cf/dashboard/templates/performance.html`:

```html
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div>
    <div class="eyebrow">CF PUBLISHED REELS · CF PERFORMANCE</div>
    <h1>ПЕРФОРМАНС</h1>
  </div>
  <nav class="chips">
    {% for key, label in [("reels","Рилсы"),("metrics","Метрики")] %}
    <a class="chip {{ 'active' if tab == key }}" href="/performance?tab={{ key }}">
      {{ label }}{% if counts.get(key) is not none %} · {{ counts[key] }}{% endif %}</a>
    {% endfor %}
  </nav>
</header>

{% if stale %}<div class="banner warn">Sheets недоступен — показаны данные из кеша.</div>{% endif %}
{% if error %}<div class="empty-state">Не удалось прочитать Google Sheets: {{ error }}</div>
{% elif not rows %}<div class="empty-state">Недостаточно данных — {{ "рилсы ещё не публиковались" if tab == "reels" else "метрики ещё не собирались" }}.</div>
{% else %}
<div class="card" style="padding: 0; overflow: hidden;">
  {% if tab == "reels" %}
  <table class="queue">
    <thead><tr><th>ID</th><th>Бриф</th><th>Платформа</th><th>Опубликован</th><th>Заметки продакшена</th><th></th></tr></thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td class="mono">{{ r.reel_id }}</td>
        <td class="mono">{{ r.brief_id or "—" }}</td>
        <td><span class="tag">{{ r.platform or "—" }}</span></td>
        <td class="mono">{{ (r.published_at or "")[:10] }}</td>
        <td>{{ (r.production_notes or "—") | truncate(60) }}</td>
        <td>{% if (r.post_url or "").startswith("http") %}<a href="{{ r.post_url }}" target="_blank" rel="noopener">↗</a>{% endif %}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <table class="queue">
    <thead><tr><th>Рилс</th><th>Бриф</th><th>Промпт</th><th class="num">Views</th><th class="num">ER</th><th>Замер</th></tr></thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td class="mono">{{ r.reel_id }}</td>
        <td class="mono">{{ r.brief_id or "—" }}</td>
        <td class="mono">{{ r.prompt_version or "—" }}</td>
        <td class="mono num">{{ r.views }}</td>
        <td class="mono num">{{ r.er }}</td>
        <td class="mono">{{ (r.measured_at or "")[:10] }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
{% endif %}
{% endblock %}
```

В `tests/test_dashboard_routes.py::test_stub_pages_render` оставить пути `("/runs", "/lab")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py tests/test_dashboard_routes.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add -A src/cf/dashboard tests
git commit -m "feat(dashboard): раздел «Перформанс» — вкладки Рилсы и Метрики"
```

---

### Task 4: runs_context — журнал запусков

**Files:**
- Modify: `src/cf/dashboard/sections.py`
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py` (импорт `runs_context` — в шапку):

```python
from cf.dashboard.sections import runs_context


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — `ImportError: cannot import name 'runs_context'`

- [ ] **Step 3: Implement**

В `src/cf/dashboard/sections.py` добавить (`import json` — в шапку файла):

```python
import json

RUN_BADGES = {"success": ("success", "Успех"),
              "failed": ("failed", "Ошибка"),
              "insufficient_data": ("insufficient", "Мало данных")}


def _errors_list(value):
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return [text]
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [str(parsed)] if str(parsed).strip() else []


def runs_context(cache):
    all_runs = cache.rows("run_log")
    runs = []
    for r in _sort_desc(all_runs, "completed_at")[:RUNS_LIMIT]:
        row = dict(r)
        status = str(r.get("status", "")).strip().lower()
        row["badge_class"], row["badge_label"] = RUN_BADGES.get(
            status, ("insufficient", status or "—"))
        row["errors_list"] = _errors_list(r.get("errors"))
        runs.append(row)
    return {"runs": runs, "total": len(all_runs)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/sections.py tests/test_dashboard_sections.py
git commit -m "feat(dashboard): runs_context — журнал CF Run Log"
```

---

### Task 5: роут и шаблон «Запуски»

**Files:**
- Create: `src/cf/dashboard/templates/runs.html`
- Modify: `src/cf/dashboard/app.py` (роут `/runs`, убрать из `STUB_SECTIONS`, импорт `runs_context`)
- Modify: `src/cf/dashboard/static/style.css` (бейджи статусов, `.run-error`)
- Modify: `tests/test_dashboard_routes.py` (`test_stub_pages_render` — остаётся только `/lab`)
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py`:

```python
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
    assert "ЗАПУСКИ" in resp.text
    assert "cf-analyze" in resp.text and "cf-eval" in resp.text
    assert "Успех" in resp.text and "Ошибка" in resp.text
    assert "sheets timeout" in resp.text
    assert "2026-07-14 12:00" in resp.text


def test_runs_page_empty_state():
    client = make_client({"run_log": []})
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Запусков ещё не было" in resp.text


def test_runs_page_error_branch():
    client = TestClient(create_app(sheets=BrokenSheets({})))
    resp = client.get("/runs")
    assert resp.status_code == 200
    assert "Не удалось прочитать Google Sheets" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — на `/runs` рендерится stub

- [ ] **Step 3: Implement route + template + CSS**

В `app.py`: расширить импорт до `from cf.dashboard.sections import performance_context, runs_context, sources_context`, убрать `"runs": "Запуски",` из `STUB_SECTIONS`, добавить роут:

```python
    @app.get("/runs")
    def runs(request: Request):
        try:
            ctx, error = runs_context(cache), None
        except Exception as exc:
            ctx, error = {"runs": [], "total": 0}, str(exc)
        return templates.TemplateResponse(request, "runs.html", {
            "title": "Запуски", "active": "runs", "stale": cache.stale,
            "error": error, "health": health, **ctx})
```

Создать `src/cf/dashboard/templates/runs.html`:

```html
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div>
    <div class="eyebrow">CF RUN LOG · ЖУРНАЛ АГЕНТОВ</div>
    <h1>ЗАПУСКИ</h1>
  </div>
</header>

{% if stale %}<div class="banner warn">Sheets недоступен — показаны данные из кеша.</div>{% endif %}
{% if error %}<div class="empty-state">Не удалось прочитать Google Sheets: {{ error }}</div>
{% elif not runs %}<div class="empty-state">Запусков ещё не было.</div>
{% else %}
<div class="card" style="padding: 0; overflow: hidden;">
  <table class="queue">
    <thead><tr><th>Агент</th><th>Триггер</th><th>Команда / вход</th><th>Статус</th><th>Завершён</th></tr></thead>
    <tbody>
      {% for run in runs %}
      <tr>
        <td class="mono">{{ run.agent }}</td>
        <td><span class="tag">{{ run.trigger_type or "—" }}</span></td>
        <td>{{ (run.input_summary or "—") | truncate(70) }}
          {% for err in run.errors_list %}<div class="run-error">{{ err }}</div>{% endfor %}</td>
        <td><span class="badge {{ run.badge_class }}">{{ run.badge_label }}</span></td>
        <td class="mono">{{ (run.completed_at or "")[:16] | replace("T", " ") }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% if total > runs | length %}<div class="empty-state">Показаны последние {{ runs | length }} из {{ total }}.</div>{% endif %}
{% endif %}
{% endblock %}
```

В `style.css` дописать после `.queue th.num, ...`:

```css
.badge.success { background: var(--success-soft); color: var(--success-text); }
.badge.failed { background: var(--error-soft); color: var(--error-text); }
.badge.insufficient { background: var(--warning-soft); color: var(--warning-text); }
.run-error { font-size: 11px; color: var(--error-text); margin-top: 4px; }
```

В `tests/test_dashboard_routes.py::test_stub_pages_render` оставить путь `("/lab",)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py tests/test_dashboard_routes.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add -A src/cf/dashboard tests
git commit -m "feat(dashboard): раздел «Запуски» — журнал CF Run Log"
```

---

### Task 6: lab-читалки файлов (frontmatter, JSON)

**Files:**
- Modify: `src/cf/dashboard/sections.py`
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py`:

```python
from cf.dashboard.sections import read_frontmatter


def test_read_frontmatter_parses_simple_yaml():
    text = ("---\nstatus: proposed\nprompt_id: brief-x\ncreated: 2026-07-14\n---\n"
            "# Заголовок\nтело")
    assert read_frontmatter(text) == {
        "status": "proposed", "prompt_id": "brief-x", "created": "2026-07-14"}


def test_read_frontmatter_without_header_or_empty():
    assert read_frontmatter("# Просто файл") == {}
    assert read_frontmatter("") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — `ImportError: cannot import name 'read_frontmatter'`

- [ ] **Step 3: Implement**

В `src/cf/dashboard/sections.py` (`from pathlib import Path` — в шапку файла):

```python
from pathlib import Path


def read_frontmatter(text):
    """Мини-парсер YAML-шапки proposal-файла: только строки «ключ: значение»."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


def _first_heading(text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/sections.py tests/test_dashboard_sections.py
git commit -m "feat(dashboard): читалки файлов Лаборатории (frontmatter, JSON)"
```

---

### Task 7: lab_context — версии, формулы, паттерны, proposals, eval

**Files:**
- Modify: `src/cf/dashboard/sections.py`
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py` (`import json` и `lab_context` — в шапку):

```python
import json

from cf.dashboard.sections import lab_context


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


def test_lab_context_reads_versions_files_and_patterns(tmp_path):
    ctx = lab_context(make_cache(LAB_VERSIONS), root=make_lab_root(tmp_path))
    assert ctx["versions"][0]["version"] == "v2"          # активная — первой
    assert ctx["versions"][0]["is_active"] is True
    assert ctx["versions"][1]["is_active"] is False
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
    from cf.dashboard.data import DataCache
    ctx = lab_context(DataCache(BrokenSheets({})), root=make_lab_root(tmp_path))
    assert ctx["versions"] is None                       # карточка покажет ошибку
    assert ctx["formulas"]                               # файлы прочитаны


def test_lab_context_empty_root(tmp_path):
    ctx = lab_context(make_cache(LAB_VERSIONS), root=tmp_path)
    assert ctx["formulas"] == [] and ctx["patterns"] == []
    assert ctx["proposals"] == [] and ctx["eval_report"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — `ImportError: cannot import name 'lab_context'`

- [ ] **Step 3: Implement**

В `src/cf/dashboard/sections.py`:

```python
def _is_active(row):
    return str(row.get("active", "")).strip().upper() in ("TRUE", "1", "YES")


def lab_context(cache, root="."):
    root = Path(root)

    try:
        versions = sorted(
            (dict(v, is_active=_is_active(v)) for v in cache.rows("prompt_versions")),
            key=lambda v: (v["is_active"], str(v.get("activated_at", ""))),
            reverse=True)
    except Exception:
        versions = None

    formulas = []
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    for entry in index.get("approved", []):
        detail = _load_json(root / str(entry.get("path", ""))) or {}
        formulas.append({
            "name": entry.get("name", ""),
            "niche": entry.get("niche", ""),
            "version": entry.get("version", ""),
            "approved_at": str(entry.get("approved_at", ""))[:10],
            "confidence": detail.get("confidence", ""),
            "conditions": detail.get("conditions", ""),
            "path": entry.get("path", ""),
        })

    patterns = []
    patterns_dir = root / "agent-runtime" / "patterns"
    if patterns_dir.is_dir():
        for path in sorted(patterns_dir.glob("*.json"), reverse=True):
            data = _load_json(path)
            if not data:
                continue
            meta = data.get("meta", {}) or {}
            items = []
            for p in data.get("patterns", []) or []:
                ev = p.get("evidence", {}) or {}
                items.append({
                    "pattern_id": p.get("pattern_id", ""),
                    "description": p.get("description", ""),
                    "confidence": p.get("confidence", ""),
                    "avg_views": ev.get("avg_views"),
                    "avg_er": ev.get("avg_er"),
                    "urls": len(ev.get("source_urls") or []),
                })
            patterns.append({
                "file": path.name,
                "niche": meta.get("niche", ""),
                "source_tab": meta.get("source_tab", ""),
                "generated_at": str(meta.get("generated_at", ""))[:10],
                "items": items,
            })

    proposals = []
    proposals_dir = root / "proposals"
    if proposals_dir.is_dir():
        for path in sorted(proposals_dir.glob("*.md"), reverse=True):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = read_frontmatter(text)
            proposals.append({
                "file": path.name,
                "title": _first_heading(text) or path.stem,
                "status": meta.get("status", "proposed"),
                "prompt_id": meta.get("prompt_id", ""),
                "created": meta.get("created", ""),
            })
    proposals.sort(key=lambda p: p["status"] != "proposed")  # proposed — первыми

    eval_report = None
    evals_dir = root / "agent-runtime" / "evals"
    if evals_dir.is_dir():
        reports = sorted(evals_dir.glob("*-weekly-eval.json"), reverse=True)
        if reports:
            data = _load_json(reports[0])
            if data:
                eval_report = {
                    "file": reports[0].name,
                    "period": data.get("period", {}) or {},
                    "by_prompt_version": data.get("by_prompt_version", {}) or {},
                    "success_criteria": data.get("success_criteria", {}) or {},
                    "insights": data.get("insights", []) or [],
                }

    return {"versions": versions, "formulas": formulas, "patterns": patterns,
            "proposals": proposals, "eval_report": eval_report}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/sections.py tests/test_dashboard_sections.py
git commit -m "feat(dashboard): lab_context — версии, формулы, паттерны, proposals, eval"
```

---

### Task 8: роут и шаблон «Лаборатория»

**Files:**
- Create: `src/cf/dashboard/templates/lab.html`
- Modify: `src/cf/dashboard/app.py` (роут `/lab`, параметр `lab_root`, убрать `STUB_SECTIONS` не полностью — к этому моменту в нём остался только `lab`)
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_dashboard_sections.py`:

```python
def test_lab_page_renders_all_cards(tmp_path):
    sheets = FakeSheets(LAB_VERSIONS)
    client = TestClient(create_app(sheets=sheets, lab_root=make_lab_root(tmp_path)))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "ЛАБОРАТОРИЯ" in resp.text
    assert "READ-ONLY" in resp.text                     # железное правило CF
    assert "brief-x" in resp.text and "v2" in resp.text  # версии промптов
    assert "f-one" in resp.text                          # формулы
    assert "p-1" in resp.text and "хук с идеей" in resp.text  # паттерны с evidence
    assert "Правка visual_direction" in resp.text        # proposals
    assert "Eval ещё не запускался" in resp.text         # empty-state eval


def test_lab_page_sheets_down_still_shows_files(tmp_path):
    client = TestClient(create_app(sheets=BrokenSheets({}),
                                   lab_root=make_lab_root(tmp_path)))
    resp = client.get("/lab")
    assert resp.status_code == 200
    assert "версии промптов не прочитаны" in resp.text
    assert "f-one" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: FAIL — `create_app` не знает `lab_root` (TypeError)

- [ ] **Step 3: Implement route + template**

В `app.py`:
- сигнатура: `def create_app(sheets=None, cache=None, runner=None, health=None, config=None, lab_root=None):`
- после `app.state.health = health` добавить: `lab_root = Path(lab_root or ".")`
- расширить импорт: `from cf.dashboard.sections import (lab_context, performance_context, runs_context, sources_context)`
- убрать `"lab": "Лаборатория",` из `STUB_SECTIONS` (останется пустой dict — цикл в конце просто не создаст роутов; окончательно уберём в Task 9) и добавить роут:

```python
    @app.get("/lab")
    def lab(request: Request):
        ctx = lab_context(cache, root=lab_root)
        return templates.TemplateResponse(request, "lab.html", {
            "title": "Лаборатория", "active": "lab", "stale": cache.stale,
            "health": health, **ctx})
```

(`lab_context` сам переживает недоступный Sheets — `versions is None`; файловые блоки защищены внутри.)

Создать `src/cf/dashboard/templates/lab.html`:

```html
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div>
    <div class="eyebrow">ПАТТЕРНЫ · ФОРМУЛЫ · ПРОМПТЫ · EVAL</div>
    <h1>ЛАБОРАТОРИЯ</h1>
  </div>
  <span class="tag">READ-ONLY · ПРАВКИ ТОЛЬКО ЧЕРЕЗ GIT-РЕВЬЮ</span>
</header>

{% if stale %}<div class="banner warn">Sheets недоступен — показаны данные из кеша.</div>{% endif %}

<div class="two-col">
  <section class="card">
    <div class="card-head"><div><div class="card-title">Версии промптов</div>
      <div class="card-sub">CF Prompt Versions</div></div></div>
    {% if versions is none %}<div class="empty-state">Sheets недоступен — версии промптов не прочитаны.</div>
    {% else %}
    {% for v in versions %}
    <div class="row">
      <span class="dot {{ 'ok' if v.is_active else 'warn' }}"></span>
      <span class="mono">{{ v.prompt_id }} · {{ v.version }}</span>
      {% if v.is_active %}<span class="badge approved">Активна</span>{% endif %}
      <span class="row-meta mono">{{ (v.activated_at or "")[:10] }}</span>
    </div>
    {% else %}<div class="empty-state">Версий промптов ещё нет.</div>{% endfor %}
    {% endif %}
  </section>

  <section class="card">
    <div class="card-head"><div><div class="card-title">Формулы</div>
      <div class="card-sub">formulas/_approved · утверждены оператором</div></div></div>
    {% for f in formulas %}
    <div class="row">
      <span class="mono">{{ f.name }} · v{{ f.version }}</span>
      <span class="tag">{{ f.niche }}</span>
      {% if f.confidence %}<span class="badge {{ 'success' if f.confidence == 'high' else 'pending' }}">{{ f.confidence }}</span>{% endif %}
      <span class="row-meta mono">{{ f.approved_at }}</span>
    </div>
    {% else %}<div class="empty-state">Утверждённых формул пока нет.</div>{% endfor %}
  </section>
</div>

<section class="card">
  <div class="card-head"><div><div class="card-title">Паттерны</div>
    <div class="card-sub">agent-runtime/patterns · каждый с evidence</div></div></div>
  {% for batch in patterns %}
  <div class="field-label" style="margin-top: 12px;">{{ batch.file }} · {{ batch.niche }} · {{ batch.source_tab }} · {{ batch.generated_at }}</div>
  {% for p in batch.items %}
  <div class="row">
    <span class="mono">{{ p.pattern_id }}</span>
    <span style="flex: 1; font-size: 12.5px;">{{ p.description | truncate(140) }}</span>
    <span class="badge {{ 'success' if p.confidence == 'high' else 'pending' }}">{{ p.confidence }}</span>
    <span class="row-meta mono">{{ p.urls }} url · views {{ p.avg_views if p.avg_views is not none else "—" }} · er {{ p.avg_er if p.avg_er is not none else "—" }}</span>
  </div>
  {% endfor %}
  {% else %}<div class="empty-state">Паттернов пока нет — их создаёт анализ батча (/cf-analyze).</div>{% endfor %}
</section>

<div class="two-col">
  <section class="card">
    <div class="card-head"><div><div class="card-title">Proposals</div>
      <div class="card-sub">очередь изменений промптов · применяются только через git</div></div></div>
    {% for p in proposals %}
    <div class="row">
      <span class="badge {{ {'proposed': 'pending', 'approved': 'approved', 'rejected': 'rejected'}.get(p.status, 'pending') }}">{{ p.status }}</span>
      <span style="flex: 1; font-size: 12.5px;">{{ p.title | truncate(80) }}</span>
      <span class="row-meta mono">{{ p.created }}</span>
    </div>
    {% else %}<div class="empty-state">Proposals пока нет.</div>{% endfor %}
  </section>

  <section class="card">
    <div class="card-head"><div><div class="card-title">Последний eval</div>
      <div class="card-sub">agent-runtime/evals · еженедельная оценка</div></div></div>
    {% if eval_report %}
    <div class="field-label">{{ eval_report.file }}{% if eval_report.period %} · {{ eval_report.period.get("since", "") }} — {{ eval_report.period.get("until", "") }}{% endif %}</div>
    {% for version, agg in eval_report.by_prompt_version.items() %}
    <div class="row"><span class="mono">{{ version }}</span>
      <span class="row-meta mono">{{ agg.get("reels", "—") }} рилсов · views {{ agg.get("avg_views", "—") }} · er {{ agg.get("avg_er", "—") }}</span></div>
    {% endfor %}
    {% for insight in eval_report.insights %}
    <div class="row" style="font-size: 12.5px;">{{ insight }}</div>
    {% endfor %}
    {% else %}<div class="empty-state">Eval ещё не запускался — отчёт появится после первой недельной оценки.</div>{% endif %}
  </section>
</div>
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_sections.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add -A src/cf/dashboard tests
git commit -m "feat(dashboard): раздел «Лаборатория» — read-only витрина глубины"
```

---

### Task 9: снести заглушки и прогнать всё

**Files:**
- Modify: `src/cf/dashboard/app.py` (удалить `STUB_SECTIONS` и цикл `make_view`)
- Delete: `src/cf/dashboard/templates/stub.html`
- Modify: `tests/test_dashboard_routes.py` (удалить `test_stub_pages_render`)

- [ ] **Step 1: Удалить мёртвый код**

В `app.py` удалить блок `STUB_SECTIONS = {...}` (после `BASE_DIR`) и цикл в конце `create_app`:

```python
    for section, title in STUB_SECTIONS.items():
        def make_view(section=section, title=title):
            ...
        app.get(f"/{section}")(make_view())
```

Удалить файл `src/cf/dashboard/templates/stub.html` и тест `test_stub_pages_render` из `tests/test_dashboard_routes.py`.

- [ ] **Step 2: Полный прогон**

Run: `.venv/Scripts/python -m pytest -q`
Expected: все тесты зелёные (148 старых минус 1 удалённый + ~27 новых)

- [ ] **Step 3: Живая проверка**

Run: `.venv/Scripts/python -m cf dashboard` → открыть `http://127.0.0.1:8787`, кликнуть все шесть пунктов сайдбара: Источники показывают реальные raw-ленты, Лаборатория — формулу short-styling-idea-reel v2, паттерны 07-10/07-14 и proposal. Останавливать сервер по владельцу порта (`Get-NetTCPConnection -LocalPort 8787 -State Listen`), не по имени процесса — venv-лаунчер порождает дочерний python.

- [ ] **Step 4: Commit**

```bash
git add -A src/cf/dashboard tests
git commit -m "feat(dashboard): этап 2 завершён — заглушки разделов удалены"
```
