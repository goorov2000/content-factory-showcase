# CF Dashboard (этап 1, MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (merge 19e643f, 2026-07-14). Чекбоксы в ходе исполнения не велись. Упоминаемый макет docs/design/cf-dashboard.pen остался пустой заглушкой — визуальная сверка выполнена по CF DS/DESIGN.md (2026-07-15).

**Goal:** Локальный веб-дашборд CF: экран «Обзор» (стат-плитки, конвейер с запуском звеньев, график ER, последние запуски), экран «Брифы» с одобрением/отклонением, индикатор систем — на живых Google Sheets.

**Architecture:** FastAPI + Jinja2 + htmx в `src/cf/dashboard/`, поверх существующих `cf.sheets.Sheets`, `cf.runlog.log_run`, `cf.config.load_config`. Один процесс `python -m cf dashboard` на `http://127.0.0.1:8787`. Все зависимости инжектируются в `create_app(...)` — тесты работают на `tests/fakes.FakeSheets` без сети.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, Jinja2, htmx (vendored, без CDN), httpx (n8n API + TestClient), pytest.

**Спека:** `docs/superpowers/specs/2026-07-14-cf-dashboard-design.md`. **Макеты:** `docs/design/cf-dashboard.pen` — визуальный эталон (палитра и типографика — `../CF DS/DESIGN.md`).

**Вне охвата этого плана:** разделы Перформанс/Запуски/Лаборатория/Источники (этап 2 — здесь только страницы-заглушки), чат с Клодом (этап 3). Для них будут отдельные планы.

## File Structure

```
src/cf/dashboard/
  __init__.py          # пусто
  app.py               # create_app(): роуты, DI, монтирование static
  data.py              # DataCache (TTL-кеш вкладок) + overview_metrics()
  actions.py           # review_brief() — одобрить/отклонить + run log
  runner.py            # StageRunner — запуск звеньев (n8n POST / claude -p), полный цикл
  health.py            # HealthMonitor — лампочки систем
  templates/
    base.html          # каркас: сайдбар, индикатор систем, блок контента
    overview.html      # плитки, конвейер, график ER, последние запуски
    briefs.html        # чипы-фильтры, таблица, карточка ревью
    stub.html          # заглушка разделов этапа 2
    partials/
      stages.html      # плитки конвейера (htmx-поллинг при запуске)
      health.html      # попап состояния систем
  static/
    style.css          # токены DESIGN.md, вся вёрстка, анимация гусеницы
    htmx.min.js        # vendored htmx
tests/
  test_dashboard_data.py
  test_dashboard_actions.py
  test_dashboard_runner.py
  test_dashboard_health.py
  test_dashboard_routes.py
```

Правки существующих файлов: `pyproject.toml` (зависимости), `src/cf/cli.py` (подкоманда `dashboard`), `cf.config.json` (секция `dashboard` — руками пользователя, см. Task 8).

Все команды из корня репо `<local-path>`; python — `.venv/Scripts/python`.

---

### Task 1: Зависимости и каркас приложения

**Files:**
- Modify: `pyproject.toml`
- Create: `src/cf/dashboard/__init__.py`, `src/cf/dashboard/app.py`
- Create: `src/cf/dashboard/templates/base.html`, `src/cf/dashboard/templates/stub.html`
- Create: `src/cf/dashboard/static/style.css` (минимум, полный CSS — Task 5)
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Добавить зависимости в `pyproject.toml`**

```toml
dependencies = [
  "gspread>=6.1",
  "google-auth>=2.30",
  "jsonschema>=4.22",
  "fastapi>=0.111",
  "uvicorn>=0.30",
  "jinja2>=3.1",
  "httpx>=0.27",
  "python-multipart>=0.0.9",
]
```

(секция `[project] dependencies` целиком; dev-секцию не трогать)

- [ ] **Step 2: Установить**

Run: `.venv/Scripts/python -m pip install -e ".[dev]"`
Expected: успешная установка fastapi/uvicorn/jinja2/httpx/python-multipart

- [ ] **Step 3: Vendored htmx**

Run: `curl -sL https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js -o src/cf/dashboard/static/htmx.min.js`
Expected: файл ~47 КБ, первая строка начинается с `(function(e,t){`

- [ ] **Step 4: Написать падающий тест**

```python
# tests/test_dashboard_routes.py
from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from tests.fakes import FakeSheets


def make_client(tables=None, **kwargs):
    sheets = FakeSheets(tables or {})
    app = create_app(sheets=sheets, **kwargs)
    return TestClient(app), sheets


def test_root_redirects_to_overview():
    client, _ = make_client()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/overview"


def test_stub_pages_render():
    client, _ = make_client()
    for path in ("/performance", "/runs", "/lab", "/sources"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "этап 2" in resp.text
```

- [ ] **Step 5: Убедиться, что тест падает**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cf.dashboard'`

- [ ] **Step 6: Каркас приложения**

`src/cf/dashboard/__init__.py` — пустой файл.

```python
# src/cf/dashboard/app.py
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent

STUB_SECTIONS = {
    "performance": "Перформанс",
    "runs": "Запуски",
    "lab": "Лаборатория",
    "sources": "Источники",
}


def create_app(sheets=None, cache=None, runner=None, health=None, config=None):
    app = FastAPI(title="CF Dashboard")
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    app.state.sheets = sheets
    app.state.templates = templates

    @app.get("/")
    def root():
        return RedirectResponse("/overview")

    @app.get("/overview")
    def overview(request: Request):
        return templates.TemplateResponse(request, "stub.html", {
            "title": "Обзор", "active": "overview"})

    for section, title in STUB_SECTIONS.items():
        def make_view(section=section, title=title):
            def view(request: Request):
                return templates.TemplateResponse(request, "stub.html", {
                    "title": title, "active": section})
            return view
        app.get(f"/{section}")(make_view())

    return app
```

```html
<!-- src/cf/dashboard/templates/base.html -->
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} · CF · JE LA PECHE</title>
  <link rel="stylesheet" href="/static/style.css">
  <script src="/static/htmx.min.js" defer></script>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">JLP</div>
      <div>
        <div class="brand-name">JE LA PECHE</div>
        <div class="brand-sub">CONTENT FACTORY</div>
      </div>
    </div>
    <nav class="nav">
      {% for key, label in [("overview","Обзор"),("briefs","Брифы"),("performance","Перформанс"),("runs","Запуски"),("lab","Лаборатория"),("sources","Источники")] %}
      <a class="nav-item {{ 'active' if active == key }}" href="/{{ key }}">{{ label }}</a>
      {% endfor %}
    </nav>
    <div class="sidebar-foot">
      {% block sidebar_foot %}{% endblock %}
      <form method="post" action="/refresh"><button class="btn-outline-dark" type="submit">Обновить данные</button></form>
    </div>
  </aside>
  <main class="main">
    {% block content %}{% endblock %}
  </main>
</div>
</body>
</html>
```

```html
<!-- src/cf/dashboard/templates/stub.html -->
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div class="eyebrow">CF · В РАЗРАБОТКЕ</div>
  <h1>{{ title }}</h1>
</header>
<div class="empty-state">Раздел появится на этапе 2. Пока данные доступны в Google Sheets и через <code>cf</code> CLI.</div>
{% endblock %}
```

`src/cf/dashboard/static/style.css` — пока заглушка (полный файл в Task 5):

```css
/* Токены и вёрстка — Task 5 */
body { font-family: Montserrat, system-ui, sans-serif; }
```

- [ ] **Step 7: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v`
Expected: 2 PASS (запасной `/refresh` из base.html — POST, роут появится в Task 4; сам шаблон на рендер stub не влияет)

Примечание: `/overview` пока рендерит stub — заменится в Task 4.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/cf/dashboard tests/test_dashboard_routes.py
git commit -m "feat(dashboard): каркас FastAPI-приложения, base-шаблон, заглушки разделов"
```

---

### Task 2: DataCache — TTL-кеш вкладок с деградацией

**Files:**
- Create: `src/cf/dashboard/data.py`
- Test: `tests/test_dashboard_data.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_dashboard_data.py
import pytest

from cf.dashboard.data import DataCache
from tests.fakes import FakeSheets


class CountingSheets(FakeSheets):
    def __init__(self, tables=None):
        super().__init__(tables)
        self.reads = 0
        self.fail_next = False

    def read_rows(self, tab_key):
        if self.fail_next:
            raise ConnectionError("sheets down")
        self.reads += 1
        return super().read_rows(tab_key)


def test_cache_serves_within_ttl():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}]})
    t = [100.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    assert cache.rows("briefs") == [{"brief_id": "B1"}]
    t[0] += 30
    cache.rows("briefs")
    assert sheets.reads == 1          # второе чтение — из кеша
    t[0] += 31
    cache.rows("briefs")
    assert sheets.reads == 2          # TTL истёк — перечитали


def test_cache_degrades_to_stale_on_error():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}]})
    t = [100.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    cache.rows("briefs")
    t[0] += 120
    sheets.fail_next = True
    assert cache.rows("briefs") == [{"brief_id": "B1"}]  # старые данные, не исключение
    assert cache.stale is True
    sheets.fail_next = False
    cache.refresh()
    cache.rows("briefs")
    assert cache.stale is False


def test_cache_raises_when_no_fallback():
    sheets = CountingSheets({})
    sheets.fail_next = True
    cache = DataCache(sheets, ttl=60)
    with pytest.raises(ConnectionError):
        cache.rows("briefs")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_data.py -v`
Expected: FAIL — `ImportError: cannot import name 'DataCache'`

- [ ] **Step 3: Реализация**

```python
# src/cf/dashboard/data.py
import time


class DataCache:
    """TTL-кеш вкладок Sheets. При ошибке чтения отдаёт старые данные и ставит stale."""

    def __init__(self, sheets, ttl=60.0, clock=time.monotonic):
        self.sheets = sheets
        self.ttl = ttl
        self.clock = clock
        self.stale = False
        self._store = {}  # tab_key -> (fetched_at, rows)

    def rows(self, tab_key):
        now = self.clock()
        cached = self._store.get(tab_key)
        if cached and now - cached[0] < self.ttl:
            return cached[1]
        try:
            rows = self.sheets.read_rows(tab_key)
        except Exception:
            if cached:
                self.stale = True
                return cached[1]
            raise
        self._store[tab_key] = (now, rows)
        self.stale = False
        return rows

    def refresh(self):
        self._store.clear()
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_data.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/data.py tests/test_dashboard_data.py
git commit -m "feat(dashboard): TTL-кеш вкладок с деградацией в stale"
```

---

### Task 3: overview_metrics — все цифры «Обзора»

Плитки: raw за период (TikTok/Instagram), брифы pending, рилсы без статистики, средний ER. Конвейер: те же числа + брифов создано за период. График: средний ER по ISO-неделям.

**Files:**
- Modify: `src/cf/dashboard/data.py`
- Test: `tests/test_dashboard_data.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_dashboard_data.py`:

```python
from datetime import datetime

from cf.dashboard.data import overview_metrics


def make_cache(tables):
    return DataCache(FakeSheets(tables), ttl=60)


TODAY = datetime(2026, 7, 14)


def test_overview_metrics_counts():
    cache = make_cache({
        "raw_tiktok": [
            {"source_url": "u1", "posted_at": "2026-07-01"},
            {"source_url": "u2", "posted_at": "2026-05-01"},   # вне периода 30 дней
        ],
        "raw_instagram": [{"source_url": "u3", "posted_at": "2026-07-10"}],
        "briefs": [
            {"brief_id": "B1", "review_status": "pending", "created_at": "2026-07-12"},
            {"brief_id": "B2", "review_status": "approved", "created_at": "2026-07-01"},
            {"brief_id": "B3", "review_status": "rejected", "created_at": "2026-05-01"},
        ],
        "reels": [
            {"reel_id": "R1", "published_at": "2026-07-05"},
            {"reel_id": "R2", "published_at": "2026-07-12"},
        ],
        "performance": [{"reel_id": "R1", "er": "4.5"}],
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["raw_total"] == 2 and m["raw_tiktok"] == 1 and m["raw_instagram"] == 1
    assert m["briefs_pending"] == 1
    assert m["briefs_created"] == 2          # B1 и B2 в периоде
    assert m["awaiting_stats"] == 1          # R2 без строки в performance
    assert m["avg_er"] == 4.5
    assert m["reels_published"] == 2


def test_overview_metrics_er_series_by_week():
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [], "briefs": [],
        "reels": [
            {"reel_id": "R1", "published_at": "2026-07-06"},   # ISO-неделя 28
            {"reel_id": "R2", "published_at": "2026-07-07"},   # неделя 28
            {"reel_id": "R3", "published_at": "2026-07-13"},   # неделя 29
        ],
        "performance": [
            {"reel_id": "R1", "er": "4.0"},
            {"reel_id": "R2", "er": "6.0"},
            {"reel_id": "R3", "er": "3.0"},
        ],
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["er_series"] == [("W28", 5.0), ("W29", 3.0)]


def test_overview_metrics_tolerates_dirty_rows():
    cache = make_cache({
        "raw_tiktok": [{"source_url": "u1", "posted_at": "не дата"}],
        "raw_instagram": [],
        "briefs": [{"brief_id": "B1"}],                      # без статуса и даты
        "reels": [{"reel_id": "R1"}],                        # без даты
        "performance": [{"reel_id": "R1", "er": ""}],        # пустой ER
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["raw_total"] == 0
    assert m["briefs_pending"] == 0
    assert m["awaiting_stats"] == 0
    assert m["avg_er"] is None
    assert m["er_series"] == []
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_data.py -v -k overview`
Expected: FAIL — `ImportError: cannot import name 'overview_metrics'`

- [ ] **Step 3: Реализация**

Добавить в `src/cf/dashboard/data.py`:

```python
from datetime import datetime, timedelta


def _parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _status(row):
    return str(row.get("review_status", "")).strip().lower()


def overview_metrics(cache, days=30, today=None):
    today = today or datetime.now()
    since = today - timedelta(days=days)

    def in_period(row, field):
        d = _parse_date(row.get(field, ""))
        return d is not None and since <= d <= today

    raw_tt = [r for r in cache.rows("raw_tiktok") if in_period(r, "posted_at")]
    raw_ig = [r for r in cache.rows("raw_instagram") if in_period(r, "posted_at")]

    briefs = cache.rows("briefs")
    pending = [b for b in briefs if _status(b) == "pending"]
    created = [b for b in briefs if in_period(b, "created_at")]
    oldest_pending = min(
        (d for d in (_parse_date(b.get("created_at", "")) for b in pending) if d),
        default=None)

    reels = cache.rows("reels")
    perf = cache.rows("performance")
    measured_ids = {str(p.get("reel_id")) for p in perf}
    awaiting = [r for r in reels if str(r.get("reel_id")) not in measured_ids]

    published = [r for r in reels if in_period(r, "published_at")]

    ers = []
    for p in perf:
        try:
            ers.append(float(p.get("er")))
        except (TypeError, ValueError):
            continue
    avg_er = round(sum(ers) / len(ers), 1) if ers else None

    reel_week = {}
    for r in reels:
        d = _parse_date(r.get("published_at", ""))
        if d:
            reel_week[str(r.get("reel_id"))] = f"W{d.isocalendar().week:02d}"
    weeks = {}
    for p in perf:
        week = reel_week.get(str(p.get("reel_id")))
        try:
            er = float(p.get("er"))
        except (TypeError, ValueError):
            continue
        if week:
            weeks.setdefault(week, []).append(er)
    er_series = [(w, round(sum(v) / len(v), 1)) for w, v in sorted(weeks.items())]

    return {
        "raw_total": len(raw_tt) + len(raw_ig),
        "raw_tiktok": len(raw_tt),
        "raw_instagram": len(raw_ig),
        "briefs_pending": len(pending),
        "oldest_pending_days": (today - oldest_pending).days if oldest_pending else None,
        "briefs_created": len(created),
        "awaiting_stats": len(awaiting),
        "reels_published": len(published),
        "avg_er": avg_er,
        "er_series": er_series,
    }
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_data.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/data.py tests/test_dashboard_data.py
git commit -m "feat(dashboard): агрегации обзора — плитки, конвейер, ER по неделям"
```

---

### Task 4: Страница «Обзор» + /refresh

**Files:**
- Modify: `src/cf/dashboard/app.py`
- Create: `src/cf/dashboard/templates/overview.html`, `src/cf/dashboard/templates/partials/stages.html`
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_dashboard_routes.py`:

```python
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
    assert "RAW В БАЗЕ" in text and "БРИФЫ ЖДУТ ВНИМАНИЯ" in text
    assert "ЖДУТ СТАТИСТИКИ" in text and "СРЕДНИЙ ER" in text
    for stage in ("Raw-ролики", "Контент-завод", "Одобрение брифов",
                  "Съёмка и публикация", "Статистика"):
        assert stage in text
    assert "cf-analyze" in text                      # последние запуски
    assert 'href="/sources"' in text                 # клик по плитке ведёт в раздел


def test_overview_days_selector():
    client, _ = make_client(OVERVIEW_TABLES)
    assert client.get("/overview?days=7").status_code == 200
    assert client.get("/overview?days=абв").status_code == 200   # мусор -> дефолт 30


def test_refresh_redirects_back():
    client, _ = make_client(OVERVIEW_TABLES)
    resp = client.post("/refresh", follow_redirects=False,
                       headers={"referer": "http://testserver/overview"})
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/overview")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v -k "overview or refresh"`
Expected: FAIL — на `/overview` рендерится stub без плиток; `/refresh` — 405

- [ ] **Step 3: Реализация роутов**

В `src/cf/dashboard/app.py` заменить заглушку `/overview` и добавить `/refresh`. Полный новый `create_app` (роуты разделов-заглушек без изменений):

```python
# заменить в create_app (после app.state.templates = templates):
    from cf.dashboard.data import DataCache, overview_metrics

    if cache is None:
        cache = DataCache(sheets)
    app.state.cache = cache

    def parse_days(raw):
        try:
            days = int(raw)
        except (TypeError, ValueError):
            return 30
        return days if days in (7, 30, 90) else 30

    @app.get("/overview")
    def overview(request: Request, days: str = "30"):
        days = parse_days(days)
        try:
            metrics = overview_metrics(cache, days=days)
            runs = sorted(cache.rows("run_log"),
                          key=lambda r: str(r.get("completed_at", "")), reverse=True)[:6]
            error = None
        except Exception as exc:
            metrics, runs, error = None, [], str(exc)
        return templates.TemplateResponse(request, "overview.html", {
            "title": "Обзор", "active": "overview", "days": days,
            "metrics": metrics, "runs": runs, "error": error,
            "stale": cache.stale, "runner": runner,
        })

    @app.post("/refresh")
    def refresh(request: Request):
        cache.refresh()
        target = request.headers.get("referer") or "/overview"
        return RedirectResponse(target, status_code=303)
```

- [ ] **Step 4: Шаблон обзора**

```html
<!-- src/cf/dashboard/templates/overview.html -->
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div>
    <div class="eyebrow">JE LA PECHE · CONTENT FACTORY</div>
    <h1>ОБЗОР <span class="blue-word">ЗАВОДА</span></h1>
  </div>
  <form method="get" action="/overview" class="period">
    <select name="days" onchange="this.form.submit()">
      {% for d in (7, 30, 90) %}
      <option value="{{ d }}" {{ 'selected' if days == d }}>{{ d }} дней</option>
      {% endfor %}
    </select>
  </form>
</header>

{% if stale %}<div class="banner warn">Sheets недоступен — показаны данные из кеша.</div>{% endif %}
{% if error %}
<div class="empty-state">Не удалось прочитать Google Sheets: {{ error }}</div>
{% else %}

<section class="stat-row">
  <div class="stat"><div class="stat-label">RAW В БАЗЕ · ПЕРИОД</div>
    <div class="stat-value">{{ metrics.raw_total }}</div>
    <div class="stat-sub">TikTok {{ metrics.raw_tiktok }} · Instagram {{ metrics.raw_instagram }}</div></div>
  <div class="stat"><div class="stat-label">БРИФЫ ЖДУТ ВНИМАНИЯ</div>
    <div class="stat-value">{{ metrics.briefs_pending }}</div>
    <div class="stat-sub">{% if metrics.oldest_pending_days is not none %}старейший ждёт {{ metrics.oldest_pending_days }} дн.{% else %}очередь пуста{% endif %}</div></div>
  <div class="stat"><div class="stat-label">ЖДУТ СТАТИСТИКИ</div>
    <div class="stat-value">{{ metrics.awaiting_stats }}</div>
    <div class="stat-sub">опубликованы, метрик ещё нет</div></div>
  <div class="stat"><div class="stat-label">СРЕДНИЙ ER</div>
    <div class="stat-value">{{ metrics.avg_er if metrics.avg_er is not none else "—" }}{% if metrics.avg_er is not none %}%{% endif %}</div>
    <div class="stat-sub">по {{ metrics.reels_published }} рилсам периода</div></div>
</section>

<section class="card pipeline">
  <div class="card-head">
    <div><div class="card-title">Конвейер</div>
      <div class="card-sub">запуск с любого звена или весь цикл целиком</div></div>
    <form method="post" action="/cycle/run"><button class="btn-primary" type="submit">▶ Полный цикл</button></form>
  </div>
  <div id="stages" {% if runner and runner.any_running() %}hx-get="/partials/stages" hx-trigger="every 2s" hx-swap="outerHTML"{% endif %}>
    {% include "partials/stages.html" %}
  </div>
  <div class="belt"><span class="gear">⚙</span><div class="belt-track"></div><span class="gear">⚙</span></div>
  <div class="loop-note">⟲ цикл замкнут: собранная статистика подпитывает следующий анализ</div>
</section>

<div class="two-col">
  <section class="card">
    <div class="card-head"><div class="card-title">Engagement rate по неделям</div></div>
    {% if metrics.er_series %}
    <div class="chart">
      {% set peak = metrics.er_series | map(attribute=1) | max %}
      {% for week, er in metrics.er_series %}
      <div class="chart-col">
        <div class="chart-bar {{ 'best' if er == peak }}" style="height: {{ (er / peak * 100) | round }}%"></div>
        <div class="chart-label">{{ week }}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}<div class="empty-state">Недостаточно данных для графика.</div>{% endif %}
  </section>
  <section class="card">
    <div class="card-head"><div class="card-title">Последние запуски</div></div>
    {% for run in runs %}
    <div class="row">
      <span class="dot {{ 'ok' if run.status == 'success' else 'warn' }}"></span>
      <span class="mono">{{ run.agent }}</span>
      <span class="row-meta mono">{{ run.completed_at[:16] | replace("T", " ") }}</span>
    </div>
    {% else %}<div class="empty-state">Запусков ещё не было.</div>{% endfor %}
  </section>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Партиал конвейера**

Плитки-ссылки в разделы; ▶ и статусы запуска подключатся в Task 9 (здесь `runner` может быть `None` — партиал это переживает).

```html
<!-- src/cf/dashboard/templates/partials/stages.html -->
<div id="stages" class="stage-row"
     {% if runner and runner.any_running() %}hx-get="/partials/stages" hx-trigger="every 2s" hx-swap="outerHTML"{% endif %}>
  {% set stages = [
    ("raw", "Raw-ролики", "N8N", metrics.raw_total, "в базе", "/sources", True),
    ("factory", "Контент-завод", "CLAUDE CODE", metrics.briefs_created, "брифов за период", "/lab", True),
    ("approve", "Одобрение брифов", "ПРОДЮСЕР", metrics.briefs_pending, "ждут решения", "/briefs", False),
    ("publish", "Съёмка и публикация", "ПРОДЮСЕР · N8N", metrics.reels_published, "рилсов вышло", "/performance", True),
    ("stats", "Статистика", "N8N", metrics.awaiting_stats, "ждут данных", "/performance", True),
  ] %}
  {% for key, name, actor, num, unit, href, runnable in stages %}
  {% if not loop.first %}<span class="stage-arrow">›</span>{% endif %}
  <a class="stage {{ runner.state[key].status if runner and key in runner.state else '' }}" href="{{ href }}">
    <div class="stage-top">
      <span class="stage-actor">{{ actor }}</span>
      {% if runnable and runner %}
      <button class="stage-run" title="Запустить звено"
              hx-post="/stages/{{ key }}/run" hx-target="#stages" hx-swap="outerHTML">▶</button>
      {% else %}<span class="stage-go">→</span>{% endif %}
    </div>
    <div class="stage-name">{{ name }}</div>
    <div class="stage-num"><span class="mono">{{ num }}</span> <span class="stage-unit">{{ unit }}</span></div>
    {% if runner and key in runner.state and runner.state[key].detail %}
    <div class="stage-detail">{{ runner.state[key].detail }}</div>
    {% endif %}
  </a>
  {% endfor %}
</div>
```

- [ ] **Step 6: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v`
Expected: все PASS (в т.ч. Task 1)

- [ ] **Step 7: Commit**

```bash
git add src/cf/dashboard tests/test_dashboard_routes.py
git commit -m "feat(dashboard): экран Обзор — плитки, конвейер, график ER, запуски"
```

---

### Task 5: CSS дизайн-системы + анимация гусеницы

Полный `style.css` по токенам `../CF DS/DESIGN.md` и макету `docs/design/cf-dashboard.pen`. Aeroport подключается локальными файлами позже, в Step 3 стоят фолбэки.

**Files:**
- Modify: `src/cf/dashboard/static/style.css` (полная замена)
- Create: `src/cf/dashboard/static/fonts/` (копия Aeroport из CF DS)

- [ ] **Step 1: Скопировать шрифты**

Run: `mkdir -p src/cf/dashboard/static/fonts && cp "../CF DS/.agents/skills/canvas-design/canvas-fonts/Aeroport"* src/cf/dashboard/static/fonts/ 2>/dev/null; ls src/cf/dashboard/static/fonts/`
Expected: файлы Aeroport (woff2/otf/ttf — какие есть). Если файлов нет — найти: `find "../CF DS" -iname "*aeroport*"` и скопировать найденное. Aeroport — trial-лицензия: только локальный инструмент, в `.gitignore` шрифты не добавлять не нужно (репо приватное), но пометить в README при первой публикации.

- [ ] **Step 2: Полный style.css**

```css
/* src/cf/dashboard/static/style.css — токены CF DS/DESIGN.md */
@font-face { font-family: Aeroport; src: url("/static/fonts/Aeroport-Medium.woff2") format("woff2"); font-weight: 500; font-display: swap; }
@font-face { font-family: "Aeroport Mono"; src: url("/static/fonts/Aeroport-Mono.woff2") format("woff2"); font-weight: 400; font-display: swap; }

:root {
  --primary: #131417; --ink: #1D232E; --accent-blue: #759AC4; --accent-blue-text: #476C96;
  --gray: #99999B; --gray-text: #5A6068; --silver: #C9CED1;
  --frost: #EEF0F2; --press: #E4E7EA; --hairline-dark: #39414E;
  --success: #6FA287; --success-text: #47695A; --success-soft: #EAF2EE;
  --warning: #C9A15E; --warning-text: #75592B; --warning-soft: #F7F1E5;
  --error: #BF6B6B; --error-text: #A14E4E; --error-soft: #F7EAEA;
  --info-soft: #EAF1F8;
  --shadow-1: 0 1px 4px rgba(29,35,46,.07);
  --shadow-2: 0 10px 24px -6px rgba(29,35,46,.14);
  --focus-ring: 0 0 0 2px rgba(117,154,196,.5);
  --font-display: Aeroport, Montserrat, system-ui, sans-serif;
  --font-ui: Montserrat, -apple-system, system-ui, sans-serif;
  --font-mono: "Aeroport Mono", "JetBrains Mono", ui-monospace, monospace;
  --ease: cubic-bezier(.2, 0, 0, 1);
}

* { box-sizing: border-box; margin: 0; }
body { font-family: var(--font-ui); font-size: 15px; line-height: 1.5; color: var(--ink); background: #fff; }
a { color: var(--accent-blue-text); }
button { font: inherit; cursor: pointer; }
:focus-visible { outline: none; box-shadow: var(--focus-ring); }
.mono { font-family: var(--font-mono); }

.shell { display: flex; min-height: 100vh; }
.main { flex: 1; padding: 28px 32px; display: flex; flex-direction: column; gap: 24px; }

/* Сайдбар */
.sidebar { width: 232px; flex-shrink: 0; background: var(--primary); color: #fff;
  display: flex; flex-direction: column; padding: 24px 0; position: sticky; top: 0; height: 100vh; }
.brand { display: flex; gap: 12px; align-items: center; padding: 0 20px 28px; }
.brand-mark { width: 36px; height: 36px; border: 1px solid #fff; border-radius: 50%;
  display: grid; place-items: center; font: 600 11px var(--font-display); letter-spacing: .05em; }
.brand-name { font: 600 13px var(--font-display); letter-spacing: .15em; }
.brand-sub { font: 500 9px var(--font-ui); letter-spacing: .24em; color: var(--gray); }
.nav { display: flex; flex-direction: column; gap: 2px; padding: 0 12px; }
.nav-item { padding: 10px 12px; border-radius: 6px; color: var(--silver); font: 500 13px var(--font-ui);
  text-decoration: none; transition: background .12s var(--ease), color .12s var(--ease); }
.nav-item:hover { background: rgba(57,65,78,.6); color: #fff; }
.nav-item.active { background: var(--hairline-dark); color: #fff; font-weight: 600; }
.sidebar-foot { margin-top: auto; padding: 16px 20px 0; border-top: 1px solid var(--hairline-dark);
  display: flex; flex-direction: column; gap: 12px; }
.btn-outline-dark { width: 100%; padding: 9px; background: none; border: 1px solid var(--hairline-dark);
  border-radius: 6px; color: var(--silver); font: 600 11px var(--font-ui); letter-spacing: .12em; text-transform: uppercase; }
.btn-outline-dark:hover { background: var(--hairline-dark); color: #fff; }

/* Шапка страницы */
.page-head { display: flex; justify-content: space-between; align-items: center; }
.eyebrow { font: 500 11px var(--font-ui); letter-spacing: .3em; color: var(--gray-text); margin-bottom: 6px; }
h1 { font: 500 32px/1.2 var(--font-display); letter-spacing: .03em; text-transform: uppercase; }
.blue-word { color: var(--accent-blue); }
.period select { padding: 8px 14px; border: 1px solid var(--silver); border-radius: 6px;
  font: 600 12px var(--font-ui); color: var(--ink); background: #fff; }

/* Карточки и плитки */
.card { background: #fff; border: 1px solid var(--silver); border-radius: 8px;
  box-shadow: var(--shadow-1); padding: 20px 24px; }
.card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.card-title { font: 600 15px var(--font-ui); }
.card-sub { font: 400 11px var(--font-ui); color: var(--gray-text); }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.stat { background: #fff; border: 1px solid var(--silver); border-radius: 8px;
  box-shadow: var(--shadow-1); padding: 18px 20px; display: flex; flex-direction: column; gap: 8px; }
.stat-label { font: 600 10px var(--font-ui); letter-spacing: .2em; color: var(--gray-text); }
.stat-value { font: 400 28px var(--font-mono); }
.stat-sub { font: 400 11px var(--font-ui); color: var(--gray-text); }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* Конвейер */
.stage-row { display: flex; align-items: stretch; gap: 6px; }
.stage-arrow { align-self: center; color: var(--gray); }
.stage { flex: 1; background: #fff; border: 1px solid var(--silver); border-radius: 6px;
  padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; text-decoration: none; color: var(--ink);
  transition: border-color .12s var(--ease), box-shadow .12s var(--ease); }
.stage:hover { border-color: var(--accent-blue); box-shadow: var(--shadow-1); }
.stage.running { border-color: var(--accent-blue); background: var(--info-soft); }
.stage.error { border-color: var(--error); background: var(--error-soft); }
.stage-top { display: flex; justify-content: space-between; align-items: center; }
.stage-actor { font: 400 8.5px var(--font-mono); letter-spacing: .1em; color: var(--gray-text); }
.stage-run { width: 20px; height: 20px; border-radius: 50%; border: 1px solid var(--silver);
  background: var(--frost); font-size: 8px; color: var(--ink); }
.stage-run:hover { background: var(--press); }
.stage-go { color: var(--gray); font-size: 12px; }
.stage-name { font: 600 12px var(--font-ui); }
.stage-num { display: flex; gap: 5px; align-items: baseline; font-size: 17px; }
.stage-unit { font: 400 10px var(--font-ui); color: var(--gray-text); }
.stage-detail { font: 400 10px var(--font-ui); color: var(--gray-text); }

/* Гусеница */
.belt { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.gear { color: var(--gray); animation: gear-spin 4s linear infinite; }
.belt-track { flex: 1; height: 18px; border: 1px solid var(--silver); border-radius: 9px; background:
  radial-gradient(circle 4px at 12px 50%, #fff 3.2px, var(--gray) 3.6px, var(--gray) 4px, transparent 4.4px)
  0 0 / 24px 100% repeat-x, var(--frost);
  animation: belt-move 1.6s linear infinite; }
@keyframes belt-move { to { background-position-x: 24px, 0; } }
@keyframes gear-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .belt-track, .gear { animation: none; } }
.loop-note { text-align: center; margin-top: 10px; font: 400 10.5px var(--font-ui); color: var(--accent-blue-text); }

/* График */
.chart { display: flex; gap: 14px; height: 150px; align-items: flex-end; }
.chart-col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 8px; height: 100%; }
.chart-bar { width: 100%; background: var(--ink); border-radius: 3px 3px 0 0; min-height: 4px; }
.chart-bar.best { background: var(--accent-blue); }
.chart-label { font: 400 10px var(--font-mono); color: var(--gray); }

/* Списки, строки, бейджи */
.row { display: flex; gap: 10px; align-items: center; padding: 11px 0; border-top: 1px solid var(--frost); font-size: 13px; }
.row-meta { margin-left: auto; color: var(--gray-text); font-size: 12px; }
.dot { width: 7px; height: 7px; border-radius: 50%; }
.dot.ok { background: var(--success); } .dot.warn { background: var(--warning); } .dot.fail { background: var(--error); }
.banner.warn { background: var(--warning-soft); color: var(--warning-text); border-radius: 6px; padding: 10px 16px; font-size: 13px; }
.empty-state { color: var(--gray-text); padding: 24px; text-align: center; font-size: 13px; }

/* Кнопки */
.btn-primary { background: var(--primary); color: #fff; border: none; border-radius: 6px;
  padding: 10px 16px; font: 600 11px var(--font-ui); letter-spacing: .14em; text-transform: uppercase;
  transition: background .12s var(--ease); }
.btn-primary:hover { background: var(--ink); }
.btn-primary:active { background: #0C0D0F; }
.btn-danger-outline { background: none; border: 1px solid var(--error); color: var(--error-text);
  border-radius: 6px; padding: 10px 16px; font: 600 11px var(--font-ui); letter-spacing: .14em; text-transform: uppercase; }
.btn-danger-outline:hover { background: var(--error-soft); }

/* Брифы */
.chips { display: flex; gap: 8px; }
.chip { padding: 7px 14px; border-radius: 16px; border: 1px solid var(--silver); background: #fff;
  font: 600 12px var(--font-ui); color: var(--gray-text); text-decoration: none; }
.chip.active { background: var(--primary); border-color: var(--primary); color: #fff; }
.briefs-layout { display: grid; grid-template-columns: 1fr 430px; gap: 20px; align-items: start; }
table.queue { width: 100%; border-collapse: collapse; font-size: 13px; }
.queue th { text-align: left; font: 600 10px var(--font-ui); letter-spacing: .16em; color: var(--gray-text);
  background: var(--frost); padding: 12px 14px; }
.queue td { padding: 13px 14px; border-bottom: 1px solid var(--frost); }
.queue tr.selected { background: var(--info-soft); }
.queue a { color: inherit; text-decoration: none; display: block; }
.tag { display: inline-block; border: 1px solid var(--silver); border-radius: 3px; padding: 3px 8px;
  font: 600 9px var(--font-ui); letter-spacing: .14em; text-transform: uppercase; color: var(--gray-text); }
.badge { display: inline-block; border-radius: 3px; padding: 3px 9px;
  font: 600 9px var(--font-ui); letter-spacing: .12em; text-transform: uppercase; }
.badge.pending { background: var(--warning-soft); color: var(--warning-text); }
.badge.approved { background: var(--success-soft); color: var(--success-text); }
.badge.rejected { background: var(--error-soft); color: var(--error-text); }
.script-box { background: var(--frost); border-radius: 6px; padding: 14px 16px; font-size: 12.5px; line-height: 1.55; }
.field-label { font: 600 10px var(--font-ui); letter-spacing: .16em; color: var(--gray-text); margin-bottom: 6px; }
textarea.note { width: 100%; min-height: 72px; border: 1px solid var(--silver); border-radius: 6px;
  padding: 10px 12px; font: 400 12.5px var(--font-ui); resize: vertical; }
textarea.note:focus { outline: none; box-shadow: var(--focus-ring); border-color: var(--accent-blue); }
.actions { display: flex; gap: 10px; margin-top: 16px; }
.actions .btn-primary { flex: 1; }

/* Индикатор систем */
.sys-btn { position: relative; display: flex; gap: 8px; align-items: center; width: 100%;
  padding: 8px 10px; border: none; border-radius: 6px; background: var(--hairline-dark);
  color: #fff; font: 600 10.5px var(--font-ui); letter-spacing: .12em; }
.sys-pop { display: none; position: absolute; bottom: calc(100% + 8px); left: 0; width: 264px;
  background: #fff; border: 1px solid var(--silver); border-radius: 8px; box-shadow: var(--shadow-2);
  color: var(--ink); text-align: left; z-index: 400; }
.sys-btn:hover .sys-pop, .sys-btn:focus-within .sys-pop { display: block; }
.sys-pop-head { display: flex; justify-content: space-between; padding: 12px 16px 10px;
  font: 600 11px var(--font-ui); letter-spacing: .16em; color: var(--gray-text); }
.sys-row { display: flex; gap: 10px; align-items: center; padding: 10px 16px; border-top: 1px solid var(--frost); font-size: 12.5px; }
.sys-row .row-meta { font-size: 10.5px; }
```

- [ ] **Step 3: Проверить шрифтовые пути**

Открыть `src/cf/dashboard/static/fonts/`: если реальные имена файлов отличаются от `Aeroport-Medium.woff2` / `Aeroport-Mono.woff2` — поправить `@font-face` под фактические имена (или конвертировать otf→woff2 не нужно, `format("opentype")` тоже допустим). Если Aeroport Mono отсутствует — оставить только фолбэк JetBrains Mono (удалить соответствующий `@font-face`).

- [ ] **Step 4: Прогнать все тесты (регресс)**

Run: `.venv/Scripts/python -m pytest -v`
Expected: все PASS (CSS не влияет на тесты — это регресс-проверка)

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/static
git commit -m "feat(dashboard): CSS дизайн-системы JE LA PECHE, анимация гусеницы конвейера"
```

---

### Task 6: actions.review_brief — одобрить/отклонить

**Files:**
- Create: `src/cf/dashboard/actions.py`
- Test: `tests/test_dashboard_actions.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_dashboard_actions.py
import pytest

from cf.dashboard.actions import review_brief
from tests.fakes import FakeSheets


def make_sheets():
    return FakeSheets({
        "briefs": [{"brief_id": "B1", "review_status": "pending",
                    "rejection_reason": "", "reviewer_notes": ""}],
        "run_log": [],
    })


def test_approve_updates_status_and_logs():
    sheets = make_sheets()
    assert review_brief(sheets, "B1", "approved", notes="хороший хук") is True
    brief = sheets.tables["briefs"][0]
    assert brief["review_status"] == "approved"
    assert brief["reviewer_notes"] == "хороший хук"
    assert brief["rejection_reason"] == ""
    (tab, row), = sheets.appended
    assert tab == "run_log" and row["agent"] == "dashboard-review"
    assert row["status"] == "success" and "B1" in row["input_summary"]


def test_reject_fills_rejection_reason():
    sheets = make_sheets()
    review_brief(sheets, "B1", "rejected", notes="слабый CTA")
    brief = sheets.tables["briefs"][0]
    assert brief["review_status"] == "rejected"
    assert brief["rejection_reason"] == "слабый CTA"


def test_unknown_brief_returns_false_and_no_log():
    sheets = make_sheets()
    assert review_brief(sheets, "NOPE", "approved") is False
    assert sheets.appended == []


def test_invalid_decision_raises():
    with pytest.raises(ValueError):
        review_brief(make_sheets(), "B1", "maybe")
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_actions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cf.dashboard.actions'`

- [ ] **Step 3: Реализация**

Тот же контракт полей, что в `cli.cmd_set_review` (`src/cf/cli.py:127`): `review_status`, `rejection_reason`, `reviewer_notes`.

```python
# src/cf/dashboard/actions.py
from cf.runlog import log_run

VALID_DECISIONS = {"approved", "rejected"}


def review_brief(sheets, brief_id, decision, notes=""):
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}")
    found = sheets.update_row_fields("briefs", "brief_id", brief_id, {
        "review_status": decision,
        "rejection_reason": notes if decision == "rejected" else "",
        "reviewer_notes": notes,
    })
    if not found:
        return False
    log_run(sheets, agent="dashboard-review", status="success",
            input_summary=f"{brief_id}: {decision}", trigger_type="dashboard")
    return True
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_actions.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/actions.py tests/test_dashboard_actions.py
git commit -m "feat(dashboard): одобрение/отклонение брифа с записью в run log"
```

---

### Task 7: Страница «Брифы»

**Files:**
- Modify: `src/cf/dashboard/app.py`
- Create: `src/cf/dashboard/templates/briefs.html`
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_dashboard_routes.py`:

```python
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


def test_briefs_filter_pending():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.get("/briefs?status=pending")
    assert "B1" in resp.text and "B2" not in resp.text


def test_review_post_approves_and_rerenders():
    client, sheets = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/B1/review",
                       data={"decision": "approved", "notes": "ок"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert sheets.tables["briefs"][0]["review_status"] == "approved"


def test_review_post_unknown_brief_404():
    client, _ = make_client(BRIEF_TABLES)
    resp = client.post("/briefs/NOPE/review", data={"decision": "approved", "notes": ""})
    assert resp.status_code == 404
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v -k brief`
Expected: FAIL — 404 на `/briefs`

- [ ] **Step 3: Роуты**

Добавить в `create_app` (после `/refresh`):

```python
    from cf.dashboard.actions import review_brief
    from fastapi import Form, HTTPException

    def _briefs_context(request, status_filter, selected_id):
        briefs = cache.rows("briefs")
        counts = {"all": len(briefs)}
        for s in ("pending", "approved", "rejected"):
            counts[s] = sum(1 for b in briefs if _brief_status(b) == s)
        shown = [b for b in briefs
                 if status_filter == "all" or _brief_status(b) == status_filter]
        selected = next((b for b in shown if str(b.get("brief_id")) == selected_id), None)
        if selected is None:
            selected = next((b for b in shown if _brief_status(b) == "pending"), None) \
                or (shown[0] if shown else None)
        return {"briefs": shown, "counts": counts, "selected": selected,
                "status_filter": status_filter}

    def _brief_status(b):
        return str(b.get("review_status", "")).strip().lower()

    @app.get("/briefs")
    def briefs(request: Request, status: str = "all", id: str = ""):
        if status not in ("all", "pending", "approved", "rejected"):
            status = "all"
        try:
            ctx = _briefs_context(request, status, id)
            error = None
        except Exception as exc:
            ctx, error = {"briefs": [], "counts": {}, "selected": None,
                          "status_filter": status}, str(exc)
        return templates.TemplateResponse(request, "briefs.html", {
            "title": "Брифы", "active": "briefs", "stale": cache.stale,
            "error": error, **ctx})

    @app.post("/briefs/{brief_id}/review")
    def post_review(brief_id: str, decision: str = Form(...), notes: str = Form("")):
        if decision not in ("approved", "rejected"):
            raise HTTPException(422, "decision должен быть approved или rejected")
        cache.refresh()
        if not review_brief(sheets, brief_id, decision, notes=notes):
            raise HTTPException(404, f"бриф {brief_id} не найден")
        cache.refresh()
        return RedirectResponse("/briefs?status=pending", status_code=303)
```

- [ ] **Step 4: Шаблон**

```html
<!-- src/cf/dashboard/templates/briefs.html -->
{% extends "base.html" %}
{% block content %}
<header class="page-head">
  <div>
    <div class="eyebrow">CF CREATIVE BRIEFS</div>
    <h1>БРИФЫ</h1>
  </div>
  <nav class="chips">
    {% for key, label in [("all","Все"),("pending","Ожидают"),("approved","Одобрены"),("rejected","Отклонены")] %}
    <a class="chip {{ 'active' if status_filter == key }}" href="/briefs?status={{ key }}">
      {{ label }}{% if counts.get(key) is not none %} · {{ counts[key] }}{% endif %}</a>
    {% endfor %}
  </nav>
</header>

{% if stale %}<div class="banner warn">Sheets недоступен — показаны данные из кеша.</div>{% endif %}
{% if error %}<div class="empty-state">Не удалось прочитать Google Sheets: {{ error }}</div>
{% elif not briefs %}<div class="empty-state">Брифов с таким статусом нет.</div>
{% else %}
<div class="briefs-layout">
  <div class="card" style="padding: 0; overflow: hidden;">
    <table class="queue">
      <thead><tr><th>ID</th><th>Хук</th><th>Ниша</th><th>Статус</th><th>Дата</th></tr></thead>
      <tbody>
        {% for b in briefs %}
        <tr class="{{ 'selected' if selected and b.brief_id == selected.brief_id }}">
          <td class="mono"><a href="/briefs?status={{ status_filter }}&id={{ b.brief_id }}">{{ b.brief_id }}</a></td>
          <td><a href="/briefs?status={{ status_filter }}&id={{ b.brief_id }}">{{ b.hook }}</a></td>
          <td><span class="tag">{{ b.niche or "—" }}</span></td>
          <td><span class="badge {{ b.review_status }}">{{ {"pending":"Ожидает","approved":"Одобрен","rejected":"Отклонён"}.get(b.review_status, b.review_status) }}</span></td>
          <td class="mono">{{ (b.created_at or "")[:10] }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% if selected %}
  <div class="card">
    <div class="eyebrow mono">{{ selected.brief_id }} · {{ selected.formula_id or "—" }} · {{ (selected.niche or "").upper() }}</div>
    <h2 style="font: 600 20px var(--font-display); margin: 8px 0 16px;">{{ selected.hook }}</h2>
    <div class="field-label">СКРИПТ</div>
    <div class="script-box">{{ selected.script }}</div>
    <div class="field-label" style="margin-top: 16px;">РЕФЕРЕНСЫ</div>
    {% for url in (selected.references or "").split() %}
    <div><a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
    {% else %}<div class="empty-state">нет</div>{% endfor %}
    {% if selected.review_status == "pending" %}
    <form method="post" action="/briefs/{{ selected.brief_id }}/review">
      <div class="field-label" style="margin-top: 16px;">ЗАМЕТКА РЕВЬЮЕРА</div>
      <textarea class="note" name="notes" placeholder="Почему одобряем или отклоняем…"></textarea>
      <div class="actions">
        <button class="btn-primary" type="submit" name="decision" value="approved">✓ Одобрить</button>
        <button class="btn-danger-outline" type="submit" name="decision" value="rejected">✕ Отклонить</button>
      </div>
    </form>
    {% else %}
    <div class="field-label" style="margin-top: 16px;">РЕШЕНИЕ</div>
    <div><span class="badge {{ selected.review_status }}">{{ {"approved":"Одобрен","rejected":"Отклонён"}.get(selected.review_status, selected.review_status) }}</span>
      {% if selected.reviewer_notes %}<span style="font-size: 12.5px; color: var(--gray-text);"> — {{ selected.reviewer_notes }}</span>{% endif %}</div>
    {% endif %}
  </div>
  {% endif %}
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v`
Expected: все PASS

- [ ] **Step 6: Commit**

```bash
git add src/cf/dashboard tests/test_dashboard_routes.py
git commit -m "feat(dashboard): экран Брифы — очередь, карточка ревью, одобрить/отклонить"
```

---

### Task 8: StageRunner — запуск звеньев и полный цикл

Механика по спеке: n8n-звенья — POST на webhook-URL из `cf.config.json` (`dashboard.workflows`); Claude-звено — `claude -p <промпт>` подпроцессом. Один запуск на звено одновременно. Результат — в CF Run Log. Полный цикл — raw → factory, останавливается перед ручным одобрением.

**Files:**
- Create: `src/cf/dashboard/runner.py`
- Test: `tests/test_dashboard_runner.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_dashboard_runner.py
import pytest

from cf.dashboard.runner import StageRunner
from tests.fakes import FakeSheets

CONFIG = {"dashboard": {"workflows": {
    "raw": "https://n8n.local/webhook/raw",
    "publish": "https://n8n.local/webhook/publish",
    "stats": "https://n8n.local/webhook/stats",
}}}


def make_runner(config=CONFIG, post_fails=False, cmd_code=0):
    sheets = FakeSheets({"run_log": []})
    posted, commands = [], []

    def http_post(url):
        posted.append(url)
        if post_fails:
            raise ConnectionError("n8n down")

    def run_command(argv):
        commands.append(argv)
        return cmd_code

    runner = StageRunner(sheets, config, http_post=http_post, run_command=run_command)
    return runner, sheets, posted, commands


def test_n8n_stage_posts_webhook_and_logs():
    runner, sheets, posted, _ = make_runner()
    runner.run_sync("raw")
    assert posted == ["https://n8n.local/webhook/raw"]
    assert runner.state["raw"]["status"] == "idle"
    (tab, row), = sheets.appended
    assert row["agent"] == "dashboard-raw" and row["status"] == "success"


def test_claude_stage_runs_prompts_in_order():
    runner, sheets, _, commands = make_runner()
    runner.run_sync("factory")
    assert commands == [["claude", "-p", "/cf-analyze"],
                        ["claude", "-p", "/cf-generate-briefs"]]
    assert sheets.appended[-1][1]["status"] == "success"


def test_stage_failure_sets_error_and_logs_failed():
    runner, sheets, _, _ = make_runner(post_fails=True)
    runner.run_sync("raw")
    assert runner.state["raw"]["status"] == "error"
    assert "n8n down" in runner.state["raw"]["detail"]
    assert sheets.appended[-1][1]["status"] == "failed"


def test_claude_nonzero_exit_is_error():
    runner, _, _, _ = make_runner(cmd_code=2)
    runner.run_sync("factory")
    assert runner.state["factory"]["status"] == "error"


def test_missing_workflow_is_honest_error():
    runner, _, _, _ = make_runner(config={})
    runner.run_sync("stats")
    assert runner.state["stats"]["status"] == "error"
    assert "не настроен" in runner.state["stats"]["detail"]


def test_start_refuses_double_run():
    runner, _, _, _ = make_runner()
    runner.state["raw"]["status"] = "running"
    assert runner.start("raw") is False


def test_unknown_stage_raises():
    runner, _, _, _ = make_runner()
    with pytest.raises(KeyError):
        runner.run_sync("nope")


def test_full_cycle_runs_auto_stages_then_waits():
    runner, sheets, posted, commands = make_runner()
    runner.run_cycle_sync()
    assert posted == ["https://n8n.local/webhook/raw"]
    assert commands[0] == ["claude", "-p", "/cf-analyze"]
    assert runner.cycle_note == "цикл дошёл до ручного одобрения — ждёт продюсера"


def test_full_cycle_stops_on_error():
    runner, _, posted, commands = make_runner(post_fails=True)
    runner.run_cycle_sync()
    assert posted == ["https://n8n.local/webhook/raw"]
    assert commands == []                       # factory не запускался
    assert runner.state["raw"]["status"] == "error"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cf.dashboard.runner'`

- [ ] **Step 3: Реализация**

```python
# src/cf/dashboard/runner.py
import subprocess
import threading

import httpx

from cf.runlog import log_run

STAGES = {
    "raw": {"kind": "n8n"},
    "factory": {"kind": "claude", "prompts": ["/cf-analyze", "/cf-generate-briefs"]},
    "publish": {"kind": "n8n"},
    "stats": {"kind": "n8n"},
}
AUTO_CYCLE = ["raw", "factory"]  # дальше — ручное одобрение, цикл ждёт продюсера


def _default_post(url):
    httpx.post(url, timeout=30.0).raise_for_status()


def _default_run(argv):
    return subprocess.run(argv, timeout=1800).returncode


class StageRunner:
    def __init__(self, sheets, config, http_post=None, run_command=None):
        self.sheets = sheets
        self.config = config or {}
        self.http_post = http_post or _default_post
        self.run_command = run_command or _default_run
        self.state = {s: {"status": "idle", "detail": ""} for s in STAGES}
        self.cycle_note = ""
        self._lock = threading.Lock()

    def any_running(self):
        return any(s["status"] == "running" for s in self.state.values())

    def start(self, stage):
        """Фоновый запуск. False — звено уже выполняется."""
        if stage not in STAGES:
            raise KeyError(stage)
        with self._lock:
            if self.state[stage]["status"] == "running":
                return False
            self.state[stage] = {"status": "running", "detail": "выполняется…"}
        threading.Thread(target=self.run_sync, args=(stage,),
                         kwargs={"already_marked": True}, daemon=True).start()
        return True

    def start_cycle(self):
        if self.any_running():
            return False
        threading.Thread(target=self.run_cycle_sync, daemon=True).start()
        return True

    def run_sync(self, stage, already_marked=False):
        spec = STAGES[stage]  # KeyError для неизвестного звена
        if not already_marked:
            self.state[stage] = {"status": "running", "detail": "выполняется…"}
        try:
            if spec["kind"] == "n8n":
                url = self.config.get("dashboard", {}).get("workflows", {}).get(stage)
                if not url:
                    raise RuntimeError(
                        f"webhook для «{stage}» не настроен в cf.config.json (dashboard.workflows)")
                self.http_post(url)
            else:
                for prompt in spec["prompts"]:
                    code = self.run_command(["claude", "-p", prompt])
                    if code != 0:
                        raise RuntimeError(f"claude -p {prompt}: код выхода {code}")
            self.state[stage] = {"status": "idle", "detail": "успешно"}
            log_run(self.sheets, agent=f"dashboard-{stage}", status="success",
                    trigger_type="dashboard")
            return True
        except Exception as exc:
            self.state[stage] = {"status": "error", "detail": str(exc)}
            log_run(self.sheets, agent=f"dashboard-{stage}", status="failed",
                    errors=[str(exc)], trigger_type="dashboard")
            return False

    def run_cycle_sync(self):
        self.cycle_note = ""
        for stage in AUTO_CYCLE:
            if not self.run_sync(stage):
                self.cycle_note = f"цикл остановлен: ошибка на звене «{stage}»"
                return
        self.cycle_note = "цикл дошёл до ручного одобрения — ждёт продюсера"
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_runner.py -v`
Expected: 9 PASS

- [ ] **Step 5: Дописать в `cf.config.json` секцию (руками, вне git — файл уже содержит секреты ids)**

Показать пользователю (не коммитить, файл настроек локальный):

```json
"dashboard": {
  "port": 8787,
  "workflows": {
    "raw": "https://n8n.example.com/webhook/XXX-raw",
    "publish": "https://n8n.example.com/webhook/XXX-publish",
    "stats": "https://n8n.example.com/webhook/XXX-stats"
  }
}
```

URL-ы вебхуков пользователь берёт из своих n8n-воркфлоу; пока не заполнены — кнопки звеньев честно показывают «webhook … не настроен».

- [ ] **Step 6: Commit**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "feat(dashboard): StageRunner — запуск звеньев n8n/claude, полный цикл"
```

---

### Task 9: Запуск звеньев из UI

**Files:**
- Modify: `src/cf/dashboard/app.py`
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_dashboard_routes.py`:

```python
from cf.dashboard.runner import StageRunner


class SyncRunner(StageRunner):
    """Для тестов: запуск синхронно, без потоков."""
    def start(self, stage):
        if self.state[stage]["status"] == "running":
            return False
        return self.run_sync(stage) or True

    def start_cycle(self):
        self.run_cycle_sync()
        return True


def make_runner_client():
    sheets = FakeSheets({**OVERVIEW_TABLES})
    posted = []
    runner = SyncRunner(
        sheets,
        {"dashboard": {"workflows": {"raw": "https://n8n.local/hook/raw"}}},
        http_post=posted.append,
        run_command=lambda argv: 0,
    )
    app = create_app(sheets=sheets, runner=runner)
    return TestClient(app), runner, posted


def test_stage_run_endpoint_triggers_and_returns_partial():
    client, runner, posted = make_runner_client()
    resp = client.post("/stages/raw/run")
    assert resp.status_code == 200
    assert posted == ["https://n8n.local/hook/raw"]
    assert 'id="stages"' in resp.text            # htmx-партиал конвейера


def test_stage_run_unknown_stage_404():
    client, _, _ = make_runner_client()
    assert client.post("/stages/nope/run").status_code == 404


def test_cycle_run_endpoint():
    client, runner, posted = make_runner_client()
    resp = client.post("/cycle/run", follow_redirects=False)
    assert resp.status_code == 303
    assert posted == ["https://n8n.local/hook/raw"]
    assert "ждёт продюсера" in runner.cycle_note


def test_stages_partial_endpoint():
    client, _, _ = make_runner_client()
    resp = client.get("/partials/stages")
    assert resp.status_code == 200
    assert "Контент-завод" in resp.text
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py -v -k "stage or cycle"`
Expected: FAIL — 404 на `/stages/...`

- [ ] **Step 3: Роуты**

В `create_app` добавить (у `overview` уже есть `runner` в контексте; сигнатура `create_app(sheets=None, cache=None, runner=None, health=None, config=None)` — используем параметр):

```python
    def stages_context(request):
        try:
            metrics = overview_metrics(cache)
        except Exception:
            metrics = {"raw_total": "—", "briefs_created": "—", "briefs_pending": "—",
                       "reels_published": "—", "awaiting_stats": "—"}
        return {"request": request, "metrics": metrics, "runner": runner}

    @app.get("/partials/stages")
    def stages_partial(request: Request):
        return templates.TemplateResponse(request, "partials/stages.html",
                                          stages_context(request))

    @app.post("/stages/{stage}/run")
    def run_stage(stage: str, request: Request):
        if runner is None or stage not in runner.state:
            raise HTTPException(404, f"звено «{stage}» не найдено")
        runner.start(stage)
        cache.refresh()
        return templates.TemplateResponse(request, "partials/stages.html",
                                          stages_context(request))

    @app.post("/cycle/run")
    def run_cycle():
        if runner is not None:
            runner.start_cycle()
        return RedirectResponse("/overview", status_code=303)
```

- [ ] **Step 4: Прогнать все тесты**

Run: `.venv/Scripts/python -m pytest -v`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/app.py tests/test_dashboard_routes.py
git commit -m "feat(dashboard): запуск звеньев и полного цикла из конвейера"
```

---

### Task 10: HealthMonitor — индикатор систем

**Files:**
- Create: `src/cf/dashboard/health.py`
- Create: `src/cf/dashboard/templates/partials/health.html`
- Modify: `src/cf/dashboard/app.py`, `src/cf/dashboard/templates/base.html`
- Test: `tests/test_dashboard_health.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_dashboard_health.py
from cf.dashboard.health import HealthMonitor


def test_run_once_collects_results():
    monitor = HealthMonitor({
        "Google Sheets": lambda: ("ok", "подключено"),
        "n8n": lambda: ("warn", "медленно"),
    })
    monitor.run_once()
    assert monitor.results["Google Sheets"] == ("ok", "подключено")
    assert monitor.results["n8n"] == ("warn", "медленно")
    assert monitor.worst() == "warn"


def test_check_exception_becomes_fail():
    def boom():
        raise ConnectionError("timeout")
    monitor = HealthMonitor({"n8n": boom})
    monitor.run_once()
    status, detail = monitor.results["n8n"]
    assert status == "fail" and "timeout" in detail
    assert monitor.worst() == "fail"


def test_unchecked_defaults_to_warn():
    monitor = HealthMonitor({"x": lambda: ("ok", "")})
    assert monitor.results["x"][0] == "warn"
    assert monitor.worst() == "warn"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_health.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализация**

```python
# src/cf/dashboard/health.py
import shutil
import threading
import time
from datetime import datetime

import httpx

_ORDER = {"ok": 0, "warn": 1, "fail": 2}


class HealthMonitor:
    def __init__(self, checks, interval=60.0):
        self.checks = checks  # name -> callable -> (status, detail); status: ok|warn|fail
        self.interval = interval
        self.results = {name: ("warn", "ещё не проверялось") for name in checks}
        self.checked_at = None

    def run_once(self):
        for name, check in self.checks.items():
            try:
                self.results[name] = check()
            except Exception as exc:
                self.results[name] = ("fail", str(exc))
        self.checked_at = datetime.now().strftime("%H:%M")

    def worst(self):
        return max((s for s, _ in self.results.values()),
                   key=_ORDER.__getitem__, default="warn")

    def start(self):
        def loop():
            while True:
                self.run_once()
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True).start()


def default_checks(sheets, config):
    def check_sheets():
        t0 = time.monotonic()
        sheets.read_rows("run_log")
        return ("ok", f"подключено · {time.monotonic() - t0:.1f} с")

    def check_n8n():
        base = (config or {}).get("n8n", {}).get("base_url", "")
        if not base:
            return ("warn", "base_url не настроен")
        httpx.get(f"{base.rstrip('/')}/healthz", timeout=10.0).raise_for_status()
        return ("ok", "отвечает")

    def check_claude():
        if shutil.which("claude"):
            return ("ok", "CLI найден")
        return ("fail", "claude CLI не найден в PATH")

    return {"Google Sheets": check_sheets, "n8n": check_n8n, "Claude Code": check_claude}
```

- [ ] **Step 4: Партиал и кнопка в сайдбаре**

```html
<!-- src/cf/dashboard/templates/partials/health.html -->
<button class="sys-btn" type="button"
        hx-get="/partials/health" hx-trigger="every 60s" hx-swap="outerHTML">
  <span class="dot {{ {'ok':'ok','warn':'warn','fail':'fail'}[health.worst()] }}"></span>
  СИСТЕМЫ · LIVE
  <div class="sys-pop">
    <div class="sys-pop-head"><span>СОСТОЯНИЕ СИСТЕМ</span><span class="mono">{{ health.checked_at or "—" }}</span></div>
    {% for name, (status, detail) in health.results.items() %}
    <div class="sys-row"><span class="dot {{ status }}"></span><strong>{{ name }}</strong>
      <span class="row-meta">{{ detail }}</span></div>
    {% endfor %}
  </div>
</button>
```

В `base.html` внутрь `{% block sidebar_foot %}{% endblock %}` — заменить блок на:

```html
      {% if health %}{% include "partials/health.html" %}{% endif %}
```

В `create_app` добавить (health передаётся параметром; по умолчанию создаётся с реальными проверками в Task 11):

```python
    app.state.health = health

    @app.get("/partials/health")
    def health_partial(request: Request):
        return templates.TemplateResponse(request, "partials/health.html",
                                          {"request": request, "health": health})
```

И во все `TemplateResponse` страниц (`overview`, `briefs`, stub-разделы) добавить в контекст `"health": health`.

- [ ] **Step 5: Тест эндпоинта**

Добавить в `tests/test_dashboard_health.py`:

```python
from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from tests.fakes import FakeSheets


def test_health_partial_renders_lamps():
    monitor = HealthMonitor({"Google Sheets": lambda: ("ok", "подключено")})
    monitor.run_once()
    app = create_app(sheets=FakeSheets({}), health=monitor)
    client = TestClient(app)
    resp = client.get("/partials/health")
    assert resp.status_code == 200
    assert "СОСТОЯНИЕ СИСТЕМ" in resp.text and "подключено" in resp.text
```

- [ ] **Step 6: Прогнать все тесты**

Run: `.venv/Scripts/python -m pytest -v`
Expected: все PASS

- [ ] **Step 7: Commit**

```bash
git add src/cf/dashboard tests/test_dashboard_health.py
git commit -m "feat(dashboard): индикатор систем с лампочками и фоновыми проверками"
```

---

### Task 11: CLI-команда `cf dashboard` и сборка прода

**Files:**
- Modify: `src/cf/cli.py`
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_dashboard_routes.py`:

```python
def test_build_production_app_wires_dependencies():
    from cf.dashboard.app import build_production_app
    sheets = FakeSheets({"run_log": []})
    app = build_production_app(sheets=sheets, config={"n8n": {"base_url": ""}})
    assert app.state.sheets is sheets
    assert app.state.health is not None
    assert app.state.cache is not None
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/Scripts/python -m pytest tests/test_dashboard_routes.py::test_build_production_app_wires_dependencies -v`
Expected: FAIL — `ImportError: cannot import name 'build_production_app'`

- [ ] **Step 3: Фабрика прода в `app.py`**

```python
# добавить в конец src/cf/dashboard/app.py
def build_production_app(sheets=None, config=None):
    from cf.config import load_config
    from cf.dashboard.data import DataCache
    from cf.dashboard.health import HealthMonitor, default_checks
    from cf.dashboard.runner import StageRunner

    if sheets is None:
        from cf.sheets import Sheets
        sheets = Sheets()
    config = config or load_config()
    cache = DataCache(sheets)
    runner = StageRunner(sheets, config)
    health = HealthMonitor(default_checks(sheets, config))
    health.start()
    return create_app(sheets=sheets, cache=cache, runner=runner,
                      health=health, config=config)
```

- [ ] **Step 4: Подкоманда CLI**

В `src/cf/cli.py` — по образцу соседних `cmd_*` и `sub.add_parser` (регистрация рядом с `install-hooks`, `src/cf/cli.py:364`):

```python
def cmd_dashboard(sheets, args):
    import uvicorn
    from cf.dashboard.app import build_production_app
    app = build_production_app(sheets=sheets)
    print(f"CF Dashboard: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)
    return 0
```

Регистрация парсера (в `build_parser`/`main` рядом с остальными):

```python
    sp = sub.add_parser("dashboard", help="Локальный веб-дашборд CF")
    sp.add_argument("--port", type=int, default=8787)
    sp.set_defaults(func=cmd_dashboard)
```

Перед вставкой посмотреть, как соседние подкоманды получают `sheets` и `func` в этом файле, и повторить их паттерн точно (если там `set_defaults(cmd=...)` или диспетч по `args.command` — сделать так же).

- [ ] **Step 5: Прогнать все тесты + ручной запуск**

Run: `.venv/Scripts/python -m pytest -v`
Expected: все PASS

Run: `.venv/Scripts/python -m cf dashboard` (прервать Ctrl+C после проверки)
Expected: строка `CF Dashboard: http://127.0.0.1:8787`, сервер поднимается без трейсбека

- [ ] **Step 6: Commit**

```bash
git add src/cf/cli.py src/cf/dashboard/app.py tests/test_dashboard_routes.py
git commit -m "feat(dashboard): команда cf dashboard, продовая сборка приложения"
```

---

### Task 12: Визуальная сверка и финальная верификация

**Files:** правки по результатам сверки (CSS/шаблоны)

- [ ] **Step 1: Запустить и сверить с макетом**

Run: `.venv/Scripts/python -m cf dashboard` и открыть `http://127.0.0.1:8787` в браузере.
Сверить «Обзор» и «Брифы» с `docs/design/cf-dashboard.pen` (страницы обзора и брифов): сайдбар #131417, плитки, конвейер с гусеницей (лента движется слева направо, при `prefers-reduced-motion` — статична), голубое слово в заголовке, моно-цифры, чипы статусов. Расхождения поправить в `style.css`/шаблонах.

- [ ] **Step 2: Проверить деградацию**

Временно переименовать `secrets/service-account.json` → запустить → на страницах честная ошибка/empty-state, а не трейсбек. Вернуть файл на место.

- [ ] **Step 3: Полный прогон + guard**

Run: `.venv/Scripts/python -m pytest -v`
Expected: все PASS

Run: `.venv/Scripts/python -m cf check-commit`
Expected: секретов в staged нет

- [ ] **Step 4: Commit финальных правок**

```bash
git add -A src/cf/dashboard
git commit -m "polish(dashboard): визуальная сверка с макетом cf-dashboard.pen"
```

---

## Self-Review

- **Spec coverage (этап 1):** плитки (Task 3, 4) ✓; конвейер 5 звеньев, клики в разделы, ▶ у автоматических, у ручного — стрелка (Task 4 партиал) ✓; полный цикл до одобрения (Task 8, 9) ✓; гусеница с анимацией и reduced-motion (Task 5) ✓; график ER + запуски (Task 3, 4) ✓; брифы: чипы, таблица, карточка, Одобрить/Отклонить, контракт `set-review` + run log (Task 6, 7) ✓; кеш 60 с + stale-плашка + refresh (Task 2, 4) ✓; индикатор систем (Task 10) ✓; `cf dashboard` на 8787 (Task 11) ✓; разделы этапа 2 — заглушки (Task 1) ✓; чат — вне охвата (заглушку-кнопку не делаем: мёртвая кнопка хуже её отсутствия, добавится в этапе 3 вместе с функцией).
- **Отступление от спеки (осознанное):** спека упоминает бейдж-счётчик у пункта «Брифы» в сайдбаре — не включён в MVP: сайдбар рендерится на каждой странице, а метрики доступны не везде; счётчик виден на плитке и конвейере. Добавить в этапе 2 вместе с общим контекстом сайдбара.
- **Placeholder scan:** «как соседние подкоманды» в Task 11 Step 4 — намеренная инструкция свериться с фактическим паттерном диспатча cli.py (файл длинный, паттерн виден на месте); остальное — полный код.
- **Type consistency:** `runner.state[key].status/detail` — dict-доступ Jinja ✓; `any_running()` есть в StageRunner ✓; `SyncRunner` переопределяет только `start`/`start_cycle` ✓; `health.worst()`/`results`/`checked_at` совпадают ✓; `cache.stale`/`refresh()` совпадают ✓.
