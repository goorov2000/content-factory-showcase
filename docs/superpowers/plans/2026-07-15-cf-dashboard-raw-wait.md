# Ожидание выгрузки raw + счётчики прокрута — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (merge ffcd892, 2026-07-15). Чекбоксы в ходе исполнения не велись.

**Goal:** Звено «raw» дашборда ждёт конца выгрузки (3 мин без прироста строк в Sheets) и показывает «+N за прокрут»; звено «factory» показывает число внутренних операций по CF Run Log; ручной запуск factory блокируется, пока raw выгружается.

**Architecture:** Всё в `src/cf/dashboard/runner.py` (StageRunner уже владеет `sheets` и `config`). Для тестируемости — инжектируемые `sleep_fn`/`now_fn`. UI не трогаем: карточки уже рендерят `runner.state[key].detail` и перерисовываются каждые 2 с.

**Tech Stack:** Python 3.13, FastAPI-дашборд (не трогаем), gspread через `cf.sheets.Sheets` (интерфейс `read_rows(tab_key)`), pytest.

**Спека:** `docs/superpowers/specs/2026-07-15-cf-dashboard-raw-wait-design.md`

**Запуск тестов:** из корня `<local-path>`:
`.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q`

**Фиксированные строки detail (использовать дословно):**

- live raw: `выгрузка: +{total} (TikTok +{tt} · Instagram +{ig})`
- финал raw (прирост > 0): `+{total} за прокрут · TikTok +{tt} · Instagram +{ig}`
- финал raw (прирост 0): `+0 за прокрут · сбор ничего не привёз` — статус `warn`, в Run Log `insufficient_data`, errors `["raw: прирост 0 строк"]`
- суффикс при несработавшем Run Log и кастомном detail: `{detail} · запись в Run Log не удалась`
- live factory: `выполняется… · операций {n} · {метка} {k} · …`
- финал factory: `операций {n} · {метка} {k} · …`; если операций нет — `операций 0`
- отказ ручного запуска: cycle_note = `«Контент-завод» не запущен: raw ещё выгружается`

---

## File Structure

- Modify: `src/cf/dashboard/runner.py` — весь функционал (инжект времени, `_wait_for_raw`, `_factory_ops_detail`, правки `run_sync`/`start`/`_finish`, константы).
- Modify: `tests/test_dashboard_runner.py` — фейковые часы в `make_runner`, новые тесты, правка трёх существующих под новое поведение raw.

Никаких новых файлов. `tests/fakes.py` не менять (FakeSheets достаточно — счётчики строк задаём переопределением `read_rows` в локальном хелпере теста).

---

### Task 1: Инжектируемые часы (без изменения поведения)

**Files:**
- Modify: `src/cf/dashboard/runner.py` (импорты, `__init__`)
- Modify: `tests/test_dashboard_runner.py` (`make_runner`)

- [ ] **Step 1: Написать падающий тест**

В конец `tests/test_dashboard_runner.py`:

```python
def test_runner_accepts_injected_clock():
    clock = {"t": 0.0}
    runner = StageRunner(FakeSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None,
                         run_command=lambda argv: (0, "{}"),
                         sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                         now_fn=lambda: clock["t"])
    assert runner.now() == 0.0
    runner.sleep(30)
    assert runner.now() == 30.0
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py::test_runner_accepts_injected_clock -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'sleep_fn'`

- [ ] **Step 3: Минимальная реализация**

В `runner.py`: добавить `import time` к импортам; в `StageRunner.__init__` подпись и тело:

```python
    def __init__(self, sheets, config, http_post=None, run_command=None,
                 sleep_fn=None, now_fn=None):
        self.sheets = sheets
        self.config = config or {}
        self.http_post = http_post or _default_post
        self.run_command = run_command or _default_run
        self.sleep = sleep_fn or time.sleep
        self.now = now_fn or time.monotonic
        self.state = {s: {"status": "idle", "detail": ""} for s in STAGES}
        self.reports = {s: [] for s in STAGES}
        self.cycle_note = ""
        self._lock = threading.Lock()
```

- [ ] **Step 4: Подготовить make_runner к будущим задачам**

В `tests/test_dashboard_runner.py` заменить `make_runner` целиком (фейковые часы по умолчанию, чтобы будущий опрос raw не спал по-настоящему):

```python
def make_runner(config=CONFIG, post_fails=False, cmd_code=0,
                cmd_output='{"result": "готово", "session_id": "sess-1"}',
                sheets=None):
    sheets = sheets if sheets is not None else FakeSheets({"run_log": []})
    posted, commands = [], []
    clock = {"t": 0.0}

    def http_post(url):
        posted.append(url)
        if post_fails:
            raise ConnectionError("n8n down")

    def run_command(argv):
        commands.append(argv)
        return (cmd_code, cmd_output)

    runner = StageRunner(sheets, config, http_post=http_post, run_command=run_command,
                         sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                         now_fn=lambda: clock["t"])
    return runner, sheets, posted, commands
```

- [ ] **Step 5: Прогнать весь файл тестов**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q`
Expected: все PASS (поведение не менялось).

- [ ] **Step 6: Commit**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "dashboard: инжектируемые sleep/now у StageRunner (подготовка raw-wait)"
```

---

### Task 2: Ожидание выгрузки raw + счётчик «+N за прокрут»

**Files:**
- Modify: `src/cf/dashboard/runner.py` (константы, `_raw_wait_params`, `_raw_counts`, `_wait_for_raw`, `_finish`, `run_sync`)
- Modify: `tests/test_dashboard_runner.py` (хелпер RawSheets, новые тесты, правка трёх существующих)

- [ ] **Step 1: Хелпер тестов — Sheets с последовательностью счётчиков строк**

В `tests/test_dashboard_runner.py` после `make_runner` добавить:

```python
class RawSheets(FakeSheets):
    """read_rows("raw_tiktok"/"raw_instagram") отдаёт заданное число строк на каждое чтение.

    Последнее значение последовательности повторяется, когда чтения кончились.
    Первый вызов на вкладку — базовый замер до вебхуков.
    """

    def __init__(self, tt=(0,), ig=(0,), run_log=None):
        super().__init__({"run_log": list(run_log or [])})
        self._seq = {"raw_tiktok": list(tt), "raw_instagram": list(ig)}
        self.reads = {"raw_tiktok": 0, "raw_instagram": 0}

    def read_rows(self, tab_key):
        if tab_key in self._seq:
            seq = self._seq[tab_key]
            n = seq[min(self.reads[tab_key], len(seq) - 1)]
            self.reads[tab_key] += 1
            return [{"raw_id": i} for i in range(n)]
        return super().read_rows(tab_key)
```

- [ ] **Step 2: Написать падающие тесты ожидания**

Добавить в конец файла (`RAW_WAIT_CONFIG` — быстрые пороги, чтобы тесты не крутили десятки опросов):

```python
RAW_WAIT_CONFIG = {"dashboard": {
    "workflows": {"raw": "https://n8n.local/webhook/raw"},
    "raw_wait": {"poll_sec": 10, "stall_sec": 30},
}}


def test_raw_wait_finishes_after_stall_and_counts_delta():
    sheets = RawSheets(tt=(100, 110, 120, 120, 120, 120, 120),
                       ig=(50, 55, 55, 55, 55, 55, 55))
    runner, _, posted, _ = make_runner(config=RAW_WAIT_CONFIG, sheets=sheets)
    assert runner.run_sync("raw") is True
    assert posted == ["https://n8n.local/webhook/raw"]
    assert runner.state["raw"] == {
        "status": "ok",
        "detail": "+25 за прокрут · TikTok +20 · Instagram +5",
    }
    assert sheets.appended[-1][1]["status"] == "success"


def test_raw_wait_zero_growth_is_warn_insufficient():
    sheets = RawSheets(tt=(100, 100, 100, 100), ig=(50, 50, 50, 50))
    runner, _, _, _ = make_runner(config=RAW_WAIT_CONFIG, sheets=sheets)
    assert runner.run_sync("raw") is True
    assert runner.state["raw"] == {
        "status": "warn",
        "detail": "+0 за прокрут · сбор ничего не привёз",
    }
    row = sheets.appended[-1][1]
    assert row["status"] == "insufficient_data"
    assert "прирост 0 строк" in row["errors"]


def test_raw_wait_live_detail_updates_during_polling():
    sheets = RawSheets(tt=(100, 110, 110, 110, 110), ig=(50, 50, 50, 50, 50))
    seen = []
    runner, _, _, _ = make_runner(config=RAW_WAIT_CONFIG, sheets=sheets)
    orig_sleep = runner.sleep

    def spy_sleep(s):
        seen.append(runner.state["raw"]["detail"])
        orig_sleep(s)

    runner.sleep = spy_sleep
    runner.run_sync("raw")
    assert "выгрузка: +10 (TikTok +10 · Instagram +0)" in seen


def test_raw_wait_uses_config_poll_interval():
    sheets = RawSheets(tt=(1, 1, 1, 1), ig=(0, 0, 0, 0))
    slept = []
    runner, _, _, _ = make_runner(config=RAW_WAIT_CONFIG, sheets=sheets)
    clock = {"t": 0.0}
    runner.now = lambda: clock["t"]

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    runner.sleep = sleep
    runner.run_sync("raw")
    assert slept and all(s == 10 for s in slept)


def test_publish_stage_stays_fire_and_forget():
    runner, sheets, posted, _ = make_runner()
    assert runner.run_sync("publish") is True
    assert posted == ["https://n8n.local/webhook/publish"]
    assert runner.state["publish"] == {"status": "ok", "detail": "успешно"}
    assert sheets.appended[-1][1]["status"] == "success"


def test_cycle_waits_raw_before_factory():
    sheets = RawSheets(tt=(0, 5, 5, 5, 5), ig=(0, 0, 0, 0, 0))
    polls_at_claude = []
    runner, _, posted, _ = make_runner(config=RAW_WAIT_CONFIG, sheets=sheets)
    orig_cmd = runner.run_command

    def cmd(argv):
        polls_at_claude.append(sheets.reads["raw_tiktok"])
        return orig_cmd(argv)

    runner.run_command = cmd
    runner.run_cycle_sync()
    assert posted == ["https://n8n.local/webhook/raw"]
    # базовый замер + 4 опроса ожидания случились ДО первого вызова claude
    assert polls_at_claude and polls_at_claude[0] == 5
    assert runner.cycle_note == "цикл дошёл до ручного одобрения — ждёт продюсера"
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q -k "raw_wait or cycle_waits"`
Expected: 5 FAIL (detail «успешно» вместо счётчиков; live-строк нет; опросов до claude нет).

- [ ] **Step 4: Реализация в runner.py**

Константы после `REPORT_TEXT_LIMIT`:

```python
RAW_TABS = (("raw_tiktok", "TikTok"), ("raw_instagram", "Instagram"))
RAW_WAIT_DEFAULTS = {"poll_sec": 30, "stall_sec": 180}
```

Методы StageRunner (рядом с `_claim`):

```python
    def _raw_wait_params(self):
        cfg = self.config.get("dashboard", {}).get("raw_wait", {})
        return {k: cfg.get(k, v) for k, v in RAW_WAIT_DEFAULTS.items()}

    def _raw_counts(self):
        return {tab: len(self.sheets.read_rows(tab)) for tab, _ in RAW_TABS}

    def _wait_for_raw(self, base):
        """Опрос raw-вкладок до 3 минут тишины (правило оператора).

        Возвращает {tab: прирост}. Прирост < 0 (чистка таблицы) считается нулём.
        """
        params = self._raw_wait_params()
        last_growth = self.now()
        deltas = {tab: 0 for tab, _ in RAW_TABS}
        while True:
            self.sleep(params["poll_sec"])
            counts = self._raw_counts()
            new = {tab: max(0, counts[tab] - base[tab]) for tab, _ in RAW_TABS}
            if sum(new.values()) > sum(deltas.values()):
                last_growth = self.now()
            deltas = new
            self.state["raw"] = {"status": "running",
                                 "detail": _raw_detail("выгрузка: ", deltas, live=True)}
            if self.now() - last_growth >= params["stall_sec"]:
                return deltas
```

Модульная функция форматирования (рядом с `_parse_claude_output`):

```python
def _raw_detail(prefix, deltas, live=False):
    total = sum(deltas.values())
    parts = " · ".join(f"{label} +{deltas[tab]}" for tab, label in RAW_TABS)
    if live:
        return f"{prefix}+{total} ({parts})"
    return f"+{total} за прокрут · {parts}"
```

`_finish` — поддержка кастомного detail и статуса Run Log:

```python
    def _finish(self, stage, exc=None, detail=None, log_status="success", log_errors=()):
        """Общий хвост run_sync/_reply_sync: статус ok/warn/error + запись в Run Log."""
        if exc is not None:
            self.state[stage] = {"status": "error", "detail": str(exc)}
            self._log(stage, "failed", errors=[str(exc)])
            return False
        logged = self._log(stage, log_status, errors=log_errors)
        status = "ok" if log_status == "success" else "warn"
        if logged:
            self.state[stage] = {"status": status, "detail": detail or "успешно"}
        elif detail:
            self.state[stage] = {"status": "warn",
                                 "detail": f"{detail} · запись в Run Log не удалась"}
        else:
            self.state[stage] = {"status": "warn",
                                 "detail": "успешно, но запись в Run Log не удалась"}
        return True
```

Ветка n8n в `run_sync` (заменить целиком блок `if spec["kind"] == "n8n":` и хвост функции):

```python
        deltas = None
        try:
            if spec["kind"] == "n8n":
                urls = self.config.get("dashboard", {}).get("workflows", {}).get(stage)
                if not urls:
                    raise RuntimeError(
                        f"webhook для «{stage}» не настроен в cf.config.json (dashboard.workflows)")
                base = self._raw_counts() if stage == "raw" else None
                # одно звено может дёргать несколько воркфлоу (raw: TikTok + Instagram)
                for url in ([urls] if isinstance(urls, str) else urls):
                    self.http_post(url)
                    self._add_report(stage, "webhook", f"POST {url} → OK")
                if stage == "raw":
                    deltas = self._wait_for_raw(base)
            else:
                for prompt in spec["prompts"]:
                    code, out = self.run_command(
                        ["claude", "-p", prompt, "--output-format", "json"])
                    text, session_id = _parse_claude_output(out)
                    # отчёт пишем ДО проверки кода выхода — провал тоже должен быть виден
                    self._add_report(stage, prompt, text, session_id)
                    if code != 0:
                        raise RuntimeError(
                            f"claude -p {prompt}: код выхода {code}: {text[-200:]}")
        except Exception as exc:
            return self._finish(stage, exc)
        if deltas is not None:
            if sum(deltas.values()) == 0:
                return self._finish(stage, detail="+0 за прокрут · сбор ничего не привёз",
                                    log_status="insufficient_data",
                                    log_errors=["raw: прирост 0 строк"])
            return self._finish(stage, detail=_raw_detail("", deltas))
        return self._finish(stage)
```

- [ ] **Step 5: Обновить три существующих теста под новое поведение raw**

В `tests/test_dashboard_runner.py`:

1. `test_n8n_stage_posts_webhook_and_logs` — FakeSheets без raw-вкладок даёт прирост 0 → лог теперь `insufficient_data`. Заменить последние две строки:

```python
    (tab, row), = sheets.appended
    assert row["agent"] == "dashboard-raw" and row["status"] == "insufficient_data"
```

2. `test_multi_url_raw_success_status_is_ok` — дать прирост и проверить счётчик. Заменить тело целиком:

```python
def test_multi_url_raw_success_status_is_ok():
    config = {"dashboard": {
        "workflows": {"raw": ["https://n8n.local/webhook/raw-tt",
                              "https://n8n.local/webhook/raw-ig"]},
        "raw_wait": {"poll_sec": 10, "stall_sec": 30},
    }}
    sheets = RawSheets(tt=(0, 3, 3, 3, 3), ig=(0, 1, 1, 1, 1))
    runner, _, posted, _ = make_runner(config=config, sheets=sheets)
    assert runner.run_sync("raw") is True
    assert posted == ["https://n8n.local/webhook/raw-tt",
                      "https://n8n.local/webhook/raw-ig"]
    assert runner.state["raw"] == {"status": "ok",
                                   "detail": "+4 за прокрут · TikTok +3 · Instagram +1"}
```

3. `test_log_failure_does_not_mask_success` — перевести на звено `stats` (путь с detail=None остаётся только у publish/stats). Заменить тело:

```python
def test_log_failure_does_not_mask_success():
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("sheets down")

    runner = StageRunner(BrokenLogSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None, run_command=lambda argv: (0, "{}"))
    assert runner.run_sync("stats") is True
    assert runner.state["stats"] == {"status": "warn",
                                     "detail": "успешно, но запись в Run Log не удалась"}
```

Примечание: `test_full_cycle_runs_auto_stages_then_waits`, `test_full_cycle_stops_on_error`, `test_cycle_stops_when_stage_already_busy`, `test_start_cycle_refuses_double_run` используют CONFIG с FakeSheets (прирост 0) — raw завершится warn (`insufficient_data`), но `run_sync` вернёт True и цикл продолжится; их ассерты не про detail raw, поэтому проходят без правок. `test_n8n_stage_posts_all_webhooks_for_list` ассертит `sheets.appended[-1][1]["status"] == "success"` — прирост 0 даст `insufficient_data`: заменить ожидание на `"insufficient_data"`.

- [ ] **Step 6: Прогнать весь файл**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q`
Expected: все PASS.

- [ ] **Step 7: Прогнать весь сьют (дашборд-эндпоинты тоже трогают runner)**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: все PASS. Если упали тесты app-слоя из-за нового поведения raw (реальный `time.sleep` в проде их не касается — там runner создаётся с дефолтными часами, но в тестах app может дергать run_sync("raw") с FakeSheets): передать в тестовый StageRunner фейковые часы аналогично make_runner.

- [ ] **Step 8: Commit**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "dashboard: звено raw ждёт конца выгрузки и показывает +N за прокрут"
```

---

### Task 3: Блок ручного запуска factory, пока raw выгружается

**Files:**
- Modify: `src/cf/dashboard/runner.py` (`start`)
- Modify: `tests/test_dashboard_runner.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def test_manual_factory_blocked_while_raw_running():
    runner, _, _, _ = make_runner()
    runner.state["raw"]["status"] = "running"
    assert runner.start("factory") is False
    assert runner.cycle_note == "«Контент-завод» не запущен: raw ещё выгружается"
    assert runner.state["factory"]["status"] == "idle"


def test_manual_factory_allowed_when_raw_idle():
    runner, _, _, _ = make_runner()
    assert runner.start("factory") is True
    assert _wait_until(lambda: runner.state["factory"]["status"] == "ok")
```

- [ ] **Step 2: Убедиться, что первый тест падает**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q -k manual_factory`
Expected: `test_manual_factory_blocked_while_raw_running` FAIL (start вернул True), второй PASS.

- [ ] **Step 3: Реализация**

В `StageRunner.start` после проверки `if stage not in STAGES:`:

```python
        if stage == "factory" and self.state["raw"]["status"] == "running":
            self.cycle_note = "«Контент-завод» не запущен: raw ещё выгружается"
            return False
```

- [ ] **Step 4: Прогнать файл тестов**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "dashboard: ручной запуск factory блокируется, пока raw выгружается"
```

---

### Task 4: Счётчик операций «Контент-завода» по CF Run Log

**Files:**
- Modify: `src/cf/dashboard/runner.py` (константа AGENT_LABELS, `_factory_ops_detail`, ветка claude в `run_sync`)
- Modify: `tests/test_dashboard_runner.py`

- [ ] **Step 1: Написать падающие тесты**

```python
def _cmd_appends_run_log(sheets, batches):
    """run_command, дописывающий строки в run_log на каждый вызов claude."""
    calls = {"i": 0}

    def run_command(argv):
        for agent in batches[min(calls["i"], len(batches) - 1)]:
            sheets.tables["run_log"].append({"agent": agent})
        calls["i"] += 1
        return (0, '{"result": "готово", "session_id": "sess-1"}')

    return run_command


def test_factory_ops_counted_from_run_log():
    sheets = FakeSheets({"run_log": [{"agent": "старый-запуск"}]})
    runner = StageRunner(sheets, CONFIG, http_post=lambda url: None,
                         run_command=_cmd_appends_run_log(sheets, [
                             ["pattern-analyzer", "pattern-analyzer", "dashboard-review"],
                             ["brief-generator"],
                         ]))
    assert runner.run_sync("factory") is True
    assert runner.state["factory"] == {"status": "ok",
                                       "detail": "операций 3 · анализ 2 · брифы 1"}


def test_factory_ops_zero_when_log_untouched():
    runner, _, _, _ = make_runner()
    runner.run_sync("factory")
    assert runner.state["factory"] == {"status": "ok", "detail": "операций 0"}


def test_factory_ops_unknown_agent_shown_as_is():
    sheets = FakeSheets({"run_log": []})
    runner = StageRunner(sheets, CONFIG, http_post=lambda url: None,
                         run_command=_cmd_appends_run_log(sheets, [["mystery-agent"], []]))
    runner.run_sync("factory")
    assert runner.state["factory"]["detail"] == "операций 1 · mystery-agent 1"


def test_factory_ops_live_detail_between_prompts():
    sheets = FakeSheets({"run_log": []})
    live = []

    def run_command(argv):
        live.append(dict(runner.state["factory"]))
        sheets.tables["run_log"].append({"agent": "pattern-analyzer"})
        return (0, '{"result": "готово", "session_id": "sess-1"}')

    runner = StageRunner(sheets, CONFIG, http_post=lambda url: None,
                         run_command=run_command)
    runner.run_sync("factory")
    # ко второму промпту в detail уже виден счёт первого
    assert live[1] == {"status": "running",
                       "detail": "выполняется… · операций 1 · анализ 1"}


def test_factory_ops_survive_run_log_read_failure():
    class NoReadSheets(FakeSheets):
        def read_rows(self, tab_key):
            raise ConnectionError("sheets down")

    runner = StageRunner(NoReadSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None,
                         run_command=lambda argv: (0, "{}"))
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "ok"
    assert runner.state["factory"]["detail"] == "успешно"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q -k factory_ops`
Expected: 5 FAIL (detail «успешно», live-строк нет).

- [ ] **Step 3: Реализация**

Константа после `RAW_WAIT_DEFAULTS`:

```python
AGENT_LABELS = {
    "pattern-analyzer": "анализ",
    "brief-generator": "брифы",
    "brief-reviewer": "ревью",
    "raw-batch-profiler": "профилирование",
    "niche-classifier": "ниши",
    "formula-writer": "формула",
    "eval-agent": "eval",
}
```

Метод StageRunner (рядом с `_raw_counts`):

```python
    def _factory_ops_detail(self, base):
        """Сводка операций звена по строкам Run Log, добавленным после base.

        Best-effort: сбой чтения не мешает звену — возвращается None.
        """
        try:
            rows = self.sheets.read_rows("run_log")
        except Exception:
            logger.warning("не удалось прочитать Run Log для сводки операций",
                           exc_info=True)
            return None
        agents = [str(r.get("agent", "")) for r in rows[base:]]
        agents = [a for a in agents if a and not a.startswith("dashboard-")]
        if not agents:
            return "операций 0"
        counts = Counter(AGENT_LABELS.get(a, a) for a in agents)
        parts = " · ".join(f"{label} {n}" for label, n in counts.most_common())
        return f"операций {len(agents)} · {parts}"
```

(Не забыть импорт наверху `runner.py`: `from collections import Counter` — рядом с `import json`.)

Ветка claude в `run_sync` (заменить блок `else:` внутри try):

```python
            else:
                base_log = None
                if stage == "factory":
                    try:
                        base_log = len(self.sheets.read_rows("run_log"))
                    except Exception:
                        logger.warning("не удалось снять базовый замер Run Log",
                                       exc_info=True)
                ops = None
                for prompt in spec["prompts"]:
                    code, out = self.run_command(
                        ["claude", "-p", prompt, "--output-format", "json"])
                    text, session_id = _parse_claude_output(out)
                    # отчёт пишем ДО проверки кода выхода — провал тоже должен быть виден
                    self._add_report(stage, prompt, text, session_id)
                    if code != 0:
                        raise RuntimeError(
                            f"claude -p {prompt}: код выхода {code}: {text[-200:]}")
                    if base_log is not None:
                        ops = self._factory_ops_detail(base_log)
                        if ops is not None:
                            self.state[stage] = {"status": "running",
                                                 "detail": f"выполняется… · {ops}"}
```

И хвост функции после `except` (вместо `return self._finish(stage)` — учесть ops; блок с deltas из Task 2 остаётся выше):

```python
        if deltas is not None:
            if sum(deltas.values()) == 0:
                return self._finish(stage, detail="+0 за прокрут · сбор ничего не привёз",
                                    log_status="insufficient_data",
                                    log_errors=["raw: прирост 0 строк"])
            return self._finish(stage, detail=_raw_detail("", deltas))
        if stage == "factory":
            return self._finish(stage, detail=ops)
        return self._finish(stage)
```

Примечание: `ops` объявлен внутри try; чтобы он был виден в хвосте, объявить `ops = None` рядом с `deltas = None` до try (а строку `ops = None` внутри ветки else убрать). Итоговое начало run_sync:

```python
    def run_sync(self, stage, already_marked=False):
        spec = STAGES[stage]  # KeyError для неизвестного звена
        if not already_marked:
            self.state[stage] = {"status": "running", "detail": "выполняется…"}
        deltas = None
        ops = None
        try:
```

- [ ] **Step 4: Проверить хвост-кейс `_finish(stage, detail=None)`**

`test_factory_ops_survive_run_log_read_failure` покрывает: `ops is None` → `_finish(stage, detail=None)` → detail «успешно». Ничего дополнительно писать не надо, просто держать в голове при реализации.

- [ ] **Step 5: Прогнать весь файл**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_runner.py -q`
Expected: два существующих теста упадут — у factory теперь detail «операций 0» вместо «успешно» (FakeSheets содержит "run_log", чтение не падает, новых строк нет). Обновить их ассерты в этом же шаге:

1. `test_stage_success_status_is_ok`:

```python
    assert runner.state["factory"] == {"status": "ok", "detail": "операций 0"}
```

2. `test_start_runs_in_background_thread` (последняя строка):

```python
    assert runner.state["factory"]["detail"] == "операций 0"
```

После правок прогнать файл ещё раз — все PASS.

- [ ] **Step 6: Прогнать весь сьют**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: все PASS.

- [ ] **Step 7: Commit**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "dashboard: карточка Контент-завода показывает счёт операций по Run Log"
```

---

### Task 5: Финальная верификация вживую

**Files:** нет правок кода.

- [ ] **Step 1: Полный сьют ещё раз**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: все PASS.

- [ ] **Step 2: Перезапустить дашборд и убедиться, что Обзор рендерится**

```powershell
$conn = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Stop-Process -Id $conn[0].OwningProcess -Force -Confirm:$false }
# из корня <local-path>:
.venv\Scripts\python.exe -m cf dashboard   # фоном
```

Открыть http://127.0.0.1:8787/overview → карточки конвейера на месте. ВАЖНО (Windows): venv-лаунчер порождает дочерний процесс — сервер убивать только по владельцу порта, как выше.

- [ ] **Step 3: Смоук ожидания без Apify (по желанию оператора)**

Реальный клик ▶ на raw тратит кредиты Apify — по умолчанию НЕ кликать. Поведение ожидания уже покрыто тестами; вживую его увидит первый рабочий прокрут оператора.

- [ ] **Step 4: Финальный коммит (если что-то правилось)**

```bash
git status --short   # чисто? если нет:
git add -A && git commit -m "dashboard: доводка raw-wait по итогам живой проверки"
```
