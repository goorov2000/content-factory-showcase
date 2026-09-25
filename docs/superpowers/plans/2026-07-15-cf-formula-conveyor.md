# Конвейер формул: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (коммиты до 4ec66e4, 2026-07-15). Чекбоксы в ходе исполнения не велись.

**Goal:** Один клик ▶ «Контент-завод» → классификация новых строк → параллельный анализ всех ниш с материалом → черновики формул в очереди одобрения на дашборде → авто-approved брифы по утверждённым формулам (спека `docs/superpowers/specs/2026-07-15-cf-formula-conveyor-design.md`).

**Architecture:** Механика (квартили, кластеризация, статусы, авто-одобрение) — детерминированные cf-команды; смысл (формулировки, верификация evidence) — headless-агенты; оркестрация — Python-раннер дашборда с пулом воркеров, один короткий `claude -p` на нишу. Git-операции делает только раннер/кнопки дашборда, никогда — параллельные воркеры.

**Tech Stack:** Python 3 (stdlib, без новых зависимостей), FastAPI+Jinja2+htmx (существующий дашборд), pytest, gspread через существующий `cf.sheets.Sheets`.

**Конфиг (дефолты в коде, переопределяются в `cf.config.json` → `dashboard.fanout`):**
`workers=2, briefs_per_formula=2, min_rows=12, min_views=1000, growth=1.5, cap_approved_7d=5, cluster_min_size=30`.

**Правило для всех задач:** запуск тестов — `.venv/Scripts/python -m pytest <файл> -v` из корня `<local-path>`. Коммит после каждой задачи. В коде и коммитах — русский, как принято в репо.

---

### Task 1: Чистая математика анализа — `cf/analyze.py`

Метод v2 из промпта pattern-analyzer переезжает в код (урок: headless-права не дают агенту считать квартили).

**Files:**
- Create: `src/cf/analyze.py`
- Test: `tests/test_analyze.py`

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_analyze.py
from cf.analyze import analyze_rows, dedupe_rows, engagement_rate

def _row(url, views, likes=0, comments=0, shares=0, saves=0, account="a",
         transcript="есть текст", collected="2026-07-01", niche="тест"):
    return {"source_url": url, "views": views, "likes": likes, "comments": comments,
            "shares": shares, "saves": saves, "account": account,
            "transcript_text": transcript, "collected_at": collected, "niche": niche,
            "hook_text": "хук", "duration_sec": 15, "caption": "капшен"}

def test_dedupe_prefers_transcript_then_freshness():
    rows = [_row("u1", 100, transcript="", collected="2026-07-02"),
            _row("u1", 100, transcript="полный", collected="2026-07-01"),
            _row("u2", 100, collected="2026-07-01"),
            _row("u2", 100, collected="2026-07-03")]
    out = dedupe_rows(rows)
    assert len(out) == 2
    assert next(r for r in out if r["source_url"] == "u1")["transcript_text"] == "полный"
    assert next(r for r in out if r["source_url"] == "u2")["collected_at"] == "2026-07-03"

def test_er_zero_views_is_none():
    assert engagement_rate(_row("u", 0, likes=5)) is None
    assert engagement_rate(_row("u", 1000, likes=30, comments=10)) == 0.04

def test_insufficient_below_12_rows_over_threshold():
    rows = [_row(f"u{i}", 500) for i in range(20)]          # все ниже порога views
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert report["status"] == "insufficient_data"
    assert report["passed_threshold"] == 0

def test_winners_losers_and_same_account():
    # 16 строк над порогом: ER растёт с индексом, views одинаковые (медиана = views)
    rows = [_row(f"u{i}", 2000, likes=i * 10, account=f"acc{i % 4}") for i in range(16)]
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert report["status"] == "ok"
    w_urls = {w["source_url"] for w in report["winners"]}
    l_urls = {l["source_url"] for l in report["losers"]}
    assert w_urls == {"u12", "u13", "u14", "u15"}            # верхний квартиль ER
    assert l_urls == {"u0", "u1", "u2", "u3"}                # нижний квартиль ER
    # acc0 держит и winner (u12), и loser (u0) — пара для same-account сравнения
    pairs = {(p["account"]) for p in report["same_account"]}
    assert "acc0" in pairs
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают** (`ModuleNotFoundError: cf.analyze`)

- [ ] **Step 3: Реализация**

```python
# src/cf/analyze.py
"""Метод v2 pattern-analyzer в коде: дедуп, пороги, квартили, same-account.

Агент получает готовые числа и делает только смысловую часть.
"""

ENGAGE_FIELDS = ("likes", "comments", "shares", "saves")
ROW_FIELDS = ("source_url", "account", "hook_text", "caption", "transcript_text",
              "duration_sec", "collected_at", "niche")


def _num(value):
    try:
        return float(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def engagement_rate(row):
    views = _num(row.get("views"))
    if views <= 0:
        return None
    return sum(_num(row.get(f)) for f in ENGAGE_FIELDS) / views


def dedupe_rows(rows):
    """Дубли по source_url: побеждает строка с транскриптом, при равенстве — свежая."""
    best = {}
    for row in rows:
        url = str(row.get("source_url", "")).strip()
        if not url:
            continue
        cur = best.get(url)
        if cur is None:
            best[url] = row
            continue
        key = (bool(str(row.get("transcript_text", "")).strip()),
               str(row.get("collected_at", "")))
        cur_key = (bool(str(cur.get("transcript_text", "")).strip()),
                   str(cur.get("collected_at", "")))
        if key > cur_key:
            best[url] = row
    return list(best.values())


def _slim(row, er):
    out = {f: row.get(f, "") for f in ROW_FIELDS}
    out["views"] = _num(row.get("views"))
    out["er"] = round(er, 5)
    return out


def _quartile_cut(values):
    """Индекс отсечения четверти списка (минимум 1 элемент)."""
    return max(1, len(values) // 4)


def analyze_rows(rows, min_rows=12, min_views=1000):
    rows = dedupe_rows(rows)
    scored, anomalies = [], 0
    for row in rows:
        er = engagement_rate(row)
        if er is None:
            anomalies += 1
            continue
        scored.append((row, er))
    passed = [(r, er) for r, er in scored if _num(r.get("views")) >= min_views]
    report = {
        "total_rows": len(rows), "anomalies_zero_views": anomalies,
        "passed_threshold": len(passed), "min_rows": min_rows, "min_views": min_views,
    }
    if len(passed) < min_rows:
        report["status"] = "insufficient_data"
        report["missing_rows"] = min_rows - len(passed)
        return report
    views_sorted = sorted(_num(r.get("views")) for r, _ in passed)
    median_views = views_sorted[len(views_sorted) // 2]
    by_er = sorted(passed, key=lambda p: p[1])
    losers = by_er[:_quartile_cut(by_er)]
    high_views = [(r, er) for r, er in by_er if _num(r.get("views")) >= median_views]
    winners = high_views[-_quartile_cut(high_views):]
    winner_accounts = {str(r.get("account", "")) for r, _ in winners}
    same_account = []
    for row, er in losers:
        acc = str(row.get("account", ""))
        if acc and acc in winner_accounts:
            same_account.append({"account": acc, "loser": _slim(row, er)})
    report.update({
        "status": "ok", "median_views": median_views,
        "winners": [_slim(r, er) for r, er in winners],
        "losers": [_slim(r, er) for r, er in losers],
        "same_account": same_account,
    })
    return report
```

- [ ] **Step 4: Тесты зелёные**
- [ ] **Step 5: Commit** `feat(analyze): метод v2 в коде — дедуп, пороги, квартили, same-account`

---

### Task 2: CLI `cf analyze-batch`

**Files:**
- Modify: `src/cf/cli.py` (по образцу `cmd_profile`, cli.py:100-118; регистрация — рядом с parser `profile`, cli.py:481-487)
- Test: `tests/test_cli_analyze_batch.py`

- [ ] **Step 1: Падающий тест** (фейковый sheets как в существующих CLI-тестах — посмотри `tests/` на `FakeSheets`/стабы и используй тот же паттерн)

```python
# tests/test_cli_analyze_batch.py
import json
from cf.cli import main

class FakeSheets:
    def __init__(self, rows):
        self._rows = rows
        self.appended = []
    def read_rows(self, tab):
        if tab == "run_log":
            return []
        return self._rows
    def append_row(self, tab, row):
        self.appended.append((tab, row))

def test_analyze_batch_writes_report(tmp_path, monkeypatch):
    rows = [{"source_url": f"u{i}", "views": 2000, "likes": i * 10, "account": "a",
             "niche": "стритвир", "transcript_text": "т", "collected_at": "2026-07-01",
             "comments": 0, "shares": 0, "saves": 0, "hook_text": "", "caption": "",
             "duration_sec": 15} for i in range(16)]
    sheets = FakeSheets(rows)
    code = main(["analyze-batch", "raw_tiktok", "--niche", "стритвир",
                 "--out-dir", str(tmp_path)], sheets=sheets)
    assert code == 0
    report = json.loads(next(tmp_path.glob("*-analysis.json")).read_text(encoding="utf-8"))
    assert report["status"] == "ok" and len(report["winners"]) == 4
```

Если `main` в cli.py не принимает `sheets=` — используй ту же точку подмены, что в существующих CLI-тестах репо (не изобретай новую).

- [ ] **Step 2: Тест падает** (нет команды)
- [ ] **Step 3: Реализация** — `cmd_analyze_batch`: `apply_filters(sheets.read_rows(tab), args.niche, args.since)` → `analyze_rows(...)` → `report["meta"] = {...}` как в profile → `write_json_atomic(out_dir / f"{date}-{tab}-{niche_slug}-analysis.json")` → `runlog.log_run(agent="batch-analyzer", status=report["status"] if ok else "insufficient_data", input_summary=..., output_paths=[path])` → печать пути и статуса. Аргументы: `tab`, `--niche` (обязательный), `--since`, `--out-dir` (дефолт `agent-runtime/analysis`), `--min-rows` (12), `--min-views` (1000).
- [ ] **Step 4: Тесты зелёные** (весь файл + `pytest tests -k analyze`)
- [ ] **Step 5: Commit** `feat(cli): analyze-batch — механика анализа для агентов`

---

### Task 3: Кластеризация «other» — `cf/cluster.py` + CLI `cf cluster-other`

**Files:**
- Create: `src/cf/cluster.py`
- Modify: `src/cf/cli.py`
- Test: `tests/test_cluster.py`

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_cluster.py
from cf.cluster import cluster_rows, tokenize

def _row(url, caption, transcript=""):
    return {"source_url": url, "caption": caption, "transcript_text": transcript,
            "niche": "other"}

def test_tokenize_strips_urls_hashtags_stopwords():
    tokens = tokenize("Подборка для высоких мужчин #tall https://a.b и самых стильных")
    assert "подборка" in tokens and "высоких" in tokens
    assert "#tall" not in tokens and "https://a.b" not in tokens and "и" not in tokens

def test_clusters_group_similar_captions():
    tall = [_row(f"t{i}", f"образы для высоких мужчин вариант {i}") for i in range(6)]
    perfume = [_row(f"p{i}", f"мужской парфюм на лето обзор {i}") for i in range(6)]
    noise = [_row("n1", "случайное видео про кота")]
    clusters = cluster_rows(tall + perfume + noise, min_size=5)
    assert len(clusters) == 2
    sizes = sorted(c["size"] for c in clusters)
    assert sizes == [6, 6]
    top = clusters[0]
    assert top["sample_rows"] and top["top_terms"]
```

- [ ] **Step 2: Тесты падают**
- [ ] **Step 3: Реализация** — чистый Python, без sklearn:

```python
# src/cf/cluster.py
"""Кластеризация «other» по капшенам/транскриптам: TF-IDF + жадная группировка.

700 сырых строк в контекст агента не загружаются: агент получает компактные
кластеры с примерами и только называет их.
"""
import math
import re
from collections import Counter

STOPWORDS = set("""и в на не с по для как это что мы вы он она они а но или же
у о от до из за то так вот бы ли к the a of to in and for is are on with
""".split())
TOKEN_RE = re.compile(r"[a-zа-яё]{3,}", re.IGNORECASE)


def tokenize(text):
    text = re.sub(r"https?://\S+|#\S+|@\S+", " ", str(text or "").lower())
    return [t for t in TOKEN_RE.findall(text) if t not in STOPWORDS]


def _vector(row, idf):
    counts = Counter(tokenize(f'{row.get("caption", "")} {row.get("transcript_text", "")}'))
    return {t: n * idf.get(t, 0.0) for t, n in counts.items()}


def _cosine(a, b):
    common = set(a) & set(b)
    num = sum(a[t] * b[t] for t in common)
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def cluster_rows(rows, min_size=30, threshold=0.25, samples=5):
    docs = [tokenize(f'{r.get("caption", "")} {r.get("transcript_text", "")}') for r in rows]
    df = Counter(t for d in docs for t in set(d))
    idf = {t: math.log(len(rows) / n) for t, n in df.items()}
    vectors = [_vector(r, idf) for r in rows]
    assigned = [False] * len(rows)
    clusters = []
    for i, vec in enumerate(vectors):
        if assigned[i] or not vec:
            continue
        members = [i]
        assigned[i] = True
        for j in range(i + 1, len(rows)):
            if not assigned[j] and _cosine(vec, vectors[j]) >= threshold:
                members.append(j)
                assigned[j] = True
        if len(members) >= min_size:
            terms = Counter(t for m in members for t in docs[m])
            clusters.append({
                "size": len(members),
                "top_terms": [t for t, _ in terms.most_common(8)],
                "sample_rows": [{"source_url": rows[m].get("source_url", ""),
                                 "caption": str(rows[m].get("caption", ""))[:160]}
                                for m in members[:samples]],
                "source_urls": [rows[m].get("source_url", "") for m in members],
            })
    return sorted(clusters, key=lambda c: -c["size"])
```

CLI `cmd_cluster_other`: `rows = [r for r in sheets.read_rows(args.tab) if str(r.get("niche","")).strip() == "other"]` → `cluster_rows(rows, min_size=args.min_size)` → `write_json_atomic(Path(args.out_dir) / f"{date}-{tab}-clusters.json", {"tab": ..., "generated_at": now_iso(), "clusters": ...})` → печать сводки. Аргументы: `tab`, `--min-size` (30), `--out-dir` (дефолт `agent-runtime/clusters`). В тесте кластеризации порог 5 задаётся явно — дефолт 30 не трогаем.

- [ ] **Step 4: Тесты зелёные**
- [ ] **Step 5: Commit** `feat(cli): cluster-other — кластеры «other» для предложений ниш`

---

### Task 4: Статусы формул — `cf formula-status`

**Files:**
- Modify: `src/cf/cli.py` (рядом с `cmd_approve_formula`, cli.py:137-152), `schemas/formula.schema.json` (добавить опциональные `status`, `status_reason` — enum `proposed|approved|paused|rejected`; посмотри схему и добавь в properties, в required НЕ добавлять)
- Test: `tests/test_formula_status.py`

- [ ] **Step 1: Падающий тест**

```python
# tests/test_formula_status.py
import json
from pathlib import Path
from cf.cli import main

def _formula(tmp_path, name="test-formula", niche="стритвир"):
    p = tmp_path / "formulas" / niche / f"{name}.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"name": name, "niche": niche, "version": 1,
                             "status": "proposed"}), encoding="utf-8")
    return p

def test_approve_adds_to_index_and_sets_status(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    code = main(["formula-status", str(p), "approved", "--index", str(index)], sheets=None)
    assert code == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "approved"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"][0]["name"] == "test-formula"

def test_pause_removes_from_index_keeps_reason(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    main(["formula-status", str(p), "approved", "--index", str(index)], sheets=None)
    code = main(["formula-status", str(p), "paused", "--reason", "3 из 5 reject",
                 "--index", str(index)], sheets=None)
    assert code == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "paused" and data["status_reason"] == "3 из 5 reject"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []
```

- [ ] **Step 2: Тесты падают**
- [ ] **Step 3: Реализация** — `cmd_formula_status`: читает JSON формулы, ставит `status`/`status_reason`, `write_json_atomic`; для `approved` — добавляет запись в индекс (переиспользуй тело `cmd_approve_formula`: тот же entry, дедуп по path); для остальных статусов — удаляет запись с этим path из индекса, если была. Аргументы: `path`, `status` (choices из enum), `--reason` (дефолт ""), `--index` (дефолт `formulas/_approved/index.json`). `cmd_approve_formula` сведи к вызову той же функции со статусом approved (обратная совместимость).
- [ ] **Step 4: Тесты зелёные** (+ прогнать `tests` на approve-formula, если есть)
- [ ] **Step 5: Commit** `feat(cli): formula-status — proposed/approved/paused/rejected + индекс`

---

### Task 5: Авто-одобрение брифа — `cf auto-approve`

**Files:**
- Modify: `src/cf/cli.py`
- Test: `tests/test_auto_approve.py`

Условия (все сразу, иначе бриф остаётся pending с печатью причины, код выхода 0 — «не одобрил» это не ошибка):
1) в review-файле `verdict == "recommend"`; 2) `formula_id` брифа есть в approved-индексе; 3) промпт `brief-{niche}-reel` активен в prompt_versions; 4) кап: `approved`-брифов этой формулы за последние 7 дней (по `generated_at`) < `cap`.

- [ ] **Step 1: Падающий тест**

```python
# tests/test_auto_approve.py
import json
from cf.cli import main

def _sheets(briefs, prompts):
    class FakeSheets:
        def __init__(self):
            self.updates = []
        def read_rows(self, tab):
            return {"briefs": briefs, "prompt_versions": prompts, "run_log": []}[tab]
        def update_row_fields(self, tab, key, value, fields):
            self.updates.append((value, fields))
            return True
        def append_row(self, tab, row):
            pass
    return FakeSheets()

def _setup(tmp_path, verdict="recommend"):
    review = tmp_path / "r.json"
    review.write_text(json.dumps({"brief_id": "b-1", "verdict": verdict}), encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"approved": [{"name": "f1", "niche": "стритвир",
                                               "path": "x", "version": 1}]}), encoding="utf-8")
    return review, index

def test_recommend_approved_formula_gets_auto_approved(tmp_path):
    review, index = _setup(tmp_path)
    briefs = [{"brief_id": "b-1", "formula_id": "f1", "review_status": "pending",
               "generated_at": "2026-07-15T10:00:00+00:00"}]
    prompts = [{"prompt_id": "brief-стритвир-reel", "active": "TRUE"}]
    sheets = _sheets(briefs, prompts)
    code = main(["auto-approve", "b-1", "--review", str(review), "--index", str(index)],
                sheets=sheets)
    assert code == 0
    brief_id, fields = sheets.updates[0]
    assert brief_id == "b-1" and fields["review_status"] == "approved"
    assert fields["reviewer_notes"].startswith("авто-одобрен")

def test_paused_formula_stays_pending(tmp_path):
    review, index = _setup(tmp_path)
    index.write_text(json.dumps({"approved": []}), encoding="utf-8")   # формулы нет в индексе
    briefs = [{"brief_id": "b-1", "formula_id": "f1", "review_status": "pending",
               "generated_at": "2026-07-15T10:00:00+00:00"}]
    prompts = [{"prompt_id": "brief-стритвир-reel", "active": "TRUE"}]
    sheets = _sheets(briefs, prompts)
    assert main(["auto-approve", "b-1", "--review", str(review), "--index", str(index)],
                sheets=sheets) == 0
    assert sheets.updates == []
```

- [ ] **Step 2: Тесты падают**
- [ ] **Step 3: Реализация** — `cmd_auto_approve`: аргументы `brief_id`, `--review` (путь к review-JSON), `--index` (дефолт `formulas/_approved/index.json`), `--cap` (5). Ниша для проверки промпта — из записи индекса формулы (`niche`). Проверка активности промпта — та же логика, что `_is_active` в `sections.py:` (строка `active` из Sheets: "TRUE"/True). Кап: считать по `read_rows("briefs")` строки той же `formula_id` с `review_status == "approved"` и `generated_at` за последние 7 дней (`datetime.now(timezone.utc) - timedelta(days=7)`; парсинг ISO с фолбэком «не считать строку»). При одобрении: `update_row_fields("briefs", "brief_id", ..., {"review_status": "approved", "reviewer_notes": "авто-одобрен: recommend ревьюера, формула <name> approved", "rejection_reason": ""})` + `log_run(agent="auto-approve", status="success", input_summary=f"{brief_id}: авто")`. Любое несоблюдение условий — печать причины, без изменений.
- [ ] **Step 4: Тесты зелёные**
- [ ] **Step 5: Commit** `feat(cli): auto-approve — брифы утверждённых формул одобряет пайплайн`

---

### Task 6: Авто-пауза формулы — `cf formula-guard`

**Files:**
- Modify: `src/cf/cli.py`
- Test: `tests/test_formula_guard.py`

- [ ] **Step 1: Падающий тест**

```python
# tests/test_formula_guard.py
import json
from cf.cli import main

def test_three_of_last_five_rejected_pauses_formula(tmp_path):
    formula = tmp_path / "formulas" / "стритвир" / "f1.json"
    formula.parent.mkdir(parents=True)
    formula.write_text(json.dumps({"name": "f1", "niche": "стритвир", "version": 1,
                                   "status": "approved"}), encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"approved": [{"name": "f1", "niche": "стритвир",
        "path": str(formula).replace("\\", "/"), "version": 1}]}), encoding="utf-8")
    briefs = [{"brief_id": f"b{i}", "formula_id": "f1",
               "review_status": "rejected" if i >= 2 else "approved",
               "generated_at": f"2026-07-1{i}T00:00:00+00:00"} for i in range(5)]
    class FakeSheets:
        def read_rows(self, tab):
            return {"briefs": briefs, "run_log": []}[tab]
        def append_row(self, tab, row): pass
    code = main(["formula-guard", "--index", str(index)], sheets=FakeSheets())
    assert code == 0
    assert json.loads(formula.read_text(encoding="utf-8"))["status"] == "paused"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []
```

- [ ] **Step 2: Тест падает**
- [ ] **Step 3: Реализация** — `cmd_formula_guard`: для каждой записи approved-индекса взять брифы этой `formula_id`, отсортировать по `generated_at`, последние 5; если `rejected >= 3` — вызвать логику `formula-status paused --reason "авто-пауза: N reject из последних 5"` (переиспользовать функцию из Task 4, не дублировать) + `log_run(agent="formula-guard", status="success", input_summary=...)`. Печать: какие формулы на паузе / «все формулы в норме».
- [ ] **Step 4: Тест зелёный**
- [ ] **Step 5: Commit** `feat(cli): formula-guard — авто-пауза формулы при 3 reject из 5`

---

### Task 7: Таксономия ниш в файл + предложения новых ниш

**Files:**
- Create: `prompts/agents/niche-taxonomy.json` — перенести текущий список ниш из `prompts/agents/niche-classifier.md` (сам список взять оттуда, формат: `{"niches": ["мужские-образы", ...], "updated_at": "2026-07-15"}`)
- Modify: `prompts/agents/niche-classifier.md` — раздел таксономии заменить на: «Список ниш читай из `prompts/agents/niche-taxonomy.json` — он единственный источник истины. Режим кластеризации: сначала `python -m cf cluster-other <tab>`, затем для каждого кластера сформируй предложение ниши в `agent-runtime/niche-proposals/YYYY-MM-DD-<slug>.json`: `{"name": "<kebab-slug>", "title": "...", "description": "...", "cluster_size": N, "examples": [{"source_url", "caption"}...до 5], "source_urls": [...], "status": "proposed"}`. Новые ниши сам НЕ вводишь — только предложения, решение за оператором.»
- Modify: `.claude/commands/cf-classify-niche.md` — добавить режим `--clusters`: шаги «cluster-other → предложения → сводка оператору»
- Test: `tests/test_taxonomy_file.py`

- [ ] **Step 1: Падающий тест**

```python
# tests/test_taxonomy_file.py
import json
from pathlib import Path

def test_taxonomy_json_exists_and_valid():
    data = json.loads(Path("prompts/agents/niche-taxonomy.json").read_text(encoding="utf-8"))
    assert "мужские-образы" in data["niches"]
    assert len(data["niches"]) >= 10
    assert len(data["niches"]) == len(set(data["niches"]))
```

- [ ] **Step 2: Тест падает** (файла нет)
- [ ] **Step 3: Создать файл и обновить оба промпта** (список ниш — дословно из niche-classifier.md, ничего не выдумывать)
- [ ] **Step 4: Тест зелёный**
- [ ] **Step 5: Commit** `feat(niches): таксономия в json + режим предложений новых ниш из кластеров`

---

### Task 8: Команда прогона одной ниши — `.claude/commands/cf-niche-run.md`

**Files:**
- Create: `.claude/commands/cf-niche-run.md`
- Modify: `prompts/agents/pattern-analyzer.md`, `.claude/commands/cf-analyze.md`, `prompts/agents/formula-writer.md`

- [ ] **Step 1: Написать команду**

```markdown
---
description: Niche Pipeline - профиль + анализ + черновики формул одной ниши (для фан-аута)
argument-hint: "<tab> <niche>"
---
Один прогон = одна ниша. Аргументы: $ARGUMENTS (вкладка, ниша).

Порядок (механика уже посчитана командами cf — НЕ пересчитывай квартили руками):
1. `.venv/Scripts/python -m cf profile <tab> --niche <niche>` — ready_for_analysis: false
   → залогируй insufficient_data и остановись (это не ошибка).
2. `.venv/Scripts/python -m cf analyze-batch <tab> --niche <niche>` — status
   insufficient_data → залогируй и остановись.
3. Прочитай analysis-JSON. Твоя работа — только смысл (промпт
   prompts/agents/pattern-analyzer.md, раздел «Верификация evidence»):
   сформулируй паттерны из winners/losers/same_account, верифицируй каждый URL
   по транскрипту/капшену, отбрось непрошедшие. Запиши
   agent-runtime/patterns/YYYY-MM-DD-<niche>-<tab>-patterns.json,
   проверь `python -m cf validate patterns <файл>`.
4. Из паттернов confidence high|medium собери черновики формул по
   prompts/agents/formula-writer.md со `"status": "proposed"` в JSON
   (утверждение — НЕ твоя работа, кнопка на дашборде). Файлы:
   formulas/<niche>/<name>.json + .md. `python -m cf validate formula <файл>`.
   Git-команды ЗАПРЕЩЕНЫ — коммитит раннер.
5. Залогируй прогон: `python -m cf log-run --agent niche-pipeline
   --status <success|insufficient_data> --input "<tab>/<niche>"
   --outputs <файлы>`.
6. Последней строкой ответа выведи ровно одну строку-сводку:
   NICHE_RESULT: {"niche": "<niche>", "patterns": N, "formulas": M, "status": "..."}
```

- [ ] **Step 2: Синхронизировать промпты.** В `pattern-analyzer.md` раздел «Метод (v2)» заменить на: «Механику (дедуп, порог views>=1000, квартили, winners/losers, same-account) считает `python -m cf analyze-batch` — бери числа из его отчёта, руками не пересчитывай. Твоя зона: формулировка паттернов (>=3 winners, отличие от losers) и верификация evidence (раздел ниже) — без изменений.» Раздел верификации и confidence не трогать. В `cf-analyze.md` шаг 2-3 заменить на вызов analyze-batch. В `formula-writer.md` в «Выход» добавить: «Новые формулы пиши со `"status": "proposed"`; в approved-индекс их добавляет оператор кнопкой (или `cf formula-status ... approved`).»
- [ ] **Step 3: Ручная проверка** — прочитать все четыре файла целиком: нет противоречий (старые шаги квартилей удалены, git нигде не разрешён агентам).
- [ ] **Step 4: Commit** `feat(prompts): cf-niche-run — прогон ниши для фан-аута; механика через CLI`

---

### Task 9: Оркестратор фан-аута в раннере

**Files:**
- Modify: `src/cf/dashboard/runner.py`
- Test: `tests/test_runner_fanout.py` (паттерн моков — как в существующих тестах раннера: `run_command`, `sleep_fn`, фейковый sheets)

Поведение `factory` меняется: вместо `prompts: [/cf-analyze, /cf-generate-briefs]` — конвейер:

```
run_fanout_sync():
  1. классификация: run_command(["claude","-p","/cf-classify-niche --pending","--output-format","json"])
     (упала — warn в отчёт, конвейер продолжается: старые ниши уже размечены)
  2. очередь ниш: для каждой вкладки RAW_TABS собрать niches из строк,
     посчитать eligible (>= min_rows строк с views >= min_views, cf.analyze._num);
     МИНУС ниши, у которых есть свежий analysis-отчёт в agent-runtime/analysis
     и рост строк с тех пор < growth (1.5x) — количество строк брать из
     report["total_rows"] последнего отчёта ниши
  3. пул ThreadPoolExecutor(max_workers=workers): на нишу —
     run_command(["claude","-p",f"/cf-niche-run {tab} {niche}","--output-format","json"]);
     отчёт каждой ниши — в self._add_report("factory", f"ниша {niche}", text, session_id);
     живой detail: f"ниши {done}/{total} · формул {formulas_found}"
     (formulas_found — парсинг строки NICHE_RESULT из вывода, отсутствие строки = 0)
  4. брифы: run_command(["claude","-p","/cf-generate-briefs","--output-format","json"]),
     затем run_command(["claude","-p","/cf-review-brief --pending","--output-format","json"])
  5. страховка: run_command([python, "-m", "cf", "formula-guard"]) — best-effort
  6. git: _git_commit("formulas: черновики прогона фан-аута") — только staged formulas/,
     best-effort (нет изменений — не ошибка)
  7. финал: _finish("factory", detail=f"ниши ok X · insufficient Y · error Z · формул N")
```

- [ ] **Step 1: Падающие тесты** — минимум четыре:

```python
# tests/test_runner_fanout.py — каркас (фейки по образцу существующих тестов раннера)
def test_fanout_queue_respects_thresholds_and_growth(tmp_path): ...
    # 2 ниши с материалом, 1 без; у одной свежий отчёт с total_rows без роста → в очереди 1
def test_fanout_one_niche_failure_does_not_kill_run(): ...
    # run_command: ниша A ok, ниша B код 1 → статус звена warn, в detail "error 1", отчёт B есть
def test_fanout_detail_counts_formulas(): ...
    # вывод ниш содержит NICHE_RESULT: {... "formulas": 2} → detail содержит "формул 2"
def test_fanout_git_called_once_from_runner(): ...
    # мок _git_commit: вызван ровно 1 раз после пула (не из воркеров)
```

Пиши их как полноценные тесты с фейковым `run_command`, который возвращает подготовленные JSON-ответы claude (формат — как `_parse_claude_output` ждёт: `{"result": "...NICHE_RESULT: {...}", "session_id": "s1"}`).

- [ ] **Step 2: Тесты падают**
- [ ] **Step 3: Реализация.** `STAGES["factory"] = {"kind": "fanout"}`. Новые методы: `_fanout_params()` (конфиг с дефолтами), `_eligible_niches()`, `_previous_analysis_rows(niche, tab)` (glob по `agent-runtime/analysis/*-{tab}-{slug}-analysis.json`, взять свежайший), `_run_niche(tab, niche)` (вызов + парсинг NICHE_RESULT, возврат dict), `_git_commit(msg)` (subprocess `git add formulas/ && git commit -m ...`, любые ошибки — warning в лог, не в статус), `run_fanout_sync()`; в `run_sync` ветка `kind == "fanout"` → делегирование. Лок `_claim`/`_finish`/Run Log — как у остальных звеньев (`dashboard-factory` остаётся именем в Run Log). `AUTO_CYCLE` не меняется.
- [ ] **Step 4: Все тесты раннера зелёные** (`pytest tests -k runner`)
- [ ] **Step 5: Commit** `feat(runner): фан-аут — пул ниш, живой прогресс, git только у раннера`

---

### Task 10: Очередь формул и ниш в данных Лаборатории

**Files:**
- Modify: `src/cf/dashboard/sections.py` (`lab_context`, sections.py:103)
- Test: `tests/test_lab_queue.py`

- [ ] **Step 1: Падающий тест**

```python
# tests/test_lab_queue.py
import json
from cf.dashboard.sections import lab_context

class FakeCache:
    stale = False
    def rows(self, tab):
        return []

def test_lab_context_lists_proposed_formulas_and_niches(tmp_path):
    f = tmp_path / "formulas" / "стритвир" / "grid.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"name": "grid", "niche": "стритвир", "version": 1,
                             "status": "proposed", "hook_structure": "5 дней — 5 образов",
                             "evidence": {"source_urls": ["u1", "u2", "u3"],
                                          "avg_views": 100000, "avg_er": 0.04}}),
                 encoding="utf-8")
    (tmp_path / "formulas" / "_approved").mkdir()
    (tmp_path / "formulas" / "_approved" / "index.json").write_text(
        json.dumps({"approved": []}), encoding="utf-8")
    np = tmp_path / "agent-runtime" / "niche-proposals"
    np.mkdir(parents=True)
    (np / "2026-07-15-tall.json").write_text(json.dumps(
        {"name": "одежда-для-высоких", "title": "Для высоких", "cluster_size": 37,
         "examples": [], "status": "proposed"}), encoding="utf-8")
    ctx = lab_context(FakeCache(), root=tmp_path)
    assert [q["name"] for q in ctx["formula_queue"]] == ["grid"]
    assert ctx["formula_queue"][0]["evidence_urls"] == ["u1", "u2", "u3"]
    assert [n["name"] for n in ctx["niche_queue"]] == ["одежда-для-высоких"]
```

- [ ] **Step 2: Тест падает** (ключей нет в контексте)
- [ ] **Step 3: Реализация** — в `lab_context` добавить: скан `root/formulas/*/*.json` (кроме `_approved`), в `formula_queue` — формулы со `status == "proposed"` (name, niche, version, hook_structure строкой, evidence_urls, avg_views, avg_er, path относительно root, status_reason), в `paused_formulas` — со `status == "paused"`; скан `root/agent-runtime/niche-proposals/*.json` со `status == "proposed"` → `niche_queue` (name, title, description, cluster_size, examples, filename). Битый JSON пропускать (паттерн `_load_json` уже так делает).
- [ ] **Step 4: Тест зелёный**
- [ ] **Step 5: Commit** `feat(lab): очередь proposed-формул и предложений ниш в контексте`

---

### Task 11: Кнопки решений — роуты и действия

**Files:**
- Create: `src/cf/dashboard/decisions.py`
- Modify: `src/cf/dashboard/app.py` (после `/briefs/{brief_id}/review`, app.py:116-124)
- Test: `tests/test_decisions.py`

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_decisions.py — decide_formula: happy paths + запреты
import json
from cf.dashboard.decisions import decide_formula, decide_niche

def _formula(tmp_path):
    p = tmp_path / "formulas" / "стритвир" / "grid.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"name": "grid", "niche": "стритвир", "version": 1,
                             "status": "proposed"}), encoding="utf-8")
    (tmp_path / "formulas" / "_approved").mkdir()
    (tmp_path / "formulas" / "_approved" / "index.json").write_text(
        json.dumps({"approved": []}), encoding="utf-8")
    return p

def test_approve_updates_json_index_and_decision_file(tmp_path):
    _formula(tmp_path)
    ok = decide_formula(tmp_path, "formulas/стритвир/grid.json", "approved",
                        reason="", git=lambda msg: None)
    assert ok
    data = json.loads((tmp_path / "formulas/стритвир/grid.json").read_text(encoding="utf-8"))
    assert data["status"] == "approved"
    index = json.loads((tmp_path / "formulas/_approved/index.json").read_text(encoding="utf-8"))
    assert index["approved"][0]["name"] == "grid"
    decisions = list((tmp_path / ".claude" / "memory" / "decisions").glob("*approve-grid*"))
    assert decisions

def test_path_traversal_rejected(tmp_path):
    _formula(tmp_path)
    assert not decide_formula(tmp_path, "../secrets/x.json", "approved",
                              reason="", git=lambda msg: None)

def test_accept_niche_appends_taxonomy(tmp_path):
    tax = tmp_path / "prompts" / "agents" / "niche-taxonomy.json"
    tax.parent.mkdir(parents=True)
    tax.write_text(json.dumps({"niches": ["мужские-образы"]}), encoding="utf-8")
    np = tmp_path / "agent-runtime" / "niche-proposals"
    np.mkdir(parents=True)
    (np / "p.json").write_text(json.dumps({"name": "одежда-для-высоких",
                                           "status": "proposed"}), encoding="utf-8")
    assert decide_niche(tmp_path, "p.json", "accepted", git=lambda msg: None)
    tax_data = json.loads(tax.read_text(encoding="utf-8"))
    assert "одежда-для-высоких" in tax_data["niches"]
    prop = json.loads((np / "p.json").read_text(encoding="utf-8"))
    assert prop["status"] == "accepted"
```

- [ ] **Step 2: Тесты падают**
- [ ] **Step 3: Реализация `decisions.py`:**
  - `decide_formula(root, rel_path, decision, reason, git)` — decision из `{"approved","paused","rejected","proposed"}` ("proposed" = отмена решения). Безопасность пути: `resolved = (root / rel_path).resolve()`; не внутри `root/formulas` → False. Обновить JSON+индекс (переиспользовать функцию Task 4 — импорт из `cf.cli` или вынести общее в `cf/formulas.py`, если импорт из cli громоздкий). На approved — decision-файл `.claude/memory/decisions/YYYY-MM-DD-approve-<name>.md` (заголовок, ниша, evidence-строка, «утверждено с дашборда») и `git(f"formula({niche}): approve {name} v{version}")`. На paused/rejected — git с соответствующим сообщением. `git` — инжектируемый колбэк (в проде — команда `git add formulas/ .claude/memory/decisions/ && git commit -m ...`, best-effort с warning).
  - `decide_niche(root, filename, decision, git)` — decision из `{"accepted","rejected"}`; файл строго внутри `root/agent-runtime/niche-proposals` (тот же resolve-гейт); на accepted — добавить name в `prompts/agents/niche-taxonomy.json` (дедуп), обновить `updated_at`, git-коммит таксономии; в обоих случаях проставить status в файле предложения.
  - Роуты в `app.py`: `POST /formulas/decision` (`path: str = Form(...)`, `decision: str = Form(...)`, `reason: str = Form("")`) → 422 на невалидный decision, 404 если функция вернула False, иначе redirect `/lab`; `POST /niches/decision` аналогично. Оба пишут в Run Log через `log_run(agent="dashboard-formula", ...)` best-effort (паттерн `actions.review_brief`).
- [ ] **Step 4: Тесты зелёные** (+ smoke-тест роутов через `TestClient`, как сделаны существующие тесты app)
- [ ] **Step 5: Commit** `feat(dashboard): кнопки решений по формулам и нишам`

---

### Task 12: UI очереди — шаблон Лаборатории и бейдж «авто» на брифах

**Files:**
- Modify: `src/cf/dashboard/templates/lab.html` (блок очереди — первым на странице), `src/cf/dashboard/templates/briefs.html` (бейдж), `src/cf/dashboard/static/style.css`
- Test: `tests/test_lab_template.py`

- [ ] **Step 1: Падающий тест** (через TestClient + tmp lab_root с фикстурами из Task 10):

```python
def test_lab_page_renders_queue_cards(...):
    # GET /lab содержит: имя формулы, кнопки "Утвердить"/"В паузу"/"Отклонить",
    # карточку ниши с "Принять нишу", бейдж "НОВАЯ НИША"
def test_brief_auto_badge(...):
    # бриф с reviewer_notes "авто-одобрен: ..." → в GET /briefs есть бейдж "авто"
```

- [ ] **Step 2: Тесты падают**
- [ ] **Step 3: Реализация.** В `lab.html` — секция «Формулы · очередь одобрения» (карточка: имя моноширинным, пилюля ниши, hook_structure, строка evidence `avg views · ER · N референсов` с `<details>` для URL, формы-кнопки POST `/formulas/decision` со скрытым path; для paused — кнопка «Вернуть в работу» = decision approved). Карточки ниш — жёлтая пилюля «НОВАЯ НИША · N роликов», примеры в `<details>`, кнопки POST `/niches/decision`. Пустая очередь — существующий `empty-state`. Классы по DESIGN.md: пилюли как `.pill`-бейджи брифов, кнопки `btn-primary`/вторичная. В `briefs.html` — рядом со статусом: `{% if selected.reviewer_notes and selected.reviewer_notes.startswith("авто-одобрен") %}<span class="pill auto">авто</span>{% endif %}` (и в списке то же по строке). CSS: `.pill.auto` — голубой фон `rgba(90,140,220,.15)`.
- [ ] **Step 4: Тесты зелёные; скрин-проверка** — поднять сервер с фикстурной формулой, headless-скрин `/lab`, глазами сверить с DESIGN.md
- [ ] **Step 5: Commit** `feat(lab-ui): карточки очереди формул и ниш, бейдж «авто» на брифах`

---

### Task 13: Промпты генерации и ревью — вширь и авто-одобрение

**Files:**
- Modify: `prompts/agents/brief-generator.md`, `.claude/commands/cf-generate-briefs.md`, `.claude/commands/cf-review-brief.md`, `prompts/agents/brief-reviewer.md`, `CF/.claude/settings.json` (только если новые cf-подкоманды не покрыты маской allowlist — проверь)

- [ ] **Step 1: brief-generator.md.** Правило 3 (дедуп «один pending на формулу») заменить: «За прогон — до `briefs_per_formula` (из cf.config.json → dashboard.fanout, дефолт 2) брифов на КАЖДУЮ формулу из approved-индекса. Внутри формулы ротируй `tested_variable` — брифы не должны отличаться только словами. Пропусти формулу, если у неё уже >= 5 approved-брифов за последние 7 дней (посмотри в снапшоте брифов) — прод не успевает снимать.» Правила про референсы/evidence (п.2, 2а, 2б) — не трогать.
- [ ] **Step 2: cf-review-brief.md шаг 6.** Ветку recommend заменить: «вердикт recommend → `python -m cf auto-approve <brief_id> --review agent-runtime/reviews/YYYY-MM-DD-<brief_id>-review.json`. Команда сама проверит формулу/промпт/кап и либо одобрит (авто), либо оставит pending с причиной — её решение не оспаривай, в сводке оператору покажи, что она напечатала.» В `brief-reviewer.md` добавить зеркальную строку в раздел выхода. Правило «ревьюер сам НЕ ставит approved» в обоих файлах оставить и усилить: «approved ставит только `cf auto-approve` или продюсер».
- [ ] **Step 3: Прочитать `CF/.claude/settings.json`** — если allowlist перечисляет подкоманды `python -m cf ...` пофамильно, добавить Bash+PowerShell зеркала для `analyze-batch`, `cluster-other`, `auto-approve`, `formula-status`, `formula-guard`; если маска общая — ничего не делать.
- [ ] **Step 4: Перечитать все изменённые промпты целиком** — противоречий нет (нигде не осталось «один pending на формулу», нигде агент не коммитит и не ставит approved руками).
- [ ] **Step 5: Commit** `feat(prompts): генерация N×M с капом, авто-одобрение через cf auto-approve`

---

### Task 14: Сквозная проверка и синхронизация доков

**Files:**
- Modify: `README.md` или `docs/` (где описаны команды cf — найти и дополнить), `MEMORY.md`-память проекта не трогать (не место)

- [ ] **Step 1: Полный прогон тестов** — `.venv/Scripts/python -m pytest tests -q`, ожидание: все зелёные (было 270+, станет больше).
- [ ] **Step 2: Живой smoke без боевых вызовов:** `python -m cf analyze-batch raw_tiktok --niche мужские-образы` (реальная таблица, читающая команда) — отчёт создан, статус напечатан; `python -m cf cluster-other raw_tiktok` — кластеры/пусто без трейсбека; `python -m cf formula-guard` — «все формулы в норме».
- [ ] **Step 3: Дашборд:** поднять `python -m cf dashboard`, убить осиротевший процесс на 8787 при конфликте (Get-NetTCPConnection — грабля из памяти проекта), проверить `/lab` (очередь пустая — empty-state), `/overview` (плитка живая). Headless-скрин.
- [ ] **Step 4: Обновить справку по командам** в доке, где перечислены cf-команды (analyze-batch, cluster-other, formula-status, formula-guard, auto-approve, режим --clusters).
- [ ] **Step 5: Commit** `docs: команды конвейера формул` и финальная сводка оператору: что готово, что проверено, ручной шаг — первый боевой ▶ фан-аута.

---

## Самопроверка плана (выполнена)

- Спека покрыта: механика в CLI (T1–T3), статусы+гейт (T4), авто-approve+кап (T5), авто-пауза (T6), новые ниши через одобрение (T7, T11), фан-аут раннером c git-политикой (T8–T9), очередь UI (T10–T12), генерация вширь (T13), критерии готовности (T14).
- Ramp-up и `/cf-tune-sources` — вне кода, действий не требуют.
- Типы согласованы: `formula_queue`/`niche_queue` (T10) ↔ шаблон (T12) ↔ роуты (T11); `NICHE_RESULT` (T8) ↔ парсер (T9); функция статуса формулы (T4) переиспользуется в T6 и T11.
