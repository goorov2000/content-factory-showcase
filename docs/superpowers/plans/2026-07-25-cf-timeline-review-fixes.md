# Правки по ревью ленты конвейера (прогресс v2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть 14 находок ревью диапазона `origin/master..master` (маркеры прогресса сбора, движок прогресса v2 в дашборде, потоковое чтение stdout, UX/a11y-проход) — от потери причины упавшего сбора и повторного платного прогона Apify до мёртвого состояния и дублей CSS.

**Architecture:** Правки точечные, архитектуру прогресса не меняем. Три группы: (1) поведенческие баги раннера/сбора — `src/cf/dashboard/runner.py`, `src/cf/collect/progress.py`; (2) математика кривой — `src/cf/dashboard/progress.py` (чистые функции, тестируются без Sheets); (3) фронт и чистота — шаблон ревью, `timeline.js`, `style.css`, снятие мёртвого кода. Каждая задача: сначала падающий тест, потом минимальная правка, потом коммит.

**Tech Stack:** Python 3.12, pytest (`tests/fakes.py::FakeSheets`), FastAPI + Jinja2, htmx **1.9.12** (важно для задачи 6), ванильный JS в `static/timeline.js`.

---

## Контекст

Диапазон `origin/master..master` привёз живой прогресс ленты: `cf collect` печатает маркеры `CF_PROGRESS`, раннер читает stdout подпроцесса потоково, клиентский тикер двигает полоску между опросами htmx. Ревью нашло 14 дефектов. Четыре из них ломают ровно то, ради чего изменение делалось:

- упавший сбор теперь оставляет **пустой** отчёт этапа (маркеры вытеснили stderr) — оператор видит «raw упало» без причины;
- проба сигнатуры `run_command` через `except TypeError` может **запустить сбор второй раз** — второй платный прогон Apify, второй `upsert_rows`, второй `collect-instagram` в Run Log;
- τ кривой берётся по длительности **всего** шага, а рисуется внутри одной единицы работы — полоска ползёт в `total` раз медленнее реальной фазы, то есть шаг снова «выглядит замёрзшим»;
- `hx-disabled-elt="find button"` в htmx 1.9 разворачивается в `querySelector` (ОДИН элемент) — гаснет только «Одобрить», двойной клик по «Отклонить» даёт две записи решения.

Остальное: телеметрия может уронить сбор после записи строк, счётчик разметки врёт при частичном отказе Sheets (нарушение правила №2 CLAUDE.md), старт дашборда ждёт лежачий Sheets, `communicate()` читает pipe одновременно с потоком-читателем, трейсбек на каждую строку stdout, шесть чтений raw-вкладок вместо четырёх, мёртвое состояние `is_sim`/`eta_median`, интервал 200 мс на страницах без ленты, дубли CSS-правил и инструментация прогресса без потребителя.

Решения оператора, зафиксированные до планирования:
- **находка 9** — убрать мёртвый код (шаг «Статистика» остаётся живым бэклогом «N ждут данных», producing-шагом его НЕ делаем);
- **объём** — все 14 находок, в три этапа.

## File Structure

- Modify: `src/cf/dashboard/runner.py` — задачи 1, 2, 5, 7, 8, 9, 10 (отчёты этапов, проба сигнатуры, честный счёт разметки, неблокирующий старт, разбор потока stdout, переиспользование чтений)
- Modify: `src/cf/dashboard/progress.py` — задачи 3, 5, 11, 14 (τ внутри единицы, подпись неполного счёта, снятие мёртвых полей, мёртвые планы фаз)
- Modify: `src/cf/collect/progress.py` — задача 4 (битый счётчик не роняет сбор)
- Modify: `src/cf/collect/performance.py` — задача 14 (снять инструментацию без потребителя)
- Modify: `src/cf/dashboard/templates/partials/briefs_review.html` — задача 6
- Modify: `src/cf/dashboard/static/timeline.js` — задача 12
- Modify: `src/cf/dashboard/static/style.css` — задача 13
- Test: `tests/test_dashboard_runner.py` (1, 2, 8, 9), `tests/test_dashboard_run_progress.py` (5, 7, 10), `tests/test_dashboard_progress.py` (3, 5, 11), `tests/test_collect_progress.py` (4), `tests/test_dashboard_routes.py` (6)

Общие соглашения проекта, которых держимся: комментарий объясняет **почему**, а не что; телеметрия и логи — best-effort и никогда не роняют прогон; «не знаем» выражается `None`, а не нулём (правило №2 CLAUDE.md); тесты используют `FakeSheets` и инжектированный `run_command`, реальных сетевых вызовов нет.

Команда тестов везде: `.venv/bin/python -m pytest <файл>::<тест> -v`.

---

## Этап A — поведенческие баги (задачи 1–9)

### Task 1: причина упавшего сбора не теряется за маркерами прогресса

**Files:**
- Modify: `src/cf/dashboard/runner.py:1069-1071`
- Test: `tests/test_dashboard_runner.py`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_dashboard_runner.py` рядом с `test_cli_report_keeps_only_stdout_tail` (~строка 94):

```python
def test_cli_report_falls_back_to_stderr_when_stdout_is_only_progress():
    # `cf collect` печатает маркеры прогресса в stdout, а причину падения — в
    # stderr (cli.py: print(f"error: {exc}", file=sys.stderr)). После отсева
    # маркеров stdout пуст, и отчёт этапа оставался пустым: «raw упало» без причины.
    runner, _, _, _ = make_runner()
    runner.run_command = lambda argv: (
        1,
        "CF_PROGRESS phase=prepare total=8\nCF_PROGRESS phase=fetch done=1 total=8\n",
        "error: Sheets 503",
    )
    assert runner.run_sync("stats") is False
    assert runner.reports["stats"][0]["text"] == "error: Sheets 503"
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_runner.py::test_cli_report_falls_back_to_stderr_when_stdout_is_only_progress -v`
Expected: FAIL — `assert '' == 'error: Sheets 503'`

- [ ] **Step 3: Правка `_run_cli_sync`**

Заменить в `src/cf/dashboard/runner.py` (строки 1069–1070):

```python
            lines = [l for l in ((out or err) or "").strip().splitlines()
                     if not collect_progress.is_marker(l)]
```

на:

```python
            # Маркеры прогресса — телеметрия, оператору в отчёт они не нужны. Но
            # если после отсева stdout пуст, причина падения живёт в stderr
            # (`cf collect` печатает «error: …» туда), иначе отчёт этапа выходит
            # пустым и «raw упало» приходит без причины.
            lines = [l for l in (out or "").strip().splitlines()
                     if not collect_progress.is_marker(l)]
            if not lines:
                lines = (err or "").strip().splitlines()
```

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_runner.py tests/test_dashboard_run_progress.py -q`
Expected: PASS (в частности `test_progress_markers_do_not_leak_into_stage_reports` — маркеры по-прежнему не утекают)

- [ ] **Step 5: Коммит**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "fix(dashboard): причина упавшего сбора не теряется за маркерами прогресса"
```

---

### Task 2: сигнатуру run_command пробуем по подписи, а не побочным вызовом

**Files:**
- Modify: `src/cf/dashboard/runner.py:820-838` (+ импорт `inspect`)
- Test: `tests/test_dashboard_run_progress.py`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_dashboard_run_progress.py` после `test_old_style_run_command_without_on_line_still_works` (~строка 214):

```python
def test_typeerror_inside_command_does_not_rerun_collect(tmp_path):
    # Проба «вызвать с on_line и поймать TypeError» неотличима от TypeError,
    # прилетевшего ИЗ ТЕЛА уже выполненной команды: сбор запускался второй раз —
    # второй платный прогон Apify, второй upsert и второй collect-* в Run Log.
    calls = []

    def run_command(argv, on_line=None):
        calls.append(argv)
        raise TypeError("упало внутри команды, а не на арности")

    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, run_command, tmp_path)
    assert r.run_sync("raw") is False
    assert len(calls) == 2          # по одному разу на платформу, без повторов
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_run_progress.py::test_typeerror_inside_command_does_not_rerun_collect -v`
Expected: FAIL — `assert 4 == 2`

- [ ] **Step 3: Добавить пробу по сигнатуре**

В шапку `src/cf/dashboard/runner.py` добавить импорт `inspect` (в алфавитном порядке рядом с `import json`).

Перед `class StageRunner` (рядом с `_int`, ~строка 338) добавить:

```python
def _accepts_on_line(fn):
    """Умеет ли ``fn`` принимать колбэк ``on_line``.

    Смотрим подпись, а НЕ «вызвать и поймать TypeError»: вызов команды сбора —
    платный побочный эффект (прогон Apify + запись в Sheets), а TypeError из тела
    уже выполненной команды неотличим от «не та арность». Прошлая версия в этом
    случае запускала сбор второй раз.

    Подпись недоступна (C-функция, Mock) → считаем, что колбэк принимается:
    лишний аргумент виден сразу и ничего не выполняет дважды.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    try:
        signature.bind_partial(["argv"], on_line=lambda line: None)
    except TypeError:
        return False
    return True
```

Заменить тело `_run3` (строки 828–838) на:

```python
        if on_line is not None and _accepts_on_line(self.run_command):
            res = self.run_command(argv, on_line=on_line)
        else:
            res = self.run_command(argv)
        if res is None:
            # инжектированный раннер вернул None: раньше это молча вызывало
            # команду второй раз — теперь падаем громко и один раз
            raise RuntimeError("run_command не вернул (code, stdout[, stderr])")
        if len(res) == 2:
            return res[0], res[1], ""
        return res
```

и в докстринге `_run3` заменить последнее предложение на: «обе формы поддерживаем: колбэк передаём только тому, кто его принимает (`_accepts_on_line`), иначе прогресс просто остаётся дискретным».

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_run_progress.py tests/test_dashboard_runner.py tests/test_runner_fanout.py -q`
Expected: PASS (в частности `test_old_style_run_command_without_on_line_still_works` — одноаргументные фейки живы)

- [ ] **Step 5: Коммит**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_run_progress.py
git commit -m "fix(dashboard): проба сигнатуры run_command не запускает сбор дважды"
```

---

### Task 3: τ кривой считается внутри одной единицы работы

**Files:**
- Modify: `src/cf/dashboard/progress.py:399-421` (`inner_fraction`), `478-495` (`phase_tau`)
- Modify: `src/cf/dashboard/runner.py:1443-1444, 1460` (метка начала единицы у шага «Анализ тем»)
- Test: `tests/test_dashboard_progress.py`

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_dashboard_progress.py` после `test_phase_tau_scales_with_phase_weight` (~строка 258):

```python
def test_phase_tau_is_scoped_to_one_unit_of_work():
    # eta_median измерен по ВСЕМУ шагу (обе платформы сбора), а кривая живёт
    # внутри ОДНОЙ единицы: без деления на их число полоска ползёт в `total` раз
    # медленнее фазы — тот самый «шаг выглядит замёрзшим», ради которого делали v2.
    state = {"status": "running", "total": 2, "phase_plan": "tiktok",
             "phase": "gate", "eta_median": 1080}
    assert phase_tau("collect", state) == 81.0                      # 1080·0.15/2
    assert phase_tau("collect", dict(state, total=None)) == 162.0    # единица одна
    # фаза с якорями: отрезок кривой — один батч ОДНОЙ платформы
    assert phase_tau("collect", dict(state, phase="fetch", phase_done=1,
                                     phase_total=8)) == 40.5        # 1080·0.6/2/8


def test_curve_inside_unit_advances_at_the_pace_of_the_phase():
    # за длительность самой фазы (81 с) кривая обязана пройти больше половины
    # своего сегмента; со старой τ=162 она проходила меньше
    state = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
             "phase": "gate", "eta_median": 1080}
    assert inner_fraction("collect", state, 81) > 0.75 + 0.15 * 0.5
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py -k "scoped_to_one_unit or pace_of_the_phase" -v`
Expected: FAIL — `assert 162.0 == 81.0`

- [ ] **Step 3: Нормировать eta на единицу работы**

В `src/cf/dashboard/progress.py`, в `inner_fraction` заменить строку `eta = state.get("eta_median") or DEFAULT_ETA_SEC` на:

```python
    # eta_median измерен по ВСЕМУ шагу (обе платформы сбора, все ниши анализа), а
    # кривая рисуется внутри ОДНОЙ единицы работы. Без нормировки на их число τ
    # завышена в `total` раз и полоска почти стоит.
    eta = (state.get("eta_median") or DEFAULT_ETA_SEC) * unit_span(state)
```

В `phase_tau` заменить `eta = state.get("eta_median") or DEFAULT_ETA_SEC` на:

```python
    # та же нормировка, что в inner_fraction: сервер и клиент обязаны считать
    # одну и ту же кривую, иначе тикер расходится с якорями
    eta = (state.get("eta_median") or DEFAULT_ETA_SEC) * unit_span(state)
```

- [ ] **Step 4: Дать шагу «Анализ тем» границы единиц**

Кривая внутри единицы отсчитывается от `phase_started_at`; у «Анализа тем» он не проставлялся, поэтому elapsed считался от старта всего шага и сегмент мгновенно упирался в кэп. В `src/cf/dashboard/runner.py` (строка 1443) заменить:

```python
            self._progress_update("analyze", status="running", started_at=self.now(),
                                  done=0, total=len(queue), count=0)
```

на:

```python
            # phase_started_at — начало ТЕКУЩЕЙ единицы (ниши): кривая внутри
            # сегмента считается от неё, иначе elapsed приезжает от старта всего
            # шага и сегмент сразу упирается в кэп
            self._progress_update("analyze", status="running", started_at=self.now(),
                                  done=0, total=len(queue), count=0,
                                  phase_started_at=self.now())
```

и в цикле пула (строка 1460):

```python
                        self._progress_update("analyze", done=done, count=formulas_sum,
                                              phase_started_at=self.now())
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py tests/test_dashboard_run_progress.py -q`
Expected: PASS (существующие `test_phase_tau_scales_with_phase_weight`, `test_progress_never_goes_backwards_across_phases_and_units`, `test_ticker_curve_reaches_next_anchor_without_overshooting` остаются зелёными: в их состояниях либо нет `total`, либо значения нормируются согласованно)

- [ ] **Step 6: Коммит**

```bash
git add src/cf/dashboard/progress.py src/cf/dashboard/runner.py tests/test_dashboard_progress.py
git commit -m "fix(dashboard): tau кривой считается внутри одной единицы работы"
```

---

### Task 4: битый счётчик в маркере не роняет сбор

**Files:**
- Modify: `src/cf/collect/progress.py:33-43`
- Test: `tests/test_collect_progress.py`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_collect_progress.py` после `test_emit_survives_broken_log`:

```python
def test_emit_survives_broken_counter():
    # rows/new приезжают из sheets.upsert_rows — уже ПОСЛЕ записи строк. Битое
    # число не должно ронять collect: печатаем фазу, счётчик отбрасываем —
    # ровно так же, как это делает parse.
    printed = []
    marker.emit(printed.append, "write", rows="много", new=None)
    assert printed == ["CF_PROGRESS phase=write"]
    marker.emit(printed.append, "fetch", done=object(), total=8)
    assert printed[-1] == "CF_PROGRESS phase=fetch total=8"
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_collect_progress.py::test_emit_survives_broken_counter -v`
Expected: FAIL — `ValueError: invalid literal for int() with base 10: 'много'`

- [ ] **Step 3: Правка `emit`**

В `src/cf/collect/progress.py` заменить строки 35–43 на:

```python
    parts = [PREFIX, f"phase={phase}"]
    for key, value in (("done", done), ("total", total),
                       ("rows", rows), ("new", new)):
        if value is None:
            continue
        try:
            parts.append(f"{key}={int(value)}")
        except (TypeError, ValueError):
            continue      # битое число — фаза важнее счётчика (симметрично parse)
    try:
        log(" ".join(parts))
    except Exception:      # noqa: BLE001 — печать прогресса не ломает сбор
        pass
```

И в докстринге `emit` дописать абзац:

```
    Числа приводятся по одному и в try: ``rows``/``new`` приезжают из
    ``upsert_rows`` уже ПОСЛЕ записи строк, и падение на форматировании оборвало
    бы сбор между записью и сводкой прогона.
```

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_collect_progress.py tests/test_collect_tiktok.py tests/test_collect_instagram.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add src/cf/collect/progress.py tests/test_collect_progress.py
git commit -m "fix(collect): битый счётчик в маркере прогресса не роняет сбор"
```

---

### Task 5: счёт разметки не выдумывает остаток при частичном отказе Sheets

**Files:**
- Modify: `src/cf/dashboard/runner.py:504-511` (`_empty_progress`), `621-627` (`_progress_begin`), `775-777` (`_load_progress`), `1419-1438` (цикл классификации)
- Modify: `src/cf/dashboard/progress.py:237-246` (`_count_label`)
- Test: `tests/test_dashboard_progress.py`, `tests/test_dashboard_run_progress.py`

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_dashboard_progress.py` после `test_build_timeline_producing_and_downstream_split`:

```python
def test_classify_label_never_claims_a_remainder_it_could_not_count():
    # Правило №2 CLAUDE.md: одна raw-вкладка прочиталась, вторая нет. Назвать
    # сумму «без темы» нельзя — «107 размечено · 0 без темы» читается как
    # «работа кончилась», пока в непрочитанной вкладке лежат десятки строк.
    rows = build_timeline({"classify": {"status": "done", "produced": 107,
                                        "left": None, "left_unknown": True}},
                          {}, now=None)
    label = next(r for r in rows if r["key"] == "classify")["count_label"]
    assert label == "107 роликов размечено · остаток неизвестен"
    # ничего не разметили и сосчитать не смогли — это НЕ «размечать было нечего»
    rows = build_timeline({"classify": {"status": "done", "produced": 0,
                                        "left": None, "left_unknown": True}},
                          {}, now=None)
    assert next(r for r in rows if r["key"] == "classify")["count_label"] \
        == "не удалось посчитать"
```

В `tests/test_dashboard_run_progress.py` (рядом с фан-аут-тестами):

```python
def test_classify_marks_count_incomplete_when_a_raw_tab_is_unreadable(tmp_path):
    # Sheets отдаёт одну вкладку и падает на второй: счётчик обязан признаться,
    # а звено — уйти в warn с внятной причиной, а не отрапортовать успех.
    class HalfBroken(FakeSheets):
        def read_rows(self, tab):
            if tab == "raw_instagram":
                raise ConnectionError("sheets 503")
            return super().read_rows(tab)

    sheets = HalfBroken({"run_log": [], "raw_instagram": [],
                         "raw_tiktok": _raw_rows("D", 20, 2000)})
    r = _runner(sheets, _dispatch({"D": (0, _niche_out("D", formulas=1))}), tmp_path)
    assert r.run_sync("factory") is True
    st = r.run_progress["classify"]
    assert st["left"] is None and st["left_unknown"] is True
    assert _timeline_row(r, "classify")["count_label"] == "не удалось посчитать"
    assert "счёт разметки неполный" in r.state["factory"]["detail"]
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py::test_classify_label_never_claims_a_remainder_it_could_not_count tests/test_dashboard_run_progress.py::test_classify_marks_count_incomplete_when_a_raw_tab_is_unreadable -v`
Expected: FAIL — сейчас label «размечать было нечего», `left == 0`, поля `left_unknown` нет

- [ ] **Step 3: Новое поле состояния**

В `src/cf/dashboard/runner.py`, `_empty_progress` — в словарь добавить `"left_unknown": False,` рядом с `"left": None`. В `_progress_begin` в `st.update(...)` добавить `left_unknown=False,` рядом с `left=None,`. В `_load_progress` после блока восстановления числовых полей (строка 777) добавить:

```python
            # признак «остаток сосчитать не удалось» переживает рестарт вместе с
            # числами: иначе прошлый прогон после перезапуска начинает врать
            st["left_unknown"] = bool(saved.get("left_unknown"))
```

- [ ] **Step 4: Честный счёт в цикле классификации**

В `run_fanout_sync` заменить строки 1421–1438 на:

```python
            classified = left = 0
            counted = False        # ни одной вкладки не сосчитали → числа не выдумываем
            uncounted = []         # вкладки, где счёт не удался: остаток неизвестен
            for i, (tab, label) in enumerate(RAW_TABS, start=1):
                # «сколько разметили» считаем сами: до и после — строки без темы
                before = self._unclassified_count(tab)
                self._fanout_claude(stage, f"/cf-classify-niche {tab}",
                                    f"классификация {label}", problems,
                                    expect_agent="niche-classifier")
                after = self._unclassified_count(tab)
                if before is not None and after is not None:
                    classified += max(before - after, 0)
                    left += after
                    counted = True
                else:
                    # правило №2: остаток по ЧАСТИ вкладок — это не остаток. Раньше
                    # хватало одной сосчитанной вкладки, чтобы лента назвала сумму,
                    # пока в непрочитанной лежали строки без темы.
                    uncounted.append(label)
                self._progress_update(
                    "classify", done=i, count=i,
                    produced=classified if counted else None,
                    left=(left if counted and not uncounted else None),
                    left_unknown=bool(uncounted))
            if uncounted:
                problems.append("счёт разметки неполный: не прочитаны вкладки "
                                + ", ".join(uncounted))
            self._progress_update("classify", status="done")
```

- [ ] **Step 5: Подпись счётчика**

В `src/cf/dashboard/progress.py`, `_count_label`, заменить ветку `if key == "classify":` (строки 237–246) на:

```python
    if key == "classify":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        # часть raw-вкладок не прочиталась — сумму «без темы» называть нельзя
        # (правило №2 CLAUDE.md): молчание про остаток читается как «не осталось»
        unknown = bool(state.get("left_unknown"))
        if produced:
            base = (f"{int(produced)} "
                    f"{plural_ru(produced, 'ролик размечен', 'ролика размечено', 'роликов размечено')}")
            if unknown:
                return f"{base} · остаток неизвестен"
            return f"{base} · {int(left)} без темы" if left else base
        if left:
            return f"{int(left)} без темы — не размечено"
        return "не удалось посчитать" if unknown else "размечать было нечего"
```

- [ ] **Step 6: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py tests/test_dashboard_run_progress.py tests/test_runner_fanout.py -q`
Expected: PASS

- [ ] **Step 7: Коммит**

```bash
git add src/cf/dashboard/runner.py src/cf/dashboard/progress.py \
        tests/test_dashboard_progress.py tests/test_dashboard_run_progress.py
git commit -m "fix(dashboard): счёт разметки не выдумывает остаток при частичном отказе Sheets"
```

---

### Task 6: гаснут обе кнопки решения по сценарию

**Files:**
- Modify: `src/cf/dashboard/templates/partials/briefs_review.html:98-103, 118`
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_dashboard_routes.py` после `test_review_reject_form_renders_reason_dropdown` (~строка 234):

```python
def test_review_form_disables_both_decision_buttons():
    # htmx 1.9 разворачивает «find X» в querySelector — ОДИН элемент, поэтому
    # «find button» гасил только «Одобрить». Двойной клик по «Отклонить» на
    # медленном Sheets давал два POST → две записи решения и две строки Run Log.
    client, _ = make_client(BRIEF_TABLES)
    html = client.get("/briefs?status=pending").text
    assert 'hx-disabled-elt="#review-actions button"' in html
    assert 'id="review-actions"' in html
    assert "find button" not in html
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_routes.py::test_review_form_disables_both_decision_buttons -v`
Expected: FAIL — в разметке `hx-disabled-elt="find button"`

- [ ] **Step 3: Правка шаблона**

Комментарий (строки 98–99) и атрибут (строка 103):

```html
    {# hx-disabled-elt: гаснут ОБЕ кнопки на время запроса. Решение пишется в
       Sheets и в Run Log — второй клик давал вторую запись. Селектор без
       префикса «find»: htmx 1.9 разворачивает «find X» в querySelector (ОДИН
       элемент) и гасил только «Одобрить». #}
    <form method="post" action="/briefs/{{ selected.brief_id }}/review"
          hx-post="/briefs/{{ selected.brief_id }}/review"
          hx-target="#briefs-review" hx-swap="outerHTML"
          hx-disabled-elt="#review-actions button">
```

Строка 118 — дать контейнеру кнопок id (в карточке он единственный, ветка pending исключает остальные формы):

```html
      <div class="actions" id="review-actions">
```

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_routes.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add src/cf/dashboard/templates/partials/briefs_review.html tests/test_dashboard_routes.py
git commit -m "fix(dashboard): на время записи решения гаснут обе кнопки ревью"
```

---

### Task 7: старт дашборда не ждёт Sheets при закрытии прерванного прогона

**Files:**
- Modify: `src/cf/dashboard/runner.py:381-391` (`__init__`), `789-818` (`_close_interrupted_runs`)
- Test: `tests/test_dashboard_run_progress.py:216-252`

- [ ] **Step 1: Написать падающий тест**

В `tests/test_dashboard_run_progress.py` (нужны `import threading`, `import time` в шапке файла):

```python
def test_startup_does_not_block_on_sheets_when_closing_interrupted_run(tmp_path):
    # Оператор рестартует сервис именно чтобы разгрести застрявший прогон.
    # Запись следа в Run Log шла синхронно из __init__ — лежачий Sheets
    # задерживал подъём дашборда (uvicorn ещё не слушает, страницы не отдаются).
    gate = threading.Event()

    class Hanging(FakeSheets):
        def append_row(self, tab, row):
            gate.wait(5)                       # «Sheets висит»
            return super().append_row(tab, row)

    ppath = tmp_path / "progress.json"
    ppath.write_text(json.dumps({"collect": {"status": "running"}}), encoding="utf-8")
    started = time.monotonic()
    r = _runner(Hanging({"run_log": []}), lambda a: (0, ""), tmp_path,
                progress_path=ppath)
    assert time.monotonic() - started < 1.0    # конструктор не ждал Sheets
    assert r.run_progress["collect"]["status"] == "interrupted"
    gate.set()
    r._startup_log_thread.join(timeout=5)
    assert len(r.sheets.tables["run_log"]) == 1
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_run_progress.py::test_startup_does_not_block_on_sheets_when_closing_interrupted_run -v`
Expected: FAIL — конструктор висит ~5 с, `assert 5.0 < 1.0`

- [ ] **Step 3: Вынести запись в фон**

В `__init__` перед блоком `if self.progress_path is not None:` (строка 389) добавить:

```python
        # поток закрытия следа прерванного прогона: ссылку держим, чтобы тесты
        # (и будущий graceful shutdown) могли дождаться записи, а не угадывать
        self._startup_log_thread = None
```

Заменить `_close_interrupted_runs` (строки 789–818) на:

```python
    def _close_interrupted_runs(self, steps):
        """Закрыть след прерванного прогона: нормализация файла + запись в Run Log.

        Процесс умер посреди прогона (рестарт сервиса), поэтому закрывающая строка
        `dashboard-<звено>` в CF Run Log не появилась — дыра в железном правиле №6.

        Порядок важен. Сперва СИНХРОННО сохраняем нормализованный прогресс (это
        локальный файл, он же гарантия от дублей строк при повторных рестартах), и
        только потом пишем в Sheets — в ФОНЕ. Синхронная запись жила в __init__, и
        лежачий Sheets задерживал подъём дашборда именно тогда, когда оператор
        рестартует сервис, чтобы разгрести застрявший прогон.

        Всё best-effort: недоступный Sheets не должен мешать дашборду подняться;
        смерть процесса в первые миллисекунды может стоить строки в Run Log —
        это дешевле, чем неотвечающий дашборд.
        """
        stages = {}
        for step in steps:
            meta = next((s for s in PIPELINE_STEPS if s["key"] == step), None)
            stage = (meta or {}).get("stage")
            if stage:
                stages.setdefault(stage, []).append(
                    (meta or {}).get("label") or step)
        try:
            with self._progress_lock:
                self._save_progress_locked()
        except Exception:
            logger.warning("нормализованный прогресс не сохранён", exc_info=True)
        self._startup_log_thread = threading.Thread(
            target=self._log_interrupted_stages, args=(stages,),
            name="cf-startup-runlog", daemon=True)
        self._startup_log_thread.start()

    def _log_interrupted_stages(self, stages):
        """Строки «прогон прерван» в CF Run Log (фоновый поток старта)."""
        for stage, labels in stages.items():
            try:
                log_run(self.sheets, agent=f"dashboard-{stage}", status="failed",
                        input_summary="прогон прерван: сервис перезапущен "
                                      f"(незакрытые шаги: {', '.join(labels)})",
                        errors=["прогон прерван рестартом сервиса"],
                        trigger_type="dashboard")
            except Exception:
                logger.warning("след прерванного прогона звена «%s» не записан",
                               stage, exc_info=True)
```

- [ ] **Step 4: Дождаться потока в существующих тестах**

В `tests/test_dashboard_run_progress.py::test_interrupted_run_gets_closing_runlog_row_once` после создания `r` (строка 225) вставить:

```python
    r._startup_log_thread.join(timeout=5)      # запись в Run Log идёт в фоне
```

В `test_interrupted_runlog_failure_does_not_block_startup` после создания `r` — то же самое (`assert` про статус остаётся как есть).

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_run_progress.py tests/test_dashboard_routes.py -q`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_run_progress.py
git commit -m "fix(dashboard): старт не ждёт Sheets при закрытии прерванного прогона"
```

---

### Task 8: pipe подпроцесса читает один читатель

**Files:**
- Modify: `src/cf/dashboard/runner.py:191-248` (`_stream_process`), `251-299` (`_default_run`)
- Test: `tests/test_dashboard_runner.py:928-942` (правка) + новый тест

- [ ] **Step 1: Написать падающий тест**

В `tests/test_dashboard_runner.py` в секцию потокового чтения (~строка 901):

```python
def test_default_run_kills_and_joins_reader_on_timeout(monkeypatch):
    # На таймауте main-поток звал p.communicate(), пока поток-читатель ещё
    # итерировал тот же stdout: хвост вывода делился между ними произвольно, а
    # читатель получал ValueError на закрытом файле (его глотал blanket except).
    monkeypatch.setattr(runner_mod, "SUBPROCESS_TIMEOUT", 0.5)
    with pytest.raises(RuntimeError, match="превысил таймаут"):
        runner_mod._default_run([sys.executable, "-c", "import time; time.sleep(30)"],
                                on_line=lambda line: None)
    assert not [t for t in threading.enumerate() if t.name == "cf-stdout-reader"]
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_runner.py::test_default_run_kills_and_joins_reader_on_timeout -v`
Expected: FAIL — `AttributeError: module 'cf.dashboard.runner' has no attribute 'SUBPROCESS_TIMEOUT'`

- [ ] **Step 3: Таймаут в константу**

Рядом с другими константами модуля (после `CLI_REPORT_TAIL`) добавить:

```python
# Дедлайн подпроцесса звена (сбор/claude). Константа, а не литерал: путь
# «убили по таймауту» иначе не проверить тестом за разумное время.
SUBPROCESS_TIMEOUT = 1800
```

- [ ] **Step 4: Уборку потока перенести в `_stream_process`**

Заменить `_stream_process` (строки 191–248) на версию с обязательным `kill` и уборкой на месте:

```python
def _stream_process(proc, on_line, timeout, kill):
    """Читает stdout подпроцесса построчно, отдавая строки в ``on_line``.

    Возвращает (stdout, stderr) целиком — контракт _default_run не меняется, но
    строки становятся доступны сразу. stderr читается фоновым потоком (иначе
    заполненный pipe stderr встал бы намертво). Дедлайн проверяется по каждой
    строке и после закрытия stdout.

    На таймауте убираем за собой ЗДЕСЬ: ``kill`` (у вызывающего это kill всей
    группы процессов) → ``wait`` → join читателей → ``_StreamTimeout``. Иначе
    вызывающий звал бы ``communicate()`` на том же pipe, который ещё читает наш
    поток: хвост stdout делился между двумя читателями произвольно, а поток
    получал ValueError на закрытом файле.
    Сбой колбэка прогресса не отменяет прогон — телеметрия не важнее данных.
    """
    deadline = time.monotonic() + timeout
    err_chunks = []
    lines = queue.Queue()

    def drain_stderr():
        try:
            err_chunks.append(proc.stderr.read() or "")
        except Exception:      # noqa: BLE001 — pipe мог закрыться при kill
            pass

    def read_stdout():
        # Читаем в потоке, а не в основном цикле: подпроцесс, который завис МОЛЧА
        # (ни строчки в stdout), иначе держал бы нас в блокирующем чтении до EOF —
        # и дедлайн никогда бы не сработал.
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:      # noqa: BLE001 — pipe закрыт при kill
            pass
        finally:
            lines.put(None)    # маркер конца потока

    # имена нужны в journal/py-spy боевого сервиса и в тестах уборки
    err_thread = threading.Thread(target=drain_stderr, daemon=True,
                                  name="cf-stderr-reader")
    out_thread = threading.Thread(target=read_stdout, daemon=True,
                                  name="cf-stdout-reader")
    err_thread.start()
    out_thread.start()

    def give_up():
        """Убить группу и дождаться читателей, прежде чем отдать управление."""
        kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        raise _StreamTimeout()

    out_lines = []
    failures = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            give_up()
        try:
            line = lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue           # тишина в stdout — просто проверяем дедлайн снова
        if line is None:
            break
        out_lines.append(line)
        try:
            on_line(line.rstrip("\n"))
        except Exception:      # noqa: BLE001 — прогресс не ломает звено
            failures += 1
            if failures == 1:
                logger.warning("колбэк прогресса упал на строке вывода",
                               exc_info=True)
    if failures > 1:
        # трейсбек на каждую строку топит настоящую причину в журнале юнита
        logger.warning("колбэк прогресса падал ещё %d раз — прогресс шага неполный",
                       failures - 1)
    try:
        proc.wait(timeout=max(deadline - time.monotonic(), 1))
    except subprocess.TimeoutExpired:
        give_up()
    err_thread.join(timeout=5)
    return "".join(out_lines), "".join(err_chunks)
```

(счётчик `failures` — это задача 9, её тест ниже; логика вписана сюда одним куском, чтобы не переписывать функцию дважды.)

- [ ] **Step 5: Упростить `_default_run`**

Заменить блок `try/except` (строки 275–296) на:

```python
    try:
        if on_line is None:
            stdout, stderr = p.communicate(timeout=SUBPROCESS_TIMEOUT)
        else:
            # Живой прогресс (спека §7): stdout читаем построчно по мере вывода, а
            # не целиком в конце — иначе маркеры `CF_PROGRESS` приходят, когда сбор
            # уже закончился, и полоска прыгает 0 → 50%. stderr тянем отдельным
            # потоком: без этого полный pipe stderr заблокировал бы подпроцесс.
            stdout, stderr = _stream_process(p, on_line, timeout=SUBPROCESS_TIMEOUT,
                                             kill=_kill_group)
    except subprocess.TimeoutExpired:
        _kill_group()
        p.communicate()
        raise RuntimeError(f"{argv[0]} превысил таймаут {SUBPROCESS_TIMEOUT:.0f}с — "
                           f"группа процессов остановлена")
    except _StreamTimeout:
        # группу уже убил и читателей дождался _stream_process — трогать pipe
        # второй раз нельзя
        raise RuntimeError(f"{argv[0]} превысил таймаут {SUBPROCESS_TIMEOUT:.0f}с — "
                           f"группа процессов остановлена")
```

- [ ] **Step 6: Поправить существующий тест дедлайна**

В `tests/test_dashboard_runner.py::test_stream_process_hits_deadline_on_silent_hang` передать kill и упростить уборку:

```python
    try:
        with pytest.raises(_StreamTimeout):
            _stream_process(p, lambda line: None, timeout=0.5, kill=p.kill)
    finally:
        p.kill()
        p.wait()
```

- [ ] **Step 7: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_runner.py -q -k "stream or default_run"`
Expected: PASS

- [ ] **Step 8: Коммит**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_runner.py
git commit -m "fix(dashboard): на таймауте stdout подпроцесса читает один читатель"
```

---

### Task 9: трейсбек упавшего колбэка прогресса — один на команду

**Files:**
- Modify: `src/cf/dashboard/runner.py` (уже сделано в задаче 8, шаг 4)
- Test: `tests/test_dashboard_runner.py`

- [ ] **Step 1: Написать тест**

В `tests/test_dashboard_runner.py` (нужны `import io`, `import logging` — logging уже импортирован):

```python
class _FakeProc:
    """Подпроцесс, который просто отдаёт готовый stdout (для _stream_process)."""

    def __init__(self, text):
        self.stdout = io.StringIO(text)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


def test_progress_callback_failure_is_logged_once_not_per_line(caplog):
    # Сбор печатает маркер на каждый батч плюс свои строки. Если колбэк падает
    # стабильно (неписучий agent-runtime/reports, зависший лок), трейсбек на
    # КАЖДУЮ строку топил настоящую причину в журнале юнита.
    proc = _FakeProc("".join(f"строка {i}\n" for i in range(50)))

    def boom(_line):
        raise RuntimeError("лок прогресса занят")

    with caplog.at_level(logging.WARNING):
        out, err = runner_mod._stream_process(proc, boom, timeout=5,
                                              kill=lambda: None)
    assert out.count("строка") == 50                     # строки не потеряны
    assert len([r for r in caplog.records if r.exc_info]) == 1   # трейсбек один
    assert any("ещё 49 раз" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Запустить тест**

Run: `.venv/bin/python -m pytest tests/test_dashboard_runner.py::test_progress_callback_failure_is_logged_once_not_per_line -v`
Expected: PASS (логика внесена в задаче 8; если тест падает — расхождение в тексте сообщения, исправить сообщение в `_stream_process`)

- [ ] **Step 3: Коммит**

```bash
git add tests/test_dashboard_runner.py
git commit -m "test(dashboard): падающий колбэк прогресса логируется один раз на команду"
```

---

## Этап B — стоимость чтений (задача 10)

### Task 10: raw-вкладки читаются четыре раза вместо шести

**Files:**
- Modify: `src/cf/dashboard/runner.py:1107-1159` (`_eligible_niches`), `1333-1346` (`_unclassified_count`), цикл классификации из задачи 5
- Test: `tests/test_dashboard_run_progress.py`

- [ ] **Step 1: Написать падающий тест**

```python
def test_fanout_reads_each_raw_tab_twice_not_three_times(tmp_path):
    # На прогон было 6 полных чтений самых больших вкладок системы: по два на
    # счётчик разметки (до/после) и ещё по одному на очередь ниш. Снимок «после»
    # и есть вход очереди: между ними raw никто не пишет — мьютекс raw↔factory
    # не пускает сбор во время фан-аута.
    reads = []

    class Counting(FakeSheets):
        def read_rows(self, tab):
            reads.append(tab)
            return super().read_rows(tab)

    sheets = Counting({"run_log": [], "raw_instagram": [],
                       "raw_tiktok": _raw_rows("D", 20, 2000)})
    r = _runner(sheets, _dispatch({"D": (0, _niche_out("D", formulas=1))}), tmp_path)
    assert r.run_sync("factory") is True
    assert reads.count("raw_tiktok") == 2
    assert reads.count("raw_instagram") == 2
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_run_progress.py::test_fanout_reads_each_raw_tab_twice_not_three_times -v`
Expected: FAIL — `assert 3 == 2`

- [ ] **Step 3: Вынести чтение и подсчёт**

Рядом с `_int` (модульный уровень `src/cf/dashboard/runner.py`) добавить:

```python
def _unclassified_in(rows):
    """Сколько строк без темы (пустая `niche`) в уже прочитанных строках."""
    return sum(1 for r in rows if not str(r.get("niche", "")).strip())
```

Заменить `_unclassified_count` (строки 1333–1346) на пару методов:

```python
    def _raw_rows(self, tab):
        """Строки raw-вкладки или None при сбое чтения (best-effort, как счётчики)."""
        try:
            return self.sheets.read_rows(tab)
        except Exception:
            logger.warning("не удалось прочитать «%s»", tab, exc_info=True)
            return None

    def _unclassified_count(self, tab):
        """Сколько строк вкладки без темы, или None при сбое чтения.

        Этим числом лента считает работу «Разметки»: до шага — сколько предстоит,
        после — сколько осталось; разница и есть «размечено N роликов». Сам агент
        отчитывается прозой, а прозу в счётчик не превратишь.
        """
        rows = self._raw_rows(tab)
        return None if rows is None else _unclassified_in(rows)
```

- [ ] **Step 4: Переиспользовать снимок «после» в очереди ниш**

В цикле классификации (задача 5, шаг 4) завести снимок и передать его дальше:

```python
            after_rows = {}        # снимок вкладки ПОСЛЕ разметки — он же вход очереди
```

вместо `after = self._unclassified_count(tab)`:

```python
                rows = self._raw_rows(tab)
                after_rows[tab] = rows
                after = None if rows is None else _unclassified_in(rows)
```

и вызов очереди (строка 1440):

```python
            queue = self._eligible_niches(problems, rows_by_tab=after_rows)
```

В `_eligible_niches` сменить подпись и источник строк:

```python
    def _eligible_niches(self, problems=None, rows_by_tab=None):
        """Очередь (tab, niche): достаточно материала и есть заметный рост с прошлого анализа.

        ``rows_by_tab`` — уже прочитанные строки вкладок (снимок сразу после
        разметки). Читать их повторно нечего: мьютекс raw↔factory не пускает сбор
        во время фан-аута, а классификация следующей вкладки в эту не пишет.
        ...
        """
```

и в цикле по `RAW_TABS` заменить `try/except` чтения на:

```python
        for order, (tab, _label) in enumerate(RAW_TABS):
            rows = (rows_by_tab or {}).get(tab)
            if rows is None:      # вызов без снимка (или тогда не прочиталось) — читаем
                rows = self._raw_rows(tab)
            if rows is None:
                failed_tabs += 1
                continue
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_dashboard_run_progress.py tests/test_runner_fanout.py tests/test_dashboard_runner.py -q`
Expected: PASS (тесты `test_runner_fanout.py` зовут `_eligible_niches()` без аргументов — путь «читаем сами» сохранён)

- [ ] **Step 6: Коммит**

```bash
git add src/cf/dashboard/runner.py tests/test_dashboard_run_progress.py
git commit -m "perf(dashboard): очередь ниш переиспользует снимок raw-вкладок"
```

---

## Этап C — чистота (задачи 11–14)

### Task 11: снять мёртвое состояние `is_sim` и `eta_median` из строк ленты

**Files:**
- Modify: `src/cf/dashboard/progress.py:335-364` (`build_timeline`)
- Test: `tests/test_dashboard_progress.py:146-154, 297-309`

- [ ] **Step 1: Обновить тесты под новый контракт**

В `tests/test_dashboard_progress.py` переименовать и переписать `test_build_timeline_simulated_bar_carries_ticker_data`:

```python
def test_build_timeline_running_bar_carries_ticker_data():
    # Шаблон гейтит тикер по status == "running" (partials/stages.html) и читает
    # только frac/ceil/tau/run. Отдельного «это симуляция» в строке нет — два
    # источника правды заставляли бы диффать шаблон, чтобы понять, кто главный.
    run_progress = {"briefs": {"status": "running", "eta_median": 60,
                               "started_at": 100.0}}
    briefs = _rows_by_key(build_timeline(run_progress, {}, now=130.0))["briefs"]
    assert 0 < briefs["fraction"] <= briefs["ceiling"] <= GLOBAL_CAP
    assert briefs["tau"] > 0
    assert "is_sim" not in briefs and "eta_median" not in briefs
```

В `test_build_timeline_exposes_ticker_contract` (строка 304) заменить `assert row["is_sim"] is True` на:

```python
    assert row["status"] == "running"        # тикер включает шаблон по статусу
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py -k ticker -v`
Expected: FAIL — `assert 'is_sim' not in {...}`

- [ ] **Step 3: Убрать поля**

В `build_timeline` из producing-ветки (строки 344–346) удалить строки `eta_median=state.get("eta_median"),` и `is_sim=running,`; в downstream-ветке (строка 360) убрать `eta_median=None, is_sim=False,`. Переменная `running` остаётся — её читают `_duration_of` и `phase_label`.

- [ ] **Step 4: Проверить, что шаблоны не ссылались на поля**

Run: `grep -rn "is_sim\|eta_median" src/cf/dashboard/templates/ src/cf/dashboard/static/timeline.js`
Expected: пусто

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py tests/test_dashboard_routes.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add src/cf/dashboard/progress.py tests/test_dashboard_progress.py
git commit -m "refactor(dashboard): строка ленты не носит мёртвые is_sim и eta_median"
```

---

### Task 12: тикер работает только там, где есть что двигать

**Files:**
- Modify: `src/cf/dashboard/static/timeline.js:105-111`

- [ ] **Step 1: Правка скрипта**

Заменить хвост `timeline.js` (строки 105–111) на:

```js
  // Скрипт подключён из base.html на КАЖДОЙ странице, а лента есть только на
  // обзоре: интервал, крутящийся на /briefs или /sources, — работа впустую.
  // Включаем таймер, только когда в DOM есть бегущая полоска или живой таймер;
  // htmx-своп сам включит его обратно, когда прогон начнётся. При reduce анимации
  // нет, подписи достаточно обновлять раз в секунду.
  var TICK_MS = reduce ? 1000 : 200;
  var timer = null;

  function schedule() {
    var live = document.querySelector('.tl-fill[data-sim], .tl-elapsed[data-elapsed]');
    if (live && timer === null) {
      timer = setInterval(tick, TICK_MS);
    } else if (!live && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  // Синхронно после подмены разметки — до отрисовки кадра: свежий элемент
  // сразу получает накопленную ширину вместо серверного якоря.
  document.addEventListener('htmx:afterSwap', function () { tick(); schedule(); });

  tick();
  schedule();
})();
```

- [ ] **Step 2: Проверить в браузере**

Run: `.venv/bin/python -m pytest tests/test_dashboard_routes.py -q` (регресс шаблонов), затем поднять превью-дашборд на 8899 (см. раздел «Проверка») и в DevTools:
- на `/briefs` — `Performance` не показывает периодической активности скрипта;
- на обзоре во время прогона — полоска двигается как раньше;
- после завершения прогона активность прекращается.

- [ ] **Step 3: Коммит**

```bash
git add src/cf/dashboard/static/timeline.js
git commit -m "perf(dashboard): тикер ленты не крутится на страницах без полоски"
```

---

### Task 13: убрать дубли CSS-правил ленты

**Files:**
- Modify: `src/cf/dashboard/static/style.css:117-119, 133-134, 157`

- [ ] **Step 1: Слить правила**

Строки 133–134:

```css
.tl-done .tl-fill { transition: width .6s cubic-bezier(.2, 0, 0, 1); }
.tl-done .tl-fill { background: var(--success); }
```

заменить на одну:

```css
.tl-done .tl-fill { background: var(--success); transition: width .6s cubic-bezier(.2, 0, 0, 1); }
```

В блок `.tl-icon` (строки 117–119) дописать `cursor: help;` в конец объявлений (значок подписан через `title` — курсор об этом и говорит), а осиротевшую строку 157 `.tl-icon { cursor: help; }` удалить. Комментарий про «шёл 18:03» (строки 155–156) оставить на месте: он документирует `.tl-took` выше.

- [ ] **Step 2: Проверить**

Run: `grep -c "\.tl-done \.tl-fill" src/cf/dashboard/static/style.css`
Expected: `1`

Run: `.venv/bin/python -m pytest tests/test_dashboard_routes.py -q`
Expected: PASS (тест на `.tl-idle .tl-fill, .tl-interrupted .tl-fill` не тронут)

- [ ] **Step 3: Коммит**

```bash
git add src/cf/dashboard/static/style.css
git commit -m "style(dashboard): дубли правил полоски и значка ленты слиты"
```

---

### Task 14: снять инструментацию прогресса без потребителя

**Files:**
- Modify: `src/cf/collect/performance.py:17, 217-218, 233, 249, 253`
- Modify: `src/cf/dashboard/progress.py:51-54` (`PHASE_PLANS`)
- Test: `tests/test_dashboard_progress.py`

Решение оператора: шаг «Статистика» остаётся живым бэклогом («N ждут данных»), producing-шагом мы его не делаем — значит `_producing_steps_for_stage('stats')` навсегда `[]`, `on_line` для него `None`, и маркеры из `performance.py` никто не читает (а `_run_cli_commands` их же и отфильтровывает обратно). `snowball` вообще не входит в `commands` ни одного звена.

- [ ] **Step 1: Написать тест-замок**

В `tests/test_dashboard_progress.py` после `test_phase_plan_resolves_from_state_then_step`:

```python
def test_phase_plans_cover_only_platforms_the_dashboard_actually_runs():
    # Планы фаз существуют для единиц работы, которые ведёт звено с полоской:
    # это раw-сбор (tiktok/instagram). У «Статистики» producing-шага нет, snowball
    # звенья не запускают вовсе — план для них выглядел бы работающей проводкой.
    assert set(PHASE_PLANS) == {"tiktok", "instagram"}
```

и добавить `PHASE_PLANS` в импорт из `cf.dashboard.progress` в шапке файла.

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_dashboard_progress.py::test_phase_plans_cover_only_platforms_the_dashboard_actually_runs -v`
Expected: FAIL — в множестве ещё `snowball` и `performance`

- [ ] **Step 3: Почистить планы фаз**

В `src/cf/dashboard/progress.py` заменить строки 51–54 на:

```python
# Планы фаз по единице работы (платформе). Раннер кладёт имя плана в состояние
# шага, когда стартует конкретную команду сбора. Только те платформы, что ведёт
# шаг с полоской: у «Статистики» producing-шага нет (она живой бэклог «ждут
# данных»), snowball звенья дашборда не запускают — добавлять их сюда значит
# рисовать проводку, которой нет.
PHASE_PLANS = {"tiktok": COLLECT_PHASES, "instagram": COLLECT_IG_PHASES}
```

- [ ] **Step 4: Снять emit из performance.py**

Удалить строки 217, 218, 233, 249, 253 (`progress_marker.emit(...)`) и импорт `from cf.collect import progress as progress_marker` (строка 17). В докстринг `collect` дописать:

```
    Маркеров прогресса тут нет намеренно: звено «Статистика» — живой бэклог, а не
    шаг с полоской, поэтому раннер читает его stdout не потоково и маркеры
    отфильтровал бы обратно. Понадобится полоска — инструментация вернётся вместе
    с producing-шагом.
```

- [ ] **Step 5: Запустить тесты**

Run: `.venv/bin/python -m pytest tests/test_collect_performance.py tests/test_dashboard_progress.py tests/test_dashboard_runner.py -q`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add src/cf/collect/performance.py src/cf/dashboard/progress.py tests/test_dashboard_progress.py
git commit -m "refactor(collect): снята инструментация прогресса без потребителя"
```

---

## Проверка (end-to-end)

1. **Весь набор тестов:**
   `.venv/bin/python -m pytest -q`
   Ожидание: зелёно, новых пропусков нет.

2. **Целостность репозитория перед коммитами:** `.venv/bin/python -m cf check-commit` (pre-commit guard секретов) — уже вызывается хуком, но прогнать вручную дешевле, чем ловить отказ на коммите.

3. **Дашборд глазами, БЕЗ боевого 8787:** боевой порт трогать нельзя — ▶ там запускает настоящие прогоны (и рестарт сервиса убивает идущий). Поднять превью на 8899 с FakeSheets, как в прошлых UI-ревью, и проверить:
   - лента на обзоре: во время прогона «Сбор» полоска двигается непрерывно, фаза `gate`/`write` не выглядит замёрзшей (задача 3);
   - `/briefs`: двойной клик по «Отклонить» на pending-сценарии даёт ОДНУ запись (обе кнопки гаснут) — задача 6;
   - DevTools → Performance на `/briefs` и `/sources`: периодической активности `timeline.js` нет (задача 12).

4. **Путь «сбор упал»:** локально сломать доступ к Sheets (или подсунуть `run_command`, печатающий маркеры в stdout и ошибку в stderr) и убедиться, что в «Отчётах этапов» видна причина, а не пустой блок (задача 1).

5. **Рестарт с прерванным прогоном:** оставить в `agent-runtime/.../pipeline-progress.json` шаг в `running`, поднять дашборд — страница отдаётся сразу, строка `dashboard-<звено>` со статусом failed появляется в CF Run Log в течение секунд (задача 7).

6. **Правило №6:** по итогам работы агента — `.venv/bin/python -m cf log-run ...` (или автоматически из CLI, если правки гонялись через него).

## Что осталось за рамками

- Живая полоска для шага «Статистика» (сделать `stats` producing-шагом): решение оператора — не сейчас; тогда придётся решать, что строка показывает в idle вместо «N ждут данных». Инструментация вернётся вместе с шагом (задача 14).
- Короткий таймаут на gspread-путь: запись в Run Log на старте теперь в фоне, но лежачий Sheets по-прежнему может держать фоновый поток минутами. Отдельная работа по всему `cf.sheets`.
