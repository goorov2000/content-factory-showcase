# Content Factory Agent System — Implementation Plan (MVP)

Дата: 2026-07-09

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (коммиты напрямую в master, финальный — 7807aeb, 2026-07-09). Чекбоксы в ходе исполнения не велись.

**Goal:** Замкнуть evidence-backed цикл контент-завода — raw rows → pattern analysis → formula → brief → review → performance → eval → prompt proposal — как intelligence-слой на Claude Code поверх существующих n8n + Google Sheets + GitHub.

**Architecture:** Тонкий Python-пакет `cf` (доступ к Google Sheets через service account, Run Log, JSON-schema-валидаторы, trace, guard) + slash-команды `.claude/commands/cf-*.md`, которые оркестрируют LLM-работу по промптам агентов из `prompts/agents/*.md`. Runtime-артефакты живут в негитуемом `agent-runtime/`; формулы/промпты/proposals версионируются в git; human-in-the-loop обязателен для изменений промптов.

**Tech Stack:** Python 3.13, gspread + google-auth (service account), jsonschema, pytest; Claude Code (slash-команды, агентские промпты, память `.claude/memory/`); git.

**Спека:** `thoughts/shared/specs/2026-07-09-content-factory-agent-system.md`

---

## Правила исполнения плана

- Рабочая директория — корень репо `CF/`. Все команды даны для Git Bash на Windows: python в venv — `.venv/Scripts/python` (на POSIX было бы `.venv/bin/python`).
- Task 1 инициализирует git. Сразу после initial commit создаётся ветка `feat/content-factory-mvp` — вся дальнейшая работа в ней, в main не работать.
- TDD: в каждой задаче с кодом сначала падающий тест, потом минимальная реализация. Не переходить к следующему шагу, пока текущий не проверен.
- Секреты: `secrets/service-account.json` (в `.gitignore`). Реальные вызовы Google API в тестах запрещены — только фейки из `tests/fakes.py`.
- Статусы Run Log: `success | failed | insufficient_data`. Честность `insufficient_data` — системное правило: мало данных → никакой аналитики, только отчёт о нехватке.
- Промпты агентов и slash-команды — тоже артефакты плана: их текст приведён полностью, копировать как есть.

## File Structure

```
CF/
├── CLAUDE.md                        # правила системы (Task 2)
├── .gitignore                       # Task 1
├── pyproject.toml                   # Task 1
├── cf.config.json                   # id таблицы, вкладки, путь к ключу (Task 4)
├── .claude/
│   ├── commands/                    # slash-команды /cf-* (Tasks 7–18)
│   └── memory/                      # память агентов: MEMORY.md + patterns/ decisions/ evaluations/ (Task 2)
├── prompts/
│   ├── agents/                      # промпты агентов (Tasks 8–17)
│   └── briefs/_shared/schema.md     # человекочитаемая схема брифа для n8n (Task 12)
├── schemas/                         # JSON-схемы: patterns, formula, brief (Tasks 9, 10, 12)
├── formulas/                        # формулы по нишам + _approved/index.json (Tasks 10, 11)
├── proposals/                       # proposals для промптов (Task 17)
├── docs/
│   ├── n8n-integration.md           # контракт с n8n (Task 15)
│   ├── dry-run-checklist.md         # ручной сквозной прогон (Task 21)
│   └── superpowers/plans/           # этот план
├── agent-runtime/                   # NOT committed: profiles/ patterns/ batches/ reviews/ evals/
├── secrets/                         # NOT committed: service-account.json
├── src/cf/                          # python-пакет
│   ├── __init__.py  __main__.py
│   ├── config.py    # загрузка cf.config.json
│   ├── retry.py     # with_retry (3 попытки)
│   ├── io.py        # атомарная запись JSON
│   ├── sheets.py    # Sheets: read_rows / append_row / update_row_fields
│   ├── runlog.py    # запись в CF Run Log
│   ├── profile.py   # профилирование батча (чистые функции)
│   ├── validate.py  # jsonschema-валидация артефактов
│   ├── evalprep.py  # join Performance×Briefs×Versions
│   ├── proposals.py # валидация proposal-файлов
│   ├── guard.py     # pre-commit проверка
│   ├── trace.py     # relationship chain по brief_id
│   └── cli.py       # argparse: все подкоманды python -m cf
├── tests/                           # pytest; fakes.py — фейковый Sheets
│   └── fixtures/                    # валидные patterns/formula/brief для e2e
└── thoughts/shared/specs/           # спека (уже существует)
```

Каждый модуль — одна ответственность; вся работа с внешним миром (Sheets) изолирована в `sheets.py` и подменяется фейком из `tests/fakes.py`.

---

### Task 1: Каркас репозитория, git, pyproject, smoke-тест

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `src/cf/__init__.py`, `tests/test_smoke.py`, `.gitkeep`-файлы в версионируемых пустых каталогах

- [ ] **Step 1: Инициализировать git и создать структуру каталогов**

```bash
cd <local-path>
git init
mkdir -p src/cf tests/fixtures schemas formulas/_approved proposals \
  prompts/agents prompts/briefs/_shared \
  .claude/commands .claude/memory/patterns .claude/memory/decisions .claude/memory/evaluations \
  agent-runtime/profiles agent-runtime/patterns agent-runtime/batches agent-runtime/reviews agent-runtime/evals \
  secrets docs
touch schemas/.gitkeep formulas/.gitkeep formulas/_approved/.gitkeep proposals/.gitkeep \
  prompts/agents/.gitkeep prompts/briefs/_shared/.gitkeep .claude/commands/.gitkeep \
  .claude/memory/patterns/.gitkeep .claude/memory/decisions/.gitkeep .claude/memory/evaluations/.gitkeep
```

- [ ] **Step 2: Создать `.gitignore`**

```gitignore
agent-runtime/
secrets/
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
```

- [ ] **Step 3: Создать `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "cf"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "gspread>=6.1",
  "google-auth>=2.30",
  "jsonschema>=4.22",
]

[project.optional-dependencies]
dev = ["pytest>=8.2"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Создать пакет и smoke-тест**

`src/cf/__init__.py` — пустой файл.

`tests/test_smoke.py`:
```python
def test_package_imports():
    import cf
    assert cf is not None
```

- [ ] **Step 5: Создать venv, установить пакет, прогнать тест**

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -v
```
Expected: `1 passed`

- [ ] **Step 6: Проверить, что agent-runtime/ игнорируется**

```bash
echo '{}' > agent-runtime/profiles/test.json
git status --porcelain | grep agent-runtime || echo "OK: ignored"
git check-ignore agent-runtime/profiles/test.json && echo "OK: check-ignore"
rm agent-runtime/profiles/test.json
```
Expected: обе строки `OK: ...`, agent-runtime не появляется в `git status`.

- [ ] **Step 7: Initial commit и рабочая ветка**

```bash
git add -A
git commit -m "chore: repo scaffold - package cf, gitignore for agent-runtime and secrets"
git checkout -b feat/content-factory-mvp
```

---

### Task 2: CLAUDE.md и скелет памяти

**Files:**
- Create: `CLAUDE.md`, `.claude/memory/MEMORY.md`

- [ ] **Step 1: Создать `CLAUDE.md`** (правила действуют на все последующие задачи и на работу агентов)

```markdown
# Content Factory — правила системы

## Что это
Intelligence-слой контент-завода. n8n = runtime (сбор данных, генерация брифов) — НЕ заменяется.
Claude Code = анализ паттернов, формулы, ревью брифов, eval, prompt proposals.

## Железные правила
1. **Evidence-first.** Ни одного вывода без ссылок на данные (source_urls, метрики).
   Паттерн, формула или proposal без evidence не создаются.
2. **insufficient_data — честный ответ.** Если данных мало — статус `insufficient_data`
   плюс перечень того, чего не хватает. Никаких выводов «на глаз».
3. **Human-in-the-loop для промптов.** Изменение промптов только через
   proposal → ревью оператора → применение + git commit. Auto-push запрещён.
4. **Runtime-артефакты только в `agent-runtime/`** (не версионируется).
   Версионируются: `prompts/`, `formulas/`, `proposals/`, `schemas/`, `.claude/`.
5. **Секреты не коммитятся.** `secrets/` в `.gitignore`; перед коммитом работает pre-commit guard.
6. **Каждый запуск агента логируется** в CF Run Log — либо автоматически из CLI,
   либо командой `python -m cf log-run ...` в конце работы агента.

## Инструменты
- CLI: `.venv/Scripts/python -m cf <command>`. Подкоманды: read, profile, status, validate,
  log-run, set-review, rejection-history, eval-prep, log-prompt-version, approve-formula,
  trace, check-commit, install-hooks.
- Конфиг: `cf.config.json` (id таблицы, имена вкладок, путь к service-account ключу).
- Slash-команды: /cf-profile, /cf-analyze, /cf-formula, /cf-review-brief,
  /cf-propose-update, /cf-eval, /cf-status.

## Память агентов
`.claude/memory/MEMORY.md` — индекс: одна строка на файл памяти.
- Наблюдения анализа → `.claude/memory/patterns/{niche}-observations.md`
- Решения оператора → `.claude/memory/decisions/YYYY-MM-DD-<решение>.md`
- Инсайты eval → `.claude/memory/evaluations/YYYY-MM-DD-insights.md`
После записи файла памяти — обновить строку в MEMORY.md.

## Данные
Google Sheets — операционное хранилище. Вкладки: CF Raw TikTok, CF Raw Instagram,
CF Creative Briefs, CF Published Reels, CF Performance, CF Prompt Versions, CF Run Log.
GitHub — версионируемое хранилище (промпты, формулы, proposals, решения).
Ключ связей: source_url → pattern_id → formula_id → brief_id → prompt_version → reel_id → performance.
```

- [ ] **Step 2: Создать `.claude/memory/MEMORY.md`**

```markdown
# CF Agent Memory — Index

<!-- Одна строка на память: - [Название](путь) — суть.
     Подкаталоги: patterns/ (наблюдения), decisions/ (решения оператора), evaluations/ (инсайты eval). -->
```

- [ ] **Step 3: Проверить и закоммитить**

```bash
grep -c "insufficient_data" CLAUDE.md   # Expected: >= 1
git add CLAUDE.md .claude/memory/MEMORY.md
git commit -m "docs: CLAUDE.md system rules and agent memory skeleton"
```

---

### Task 3: cf.retry — повторы с понятной ошибкой

**Files:**
- Create: `src/cf/retry.py`
- Test: `tests/test_retry.py`

- [ ] **Step 1: Написать падающие тесты** — `tests/test_retry.py`:

```python
import pytest

from cf.retry import RetryError, with_retry


def test_returns_result_on_first_success():
    assert with_retry(lambda: 42) == 42


def test_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    assert with_retry(flaky, base_delay=0) == "ok"
    assert calls["n"] == 3


def test_raises_retry_error_with_label_after_attempts():
    def always_fails():
        raise ValueError("boom")

    with pytest.raises(RetryError) as exc_info:
        with_retry(always_fails, base_delay=0, label="sheets read")
    assert "sheets read" in str(exc_info.value)
    assert "3 attempts" in str(exc_info.value)
```

- [ ] **Step 2: Убедиться, что тесты падают**

```bash
.venv/Scripts/python -m pytest tests/test_retry.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.retry'`

- [ ] **Step 3: Минимальная реализация** — `src/cf/retry.py`:

```python
import time


class RetryError(RuntimeError):
    pass


def with_retry(fn, *, attempts=3, base_delay=1.0, retriable=(Exception,), label=""):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retriable as exc:
            last = exc
            if attempt < attempts:
                time.sleep(base_delay * 2 ** (attempt - 1))
    raise RetryError(
        f"{label or 'operation'} failed after {attempts} attempts: {last}"
    ) from last
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_retry.py -v
```
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/cf/retry.py tests/test_retry.py
git commit -m "feat: with_retry helper - 3 attempts, clear error"
```

---

### Task 4: cf.config + cf.io — конфиг и атомарная запись JSON

**Files:**
- Create: `src/cf/config.py`, `src/cf/io.py`, `cf.config.json`
- Test: `tests/test_config.py`, `tests/test_io.py`

- [ ] **Step 1: Падающие тесты конфига** — `tests/test_config.py`:

```python
import json

import pytest

from cf.config import load_config


def test_loads_config_from_explicit_path(tmp_path):
    p = tmp_path / "cf.config.json"
    p.write_text(json.dumps({"spreadsheet_id": "abc", "tabs": {}}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg["spreadsheet_id"] == "abc"


def test_missing_config_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError) as exc_info:
        load_config(tmp_path / "nope.json")
    assert "cf.config.json" in str(exc_info.value)
```

- [ ] **Step 2: Падающие тесты io** — `tests/test_io.py`:

```python
from cf.io import read_json, write_json_atomic


def test_write_and_read_roundtrip(tmp_path):
    path = tmp_path / "sub" / "data.json"
    write_json_atomic(path, {"x": 1, "текст": "да"})
    assert read_json(path) == {"x": 1, "текст": "да"}


def test_no_tmp_leftovers(tmp_path):
    write_json_atomic(tmp_path / "d.json", [1, 2])
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
```

- [ ] **Step 3: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_config.py tests/test_io.py -v
```
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 4: Реализация** — `src/cf/config.py`:

```python
import json
import os
from pathlib import Path


def load_config(path=None):
    p = Path(path or os.environ.get("CF_CONFIG", "cf.config.json"))
    if not p.exists():
        raise FileNotFoundError(
            f"cf.config.json не найден по пути {p}. Создай его в корне репо: "
            f"spreadsheet_id, tabs, service_account_file (см. образец в плане, Task 4)."
        )
    return json.loads(p.read_text(encoding="utf-8"))
```

`src/cf/io.py`:

```python
import json
import os
import tempfile
from pathlib import Path


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
```

- [ ] **Step 5: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_config.py tests/test_io.py -v
```
Expected: `4 passed`

- [ ] **Step 6: Создать реальный `cf.config.json`** (spreadsheet_id оператор впишет свой):

```json
{
  "spreadsheet_id": "REPLACE_WITH_SPREADSHEET_ID",
  "service_account_file": "secrets/service-account.json",
  "tabs": {
    "raw_tiktok": "CF Raw TikTok",
    "raw_instagram": "CF Raw Instagram",
    "briefs": "CF Creative Briefs",
    "reels": "CF Published Reels",
    "performance": "CF Performance",
    "prompt_versions": "CF Prompt Versions",
    "run_log": "CF Run Log"
  }
}
```

- [ ] **Step 7: Commit**

```bash
git add src/cf/config.py src/cf/io.py cf.config.json tests/test_config.py tests/test_io.py
git commit -m "feat: config loader and atomic json io"
```

---

### Task 5: cf.sheets — клиент Google Sheets (service account)

Решение открытого вопроса №1 спеки: прямой доступ через Google Sheets API (gspread + service account). Оператор создаёт service account в Google Cloud, включает Sheets API, шарит таблицу на email сервисного аккаунта и кладёт JSON-ключ в `secrets/service-account.json`.

**Files:**
- Create: `src/cf/sheets.py`, `tests/fakes.py`
- Test: `tests/test_sheets.py`

- [ ] **Step 1: Создать фейки** — `tests/fakes.py` (используются во всех последующих тестах):

```python
class FakeWorksheet:
    def __init__(self, headers, rows=None, fail_reads=0):
        self.headers = list(headers)
        self.rows = [list(r) for r in (rows or [])]
        self.fail_reads = fail_reads

    def get_all_records(self):
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise ConnectionError("transient")
        return [dict(zip(self.headers, r)) for r in self.rows]

    def row_values(self, n):
        return self.headers if n == 1 else self.rows[n - 2]

    def append_row(self, values, value_input_option="RAW"):
        self.rows.append(list(values))

    def update_cell(self, row, col, value):
        self.rows[row - 2][col - 1] = value


class FakeClient:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def open_by_key(self, key):
        return self

    def worksheet(self, title):
        return self._worksheets[title]


class FakeSheets:
    """Мимикрирует публичный интерфейс cf.sheets.Sheets для тестов CLI и логики."""

    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.appended = []

    def read_rows(self, tab_key):
        return [dict(r) for r in self.tables.get(tab_key, [])]

    def append_row(self, tab_key, row):
        self.tables.setdefault(tab_key, []).append(dict(row))
        self.appended.append((tab_key, dict(row)))

    def update_row_fields(self, tab_key, key_column, key_value, fields):
        for r in self.tables.get(tab_key, []):
            if str(r.get(key_column)) == str(key_value):
                r.update(fields)
                return True
        return False
```

- [ ] **Step 2: Падающие тесты** — `tests/test_sheets.py`:

```python
from cf.sheets import Sheets

from tests.fakes import FakeClient, FakeWorksheet

CFG = {
    "spreadsheet_id": "x",
    "service_account_file": "unused",
    "tabs": {"run_log": "CF Run Log", "briefs": "CF Creative Briefs"},
}


def make_sheets(worksheets):
    return Sheets(config=CFG, client=FakeClient(worksheets), retry_delay=0)


def test_read_rows_returns_dicts():
    ws = FakeWorksheet(["run_id", "agent"], [["r1", "profiler"]])
    s = make_sheets({"CF Run Log": ws})
    assert s.read_rows("run_log") == [{"run_id": "r1", "agent": "profiler"}]


def test_read_rows_retries_transient_errors():
    ws = FakeWorksheet(["run_id"], [["r1"]], fail_reads=2)
    s = make_sheets({"CF Run Log": ws})
    assert s.read_rows("run_log") == [{"run_id": "r1"}]


def test_append_row_maps_by_headers():
    ws = FakeWorksheet(["run_id", "agent", "status"])
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"agent": "eval", "run_id": "r2"})
    assert ws.rows == [["r2", "eval", ""]]


def test_update_row_fields_updates_matching_row():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"], ["b2", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "b2", {"review_status": "approved"}) is True
    assert ws.rows[1] == ["b2", "approved"]


def test_update_row_fields_returns_false_when_not_found():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "nope", {"review_status": "x"}) is False
```

- [ ] **Step 3: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_sheets.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.sheets'`

- [ ] **Step 4: Реализация** — `src/cf/sheets.py`:

```python
import gspread
from google.oauth2.service_account import Credentials

from cf.config import load_config
from cf.retry import with_retry

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class Sheets:
    def __init__(self, config=None, client=None, retry_delay=1.0):
        self._config = config
        self._client = client
        self.retry_delay = retry_delay

    @property
    def config(self):
        if self._config is None:
            self._config = load_config()
        return self._config

    @property
    def client(self):
        if self._client is None:
            creds = Credentials.from_service_account_file(
                self.config["service_account_file"], scopes=SCOPES
            )
            self._client = gspread.authorize(creds)
        return self._client

    def _ws(self, tab_key):
        title = self.config["tabs"][tab_key]
        return self.client.open_by_key(self.config["spreadsheet_id"]).worksheet(title)

    def read_rows(self, tab_key):
        def op():
            return self._ws(tab_key).get_all_records()

        return with_retry(op, base_delay=self.retry_delay, label=f"read {tab_key}")

    def append_row(self, tab_key, row):
        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            ws.append_row([str(row.get(h, "")) for h in headers], value_input_option="RAW")

        with_retry(op, base_delay=self.retry_delay, label=f"append {tab_key}")

    def update_row_fields(self, tab_key, key_column, key_value, fields):
        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            for i, rec in enumerate(ws.get_all_records()):
                if str(rec.get(key_column)) == str(key_value):
                    for col_name, value in fields.items():
                        if col_name in headers:
                            ws.update_cell(i + 2, headers.index(col_name) + 1, str(value))
                    return True
            return False

        return with_retry(op, base_delay=self.retry_delay, label=f"update {tab_key}")
```

- [ ] **Step 5: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_sheets.py -v
```
Expected: `5 passed`

- [ ] **Step 6: Commit**

```bash
git add src/cf/sheets.py tests/fakes.py tests/test_sheets.py
git commit -m "feat: sheets client - service account, retry, read/append/update"
```

---

### Task 6: cf.runlog — запись запусков в CF Run Log

**Files:**
- Create: `src/cf/runlog.py`
- Test: `tests/test_runlog.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_runlog.py`:

```python
import json

import pytest

from cf import runlog

from tests.fakes import FakeSheets


def test_log_run_appends_row_to_run_log():
    sheets = FakeSheets()
    row = runlog.log_run(sheets, agent="raw-batch-profiler", status="success",
                         input_summary="raw_tiktok", output_paths=["agent-runtime/x.json"])
    tab, appended = sheets.appended[0]
    assert tab == "run_log"
    assert appended["agent"] == "raw-batch-profiler"
    assert appended["status"] == "success"
    assert json.loads(appended["output_paths"]) == ["agent-runtime/x.json"]
    assert row["run_id"] and row["started_at"] and row["completed_at"]


def test_log_run_rejects_unknown_status():
    with pytest.raises(ValueError):
        runlog.log_run(FakeSheets(), agent="x", status="meh")
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_runlog.py -v
```
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Реализация** — `src/cf/runlog.py`:

```python
import json
import uuid
from datetime import datetime, timezone

VALID_STATUSES = {"success", "failed", "insufficient_data"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_run(sheets, agent, status, input_summary="", output_paths=(), errors=(),
            trigger_type="manual", started_at=None, run_id=None):
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}, got {status!r}")
    completed = now_iso()
    row = {
        "run_id": run_id or uuid.uuid4().hex[:12],
        "agent": agent,
        "trigger_type": trigger_type,
        "started_at": started_at or completed,
        "completed_at": completed,
        "status": status,
        "input_summary": input_summary,
        "output_paths": json.dumps(list(output_paths), ensure_ascii=False),
        "errors": json.dumps(list(errors), ensure_ascii=False),
    }
    sheets.append_row("run_log", row)
    return row
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_runlog.py -v
```
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/cf/runlog.py tests/test_runlog.py
git commit -m "feat: run log writer with status validation"
```

---

### Task 7: CLI `python -m cf` — read / log-run / status + команда /cf-status

**Files:**
- Create: `src/cf/cli.py`, `src/cf/__main__.py`, `.claude/commands/cf-status.md`
- Test: `tests/test_cli_basic.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_cli_basic.py`:

```python
import argparse
import json

from cf.cli import apply_filters, build_parser, cmd_read, cmd_status

from tests.fakes import FakeSheets


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


def test_cmd_status_prints_recent_runs_newest_first(capsys):
    runs = [{"run_id": f"r{i}", "agent": "profiler", "status": "success",
             "completed_at": f"2026-07-0{i}"} for i in range(1, 6)]
    sheets = FakeSheets({"run_log": runs})
    assert cmd_status(sheets, ns(limit=3)) == 0
    out = capsys.readouterr().out
    assert "r5" in out and "r3" in out and "r2" not in out
    assert out.index("r5") < out.index("r3")


def test_parser_wires_subcommands():
    args = build_parser().parse_args(["read", "raw_tiktok", "--niche", "pets"])
    assert args.tab == "raw_tiktok" and args.niche == "pets" and args.func is cmd_read
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_basic.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.cli'`

- [ ] **Step 3: Реализация** — `src/cf/cli.py`:

```python
import argparse
import json
import sys

from cf import runlog
from cf.io import write_json_atomic
from cf.sheets import Sheets


def apply_filters(rows, niche=None, since=None):
    if niche:
        rows = [r for r in rows if str(r.get("niche", "")).lower() == niche.lower()]
    if since:
        rows = [r for r in rows if str(r.get("posted_at", "")) >= since]
    return rows


def cmd_read(sheets, args):
    rows = apply_filters(sheets.read_rows(args.tab), args.niche, args.since)
    if args.out:
        write_json_atomic(args.out, rows)
        print(f"Wrote {len(rows)} rows to {args.out}")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_log_run(sheets, args):
    row = runlog.log_run(sheets, agent=args.agent, status=args.status,
                         input_summary=args.input or "",
                         output_paths=args.outputs, errors=args.errors,
                         trigger_type=args.trigger)
    print(f"logged run {row['run_id']} ({args.agent}: {args.status})")
    return 0


def cmd_status(sheets, args):
    runs = sheets.read_rows("run_log")[-args.limit:][::-1]
    print(f"Recent runs ({len(runs)}):")
    for r in runs:
        print(f"  {str(r.get('run_id', '')):14} {str(r.get('agent', '')):22} "
              f"{str(r.get('status', '')):18} {r.get('completed_at', '')}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="cf", description="Content Factory CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("read", help="Прочитать вкладку Sheets, вывести/сохранить JSON")
    sp.add_argument("tab")
    sp.add_argument("--niche")
    sp.add_argument("--since")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("log-run", help="Записать запуск агента в CF Run Log")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--status", required=True, choices=sorted(runlog.VALID_STATUSES))
    sp.add_argument("--input")
    sp.add_argument("--outputs", nargs="*", default=[])
    sp.add_argument("--errors", nargs="*", default=[])
    sp.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "event"])
    sp.set_defaults(func=cmd_log_run)

    sp = sub.add_parser("status", help="Последние запуски агентов")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_status)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(Sheets(), args)
    except Exception as exc:  # оператору нужна причина, а не трейсбек
        print(f"error: {exc}", file=sys.stderr)
        return 1
```

`src/cf/__main__.py`:

```python
import sys

from cf.cli import main

sys.exit(main())
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_cli_basic.py -v
```
Expected: `5 passed`

- [ ] **Step 5: Проверить понятную ошибку без настроенных секретов**

```bash
.venv/Scripts/python -m cf status; echo "exit=$?"
```
Expected: `error: ...` (про cf.config.json placeholder или secrets/service-account.json), `exit=1`. Никакого трейсбека.

- [ ] **Step 6: Создать `.claude/commands/cf-status.md`** (базовая версия; расширится в Task 18)

```markdown
---
description: Content Factory - последние запуски агентов
---
Выполни `.venv/Scripts/python -m cf status --limit 10` и покажи оператору результат.

Если команда падает с ошибкой про cf.config.json или secrets/service-account.json —
не чини сам: объясни оператору, что настроить (spreadsheet_id в cf.config.json;
JSON-ключ сервисного аккаунта в secrets/service-account.json; таблица должна быть
расшарена на email сервисного аккаунта).
```

- [ ] **Step 7: Commit**

```bash
git add src/cf/cli.py src/cf/__main__.py .claude/commands/cf-status.md tests/test_cli_basic.py
git commit -m "feat: cf CLI (read/log-run/status) and /cf-status command"
```

---

### Task 8: Raw Batch Profiler — cf.profile + `cf profile` + /cf-profile

**Files:**
- Create: `src/cf/profile.py`, `prompts/agents/raw-batch-profiler.md`, `.claude/commands/cf-profile.md`
- Modify: `src/cf/cli.py` (cmd_profile + parser), `tests/fakes.py` (make_raw_row)
- Test: `tests/test_profile.py`, `tests/test_cli_profile.py`

- [ ] **Step 1: Добавить фабрику строк в `tests/fakes.py`** (в конец файла):

```python
def make_raw_row(i, **overrides):
    row = {
        "source_url": f"https://tiktok.com/@acc/video/{i}",
        "account": "acc",
        "views": 1000 + i * 100,
        "likes": 100, "comments": 10, "shares": 5, "saves": 8,
        "posted_at": "2026-07-01",
        "niche": "pets",
        "hook_text": "А вы знали?",
        "duration": 20,
    }
    row.update(overrides)
    return row
```

- [ ] **Step 2: Падающие тесты логики** — `tests/test_profile.py`:

```python
from cf.profile import CRITICAL_FIELDS, profile_rows

from tests.fakes import make_raw_row


def test_happy_path_ready():
    report = profile_rows([make_raw_row(i) for i in range(25)], min_rows=20)
    assert report["ready_for_analysis"] is True
    assert report["clean_rows"] == 25
    assert report["issues_list"] == []


def test_dedupes_by_source_url():
    report = profile_rows([make_raw_row(1), make_raw_row(1), make_raw_row(2)], min_rows=1)
    assert report["duplicates_removed"] == 1
    assert report["clean_rows"] == 2


def test_missing_critical_fields_lead_to_insufficient_data():
    report = profile_rows([make_raw_row(i, niche="") for i in range(30)], min_rows=20)
    assert report["ready_for_analysis"] is False
    assert report["rows_missing_critical_fields"] == 30
    assert any("insufficient_data" in issue for issue in report["issues_list"])


def test_duplicate_rate_warning():
    rows = [make_raw_row(1) for _ in range(5)] + [make_raw_row(i) for i in range(2, 30)]
    report = profile_rows(rows, min_rows=20)
    assert any("duplicate" in issue for issue in report["issues_list"])
```

- [ ] **Step 3: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_profile.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.profile'`

- [ ] **Step 4: Реализация** — `src/cf/profile.py`:

```python
CRITICAL_FIELDS = ["source_url", "account", "views", "posted_at", "niche"]


def profile_rows(rows, min_rows=20, dup_threshold=0.2):
    issues = []
    seen, deduped, duplicates = set(), [], 0
    for r in rows:
        key = str(r.get("source_url", "")).strip()
        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        deduped.append(r)

    clean, missing = [], []
    for r in deduped:
        if any(not str(r.get(f, "")).strip() for f in CRITICAL_FIELDS):
            missing.append(str(r.get("source_url") or "<no source_url>"))
        else:
            clean.append(r)

    if missing:
        issues.append(
            f"{len(missing)} rows missing critical fields "
            f"({', '.join(CRITICAL_FIELDS)}), например: {missing[:5]}"
        )
    if rows and duplicates / len(rows) > dup_threshold:
        issues.append(f"duplicate rate {duplicates}/{len(rows)} выше порога {dup_threshold:.0%}")
    ready = len(clean) >= min_rows
    if not ready:
        issues.append(f"insufficient_data: нужно >= {min_rows} чистых строк, есть {len(clean)}")

    return {
        "total_rows": len(rows),
        "duplicates_removed": duplicates,
        "rows_missing_critical_fields": len(missing),
        "clean_rows": len(clean),
        "issues_list": issues,
        "ready_for_analysis": ready,
    }
```

- [ ] **Step 5: Тесты логики зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_profile.py -v
```
Expected: `4 passed`

- [ ] **Step 6: Падающие тесты CLI** — `tests/test_cli_profile.py`:

```python
import argparse

from cf.cli import cmd_profile
from cf.io import read_json

from tests.fakes import FakeSheets, make_raw_row


def ns(**kw):
    return argparse.Namespace(**kw)


def test_cmd_profile_writes_report_and_logs_success(tmp_path):
    sheets = FakeSheets({"raw_tiktok": [make_raw_row(i) for i in range(25)]})
    rc = cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                min_rows=20, out_dir=str(tmp_path)))
    assert rc == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-profile.json")))
    assert report["ready_for_analysis"] is True
    assert report["meta"]["source_tab"] == "raw_tiktok"
    tab, row = sheets.appended[0]
    assert tab == "run_log" and row["agent"] == "raw-batch-profiler" and row["status"] == "success"


def test_cmd_profile_logs_insufficient_data(tmp_path):
    sheets = FakeSheets({"raw_tiktok": [make_raw_row(1)]})
    rc = cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                min_rows=20, out_dir=str(tmp_path)))
    assert rc == 0
    assert sheets.appended[0][1]["status"] == "insufficient_data"
```

- [ ] **Step 7: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_profile.py -v
```
Expected: FAIL, `ImportError: cannot import name 'cmd_profile'`

- [ ] **Step 8: Добавить в `src/cf/cli.py`**

Импорты (вверху, к существующим):

```python
from datetime import date
from pathlib import Path

from cf.profile import profile_rows
from cf.runlog import now_iso
```

Функция (после cmd_status):

```python
def cmd_profile(sheets, args):
    rows = apply_filters(sheets.read_rows(args.tab), args.niche, args.since)
    report = profile_rows(rows, min_rows=args.min_rows)
    report["meta"] = {"source_tab": args.tab, "niche": args.niche,
                      "since": args.since, "generated_at": now_iso()}
    path = Path(args.out_dir) / f"{date.today().isoformat()}-{args.tab}-profile.json"
    write_json_atomic(path, report)
    status = "success" if report["ready_for_analysis"] else "insufficient_data"
    runlog.log_run(sheets, agent="raw-batch-profiler", status=status,
                   input_summary=f"{args.tab} niche={args.niche or '-'} "
                                 f"since={args.since or '-'} rows={report['total_rows']}",
                   output_paths=[str(path)])
    print(f"profile report: {path}")
    print(f"ready_for_analysis: {report['ready_for_analysis']}")
    for issue in report["issues_list"]:
        print(f"  - {issue}")
    return 0
```

Блок в build_parser() (перед `return p`):

```python
    sp = sub.add_parser("profile", help="Raw Batch Profiler: качество батча")
    sp.add_argument("tab")
    sp.add_argument("--niche")
    sp.add_argument("--since")
    sp.add_argument("--min-rows", dest="min_rows", type=int, default=20)
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/profiles")
    sp.set_defaults(func=cmd_profile)
```

- [ ] **Step 9: Все тесты зелёные**

```bash
.venv/Scripts/python -m pytest -v
```
Expected: все пройдены, 0 failed

- [ ] **Step 10: Создать `prompts/agents/raw-batch-profiler.md`**

```markdown
# Raw Batch Profiler

Роль: проверка качества батча raw-строк перед анализом паттернов. Детерминированная
часть уже в CLI (`cf profile`) — твоя работа: запустить и интерпретировать отчёт.

## Порядок
1. Запусти `.venv/Scripts/python -m cf profile <tab> [--niche X] [--since YYYY-MM-DD]`.
2. Прочитай отчёт из agent-runtime/profiles/ (путь печатает CLI).
3. ready_for_analysis: true → батч готов; покажи цифры (total/clean/duplicates).
4. ready_for_analysis: false → честно: анализ невозможен. Перечисли issues_list и что
   делать оператору (добрать данные, починить критические поля: source_url, account,
   views, posted_at, niche).

## Правила
- Не приукрашивай качество данных. insufficient_data — валидный результат работы.
- Не запускай анализ паттернов и не обещай его — это /cf-analyze.
```

- [ ] **Step 11: Создать `.claude/commands/cf-profile.md`**

```markdown
---
description: Raw Batch Profiler - проверить качество батча перед анализом
argument-hint: "[tab=raw_tiktok] [--niche X] [--since YYYY-MM-DD]"
---
Следуй промпту prompts/agents/raw-batch-profiler.md. Аргументы: $ARGUMENTS
(по умолчанию tab=raw_tiktok). Запуск логируется самим CLI — log-run вручную не нужен.
```

- [ ] **Step 12: Commit**

```bash
git add src/cf/profile.py src/cf/cli.py tests/fakes.py tests/test_profile.py \
  tests/test_cli_profile.py prompts/agents/raw-batch-profiler.md .claude/commands/cf-profile.md
git commit -m "feat: raw batch profiler - dedupe, critical fields, insufficient_data"
```

---

### Task 9: Pattern Analyzer — схема паттернов, валидатор, /cf-analyze

Здесь фиксируется ответ на открытый вопрос №4 спеки (пороги confidence) — в промпте агента.

**Files:**
- Create: `schemas/patterns.schema.json`, `src/cf/validate.py`, `prompts/agents/pattern-analyzer.md`, `.claude/commands/cf-analyze.md`
- Modify: `src/cf/cli.py` (cmd_validate + parser)
- Test: `tests/test_validate.py`

- [ ] **Step 1: Создать `schemas/patterns.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CF pattern analysis output",
  "type": "object",
  "required": ["meta", "patterns"],
  "properties": {
    "meta": {
      "type": "object",
      "required": ["niche", "generated_at", "source_profile"],
      "properties": {
        "niche": {"type": "string", "minLength": 1},
        "generated_at": {"type": "string"},
        "source_profile": {"type": "string", "minLength": 1}
      }
    },
    "patterns": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["pattern_id", "description", "evidence", "confidence"],
        "properties": {
          "pattern_id": {"type": "string", "minLength": 1},
          "description": {"type": "string", "minLength": 10},
          "evidence": {
            "type": "object",
            "required": ["source_urls", "avg_views", "avg_er"],
            "properties": {
              "source_urls": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "pattern": "^https?://"}
              },
              "avg_views": {"type": "number"},
              "avg_er": {"type": "number"}
            }
          },
          "confidence": {"enum": ["high", "medium", "low"]},
          "conditions": {"type": "string"},
          "human_review": {"type": "boolean"}
        }
      }
    }
  }
}
```

- [ ] **Step 2: Падающие тесты** — `tests/test_validate.py`:

```python
import json

from cf.validate import validate_json_file

VALID_PATTERNS = {
    "meta": {"niche": "pets", "generated_at": "2026-07-09T00:00:00+00:00",
             "source_profile": "agent-runtime/profiles/x.json"},
    "patterns": [{
        "pattern_id": "pets-hook-question-01",
        "description": "Хук-вопрос в первые 2 секунды даёт выше ER",
        "evidence": {
            "source_urls": ["https://tiktok.com/@a/video/1",
                            "https://tiktok.com/@b/video/2",
                            "https://tiktok.com/@c/video/3"],
            "avg_views": 120000,
            "avg_er": 0.081
        },
        "confidence": "medium"
    }]
}


def write(tmp_path, data):
    p = tmp_path / "artifact.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_valid_patterns_pass(tmp_path):
    assert validate_json_file("patterns", write(tmp_path, VALID_PATTERNS)) == []


def test_pattern_without_evidence_urls_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["evidence"]["source_urls"] = []
    errors = validate_json_file("patterns", write(tmp_path, bad))
    assert errors and "source_urls" in errors[0]


def test_bad_confidence_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["confidence"] = "great"
    assert validate_json_file("patterns", write(tmp_path, bad)) != []
```

- [ ] **Step 3: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_validate.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.validate'`

- [ ] **Step 4: Реализация** — `src/cf/validate.py`:

```python
import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMAS = {
    "patterns": "schemas/patterns.schema.json",
    "formula": "schemas/formula.schema.json",
    "brief": "schemas/brief.schema.json",
}


def validate_json_file(kind, path, schema_root="."):
    schema = json.loads((Path(schema_root) / SCHEMAS[kind]).read_text(encoding="utf-8"))
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in Draft202012Validator(schema).iter_errors(data)
    ]
```

- [ ] **Step 5: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_validate.py -v
```
Expected: `3 passed`

- [ ] **Step 6: Добавить подкоманду validate в `src/cf/cli.py`**

Функция (после cmd_profile):

```python
def cmd_validate(sheets, args):
    from cf.validate import validate_json_file
    errors = validate_json_file(args.kind, args.file)
    if errors:
        print(f"INVALID {args.file}:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK {args.file}")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("validate", help="Проверить артефакт по схеме")
    sp.add_argument("kind", choices=["patterns", "formula", "brief"])
    sp.add_argument("file")
    sp.set_defaults(func=cmd_validate)
```

Быстрая проверка руками (валидный файл из теста):

```bash
.venv/Scripts/python -m pytest tests/test_validate.py -v   # всё ещё 3 passed
```

- [ ] **Step 7: Создать `prompts/agents/pattern-analyzer.md`**

```markdown
# Pattern Analyzer

Роль: анализ профилированного батча raw-контента — паттерны успешного и слабого контента.

## Вход
- Свежий profile report (agent-runtime/profiles/*.json) с ready_for_analysis: true.
  Без него анализ ЗАПРЕЩЁН — требуй /cf-profile.
- Батч: `.venv/Scripts/python -m cf read <tab> [--niche X] [--since YYYY-MM-DD] --out agent-runtime/batches/<дата>-<tab>.json`.

## Метод
1. Для каждой строки посчитай ER = (likes+comments+shares+saves)/views
   (views=0 → строка не участвует, отметь как аномалию).
2. Winners = верхний квартиль по views И по ER внутри ниши; losers = нижний квартиль.
   Опорная точка — медиана ниши.
3. Same-account comparison: внутри одного account сравни его winners с его же losers —
   что отличается (hook_text, duration, тема transcript). Это изолирует эффект контента
   от эффекта аккаунта.
4. Паттерн формулируется, только если наблюдается в >= 3 winners И отличает их от losers.

## Confidence (решение открытого вопроса №4 спеки)
- high: >= 5 source_urls, единое направление, avg_views паттерна >= 2x медианы ниши
- medium: 3-4 source_urls, направление согласовано
- low: <= 2 примера → в patterns-файл НЕ писать, только наблюдение в память

## Правила
- Evidence-first: у каждого паттерна непустой evidence.source_urls (реальные ссылки из
  батча) + avg_views + avg_er по этим ссылкам.
- Конфликтующие паттерны (противоположные выводы) → записывай ОБА, каждому — conditions
  (когда применим) и human_review: true. Решение оставь оператору.
- < 3 winners в нише → insufficient_data: паттерны не пишутся, в ответе — чего не хватает.

## Выход
- agent-runtime/patterns/YYYY-MM-DD-{niche}-patterns.json по schemas/patterns.schema.json.
- Наблюдения → .claude/memory/patterns/{niche}-observations.md (+ строка в MEMORY.md).
- Лог: `python -m cf log-run --agent pattern-analyzer --status <success|insufficient_data> --outputs <файл>`.
```

- [ ] **Step 8: Создать `.claude/commands/cf-analyze.md`**

```markdown
---
description: Pattern Analyzer - анализ паттернов профилированного батча
argument-hint: "[tab=raw_tiktok] [--niche X] [--since YYYY-MM-DD]"
---
Следуй промпту prompts/agents/pattern-analyzer.md. Аргументы: $ARGUMENTS

Порядок:
1. Найди свежайший agent-runtime/profiles/*-profile.json для этой вкладки (и ниши, если
   задана). Отчёта нет или ready_for_analysis: false → остановись и попроси оператора
   запустить /cf-profile. Анализ без профиля запрещён.
2. Выгрузи батч через `cf read` с теми же фильтрами (--out в agent-runtime/batches/).
3. Выполни анализ по промпту агента; запиши patterns-файл; проверь
   `python -m cf validate patterns <файл>`; исправь ошибки валидации и перепроверь.
4. Запиши наблюдения в память и залогируй запуск (log-run).
5. Сводка оператору: сколько паттернов, какие confidence, есть ли конфликтующие
   (по ним попроси решение оператора).
```

- [ ] **Step 9: Commit**

```bash
git add schemas/patterns.schema.json src/cf/validate.py src/cf/cli.py tests/test_validate.py \
  prompts/agents/pattern-analyzer.md .claude/commands/cf-analyze.md
git commit -m "feat: pattern analyzer - schema, validator, /cf-analyze, confidence thresholds"
```

---

### Task 10: Formula Writer — схема формул, /cf-formula

Evidence-gate зашит в схему: >= 3 source_urls, confidence только high|medium, conditions обязательны.

**Files:**
- Create: `schemas/formula.schema.json`, `prompts/agents/formula-writer.md`, `.claude/commands/cf-formula.md`
- Test: `tests/test_validate_formula.py`

- [ ] **Step 1: Создать `schemas/formula.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CF content formula",
  "type": "object",
  "required": ["name", "niche", "hook_structure", "problem_definition",
               "solution_structure", "visual_requirements", "cta_type", "prohibitions",
               "evidence", "confidence", "conditions", "source_pattern_ids",
               "version", "updated_at"],
  "properties": {
    "name": {"type": "string", "minLength": 3},
    "niche": {"type": "string", "minLength": 1},
    "hook_structure": {
      "type": "object",
      "required": ["timing", "elements"],
      "properties": {
        "timing": {"type": "string", "minLength": 1},
        "elements": {"type": "array", "minItems": 1, "items": {"type": "string"}}
      }
    },
    "problem_definition": {"type": "string", "minLength": 10},
    "solution_structure": {"type": "object"},
    "visual_requirements": {"type": "array", "items": {"type": "string"}},
    "cta_type": {"type": "string", "minLength": 1},
    "prohibitions": {"type": "array", "items": {"type": "string"}},
    "evidence": {
      "type": "object",
      "required": ["source_urls", "avg_views", "avg_er"],
      "properties": {
        "source_urls": {
          "type": "array",
          "minItems": 3,
          "items": {"type": "string", "pattern": "^https?://"}
        },
        "avg_views": {"type": "number"},
        "avg_er": {"type": "number"}
      }
    },
    "confidence": {"enum": ["high", "medium"]},
    "conditions": {"type": "string", "minLength": 5},
    "source_pattern_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    "version": {"type": "integer", "minimum": 1},
    "updated_at": {"type": "string"}
  }
}
```

- [ ] **Step 2: Падающие тесты** — `tests/test_validate_formula.py`:

```python
import json

from cf.validate import validate_json_file

VALID_FORMULA = {
    "name": "pets-question-hook",
    "niche": "pets",
    "hook_structure": {"timing": "0-3s", "elements": ["question"]},
    "problem_definition": "Владельцы питомцев не удерживают внимание в первые секунды",
    "solution_structure": {"beats": ["хук-вопрос", "демонстрация", "результат"]},
    "visual_requirements": ["крупный план питомца в первые 2 секунды"],
    "cta_type": "follow",
    "prohibitions": ["длинное интро"],
    "evidence": {
        "source_urls": ["https://tiktok.com/@a/video/1",
                        "https://tiktok.com/@b/video/2",
                        "https://tiktok.com/@c/video/3"],
        "avg_views": 120000,
        "avg_er": 0.081
    },
    "confidence": "medium",
    "conditions": "короткие ролики до 30с в нише pets",
    "source_pattern_ids": ["pets-hook-question-01"],
    "version": 1,
    "updated_at": "2026-07-09T00:00:00+00:00"
}


def write(tmp_path, data):
    p = tmp_path / "formula.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_valid_formula_passes(tmp_path):
    assert validate_json_file("formula", write(tmp_path, VALID_FORMULA)) == []


def test_evidence_gate_two_urls_fail(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["source_urls"] = bad["evidence"]["source_urls"][:2]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_low_confidence_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["confidence"] = "low"
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_missing_conditions_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    del bad["conditions"]
    assert validate_json_file("formula", write(tmp_path, bad)) != []
```

- [ ] **Step 3: Прогнать — схема уже подключена через validate.py, тесты должны пройти сразу**

```bash
.venv/Scripts/python -m pytest tests/test_validate_formula.py -v
```
Expected: `4 passed` (если FAIL — чинить схему, не тесты)

- [ ] **Step 4: Создать `prompts/agents/formula-writer.md`**

```markdown
# Formula Writer

Роль: превратить валидированные паттерны в переиспользуемые формулы контента.

## Правила
- **Evidence-gate:** формула наследует evidence своих паттернов; суммарно >= 3 source_urls,
  иначе формулу не писать (схема не пропустит). Паттерны с confidence low в формулы не идут.
- **conditions обязательны** — когда формула применима (ниша, длительность, формат).
  Для формул из конфликтующих паттернов conditions должны взаимно исключаться.
- **Обновление вместо дубликата:** формула той же ниши о том же приёме → обновить
  существующий файл: name сохранить, version += 1, evidence.source_urls объединить,
  updated_at обновить. Новый файл — только для действительно нового приёма.
- prohibitions выводи из losers: что систематически делали проигравшие ролики.
- name — kebab-case, уникален внутри ниши, совпадает с именем файла (без .json).

## Выход
- formulas/{niche}/{name}.json — по schemas/formula.schema.json;
- formulas/{niche}/{name}.md — те же поля прозой для человека (структура: Хук, Проблема,
  Решение, Визуал, CTA, Запреты, Когда применять, Evidence).
```

- [ ] **Step 5: Создать `.claude/commands/cf-formula.md`**

```markdown
---
description: Formula Writer - вывести формулу из валидированных паттернов
argument-hint: "[путь к patterns-файлу] [pattern_id ...]"
---
Следуй промпту prompts/agents/formula-writer.md. Аргументы: $ARGUMENTS

Порядок:
1. Прочитай patterns-файл (не указан → свежайший в agent-runtime/patterns/).
   Возьми паттерны с confidence high|medium; если заданы pattern_id — только их.
2. Подходящих паттернов нет → сообщи insufficient_data, залогируй
   (`cf log-run --agent formula-writer --status insufficient_data`) и остановись.
3. Паттерны с human_review: true (конфликты) → сначала покажи оба варианта оператору
   и спроси решение; в формулы конфликтующие паттерны идут только с взаимоисключающими
   conditions.
4. Собери формулу(ы) по правилам промпта агента; проверь
   `python -m cf validate formula formulas/{niche}/{name}.json`; исправь ошибки.
5. Напиши человекочитаемый formulas/{niche}/{name}.md.
6. Залогируй: `cf log-run --agent formula-writer --status success --outputs <файлы>`.
7. Покажи формулы оператору. Утверждение — отдельный шаг (см. Task 11): по явному
   «утверждаю» выполни approve-formula и запиши решение в память.
```

- [ ] **Step 6: Commit**

```bash
git add schemas/formula.schema.json tests/test_validate_formula.py \
  prompts/agents/formula-writer.md .claude/commands/cf-formula.md
git commit -m "feat: formula writer - schema with evidence gate, /cf-formula"
```

---

### Task 11: Approved inputs для n8n + фиксация решений оператора

**Files:**
- Modify: `src/cf/cli.py` (cmd_approve_formula + parser)
- Test: `tests/test_cli_approve.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_cli_approve.py`:

```python
import argparse
import json

from cf.cli import cmd_approve_formula
from cf.io import read_json

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def write_formula(tmp_path, version=1):
    p = tmp_path / "pets-question-hook.json"
    p.write_text(json.dumps({"name": "pets-question-hook", "niche": "pets",
                             "version": version}), encoding="utf-8")
    return p


def test_approve_adds_entry_to_index(tmp_path):
    formula = write_formula(tmp_path)
    index = tmp_path / "index.json"
    rc = cmd_approve_formula(FakeSheets(), ns(path=str(formula), index=str(index)))
    assert rc == 0
    data = read_json(index)
    assert data["approved"][0]["name"] == "pets-question-hook"
    assert data["approved"][0]["version"] == 1
    assert data["approved"][0]["approved_at"]


def test_reapprove_same_path_replaces_entry(tmp_path):
    index = tmp_path / "index.json"
    cmd_approve_formula(FakeSheets(), ns(path=str(write_formula(tmp_path, 1)), index=str(index)))
    cmd_approve_formula(FakeSheets(), ns(path=str(write_formula(tmp_path, 2)), index=str(index)))
    data = read_json(index)
    assert len(data["approved"]) == 1
    assert data["approved"][0]["version"] == 2
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_approve.py -v
```
Expected: FAIL, `ImportError: cannot import name 'cmd_approve_formula'`

- [ ] **Step 3: Добавить в `src/cf/cli.py`**

Импорт (вверху, к существующим):

```python
from cf.io import read_json, write_json_atomic
```
(заменяет прежний `from cf.io import write_json_atomic`)

Функция:

```python
def cmd_approve_formula(sheets, args):
    formula = read_json(args.path)
    index_path = Path(args.index)
    index = read_json(index_path) if index_path.exists() else {"approved": []}
    entry = {
        "name": formula["name"],
        "niche": formula["niche"],
        "path": str(args.path).replace("\\", "/"),
        "version": formula["version"],
        "approved_at": now_iso(),
    }
    index["approved"] = [e for e in index["approved"] if e["path"] != entry["path"]]
    index["approved"].append(entry)
    write_json_atomic(index_path, index)
    print(f"approved: {entry['name']} v{entry['version']} -> {index_path}")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("approve-formula", help="Добавить формулу в approved inputs для n8n")
    sp.add_argument("path")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_approve_formula)
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_cli_approve.py -v
```
Expected: `2 passed`

- [ ] **Step 5: Дописать фиксацию решения в `.claude/commands/cf-formula.md`** (в конец файла)

```markdown

После явного «утверждаю» от оператора:
1. `.venv/Scripts/python -m cf approve-formula formulas/{niche}/{name}.json`
2. Запиши решение: .claude/memory/decisions/YYYY-MM-DD-approve-{name}.md — что утверждено,
   почему (evidence кратко), какие альтернативы отклонены. Добавь строку в MEMORY.md.
3. `git add formulas/` и commit "formula({niche}): approve {name} vN".
n8n читает formulas/_approved/index.json из GitHub — это и есть approved inputs.
```

- [ ] **Step 6: Commit**

```bash
git add src/cf/cli.py tests/test_cli_approve.py .claude/commands/cf-formula.md
git commit -m "feat: approve-formula - approved inputs index for n8n"
```

---

### Task 12: Схема брифа — машинная и человекочитаемая

**Files:**
- Create: `schemas/brief.schema.json`, `prompts/briefs/_shared/schema.md`
- Test: `tests/test_validate_brief.py`

- [ ] **Step 1: Создать `schemas/brief.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CF creative brief",
  "type": "object",
  "required": ["brief_id", "source_pattern_ids", "formula_id", "prompt_version",
               "hook", "script", "visual_direction", "cta", "references"],
  "properties": {
    "brief_id": {"type": "string", "minLength": 1},
    "source_pattern_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    "formula_id": {"type": "string", "minLength": 1},
    "prompt_version": {"type": "string", "minLength": 1},
    "hook": {"type": "string", "minLength": 5},
    "script": {"type": "string", "minLength": 30},
    "visual_direction": {"type": "string", "minLength": 10},
    "cta": {"type": "string", "minLength": 3},
    "references": {
      "type": "array",
      "minItems": 1,
      "items": {"type": "string", "pattern": "^https?://"}
    }
  }
}
```

- [ ] **Step 2: Падающие тесты** — `tests/test_validate_brief.py`:

```python
import json

from cf.validate import validate_json_file

VALID_BRIEF = {
    "brief_id": "b-001",
    "source_pattern_ids": ["pets-hook-question-01"],
    "formula_id": "pets-question-hook",
    "prompt_version": "v1",
    "hook": "А вы знали, что кошки различают ваш голос из тысячи?",
    "script": "0-3с: хук-вопрос с крупным планом кошки. 3-15с: домашний эксперимент. "
              "15-25с: реакция кошки и вывод. Финал: CTA подписаться.",
    "visual_direction": "Съёмка на телефон, дневной свет, кошка в кадре с первой секунды",
    "cta": "Подпишись, чтобы узнать больше о своём питомце",
    "references": ["https://tiktok.com/@a/video/1", "https://tiktok.com/@b/video/2"]
}


def write(tmp_path, data):
    p = tmp_path / "brief.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_valid_brief_passes(tmp_path):
    assert validate_json_file("brief", write(tmp_path, VALID_BRIEF)) == []


def test_brief_without_references_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_BRIEF))
    bad["references"] = []
    errors = validate_json_file("brief", write(tmp_path, bad))
    assert errors and "references" in errors[0]


def test_brief_without_formula_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_BRIEF))
    del bad["formula_id"]
    assert validate_json_file("brief", write(tmp_path, bad)) != []
```

- [ ] **Step 3: Прогнать** (схема подключена через validate.py из Task 9)

```bash
.venv/Scripts/python -m pytest tests/test_validate_brief.py -v
```
Expected: `3 passed` (если FAIL — чинить схему)

- [ ] **Step 4: Создать `prompts/briefs/_shared/schema.md`** (для авторов промптов генерации в n8n)

```markdown
# Схема брифа (для промптов генерации в n8n)

Каждый бриф — строка в Google Sheets «CF Creative Briefs» и одновременно JSON-объект
по schemas/brief.schema.json. Поля:

| Поле | Обязательное | Что содержит |
|---|---|---|
| brief_id | да | уникальный id (генерирует n8n) |
| source_pattern_ids | да, >= 1 | id паттернов, на которых основана формула |
| formula_id | да | name формулы из formulas/{niche}/ |
| prompt_version | да | активная версия из CF Prompt Versions |
| hook | да | текст хука первых 3 секунд |
| script | да, >= 30 симв. | посекундный сценарий |
| visual_direction | да | как снимать: свет, план, локация |
| cta | да | призыв к действию (тип должен соответствовать cta_type формулы) |
| references | да, >= 1 url | ссылки на референсные ролики (в идеале из evidence формулы) |

Бриф, нарушающий prohibitions формулы, будет отклонён ревьюером автоматически.
```

- [ ] **Step 5: Commit**

```bash
git add schemas/brief.schema.json prompts/briefs/_shared/schema.md tests/test_validate_brief.py
git commit -m "feat: brief schema - machine and human readable"
```

---

### Task 13: Brief Reviewer — /cf-review-brief + обновление review_status

**Files:**
- Create: `prompts/agents/brief-reviewer.md`, `.claude/commands/cf-review-brief.md`
- Modify: `src/cf/cli.py` (cmd_set_review + parser)
- Test: `tests/test_cli_review.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_cli_review.py`:

```python
import argparse

from cf.cli import cmd_set_review

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def test_set_review_updates_brief_row():
    sheets = FakeSheets({"briefs": [
        {"brief_id": "b-001", "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
    ]})
    rc = cmd_set_review(sheets, ns(brief_id="b-001", status="rejected",
                                   reason="слабый референс", notes="см. review-файл"))
    assert rc == 0
    row = sheets.read_rows("briefs")[0]
    assert row["review_status"] == "rejected"
    assert row["rejection_reason"] == "слабый референс"


def test_set_review_unknown_brief_returns_error():
    rc = cmd_set_review(FakeSheets({"briefs": []}),
                        ns(brief_id="nope", status="approved", reason="", notes=""))
    assert rc == 1
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_review.py -v
```
Expected: FAIL, `ImportError: cannot import name 'cmd_set_review'`

- [ ] **Step 3: Добавить в `src/cf/cli.py`**

Функция:

```python
def cmd_set_review(sheets, args):
    found = sheets.update_row_fields("briefs", "brief_id", args.brief_id, {
        "review_status": args.status,
        "rejection_reason": args.reason,
        "reviewer_notes": args.notes,
    })
    if not found:
        print(f"brief_id {args.brief_id} не найден в CF Creative Briefs", file=sys.stderr)
        return 1
    print(f"{args.brief_id}: review_status={args.status}")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("set-review", help="Обновить review_status брифа")
    sp.add_argument("--brief-id", dest="brief_id", required=True)
    sp.add_argument("--status", required=True,
                    choices=["approved", "rejected", "revised", "pending"])
    sp.add_argument("--reason", default="")
    sp.add_argument("--notes", default="")
    sp.set_defaults(func=cmd_set_review)
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_cli_review.py -v
```
Expected: `2 passed`

- [ ] **Step 5: Создать `prompts/agents/brief-reviewer.md`**

```markdown
# Brief Reviewer

Роль: валидация сгенерированного брифа против формулы до передачи в продакшн.

## Проверки (по строгости)
1. **Schema.** Бриф проходит `python -m cf validate brief <файл>` — иначе reject.
2. **Соответствие формуле** (formulas/{niche}/{formula_id}.json):
   - hook укладывается в hook_structure.timing и использует хотя бы один элемент из elements;
   - script НЕ нарушает ни один пункт prohibitions — нарушение = reject;
   - cta соответствует cta_type формулы.
3. **Reference quality.** references непустые и правдоподобные; хотя бы один пересекается
   с evidence.source_urls формулы или её паттернов — иначе revise.
4. **Producibility.** Реально снять одним креатором на телефон: без сложного продакшна,
   недоступных локаций, эффектов вне visual_requirements. Нереально → revise с конкретикой.
5. **Product connection.** Бриф упоминает продукт → связь продукта с сюжетом явная. Нет → revise.

## Вердикты
- approved — все проверки пройдены;
- revise — исправимые проблемы; перечисли по пунктам, что именно поправить;
- reject — нарушение схемы, prohibitions или неизвестная формула; причина обязательна.

## Правила
- Каждая причина конкретна и проверяема — никаких «в целом слабовато».
- Повторяющиеся причины формулируй одинаковыми словами — это сырьё для Prompt Optimizer
  (rejection history группирует по точному тексту причины).
```

- [ ] **Step 6: Создать `.claude/commands/cf-review-brief.md`**

```markdown
---
description: Brief Reviewer - ревью брифа по формуле (approved/revise/reject)
argument-hint: "<brief_id> | --pending"
---
Следуй промпту prompts/agents/brief-reviewer.md. Аргументы: $ARGUMENTS

Порядок:
1. Сними снапшот брифов: `.venv/Scripts/python -m cf read briefs --out agent-runtime/reviews/briefs-snapshot.json`.
   Выбери бриф по brief_id; для --pending — все с review_status=pending
   (таких нет → скажи об этом и остановись).
2. Для каждого брифа: собери JSON-объект по schemas/brief.schema.json из полей строки
   (source_pattern_ids и references в таблице — строки через запятую, преврати в массивы),
   сохрани в agent-runtime/reviews/<brief_id>-input.json, проверь
   `python -m cf validate brief <файл>`. Схема невалидна → вердикт reject
   с причиной "schema: <первая ошибка>".
3. Загрузи формулу formulas/{niche}/{formula_id}.json. Файла нет → reject "unknown formula".
4. Прогони проверки промпта агента, определи вердикт.
5. Запиши agent-runtime/reviews/YYYY-MM-DD-<brief_id>-review.json:
   {"brief_id": ..., "verdict": "approved|revise|reject", "reasons": [...],
    "formula_id": ..., "checked_at": ...}.
6. Обнови таблицу: `python -m cf set-review --brief-id <id> --status <approved|revised|rejected>
   --reason "<причина или пусто>" --notes "<кратко>"` (вердикт revise → статус revised).
7. Залогируй: `python -m cf log-run --agent brief-reviewer --status success
   --input "<brief_id или pending:N>" --outputs <review-файлы>`.
8. Покажи оператору вердикты и причины по каждому брифу.
```

- [ ] **Step 7: Commit**

```bash
git add src/cf/cli.py tests/test_cli_review.py prompts/agents/brief-reviewer.md \
  .claude/commands/cf-review-brief.md
git commit -m "feat: brief reviewer - /cf-review-brief, set-review, verdict rules"
```

---

### Task 14: Rejection history для Prompt Optimizer

**Files:**
- Modify: `src/cf/cli.py` (cmd_rejection_history + parser)
- Test: `tests/test_cli_rejections.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_cli_rejections.py`:

```python
import argparse

from cf.cli import cmd_rejection_history
from cf.io import read_json

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


BRIEFS = [
    {"brief_id": "b-1", "review_status": "rejected", "rejection_reason": "Слабый референс"},
    {"brief_id": "b-2", "review_status": "rejected", "rejection_reason": "слабый референс"},
    {"brief_id": "b-3", "review_status": "revised", "rejection_reason": "нет CTA"},
    {"brief_id": "b-4", "review_status": "approved", "rejection_reason": ""},
]


def test_groups_and_sorts_by_count(tmp_path):
    out = tmp_path / "history.json"
    rc = cmd_rejection_history(FakeSheets({"briefs": BRIEFS}), ns(out=str(out)))
    assert rc == 0
    report = read_json(out)
    assert report["total_rejected_or_revised"] == 3
    assert report["reasons"][0] == {"reason": "слабый референс", "count": 2,
                                    "brief_ids": ["b-1", "b-2"]}
    assert report["reasons"][1]["reason"] == "нет cta"
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_rejections.py -v
```
Expected: FAIL, `ImportError: cannot import name 'cmd_rejection_history'`

- [ ] **Step 3: Добавить в `src/cf/cli.py`**

Функция:

```python
def cmd_rejection_history(sheets, args):
    reasons = {}
    for b in sheets.read_rows("briefs"):
        if str(b.get("review_status")) in ("rejected", "revised"):
            key = str(b.get("rejection_reason", "")).strip().lower() or "(no reason logged)"
            item = reasons.setdefault(key, {"reason": key, "count": 0, "brief_ids": []})
            item["count"] += 1
            item["brief_ids"].append(str(b.get("brief_id")))
    report = {
        "generated_at": now_iso(),
        "total_rejected_or_revised": sum(i["count"] for i in reasons.values()),
        "reasons": sorted(reasons.values(), key=lambda i: -i["count"]),
    }
    write_json_atomic(args.out, report)
    print(f"rejection history -> {args.out}")
    for item in report["reasons"][:5]:
        print(f"  {item['count']}x {item['reason']}")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("rejection-history", help="Свод повторяющихся причин reject/revise")
    sp.add_argument("--out", default="agent-runtime/reviews/rejection_history.json")
    sp.set_defaults(func=cmd_rejection_history)
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_cli_rejections.py -v
```
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add src/cf/cli.py tests/test_cli_rejections.py
git commit -m "feat: rejection history aggregation for prompt optimizer"
```

---

### Task 15: Контракт с n8n — pending-flow и approved inputs

Решение открытого вопроса №3 спеки: триггер через review_status=pending + ручной/headless запуск /cf-review-brief. Кода нет — только зафиксированный контракт.

**Files:**
- Create: `docs/n8n-integration.md`
- Modify: `CLAUDE.md` (ссылка на контракт)

- [ ] **Step 1: Создать `docs/n8n-integration.md`**

```markdown
# Интеграция с n8n

Контракт между n8n (runtime) и Claude Code (intelligence). n8n не заменяется.

## Что n8n берёт из GitHub
- Approved inputs для генерации брифов: `formulas/_approved/index.json` (raw URL) —
  список утверждённых формул; сами формулы — по `path` из индекса.
- Промпты брифов: `prompts/briefs/{niche}/{format}.md` (как и раньше).

## Что n8n пишет в Google Sheets
- Raw-данные TikTok/Instagram → CF Raw TikTok / CF Raw Instagram, ежедневно (как и раньше).
- Новые брифы → CF Creative Briefs, review_status=**pending**,
  prompt_version — из активной строки CF Prompt Versions.
- Performance-строки → CF Performance, ежедневно.

## Триггер ревью брифов (решение открытого вопроса №3 спеки)
MVP: оператор запускает `/cf-review-brief --pending` — обрабатываются все pending разом.
Опциональная автоматизация: n8n Execute Command после генерации брифа:
`claude -p "/cf-review-brief --pending"` на машине оператора.
Отдельный webhook-сервер не строим — вне скоупа MVP.

## Ритм
| Поток | Кто | Частота |
|---|---|---|
| Сбор raw-данных | n8n | ежедневно |
| Генерация брифов | n8n | по событию (обновился approved index) |
| Ревью брифов | /cf-review-brief --pending | по мере появления pending |
| Performance | n8n | ежедневно |
| Eval | /cf-eval | еженедельно |
```

- [ ] **Step 2: Добавить ссылку в `CLAUDE.md`** — в конец раздела «Данные»:

```markdown
Контракт с n8n (pending-flow, approved inputs): docs/n8n-integration.md.
```

- [ ] **Step 3: Проверить и закоммитить**

```bash
grep -c "pending" docs/n8n-integration.md   # Expected: >= 3
git add docs/n8n-integration.md CLAUDE.md
git commit -m "docs: n8n contract - pending flow and approved inputs"
```

---

### Task 16: Eval Agent — eval-prep, /cf-eval, attribution, success criteria

Здесь фиксируется ответ на открытый вопрос №5 спеки (веса attribution) — в промпте агента.

**Files:**
- Create: `src/cf/evalprep.py`, `prompts/agents/eval-agent.md`, `.claude/commands/cf-eval.md`
- Modify: `src/cf/cli.py` (cmd_eval_prep + parser)
- Test: `tests/test_evalprep.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_evalprep.py`:

```python
from cf.evalprep import build_eval_dataset

PERFORMANCE = [
    {"reel_id": "r1", "brief_id": "b-1", "prompt_version": "v1", "views": "10000", "er": "0.05"},
    {"reel_id": "r2", "brief_id": "b-2", "prompt_version": "v1", "views": "30000", "er": "0.07"},
    {"reel_id": "r3", "brief_id": "b-3", "prompt_version": "v2", "views": "5000", "er": "0.02"},
]
BRIEFS = [
    {"brief_id": "b-1", "formula_id": "f-1", "prompt_version": "v1", "review_status": "approved"},
    {"brief_id": "b-2", "formula_id": "f-1", "prompt_version": "v1", "review_status": "approved"},
    {"brief_id": "b-3", "formula_id": "f-2", "prompt_version": "v2", "review_status": "rejected"},
]
VERSIONS = [
    {"prompt_id": "brief-pets", "version": "v2", "github_path": "prompts/briefs/pets/x.md", "active": "TRUE"},
    {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md", "active": "FALSE"},
]


def test_aggregates_by_prompt_version():
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert ds["by_prompt_version"]["v1"] == {"reels": 2, "avg_views": 20000.0, "avg_er": 0.06}
    assert ds["by_prompt_version"]["v2"]["reels"] == 1


def test_rows_carry_formula_link():
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert ds["rows"][0]["formula_id"] == "f-1"
    assert ds["rows"][0]["brief_found"] is True


def test_reviewer_pass_rate():
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert ds["reviewer_pass_rate"] == 2 / 3


def test_unknown_brief_falls_back():
    perf = [{"reel_id": "rX", "brief_id": "ghost", "prompt_version": "", "views": "1", "er": ""}]
    ds = build_eval_dataset(perf, [], [])
    assert ds["rows"][0]["prompt_version"] == "unknown"
    assert ds["rows"][0]["brief_found"] is False
    assert ds["reviewer_pass_rate"] is None


def test_active_versions_listed():
    ds = build_eval_dataset([], [], VERSIONS)
    assert ds["active_prompt_versions"] == [
        {"prompt_id": "brief-pets", "version": "v2", "github_path": "prompts/briefs/pets/x.md"}
    ]
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_evalprep.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.evalprep'`

- [ ] **Step 3: Реализация** — `src/cf/evalprep.py`:

```python
def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_eval_dataset(performance, briefs, prompt_versions):
    briefs_by_id = {str(b.get("brief_id")): b for b in briefs}
    rows, by_version = [], {}
    for p in performance:
        brief = briefs_by_id.get(str(p.get("brief_id")))
        version = str(p.get("prompt_version") or (brief or {}).get("prompt_version")
                      or "unknown")
        views, er = _num(p.get("views")), _num(p.get("er"))
        rows.append({
            "reel_id": str(p.get("reel_id", "")),
            "brief_id": str(p.get("brief_id", "")),
            "prompt_version": version,
            "views": views,
            "er": er,
            "formula_id": (brief or {}).get("formula_id", ""),
            "source_pattern_ids": (brief or {}).get("source_pattern_ids", ""),
            "brief_found": brief is not None,
        })
        agg = by_version.setdefault(version, {"reels": 0, "views": 0.0, "er": 0.0})
        agg["reels"] += 1
        agg["views"] += views
        agg["er"] += er

    versions = {
        v: {"reels": a["reels"], "avg_views": a["views"] / a["reels"],
            "avg_er": a["er"] / a["reels"]}
        for v, a in by_version.items()
    }
    reviewed = [b for b in briefs
                if str(b.get("review_status")) in ("approved", "rejected", "revised")]
    approved = [b for b in reviewed if str(b.get("review_status")) == "approved"]
    active = [
        {"prompt_id": v.get("prompt_id"), "version": v.get("version"),
         "github_path": v.get("github_path")}
        for v in prompt_versions
        if str(v.get("active")).strip().upper() in ("TRUE", "1", "YES")
    ]
    return {
        "by_prompt_version": versions,
        "rows": rows,
        "reviewer_pass_rate": (len(approved) / len(reviewed)) if reviewed else None,
        "reviewed_briefs": len(reviewed),
        "active_prompt_versions": active,
    }
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_evalprep.py -v
```
Expected: `5 passed`

- [ ] **Step 5: Добавить подкоманду eval-prep в `src/cf/cli.py`**

Функция:

```python
def cmd_eval_prep(sheets, args):
    from cf.evalprep import build_eval_dataset
    performance = sheets.read_rows("performance")
    if args.since:
        performance = [p for p in performance if str(p.get("measured_at", "")) >= args.since]
    dataset = build_eval_dataset(performance, sheets.read_rows("briefs"),
                                 sheets.read_rows("prompt_versions"))
    dataset["generated_at"] = now_iso()
    dataset["since"] = args.since
    path = Path(args.out_dir) / f"{date.today().isoformat()}-eval-dataset.json"
    write_json_atomic(path, dataset)
    print(f"eval dataset -> {path}")
    print(f"  performance rows: {len(dataset['rows'])}, "
          f"versions: {len(dataset['by_prompt_version'])}, "
          f"pass rate: {dataset['reviewer_pass_rate']}")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("eval-prep", help="Собрать датасет для еженедельного eval")
    sp.add_argument("--since")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/evals")
    sp.set_defaults(func=cmd_eval_prep)
```

Проверка: `.venv/Scripts/python -m pytest -v` — весь набор зелёный.

- [ ] **Step 6: Создать `prompts/agents/eval-agent.md`**

```markdown
# Eval Agent

Роль: еженедельная оценка — связать performance с prompt_version и формулами,
атрибутировать провалы, посчитать success criteria.

## Вход
- Датасет: `.venv/Scripts/python -m cf eval-prep [--since YYYY-MM-DD]`
  → agent-runtime/evals/<дата>-eval-dataset.json
- rejection history: `python -m cf rejection-history`
- Формулы из formulas/, паттерны из agent-runtime/patterns/.

## Отчёт: agent-runtime/evals/YYYY-MM-DD-weekly-eval.json
Структура (объект JSON):
- period: {since, until}
- by_prompt_version: из датасета + вывод по каждой версии (лучше/хуже и почему)
- formula_performance: [{formula_id, reels, avg_views, avg_er, verdict}]
- attribution: [{reel_id, category, reasoning, confidence}]
- success_criteria:
    reviewer_pass_rate (из датасета), target: 0.8, met: true|false;
    prompt_performance_link: видна ли разница между версиями (строка-вывод);
    proposals_evidence_complete: все ли proposals в proposals/ проходят validate proposal
- insights: ["..."] — каждый со ссылкой на reel_id/цифры
- trends: ["..."] — динамика между этим и прошлым eval (если прошлый есть)

## Attribution (решение открытого вопроса №5 спеки)
Underperforming = views < 0.5 x медианы своей prompt_version в датасете.
Категории и их сигналы:
- prompt_failure — у брифа история revise/reject; hook или script отклоняются от формулы.
- reference_failure — формула/паттерн с confidence medium при тонком evidence;
  references брифа не из evidence.
- production_failure — production_notes в CF Published Reels указывают на отступления
  от брифа или качество съёмки.
- publishing_failure — постинг вне окна 9:00-22:00 аудитории или пустое описание.
Несколько сигналов → выбирай по приоритету prompt > reference > production > publishing
(prompt-слой — единственный, который мы правим напрямую, ошибка в его пользу дешевле);
второго кандидата укажи в reasoning.

## Правила
- < 5 performance-строк в датасете → insufficient_data: отчёт не пишется,
  залогируй с этим статусом и перечисли, каких данных ждать.
- Каждый инсайт опирается на конкретные reel_id и цифры — без общих слов.
- Инсайты продублируй в .claude/memory/evaluations/YYYY-MM-DD-insights.md (+ MEMORY.md).
```

- [ ] **Step 7: Создать `.claude/commands/cf-eval.md`**

```markdown
---
description: Eval Agent - еженедельная оценка performance по prompt_version
argument-hint: "[--since YYYY-MM-DD]"
---
Следуй промпту prompts/agents/eval-agent.md. Аргументы: $ARGUMENTS

1. `.venv/Scripts/python -m cf eval-prep $ARGUMENTS` — собери датасет.
2. В датасете < 5 performance-строк → `python -m cf log-run --agent eval-agent
   --status insufficient_data --input "<since>"`, скажи оператору, чего не хватает,
   и остановись.
3. Построй weekly-eval.json по промпту агента; запиши инсайты в память.
4. `python -m cf log-run --agent eval-agent --status success --outputs <отчёт>`.
5. Покажи оператору: success_criteria (met/не met), топ-3 инсайта, атрибуцию провалов.
```

- [ ] **Step 8: Commit**

```bash
git add src/cf/evalprep.py src/cf/cli.py tests/test_evalprep.py \
  prompts/agents/eval-agent.md .claude/commands/cf-eval.md
git commit -m "feat: eval loop - dataset join, attribution rules, success criteria"
```

---

### Task 17: Prompt Optimizer — proposals, версии промптов, approval flow

**Files:**
- Create: `src/cf/proposals.py`, `prompts/agents/prompt-optimizer.md`, `.claude/commands/cf-propose-update.md`
- Modify: `src/cf/cli.py` (cmd_log_prompt_version, validate с kind=proposal), `src/cf/sheets.py` (update_rows_where), `tests/fakes.py` (update_rows_where)
- Test: `tests/test_proposals.py`, `tests/test_cli_versions.py`

- [ ] **Step 1: Падающие тесты валидатора** — `tests/test_proposals.py`:

```python
from cf.proposals import validate_proposal_text

VALID = """---
status: proposed
prompt_id: brief-pets
created: 2026-07-09
---
# Proposal: усилить требования к хуку

## Current version
Версия v1, файл `prompts/briefs/pets/talking-head.md`.

## Proposed changes
Заменить требование к хуку на явное: вопрос в первых 2 секундах.

## Evidence
- "слабый референс" — 4 случая (agent-runtime/reviews/rejection_history.json).
- v1 avg_views=20000 при формуле с avg_views=120000 (https://tiktok.com/@a/video/1).

## Confidence
medium — evidence из одного eval-периода.

## Risks
Хук-вопрос может выгореть в нише; заметим по падению avg_er в следующем eval.
"""


def test_valid_proposal_passes():
    assert validate_proposal_text(VALID) == []


def test_missing_section_fails():
    text = VALID.replace("## Risks", "## Прочее")
    assert any("Risks" in e for e in validate_proposal_text(text))


def test_evidence_without_numbers_fails():
    text = (VALID.split("## Evidence")[0]
            + "## Evidence\nпросто мнение без цифр и ссылок\n\n"
            + "## Confidence\nmedium\n\n## Risks\nнет\n")
    errors = validate_proposal_text(text)
    assert any("цифр" in e or "ссыл" in e for e in errors)


def test_wrong_frontmatter_status_fails():
    text = VALID.replace("status: proposed", "status: draft")
    assert any("status: proposed" in e for e in validate_proposal_text(text))
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_proposals.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.proposals'`

- [ ] **Step 3: Реализация** — `src/cf/proposals.py`:

```python
import re

REQUIRED_SECTIONS = (
    "## Current version",
    "## Proposed changes",
    "## Evidence",
    "## Confidence",
    "## Risks",
)


def validate_proposal_text(text):
    errors = []
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        errors.append("нет YAML frontmatter (--- в начале файла)")
    elif "status: proposed" not in stripped.split("---")[1]:
        errors.append("frontmatter должен содержать status: proposed")
    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"нет секции {section}")
    if "## Evidence" in text:
        evidence = text.split("## Evidence", 1)[1].split("\n## ", 1)[0]
        if not re.search(r"\d", evidence):
            errors.append("Evidence без цифр — метрики обязательны")
        if not any(m in evidence for m in ("http", "agent-runtime/", "formulas/")):
            errors.append("Evidence без ссылок на источники (url или путь к артефакту)")
    return errors
```

- [ ] **Step 4: Тесты валидатора зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_proposals.py -v
```
Expected: `4 passed`

- [ ] **Step 5: Падающие тесты версий промптов** — `tests/test_cli_versions.py`:

```python
import argparse

from cf.cli import cmd_log_prompt_version

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def test_new_version_appended_and_previous_deactivated():
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md",
         "active": "TRUE", "deactivated_at": ""},
        {"prompt_id": "brief-food", "version": "v3", "github_path": "prompts/briefs/food/y.md",
         "active": "TRUE", "deactivated_at": ""},
    ]})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/x.md",
                                           changelog="хук-вопрос обязателен"))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    old = next(r for r in rows if r["version"] == "v1")
    other = next(r for r in rows if r["prompt_id"] == "brief-food")
    new = next(r for r in rows if r["version"] == "v2")
    assert old["active"] == "FALSE" and old["deactivated_at"]
    assert other["active"] == "TRUE"          # чужой промпт не тронут
    assert new["active"] == "TRUE" and new["changelog"] == "хук-вопрос обязателен"
```

- [ ] **Step 6: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_versions.py -v
```
Expected: FAIL, `ImportError: cannot import name 'cmd_log_prompt_version'`

- [ ] **Step 7: Добавить `update_rows_where`**

В `src/cf/sheets.py` (метод класса Sheets, после update_row_fields):

```python
    def update_rows_where(self, tab_key, match, fields):
        def norm(v):
            return str(v).strip().upper()

        def op():
            ws = self._ws(tab_key)
            headers = ws.row_values(1)
            updated = 0
            for i, rec in enumerate(ws.get_all_records()):
                if all(norm(rec.get(k)) == norm(v) for k, v in match.items()):
                    for col_name, value in fields.items():
                        if col_name in headers:
                            ws.update_cell(i + 2, headers.index(col_name) + 1, str(value))
                    updated += 1
            return updated

        return with_retry(op, base_delay=self.retry_delay, label=f"update {tab_key}")
```

В `tests/fakes.py` (метод FakeSheets, после update_row_fields):

```python
    def update_rows_where(self, tab_key, match, fields):
        def norm(v):
            return str(v).strip().upper()

        updated = 0
        for r in self.tables.get(tab_key, []):
            if all(norm(r.get(k)) == norm(v) for k, v in match.items()):
                r.update(fields)
                updated += 1
        return updated
```

- [ ] **Step 8: Добавить в `src/cf/cli.py`**

Функция:

```python
def cmd_log_prompt_version(sheets, args):
    sheets.update_rows_where("prompt_versions",
                             {"prompt_id": args.prompt_id, "active": "TRUE"},
                             {"active": "FALSE", "deactivated_at": now_iso()})
    sheets.append_row("prompt_versions", {
        "prompt_id": args.prompt_id,
        "version": args.version,
        "github_path": args.path,
        "active": "TRUE",
        "activated_at": now_iso(),
        "deactivated_at": "",
        "changelog": args.changelog,
    })
    print(f"{args.prompt_id} {args.version} активирована ({args.path})")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("log-prompt-version", help="Зафиксировать новую версию промпта")
    sp.add_argument("--prompt-id", dest="prompt_id", required=True)
    sp.add_argument("--version", required=True)
    sp.add_argument("--path", required=True)
    sp.add_argument("--changelog", required=True)
    sp.set_defaults(func=cmd_log_prompt_version)
```

Расширить cmd_validate и его парсер (kind=proposal):

```python
def cmd_validate(sheets, args):
    if args.kind == "proposal":
        from cf.proposals import validate_proposal_text
        errors = validate_proposal_text(Path(args.file).read_text(encoding="utf-8"))
    else:
        from cf.validate import validate_json_file
        errors = validate_json_file(args.kind, args.file)
    if errors:
        print(f"INVALID {args.file}:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK {args.file}")
    return 0
```

В парсере validate заменить choices:

```python
    sp.add_argument("kind", choices=["patterns", "formula", "brief", "proposal"])
```

- [ ] **Step 9: Все тесты зелёные**

```bash
.venv/Scripts/python -m pytest -v
```
Expected: все пройдены, 0 failed

- [ ] **Step 10: Создать `prompts/agents/prompt-optimizer.md`**

````markdown
# Prompt Optimizer

Роль: предлагать изменения промптов на основе evidence. Только proposal — никаких
прямых правок промптов. Решение всегда за оператором.

## Шаблон proposal (proposals/YYYY-MM-DD-<prompt_id>.md)

---
status: proposed
prompt_id: <id>
created: YYYY-MM-DD
---
# Proposal: <человеческое название изменения>

## Current version
Версия <vN>, файл `<github_path>` (из активной строки CF Prompt Versions).

## Proposed changes
```diff
- старый фрагмент промпта (дословно из текущего файла)
+ новый фрагмент
```

## Evidence
- Rejection reasons, которые правка закрывает: "<причина>" — N случаев (rejection_history.json).
- Performance: <vN> avg_views=..., avg_er=... против <vN-1> (weekly-eval.json).
- Паттерны/формулы, подтверждающие изменение: <pattern_id / formula name>.

## Confidence
high|medium|low + одно предложение почему.

## Risks
Что может ухудшиться и по какой метрике это заметим в следующем eval.

## Правила
- Каждое изменение привязано к конкретной причине: rejection reason, метрика или паттерн.
- Одно proposal = один промпт. Несвязанные правки — отдельными proposals.
- diff применим к текущему тексту промпта дословно (проверь сам перед сохранением).
- Evidence мало (нет eval-отчёта И нет повторяющихся rejection reasons) → insufficient_data,
  proposal не пишется.
````

- [ ] **Step 11: Создать `.claude/commands/cf-propose-update.md`**

```markdown
---
description: Prompt Optimizer - evidence-backed proposal изменения промпта
argument-hint: "<prompt_id>"
---
Следуй промпту prompts/agents/prompt-optimizer.md. Аргументы: $ARGUMENTS

Порядок:
1. Собери evidence:
   - свежий agent-runtime/evals/*-weekly-eval.json (нет → предложи сначала /cf-eval);
   - `.venv/Scripts/python -m cf rejection-history`;
   - активная версия: `python -m cf read prompt_versions` (active=TRUE → github_path);
   - текущий текст промпта по github_path.
2. Evidence мало → `python -m cf log-run --agent prompt-optimizer --status insufficient_data`
   и остановись, объяснив, каких данных ждать.
3. Напиши proposals/YYYY-MM-DD-<prompt_id>.md по шаблону из промпта агента.
4. Проверь: `python -m cf validate proposal proposals/<файл>`; исправь ошибки.
5. `python -m cf log-run --agent prompt-optimizer --status success --outputs <файл>`.
6. Покажи proposal оператору и ЖДИ явного решения. Менять промпт до решения запрещено.

После решения оператора:
- **approve**: примени Proposed changes к файлу промпта; в frontmatter proposal —
  status: approved и applied: YYYY-MM-DD; выполни
  `python -m cf log-prompt-version --prompt-id <id> --version <vN+1> --path <github_path>
  --changelog "<суть одной строкой>"`;
  запиши решение в .claude/memory/decisions/YYYY-MM-DD-prompt-<id>.md (+ MEMORY.md);
  `git add <файл промпта> <proposal>` и commit "prompt(<id>): <суть> [vN+1]".
- **reject**: в frontmatter — status: rejected и reason: "<причина оператора>";
  запиши решение в память; файл промпта НЕ трогать.
```

- [ ] **Step 12: Commit**

```bash
git add src/cf/proposals.py src/cf/sheets.py src/cf/cli.py tests/fakes.py \
  tests/test_proposals.py tests/test_cli_versions.py \
  prompts/agents/prompt-optimizer.md .claude/commands/cf-propose-update.md
git commit -m "feat: prompt optimizer - proposal validation, version log, approval flow"
```

---

### Task 18: /cf-status — полная картина

**Files:**
- Modify: `src/cf/cli.py` (расширить cmd_status + parser), `tests/test_cli_basic.py`, `.claude/commands/cf-status.md`

- [ ] **Step 1: Обновить тесты** — в `tests/test_cli_basic.py` заменить `test_cmd_status_prints_recent_runs_newest_first` и добавить полный тест:

```python
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
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_cli_basic.py -v
```
Expected: FAIL (`AttributeError: ... proposals_dir` / нет секций в выводе)

- [ ] **Step 3: Заменить cmd_status в `src/cf/cli.py`**

```python
def cmd_status(sheets, args):
    print("=== Content Factory Status ===")
    runs = sheets.read_rows("run_log")[-args.limit:][::-1]
    print(f"Recent runs ({len(runs)}):")
    for r in runs:
        print(f"  {str(r.get('run_id', '')):14} {str(r.get('agent', '')):22} "
              f"{str(r.get('status', '')):18} {r.get('completed_at', '')}")

    pending = [str(b.get("brief_id")) for b in sheets.read_rows("briefs")
               if str(b.get("review_status", "")).strip().lower() == "pending"]
    suffix = f" ({', '.join(pending)})" if pending else ""
    print(f"Pending briefs: {len(pending)}{suffix}")

    counts = {"proposed": 0, "approved": 0, "rejected": 0}
    proposals_dir = Path(args.proposals_dir)
    if proposals_dir.exists():
        for p in sorted(proposals_dir.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            for state in counts:
                if f"status: {state}" in text:
                    counts[state] += 1
                    break
    print(f"Proposals: {counts['proposed']} proposed, "
          f"{counts['approved']} approved, {counts['rejected']} rejected")

    print("Active prompts:")
    for v in sheets.read_rows("prompt_versions"):
        if str(v.get("active", "")).strip().upper() in ("TRUE", "1", "YES"):
            print(f"  {v.get('prompt_id', '')} {v.get('version', '')} {v.get('github_path', '')}")
    return 0
```

В парсере status добавить аргумент:

```python
    sp.add_argument("--proposals-dir", dest="proposals_dir", default="proposals")
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_cli_basic.py -v
```
Expected: `6 passed`

- [ ] **Step 5: Обновить `.claude/commands/cf-status.md`** (заменить целиком)

```markdown
---
description: Content Factory - запуски, pending-ревью, proposals, активные промпты
---
Выполни `.venv/Scripts/python -m cf status --limit 10` и покажи оператору результат.

Дальше подскажи следующий шаг:
- есть pending briefs → предложи /cf-review-brief --pending;
- есть proposed proposals → напомни, что они ждут решения (см. proposals/);
- в Recent runs есть failed/insufficient_data → покажи, какие агенты и почему (input_summary).

Если команда падает с ошибкой про cf.config.json или secrets/service-account.json —
объясни оператору, что настроить (spreadsheet_id; JSON-ключ сервисного аккаунта;
таблица расшарена на его email).
```

- [ ] **Step 6: Commit**

```bash
git add src/cf/cli.py tests/test_cli_basic.py .claude/commands/cf-status.md
git commit -m "feat: full /cf-status - pending briefs, proposals, active prompts"
```

---

### Task 19: Pre-commit guard — runtime-артефакты и секреты не коммитятся

**Files:**
- Create: `src/cf/guard.py`
- Modify: `src/cf/cli.py` (cmd_check_commit, cmd_install_hooks + parser)
- Test: `tests/test_guard.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_guard.py` (маркеры секретов собираются конкатенацией, чтобы сами тесты не триггерили guard):

```python
from cf.guard import check_staged

PEM = "-----BEGIN " + "PRIVATE" + " KEY-----"


def read_map(files):
    def read_text(path):
        if path not in files:
            raise OSError("deleted")
        return files[path]
    return read_text


def test_blocks_runtime_and_secret_paths():
    violations = check_staged(
        ["agent-runtime/evals/x.json", "secrets/key.json", "src/cf/cli.py"],
        read_map({"src/cf/cli.py": "print('ok')"}),
    )
    assert len(violations) == 2
    assert any("agent-runtime" in v for v in violations)


def test_blocks_pem_content_anywhere():
    violations = check_staged(["oops.txt"], read_map({"oops.txt": f"header\n{PEM}\n"}))
    assert len(violations) == 1


def test_deleted_staged_file_skipped():
    assert check_staged(["gone.txt"], read_map({})) == []


def test_clean_files_pass():
    assert check_staged(["a.py"], read_map({"a.py": "x = 1"})) == []
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_guard.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.guard'`

- [ ] **Step 3: Реализация** — `src/cf/guard.py`:

```python
BLOCKED_PREFIXES = ("agent-runtime/", "secrets/")
# Конкатенация — чтобы сам guard.py не содержал маркеры и проходил собственную проверку.
SECRET_MARKERS = ("PRIVATE" + " KEY", '"private' + '_key"')


def check_staged(paths, read_text):
    violations = []
    for path in paths:
        norm = path.replace("\\", "/")
        if norm.startswith(BLOCKED_PREFIXES):
            violations.append(f"{path}: runtime/секретный путь не коммитится")
            continue
        try:
            text = read_text(path)
        except OSError:
            continue  # файл удалён из индекса
        for marker in SECRET_MARKERS:
            if marker in text:
                violations.append(f"{path}: содержит маркер секрета")
                break
    return violations
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_guard.py -v
```
Expected: `4 passed`

- [ ] **Step 5: Добавить в `src/cf/cli.py`**

Импорт вверху:

```python
import subprocess
```

Функции:

```python
def cmd_check_commit(sheets, args):
    from cf.guard import check_staged
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"],
                         capture_output=True, text=True, check=True).stdout
    paths = [p for p in out.split("\0") if p]

    def read_text(path):
        return Path(path).read_text(encoding="utf-8", errors="ignore")

    violations = check_staged(paths, read_text)
    if violations:
        print("COMMIT BLOCKED:")
        for v in violations:
            print(f"  - {v}")
        return 1
    return 0


def cmd_install_hooks(sheets, args):
    hook = Path(".git/hooks/pre-commit")
    hook.write_text("#!/bin/sh\nexec .venv/Scripts/python -m cf check-commit\n",
                    encoding="utf-8", newline="\n")
    print(f"pre-commit hook installed: {hook}")
    return 0
```

Блоки в build_parser():

```python
    sp = sub.add_parser("check-commit", help="Проверить staged-файлы на секреты/runtime")
    sp.set_defaults(func=cmd_check_commit)

    sp = sub.add_parser("install-hooks", help="Установить pre-commit guard")
    sp.set_defaults(func=cmd_install_hooks)
```

- [ ] **Step 6: Установить hook и проверить вживую**

```bash
.venv/Scripts/python -m cf install-hooks
python -c "print('-----BEGIN ' + 'PRIVATE' + ' KEY-----')" > oops.pem
git add oops.pem
git commit -m "test guard"; echo "exit=$?"
git reset -q oops.pem && rm oops.pem
```
Expected: `COMMIT BLOCKED: ... oops.pem`, `exit=1`, коммит не создан (`git log -1` — прежний).

- [ ] **Step 7: Commit**

```bash
git add src/cf/guard.py src/cf/cli.py tests/test_guard.py
git commit -m "feat: pre-commit guard for runtime artifacts and secrets"
```

---

### Task 20: Трассировка relationship chain по brief_id

**Files:**
- Create: `src/cf/trace.py`
- Modify: `src/cf/cli.py` (cmd_trace + parser)
- Test: `tests/test_trace.py`

- [ ] **Step 1: Падающие тесты** — `tests/test_trace.py`:

```python
from cf.trace import build_trace

BRIEFS = [{"brief_id": "b-1", "source_pattern_ids": "p-1,p-2", "formula_id": "f-1",
           "prompt_version": "v1"}]
REELS = [{"reel_id": "r-1", "brief_id": "b-1", "platform": "tiktok",
          "post_url": "https://tiktok.com/@x/video/9"}]
PERF = [{"reel_id": "r-1", "brief_id": "b-1", "views": "50000", "er": "0.07",
         "measured_at": "2026-07-08"}]


def test_full_chain_no_missing_links():
    trace = build_trace("b-1", BRIEFS, REELS, PERF)
    assert trace["found"] is True
    assert trace["formula_id"] == "f-1"
    assert trace["reels"][0]["reel_id"] == "r-1"
    assert trace["performance"][0]["views"] == "50000"
    assert trace["missing_links"] == []


def test_reports_missing_links():
    trace = build_trace("b-1", [{"brief_id": "b-1", "source_pattern_ids": "",
                                 "formula_id": "f-1", "prompt_version": "v1"}], [], [])
    assert set(trace["missing_links"]) == {"source_pattern_ids", "published_reels"}


def test_unknown_brief():
    trace = build_trace("ghost", BRIEFS, REELS, PERF)
    assert trace["found"] is False and trace["missing_links"] == ["brief"]
```

- [ ] **Step 2: Убедиться, что падают**

```bash
.venv/Scripts/python -m pytest tests/test_trace.py -v
```
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.trace'`

- [ ] **Step 3: Реализация** — `src/cf/trace.py`:

```python
def build_trace(brief_id, briefs, reels, performance):
    brief = next((b for b in briefs if str(b.get("brief_id")) == str(brief_id)), None)
    if brief is None:
        return {"brief_id": str(brief_id), "found": False, "missing_links": ["brief"]}

    linked_reels = [r for r in reels if str(r.get("brief_id")) == str(brief_id)]
    perf = [p for p in performance if str(p.get("brief_id")) == str(brief_id)]

    missing = []
    for field in ("source_pattern_ids", "formula_id", "prompt_version"):
        if not str(brief.get(field, "")).strip():
            missing.append(field)
    if not linked_reels:
        missing.append("published_reels")
    if linked_reels and not perf:
        missing.append("performance_rows")

    return {
        "brief_id": str(brief_id),
        "found": True,
        "source_pattern_ids": brief.get("source_pattern_ids", ""),
        "formula_id": brief.get("formula_id", ""),
        "prompt_version": brief.get("prompt_version", ""),
        "reels": [{"reel_id": r.get("reel_id"), "platform": r.get("platform"),
                   "post_url": r.get("post_url")} for r in linked_reels],
        "performance": [{"reel_id": p.get("reel_id"), "views": p.get("views"),
                         "er": p.get("er"), "measured_at": p.get("measured_at")}
                        for p in perf],
        "missing_links": missing,
    }
```

- [ ] **Step 4: Тесты зелёные**

```bash
.venv/Scripts/python -m pytest tests/test_trace.py -v
```
Expected: `3 passed`

- [ ] **Step 5: Добавить в `src/cf/cli.py`**

Функция:

```python
def cmd_trace(sheets, args):
    from cf.trace import build_trace
    trace = build_trace(args.brief_id, sheets.read_rows("briefs"),
                        sheets.read_rows("reels"), sheets.read_rows("performance"))
    if not trace["found"]:
        print(f"brief {args.brief_id} не найден")
        return 1
    print(f"brief {trace['brief_id']}")
    print(f"  patterns: {trace['source_pattern_ids']}")
    print(f"  formula:  {trace['formula_id']}")
    print(f"  prompt:   {trace['prompt_version']}")
    for r in trace["reels"]:
        print(f"  reel {r['reel_id']} ({r['platform']}): {r['post_url']}")
    for p in trace["performance"]:
        print(f"    perf {p['measured_at']}: views={p['views']} er={p['er']}")
    if trace["missing_links"]:
        print(f"  MISSING: {', '.join(trace['missing_links'])}")
    return 0
```

Блок в build_parser():

```python
    sp = sub.add_parser("trace", help="Цепочка pattern->formula->brief->reel->performance")
    sp.add_argument("brief_id")
    sp.set_defaults(func=cmd_trace)
```

- [ ] **Step 6: Все тесты зелёные + commit**

```bash
.venv/Scripts/python -m pytest -v
git add src/cf/trace.py src/cf/cli.py tests/test_trace.py
git commit -m "feat: trace relationship chain by brief_id"
```

---

### Task 21: Сквозной e2e dry run и чеклист ручного прогона

**Files:**
- Create: `tests/fixtures/patterns.json`, `tests/fixtures/formula.json`, `tests/fixtures/brief.json`, `tests/test_e2e_dry_run.py`, `docs/dry-run-checklist.md`

- [ ] **Step 1: Создать фикстуры** (валидные артефакты полного цикла, связанные общими id)

`tests/fixtures/patterns.json`:

```json
{
  "meta": {
    "niche": "pets",
    "generated_at": "2026-07-09T00:00:00+00:00",
    "source_profile": "agent-runtime/profiles/2026-07-09-raw_tiktok-profile.json"
  },
  "patterns": [
    {
      "pattern_id": "pets-hook-question-01",
      "description": "Хук-вопрос в первые 2 секунды даёт выше ER",
      "evidence": {
        "source_urls": [
          "https://tiktok.com/@a/video/1",
          "https://tiktok.com/@b/video/2",
          "https://tiktok.com/@c/video/3"
        ],
        "avg_views": 120000,
        "avg_er": 0.081
      },
      "confidence": "medium"
    }
  ]
}
```

`tests/fixtures/formula.json`:

```json
{
  "name": "pets-question-hook",
  "niche": "pets",
  "hook_structure": {"timing": "0-3s", "elements": ["question"]},
  "problem_definition": "Владельцы питомцев не удерживают внимание в первые секунды",
  "solution_structure": {"beats": ["хук-вопрос", "демонстрация", "результат"]},
  "visual_requirements": ["крупный план питомца в первые 2 секунды"],
  "cta_type": "follow",
  "prohibitions": ["длинное интро", "текст без озвучки дольше 3с"],
  "evidence": {
    "source_urls": [
      "https://tiktok.com/@a/video/1",
      "https://tiktok.com/@b/video/2",
      "https://tiktok.com/@c/video/3"
    ],
    "avg_views": 120000,
    "avg_er": 0.081
  },
  "confidence": "medium",
  "conditions": "короткие ролики до 30с в нише pets",
  "source_pattern_ids": ["pets-hook-question-01"],
  "version": 1,
  "updated_at": "2026-07-09T00:00:00+00:00"
}
```

`tests/fixtures/brief.json`:

```json
{
  "brief_id": "b-001",
  "source_pattern_ids": ["pets-hook-question-01"],
  "formula_id": "pets-question-hook",
  "prompt_version": "v1",
  "hook": "А вы знали, что кошки различают ваш голос из тысячи?",
  "script": "0-3с: хук-вопрос с крупным планом кошки. 3-15с: домашний эксперимент. 15-25с: реакция кошки и вывод. Финал: CTA подписаться.",
  "visual_direction": "Съёмка на телефон, дневной свет, кошка в кадре с первой секунды",
  "cta": "Подпишись, чтобы узнать больше о своём питомце",
  "references": ["https://tiktok.com/@a/video/1", "https://tiktok.com/@b/video/2"]
}
```

- [ ] **Step 2: Написать e2e-тест** — `tests/test_e2e_dry_run.py`:

```python
import argparse

from cf.cli import cmd_profile, cmd_rejection_history, cmd_set_review
from cf.evalprep import build_eval_dataset
from cf.io import read_json
from cf.trace import build_trace
from cf.validate import validate_json_file

from tests.fakes import FakeSheets, make_raw_row


def ns(**kw):
    return argparse.Namespace(**kw)


def seed_sheets():
    briefs = [
        {"brief_id": "b-001", "source_pattern_ids": "pets-hook-question-01",
         "formula_id": "pets-question-hook", "prompt_version": "v1",
         "review_status": "approved", "rejection_reason": "", "reviewer_notes": ""},
        {"brief_id": "b-002", "source_pattern_ids": "pets-hook-question-01",
         "formula_id": "pets-question-hook", "prompt_version": "v1",
         "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
        {"brief_id": "b-003", "source_pattern_ids": "pets-hook-question-01",
         "formula_id": "pets-question-hook", "prompt_version": "v1",
         "review_status": "rejected", "rejection_reason": "слабый референс",
         "reviewer_notes": ""},
    ]
    reels = [{"reel_id": "reel-1", "brief_id": "b-001", "platform": "tiktok",
              "post_url": "https://tiktok.com/@x/video/9", "posted_at": "2026-07-05"}]
    performance = [{"reel_id": "reel-1", "brief_id": "b-001", "prompt_version": "v1",
                    "views": "50000", "er": "0.07", "measured_at": "2026-07-08"}]
    versions = [{"prompt_id": "brief-pets", "version": "v1",
                 "github_path": "prompts/briefs/pets/talking-head.md", "active": "TRUE"}]
    return FakeSheets({"raw_tiktok": [make_raw_row(i) for i in range(25)],
                       "briefs": briefs, "reels": reels,
                       "performance": performance, "prompt_versions": versions})


def test_full_cycle_dry_run(tmp_path):
    sheets = seed_sheets()

    # 1. Профилирование: батч готов к анализу
    assert cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                  min_rows=20, out_dir=str(tmp_path))) == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-profile.json")))
    assert report["ready_for_analysis"] is True

    # 2. Артефакты цикла валидны по схемам (evidence-цепочка не рвётся)
    assert validate_json_file("patterns", "tests/fixtures/patterns.json") == []
    assert validate_json_file("formula", "tests/fixtures/formula.json") == []
    assert validate_json_file("brief", "tests/fixtures/brief.json") == []

    # 3. Ревью pending-брифа обновляет таблицу
    assert cmd_set_review(sheets, ns(brief_id="b-002", status="approved",
                                     reason="", notes="соответствует формуле")) == 0
    statuses = {b["brief_id"]: b["review_status"] for b in sheets.read_rows("briefs")}
    assert statuses["b-002"] == "approved"

    # 4. Rejection history собирает причины
    out = tmp_path / "rejection_history.json"
    assert cmd_rejection_history(sheets, ns(out=str(out))) == 0
    assert read_json(out)["reasons"][0]["reason"] == "слабый референс"

    # 5. Eval связывает performance с prompt_version и формулой
    dataset = build_eval_dataset(sheets.read_rows("performance"),
                                 sheets.read_rows("briefs"),
                                 sheets.read_rows("prompt_versions"))
    assert dataset["by_prompt_version"]["v1"]["reels"] == 1
    assert dataset["rows"][0]["formula_id"] == "pets-question-hook"
    assert dataset["reviewer_pass_rate"] == 2 / 3

    # 6. Relationship chain целая
    trace = build_trace("b-001", sheets.read_rows("briefs"),
                        sheets.read_rows("reels"), sheets.read_rows("performance"))
    assert trace["missing_links"] == []

    # 7. Запуски логировались
    assert any(tab == "run_log" for tab, _ in sheets.appended)
```

- [ ] **Step 3: Прогнать e2e и весь набор**

```bash
.venv/Scripts/python -m pytest tests/test_e2e_dry_run.py -v
.venv/Scripts/python -m pytest
```
Expected: e2e `1 passed`; полный набор — все пройдены, 0 failed.

- [ ] **Step 4: Создать `docs/dry-run-checklist.md`** (ручной прогон с реальными Sheets и LLM-агентами)

```markdown
# Сквозной прогон полного цикла (ручной dry run)

Предусловия: cf.config.json заполнен реальным spreadsheet_id;
secrets/service-account.json на месте; таблица расшарена на email сервисного аккаунта;
в CF Raw TikTok >= 20 строк.

1. `/cf-status` — таблица доступна, ошибок нет.
2. `/cf-profile raw_tiktok` — отчёт с ready_for_analysis: true (иначе добрать данные).
3. `/cf-analyze raw_tiktok --niche <ниша>` — patterns-файл создан и валиден,
   наблюдения в памяти, запись в Run Log.
4. `/cf-formula` — формула в formulas/<ниша>/ валидна; после «утверждаю» попала в
   formulas/_approved/index.json, решение в памяти, commit прошёл guard.
5. n8n сгенерировал бриф из approved-формулы → строка в CF Creative Briefs со
   status=pending. (n8n недоступен → добавить строку руками по prompts/briefs/_shared/schema.md.)
6. `/cf-review-brief --pending` — вердикт вынесен, review_status обновлён,
   review-файл в agent-runtime/reviews/.
7. Заполнить CF Published Reels и CF Performance для одобренного брифа (или дождаться n8n).
8. `/cf-eval` — weekly-eval.json с success_criteria и атрибуцией; инсайты в памяти.
9. `/cf-propose-update <prompt_id>` — proposal с evidence; проверить оба исхода:
   approve (промпт изменён, новая строка в CF Prompt Versions, commit) и
   reject (промпт не тронут, решение в памяти).
10. `.venv/Scripts/python -m cf trace <brief_id>` — цепочка без MISSING.
11. `git log --oneline` — нет коммитов с agent-runtime/ или секретами.

Все 11 шагов прошли без правок кода → success criteria спеки закрыты, MVP готов.
```

- [ ] **Step 5: Финальный commit**

```bash
git add tests/fixtures/ tests/test_e2e_dry_run.py docs/dry-run-checklist.md
git commit -m "test: e2e dry run of the full cycle + manual checklist"
```

---

## Порядок выполнения и параллелизация

Ядро строго последовательно: **1 → 2 → 3 → 4 → 5 → 6 → 7** (инфраструктура, от которой зависит всё).

После Task 7 допустима параллельная работа (независимые файлы):
- **Аналитическая цепочка:** 8 → 9 → 10 → 11
- **Ревью-цепочка:** 12 → 13 → 14 → 15
- **Независимые:** 19 (guard), 20 (trace) — в любой момент после 7
- **Eval-цепочка:** 16 (после 14) → 17
- **Финал:** 18 (после 13 и 17), затем 21 — строго последняя.

## Решения открытых вопросов спеки

| Вопрос спеки | Решение | Где в плане |
|---|---|---|
| 1. Sheets: MCP или API | gspread + service account, ключ в secrets/ | Task 5 |
| 2. GitHub: git или MCP | обычный git workflow + pre-commit guard | Tasks 17, 19 |
| 3. Триггер n8n → Claude Code | review_status=pending + /cf-review-brief --pending (опц. `claude -p`) | Task 15 |
| 4. Пороги confidence | high: >=5 url и 2x медианы; medium: 3-4; low: <=2 → не в формулы | Task 9 (промпт) |
| 5. Веса attribution | приоритет правил prompt > reference > production > publishing | Task 16 (промпт) |

## Соответствие спеке (self-review)

- **6 агентов** → Raw Batch Profiler (Task 8), Pattern Analyzer (9), Formula Writer (10), Brief Reviewer (13), Eval Agent (16), Prompt Optimizer (17).
- **7 slash-команд** → /cf-status (7, 18), /cf-profile (8), /cf-analyze (9), /cf-formula (10), /cf-review-brief (13), /cf-eval (16), /cf-propose-update (17).
- **Error handling спеки** → retry 3x (3, 5); insufficient_data (8, 9, 16); rejected/revise-логирование (13, 14); частичное состояние — атомарная запись артефактов (4) + Run Log; понятные ошибки CLI (7). Отдельного механизма resume в MVP нет: артефакты перезаписываются идемпотентно по дате — осознанное упрощение.
- **Security** → секреты вне git (1, 19), human-in-the-loop и no-auto-push (17), runtime-артефакты не версионируются (1, 19).
- **Success criteria** → метрики в eval-отчёте (16), сквозная проверка (21).
- **Out of Scope спеки** (dashboard, боты, автопубликация и т.д.) — задач нет, как и требуется.
- Плейсхолдеров нет; сигнатуры сверены: `with_retry` / `log_run` / `validate_json_file` / `build_eval_dataset` / `check_staged` / `build_trace` используются в тестах, CLI и промптах единообразно.

## Завершение

После Task 21 (полный `pytest` зелёный, чеклист dry run записан):
использовать **superpowers:finishing-a-development-branch** — прогнать все тесты, выбрать merge в main / PR / дальнейшую работу.
