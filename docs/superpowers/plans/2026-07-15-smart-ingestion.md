# Smart Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (коммиты напрямую в master, финальный — 387dbb5, 2026-07-15). Чекбоксы в ходе исполнения не велись.

**Goal:** Умный сбор raw-данных: атрибуция источников, гейт на входе, двухступенчатая воронка, snowball от winners, петля скоринга источников (спека: `docs/superpowers/specs/2026-07-15-smart-ingestion-design.md`).

**Architecture:** n8n остаётся runtime (правки через n8n API, воркфлоу версионируются в новой папке `n8n/`); intelligence-часть — новые CLI-команды (`backfill-attribution`, `source-stats`, `pull-n8n`/`push-n8n`, `export-seeds`, `apply-sources`) и агент `/cf-tune-sources` с proposal-циклом как у промптов.

**Tech Stack:** Python (argparse CLI, pytest+FakeSheets, gspread), n8n REST API (`cf.config.json → n8n.base_url`, ключ `secrets/n8n-api-key.txt`), Apify actors clockworks/tiktok-scraper и apify/instagram-*.

**Живые воркфлоу:** CF 01 TikTok `w2FRPA06jd2Eqb5u`, CF 01 Instagram `wSGoTAJ34612XYLO`.
Слепки на момент разведки: `agent-runtime/n8n-live-*.json` (не версионируются).

---

### Task 1: Парсер атрибуции из raw_json

**Files:**
- Create: `src/cf/attribution.py`
- Test: `tests/test_attribution.py`

- [ ] **Step 1: Написать падающий тест**

```python
from cf.attribution import source_query_from_raw_json

def test_hashtag_attribution():
    raw = '{"searchHashtag": {"name": "мужскаяодежда", "views": 1}, "id": "1"}'
    assert source_query_from_raw_json(raw) == "hashtag:#мужскаяодежда"

def test_query_attribution():
    raw = '{"searchQuery": "outfit ideas men", "id": "1"}'
    assert source_query_from_raw_json(raw) == "query:outfit ideas men"

def test_hashtag_wins_over_query():
    raw = '{"searchHashtag": {"name": "menswear"}, "searchQuery": "menswear"}'
    assert source_query_from_raw_json(raw) == "hashtag:#menswear"

def test_unknown_on_missing_fields():
    assert source_query_from_raw_json('{"id": "1"}') == "unknown"

def test_unknown_on_broken_json():
    assert source_query_from_raw_json('{"id": tru') == "unknown"
    assert source_query_from_raw_json("") == "unknown"
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/Scripts/python -m pytest tests/test_attribution.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'cf.attribution'`

- [ ] **Step 3: Минимальная реализация**

```python
import json


def source_query_from_raw_json(raw_json_text):
    """Источник строки из Apify-итема: hashtag:#x | query:x | unknown.

    raw_json обрезается при записи до 45000 символов, поэтому парс может падать —
    это не ошибка данных, а норма: возвращаем unknown.
    """
    try:
        raw = json.loads(raw_json_text or "")
    except (json.JSONDecodeError, TypeError):
        return "unknown"
    if not isinstance(raw, dict):
        return "unknown"
    hashtag = raw.get("searchHashtag")
    if isinstance(hashtag, dict) and str(hashtag.get("name", "")).strip():
        return f"hashtag:#{str(hashtag['name']).strip().lstrip('#')}"
    query = str(raw.get("searchQuery", "")).strip()
    if query:
        return f"query:{query}"
    return "unknown"
```

- [ ] **Step 4: Тесты зелёные**

Run: `.venv/Scripts/python -m pytest tests/test_attribution.py -v` → PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cf/attribution.py tests/test_attribution.py
git commit -m "feat: парсер атрибуции source_query из raw_json"
```

---

### Task 2: CLI `cf backfill-attribution`

**Files:**
- Modify: `src/cf/cli.py` (новый cmd_ + сабпарсер)
- Test: `tests/test_cli_backfill.py`

Заглушки, подлежащие замене: пустое значение, `managed_tiktok_keywords` (TikTok),
любая строка с запятой (IG-список хэштегов). Настоящие значения (`hashtag:…`,
`query:…`, `snowball:…`, `unknown`) не трогаем — повторный запуск идемпотентен.

- [ ] **Step 1: Падающий тест**

```python
import argparse

from cf.cli import cmd_backfill_attribution

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def make_rows():
    return [
        {"raw_id": "t1", "source_query": "managed_tiktok_keywords",
         "raw_json": '{"searchHashtag": {"name": "menswear"}}'},
        {"raw_id": "t2", "source_query": "managed_tiktok_keywords",
         "raw_json": '{"searchQuery": "pov парень"}'},
        {"raw_id": "t3", "source_query": "hashtag:#уже-заполнено",
         "raw_json": '{"searchQuery": "не трогать"}'},
        {"raw_id": "t4", "source_query": "тег1,тег2,тег3", "raw_json": "{broken"},
    ]


def test_backfill_fills_placeholders_only(capsys):
    sheets = FakeSheets({"raw_tiktok": make_rows()})
    assert cmd_backfill_attribution(sheets, ns(tab="raw_tiktok", dry_run=False)) == 0
    rows = {r["raw_id"]: r["source_query"] for r in sheets.tables["raw_tiktok"]}
    assert rows["t1"] == "hashtag:#menswear"
    assert rows["t2"] == "query:pov парень"
    assert rows["t3"] == "hashtag:#уже-заполнено"
    assert rows["t4"] == "unknown"


def test_backfill_dry_run_changes_nothing(capsys):
    sheets = FakeSheets({"raw_tiktok": make_rows()})
    assert cmd_backfill_attribution(sheets, ns(tab="raw_tiktok", dry_run=True)) == 0
    assert sheets.tables["raw_tiktok"][0]["source_query"] == "managed_tiktok_keywords"
    assert "would update 3" in capsys.readouterr().out
```

- [ ] **Step 2: Убедиться, что падает** — `pytest tests/test_cli_backfill.py -v` → FAIL (ImportError)

- [ ] **Step 3: Реализация в cli.py**

```python
def cmd_backfill_attribution(sheets, args):
    from cf.attribution import source_query_from_raw_json

    def is_placeholder(value):
        v = str(value or "").strip()
        return not v or v == "managed_tiktok_keywords" or "," in v

    mapping = {}
    for r in sheets.read_rows(args.tab):
        raw_id = str(r.get("raw_id", "")).strip()
        if raw_id and is_placeholder(r.get("source_query")):
            mapping[raw_id] = source_query_from_raw_json(r.get("raw_json"))
    if getattr(args, "dry_run", False):
        print(f"would update {len(mapping)} rows in {args.tab}")
        return 0
    updated = sheets.set_column_by_key(args.tab, "raw_id", "source_query", mapping)
    print(f"source_query заполнен у {updated} строк в {args.tab}")
    return 0
```

Сабпарсер в `build_parser()` (рядом с apply-niches):

```python
    sp = sub.add_parser("backfill-attribution",
                        help="Заполнить source_query из raw_json (только заглушки)")
    sp.add_argument("tab")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true")
    sp.set_defaults(func=cmd_backfill_attribution)
```

- [ ] **Step 4: Тесты зелёные** — `pytest tests/test_cli_backfill.py -v` → PASS
- [ ] **Step 5: Полный прогон** — `pytest -q` → всё зелёное
- [ ] **Step 6: Commit** — `git add -A src tests && git commit -m "feat: cf backfill-attribution"`

---

### Task 3: `target_niches` в конфиге + агрегация source-stats

**Files:**
- Modify: `cf.config.json` (после ключа `niches`)
- Create: `src/cf/sourcestats.py`
- Test: `tests/test_sourcestats.py`

- [ ] **Step 1: cf.config.json — добавить ключ**

```json
  "target_niches": ["мужские-образы", "мужской-стиль", "стритвир"],
```

(внутри корневого объекта, после массива `niches`; в git попадает вместе с задачей).

- [ ] **Step 2: Падающий тест**

```python
from cf.sourcestats import build_source_stats


def row(source, niche="мужские-образы", views=5000, transcript="слова", caption=""):
    return {"source_query": source, "niche": niche, "views": views,
            "transcript_text": transcript, "caption": caption,
            "source_url": f"https://t/{source}/{views}"}


def test_per_source_aggregates():
    rows = [
        row("hashtag:#menswear"),
        row("hashtag:#menswear", niche="other", views=100, transcript=""),
        row("query:pov парень", niche="женская-мода"),
    ]
    stats = build_source_stats(rows, ["мужские-образы"])
    by = {s["source"]: s for s in stats["sources"]}
    m = by["hashtag:#menswear"]
    assert (m["rows"], m["target_niche_rows"], m["views_1000_plus"], m["with_transcript"]) == (2, 1, 1, 1)
    assert m["target_yield"] == 0.5
    assert by["query:pov парень"]["target_yield"] == 0.0


def test_sources_sorted_by_yield_then_rows():
    rows = [row("query:слабый", niche="other")] + [row("hashtag:#сильный")] * 3
    stats = build_source_stats(rows, ["мужские-образы"])
    assert [s["source"] for s in stats["sources"]] == ["hashtag:#сильный", "query:слабый"]


def test_candidate_hashtags_mined_from_target_captions():
    rows = [
        row("hashtag:#a", caption="лук дня #мужскойстиль #капсула", views=2000),
        row("hashtag:#a", caption="#капсула снова", views=3000),
        row("hashtag:#b", niche="other", caption="#мусорныйтег" * 3, views=9000),
        row("hashtag:#a", caption="#редкий", views=50),  # < 1000 views не участвует
    ]
    stats = build_source_stats(rows, ["мужские-образы"])
    tags = {c["hashtag"]: c["count"] for c in stats["candidate_hashtags"]}
    assert tags["#капсула"] == 2
    assert "#мусорныйтег" not in tags and "#редкий" not in tags
```

- [ ] **Step 3: Убедиться, что падает** — `pytest tests/test_sourcestats.py -v` → FAIL

- [ ] **Step 4: Реализация**

```python
import re
from collections import Counter


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_source_stats(rows, target_niches, candidate_limit=25):
    """Детерминированная агрегация по source_query. Evidence для /cf-tune-sources."""
    targets = {n.lower() for n in target_niches}
    per_source, tag_counter, tag_examples = {}, Counter(), {}
    for r in rows:
        source = str(r.get("source_query", "")).strip() or "unknown"
        s = per_source.setdefault(source, {
            "source": source, "rows": 0, "target_niche_rows": 0,
            "views_1000_plus": 0, "with_transcript": 0,
        })
        s["rows"] += 1
        views = _num(r.get("views"))
        on_target = str(r.get("niche", "")).strip().lower() in targets
        if on_target:
            s["target_niche_rows"] += 1
        if views >= 1000:
            s["views_1000_plus"] += 1
        if str(r.get("transcript_text", "")).strip():
            s["with_transcript"] += 1
        if on_target and views >= 1000:
            for tag in re.findall(r"#[\w\d_]+", str(r.get("caption", "")), re.UNICODE):
                tag = tag.lower()
                tag_counter[tag] += 1
                tag_examples.setdefault(tag, str(r.get("source_url", "")))
    for s in per_source.values():
        s["target_yield"] = round(s["target_niche_rows"] / s["rows"], 4) if s["rows"] else 0.0
    return {
        "sources": sorted(per_source.values(),
                          key=lambda s: (-s["target_yield"], -s["rows"], s["source"])),
        "candidate_hashtags": [
            {"hashtag": tag, "count": count, "example_url": tag_examples[tag]}
            for tag, count in tag_counter.most_common(candidate_limit) if count >= 2
        ],
        "target_niches": sorted(targets),
        "total_rows": len(rows),
    }
```

- [ ] **Step 5: Тесты зелёные** → PASS
- [ ] **Step 6: Commit** — `git commit -m "feat: агрегация source-stats + target_niches в конфиге"`

---

### Task 4: CLI `cf source-stats`

**Files:**
- Modify: `src/cf/cli.py`
- Test: `tests/test_cli_source_stats.py`

- [ ] **Step 1: Падающий тест**

```python
import argparse
import json

from cf.cli import cmd_source_stats

from tests.fakes import FakeSheets


def test_source_stats_writes_report(tmp_path, capsys):
    sheets = FakeSheets({"raw_tiktok": [
        {"source_query": "hashtag:#a", "niche": "мужские-образы", "views": 5000,
         "transcript_text": "т", "caption": "", "source_url": "https://x/1",
         "posted_at": "2026-07-10"},
    ]})
    args = argparse.Namespace(tab="raw_tiktok", since=None, out_dir=str(tmp_path),
                              target=None)
    assert cmd_source_stats(sheets, args) == 0
    report_path = next(tmp_path.glob("*-raw_tiktok-source-stats.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sources"][0]["source"] == "hashtag:#a"
    assert "hashtag:#a" in capsys.readouterr().out
```

- [ ] **Step 2: Падает** — ImportError.

- [ ] **Step 3: Реализация в cli.py**

`cmd_source_stats` использует `target_niches` из `sheets.config` (FakeSheets не имеет
config — параметр `--target` и fallback):

```python
def cmd_source_stats(sheets, args):
    from cf.sourcestats import build_source_stats
    rows = apply_filters(sheets.read_rows(args.tab), None, args.since)
    rows = dedupe_rows(rows)
    if args.target:
        targets = [t.strip() for t in args.target.split(",") if t.strip()]
    else:
        targets = getattr(sheets, "config", {}).get("target_niches", []) \
            if hasattr(sheets, "config") else []
        targets = targets or ["мужские-образы"]
    stats = build_source_stats(rows, targets)
    stats["meta"] = {"tab": args.tab, "since": args.since, "generated_at": now_iso()}
    path = Path(args.out_dir) / f"{date.today().isoformat()}-{args.tab}-source-stats.json"
    write_json_atomic(path, stats)
    print(f"source stats -> {path}")
    print(f"{'source':44} {'rows':>5} {'target':>6} {'yield':>6} {'>=1k':>5} {'transcr':>7}")
    for s in stats["sources"]:
        print(f"{s['source'][:44]:44} {s['rows']:>5} {s['target_niche_rows']:>6} "
              f"{s['target_yield']:>6.2f} {s['views_1000_plus']:>5} {s['with_transcript']:>7}")
    if stats["candidate_hashtags"]:
        print("Кандидаты в хэштеги (из капшенов целевых ниш, views>=1000):")
        for c in stats["candidate_hashtags"][:10]:
            print(f"  {c['count']:>3}x {c['hashtag']}  {c['example_url']}")
    return 0
```

Сабпарсер:

```python
    sp = sub.add_parser("source-stats", help="Скоринг источников по source_query")
    sp.add_argument("tab")
    sp.add_argument("--since")
    sp.add_argument("--target", help="целевые ниши через запятую (default: config target_niches)")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/source-stats")
    sp.set_defaults(func=cmd_source_stats)
```

- [ ] **Step 4: Тесты зелёные**, полный `pytest -q` зелёный.
- [ ] **Step 5: Commit** — `git commit -m "feat: cf source-stats"`

---

### Task 5: n8n API-клиент + pull/push воркфлоу

**Files:**
- Create: `src/cf/n8napi.py` (HTTP-клиент, тонкий — без юнит-тестов)
- Create: `src/cf/n8nsync.py` (чистые функции extract/inject — под тестами)
- Modify: `src/cf/cli.py` (pull-n8n / push-n8n)
- Test: `tests/test_n8nsync.py`

Идея: воркфлоу версионируется как `n8n/<slug>/workflow.json` + jsCode каждого
Code-узла выносится в `n8n/<slug>/code/<node-name>.js` для читаемых диффов.
`push-n8n` собирает обратно и PUT'ит.

- [ ] **Step 1: Падающий тест (round-trip)**

```python
from cf.n8nsync import code_filename, extract_code_nodes, inject_code_nodes


def wf():
    return {"name": "T", "nodes": [
        {"name": "Build Input", "type": "n8n-nodes-base.code",
         "parameters": {"jsCode": "return 1;"}},
        {"name": "Fetch", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
    ], "connections": {}, "settings": {}}


def test_extract_returns_code_nodes_only():
    assert extract_code_nodes(wf()) == {"Build Input": "return 1;"}


def test_code_filename_sanitizes():
    assert code_filename("Normalize TikTok Raw Rows") == "Normalize-TikTok-Raw-Rows.js"


def test_inject_replaces_jscode():
    updated = inject_code_nodes(wf(), {"Build Input": "return 2;"})
    assert updated["nodes"][0]["parameters"]["jsCode"] == "return 2;"


def test_inject_unknown_node_raises():
    import pytest
    with pytest.raises(KeyError):
        inject_code_nodes(wf(), {"Нет такого": "x"})
```

- [ ] **Step 2: Падает.** → ModuleNotFoundError

- [ ] **Step 3: Реализация `src/cf/n8nsync.py`**

```python
import re

CODE_TYPE = "n8n-nodes-base.code"


def code_filename(node_name):
    return re.sub(r"[^\w\-]+", "-", node_name).strip("-") + ".js"


def extract_code_nodes(workflow):
    return {n["name"]: n["parameters"].get("jsCode", "")
            for n in workflow.get("nodes", []) if n.get("type") == CODE_TYPE}


def inject_code_nodes(workflow, code_by_node):
    by_name = {n["name"]: n for n in workflow.get("nodes", [])
               if n.get("type") == CODE_TYPE}
    for name, code in code_by_node.items():
        by_name[name]["parameters"]["jsCode"] = code  # KeyError = узел переименован
    return workflow
```

`src/cf/n8napi.py`:

```python
import requests

from cf.config import load_config

# PUT принимает только эти ключи; id/active/tags и пр. API отвергает.
WORKFLOW_KEYS = ("name", "nodes", "connections", "settings")


class N8nApi:
    def __init__(self, config=None):
        config = config or load_config()
        self.base = config["n8n"]["base_url"].rstrip("/") + "/api/v1"
        with open(config["n8n"]["api_key_file"], encoding="utf-8") as f:
            self.headers = {"X-N8N-API-KEY": f.read().strip()}

    def _check(self, resp):
        if resp.status_code >= 400:
            raise RuntimeError(f"n8n API {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def get_workflow(self, workflow_id):
        return self._check(requests.get(f"{self.base}/workflows/{workflow_id}",
                                        headers=self.headers, timeout=30))

    def put_workflow(self, workflow_id, workflow):
        body = {k: workflow[k] for k in WORKFLOW_KEYS}
        return self._check(requests.put(f"{self.base}/workflows/{workflow_id}",
                                        headers=self.headers, json=body, timeout=30))

    def create_workflow(self, workflow):
        body = {k: workflow[k] for k in WORKFLOW_KEYS}
        return self._check(requests.post(f"{self.base}/workflows",
                                         headers=self.headers, json=body, timeout=30))
```

CLI (в cli.py):

```python
def cmd_pull_n8n(sheets, args):
    from cf.n8napi import N8nApi
    from cf.n8nsync import code_filename, extract_code_nodes
    wf = N8nApi().get_workflow(args.workflow_id)
    out = Path(args.dir)
    (out / "code").mkdir(parents=True, exist_ok=True)
    write_json_atomic(out / "workflow.json", wf)
    for name, code in extract_code_nodes(wf).items():
        (out / "code" / code_filename(name)).write_text(code, encoding="utf-8", newline="\n")
    print(f"pulled '{wf['name']}' -> {out}")
    return 0


def cmd_push_n8n(sheets, args):
    from cf.n8napi import N8nApi
    from cf.n8nsync import code_filename, extract_code_nodes, inject_code_nodes
    src = Path(args.dir)
    wf = read_json(src / "workflow.json")
    code = {}
    for name in extract_code_nodes(wf):
        f = src / "code" / code_filename(name)
        if f.exists():
            code[name] = f.read_text(encoding="utf-8")
    wf = inject_code_nodes(wf, code)
    api = N8nApi()
    if getattr(args, "create", False):
        created = api.create_workflow(wf)
        wf["id"] = created["id"]
        write_json_atomic(src / "workflow.json", wf)
        print(f"created workflow {created['id']} ('{wf['name']}')")
    else:
        api.put_workflow(wf["id"], wf)
        print(f"pushed '{wf['name']}' ({wf['id']})")
    return 0
```

Сабпарсеры:

```python
    sp = sub.add_parser("pull-n8n", help="Скачать воркфлоу n8n в папку (workflow.json + code/*.js)")
    sp.add_argument("workflow_id")
    sp.add_argument("--dir", required=True)
    sp.set_defaults(func=cmd_pull_n8n)

    sp = sub.add_parser("push-n8n", help="Залить папку воркфлоу обратно в n8n")
    sp.add_argument("dir")
    sp.add_argument("--create", action="store_true", help="создать новый воркфлоу (POST)")
    sp.set_defaults(func=cmd_push_n8n)
```

- [ ] **Step 4: Тесты зелёные**, `pytest -q` зелёный.
- [ ] **Step 5: Commit** — `git commit -m "feat: n8n pull/push — воркфлоу версионируются в n8n/"`

---

### Task 6: Забрать baseline обоих CF 01 в репозиторий

- [ ] **Step 1:**
```bash
.venv/Scripts/python -m cf pull-n8n w2FRPA06jd2Eqb5u --dir n8n/cf01-tiktok
.venv/Scripts/python -m cf pull-n8n wSGoTAJ34612XYLO --dir n8n/cf01-instagram
```
- [ ] **Step 2: Проверить**: `n8n/cf01-tiktok/code/` содержит `Build-TikTok-Actor-Input.js`,
  `Normalize-TikTok-Raw-Rows.js`, `Attach-TikTok-Transcript.js`; IG — свои 4 файла.
- [ ] **Step 3: Commit** — `git add n8n && git commit -m "chore: baseline CF 01 воркфлоу из n8n"`

---

### Task 7: CF 01 TikTok — атрибуция, гейт, воронка

**Files:**
- Modify: `n8n/cf01-tiktok/code/Build-TikTok-Actor-Input.js`
- Modify: `n8n/cf01-tiktok/code/Normalize-TikTok-Raw-Rows.js`
- Create: `n8n/cf01-tiktok/code/Ingestion-Gate.js`
- Modify: `n8n/cf01-tiktok/workflow.json` (новый узел + связи)

- [ ] **Step 1: Build-Input — маркеры SOURCES + охват 8→20**

Заменить объявления `const hashtags = [...]` и `const searchQueries = [...]` на:

```javascript
// SOURCES:BEGIN — правится только через cf apply-sources (proposal-цикл)
const SOURCES = {
  "hashtags": ["мужскаяодежда", "мужскиеобразы", "мужскойстиль", "мужскойгардероб",
    "мужскаямода", "мужскиефутболки", "мужскиеджинсы", "стильмужчины",
    "menswear", "menstyle", "menfashion", "outfitideas", "streetwearmen",
    "wildberriesмужскаяодежда"],
  "searchQueries": ["мужская одежда", "мужские образы", "мужской стиль",
    "базовый гардероб мужчины", "outfit ideas men", "menswear", "streetwear men",
    "мужской юмор", "pov парень", "grwm men"]
};
// SOURCES:END
const hashtags = SOURCES.hashtags;
const searchQueries = SOURCES.searchQueries;
```

и в `return` поменять `results_per_page: 8` → `results_per_page: 20`.

- [ ] **Step 2: Normalize — настоящий source_query**

В `Normalize-TikTok-Raw-Rows.js` заменить строку

```javascript
      source_query: 'managed_tiktok_keywords',
```

на

```javascript
      source_query: (raw.searchHashtag && raw.searchHashtag.name)
        ? 'hashtag:#' + String(raw.searchHashtag.name).replace(/^#/, '')
        : (raw.searchQuery ? 'query:' + raw.searchQuery : 'unknown'),
      is_ad: Boolean(raw.isAd || raw.isSponsored),
      text_language: String(raw.textLanguage || ''),
```

(`is_ad`/`text_language` — служебные поля для гейта; в Sheets колонок с такими
именами нет, Write-узел с маппингом по заголовкам их игнорирует.)

- [ ] **Step 3: Новый узел Ingestion-Gate.js (полный файл)**

```javascript
// Ingestion Gate: дропает мусор ДО скачивания субтитров и записи в Sheets.
// Фейл-открытый: при пустом конфиге пропускает всё, кроме рекламы.
const CFG = {
  maxAgeDays: 30,
  minViewsStale: 500,   // применяется к постам старше staleAfterDays
  staleAfterDays: 14,
  maxCaptionHashtags: 9,
  languagesAllow: ['ru', 'en', 'un', ''],
  blockAuthors: ['wbinsidik', 'wb.naxodkaaa', 'wb.kiko2', 'baza.store.kz',
    'kingsman.premium', 'dom_sumok42', 'alex_fitrend'],
  blockAuthorPatterns: [/^wb[._]/i, /^вб[._]/i],
};

const drops = {};
function drop(reason) { drops[reason] = (drops[reason] || 0) + 1; return null; }

const kept = $input.all().filter((item) => {
  const row = item.json || {};
  if (row.is_ad) return drop('ad');
  const author = String(row.author || '').toLowerCase();
  if (CFG.blockAuthors.includes(author)) return drop('blocked_author');
  if (CFG.blockAuthorPatterns.some((re) => re.test(author))) return drop('blocked_author');
  const lang = String(row.text_language || '').toLowerCase();
  if (CFG.languagesAllow.length && !CFG.languagesAllow.includes(lang)) return drop('language');
  const tags = (String(row.caption || '').match(/#\S+/g) || []).length;
  if (tags > CFG.maxCaptionHashtags) return drop('hashtag_stuffing');
  const created = row.created_at ? new Date(row.created_at) : null;
  const ageDays = created && !Number.isNaN(created.getTime())
    ? (Date.now() - created.getTime()) / 864e5 : null;
  if (ageDays !== null && ageDays > CFG.maxAgeDays) return drop('too_old');
  const views = Number(row.views) || 0;
  if (ageDays !== null && ageDays > CFG.staleAfterDays && views < CFG.minViewsStale) {
    return drop('stale_low_views');
  }
  return true;
});

console.log('Ingestion Gate:', JSON.stringify({
  input: $input.all().length, kept: kept.length, drops,
}));
return kept;
```

- [ ] **Step 4: workflow.json — вставить узел и переключить связи**

В `n8n/cf01-tiktok/workflow.json`:
1. В `nodes` добавить (позицию взять как у «Normalize TikTok Raw Rows» + [220, 0]):
```json
{
  "parameters": {"jsCode": ""},
  "type": "n8n-nodes-base.code",
  "typeVersion": 2,
  "position": [<x+220>, <y>],
  "id": "cf-ingestion-gate-tt",
  "name": "Ingestion Gate"
}
```
(`jsCode` пустой — его заполнит push-n8n из `code/Ingestion-Gate.js`.)
2. В `connections`: у `"Normalize TikTok Raw Rows"` заменить целевой узел
   (сейчас — вход цепочки субтитров, проверить фактическое имя в JSON, ожидается
   `Has TikTok Subtitle URL?`) на `Ingestion Gate`; добавить
   `"Ingestion Gate": {"main": [[{"node": "Has TikTok Subtitle URL?", "type": "main", "index": 0}]]}`.

- [ ] **Step 5: Push + сквозная проба**

```bash
.venv/Scripts/python -m cf push-n8n n8n/cf01-tiktok
```
Затем ручной запуск воркфлоу (POST на webhook из cf.config.json → dashboard.workflows.raw[0]
или кнопка ▶ на дашборде), дождаться завершения, проверить свежие строки:

```bash
.venv/Scripts/python -m cf read raw_tiktok --out agent-runtime/tmp-probe.json
```
Ожидание: у строк с сегодняшним `collected_at` source_query начинается с `hashtag:#`
или `query:`; нет постов старше 30 дней; нет авторов из блок-листа.

- [ ] **Step 6: Commit** — `git add n8n/cf01-tiktok && git commit -m "feat(n8n): CF 01 TikTok — атрибуция, ingestion gate, охват x2.5"`

---

### Task 8: CF 01 Instagram — атрибуция и гейт

**Files:**
- Modify: `n8n/cf01-instagram/code/Prepare-Instagram-Reel-Transcript-Input.js`
- Modify: `n8n/cf01-instagram/code/Normalize-Instagram-Raw-Rows.js`
- Create: `n8n/cf01-instagram/code/Ingestion-Gate-IG.js`
- Modify: `n8n/cf01-instagram/workflow.json`

Поток: Reels Raw Fetch (hashtag-scraper, метаданные) → **Ingestion Gate IG** →
Prepare (строит url→hashtag map, лимит 10→30) → Reel Transcript Fetch → Normalize
(source_query из map).

- [ ] **Step 1: Ingestion-Gate-IG.js (полный файл)**

```javascript
// IG Ingestion Gate: работает на выходе hashtag-scraper (метаданные),
// ДО дорогого reel-scraper. Поля другие, чем в TikTok-гейте.
const CFG = {
  maxAgeDays: 30,
  minViewsStale: 500,
  staleAfterDays: 14,
  maxCaptionHashtags: 9,
  blockAuthors: ['meijorofficial', 'dom_sumok42', 'renaciuki'],
  blockAuthorPatterns: [/^wb[._]/i, /shop$/i, /store$/i],
};

const drops = {};
function drop(reason) { drops[reason] = (drops[reason] || 0) + 1; return null; }

const items = $input.all().flatMap((item) => {
  if (Array.isArray(item.json)) return item.json.map((json) => ({ json }));
  return [item];
});

const kept = items.filter((item) => {
  const raw = item.json || {};
  const author = String(raw.ownerUsername || raw.username || '').toLowerCase();
  if (CFG.blockAuthors.includes(author)) return drop('blocked_author');
  if (CFG.blockAuthorPatterns.some((re) => re.test(author))) return drop('blocked_author');
  const caption = String(raw.caption || '');
  if ((caption.match(/#\S+/g) || []).length > CFG.maxCaptionHashtags) return drop('hashtag_stuffing');
  const ts = raw.timestamp ? new Date(raw.timestamp) : null;
  const ageDays = ts && !Number.isNaN(ts.getTime()) ? (Date.now() - ts.getTime()) / 864e5 : null;
  if (ageDays !== null && ageDays > CFG.maxAgeDays) return drop('too_old');
  const views = Number(raw.videoViewCount || raw.videoPlayCount) || 0;
  if (views && ageDays !== null && ageDays > CFG.staleAfterDays && views < CFG.minViewsStale) {
    return drop('stale_low_views');
  }
  return true;
});

console.log('IG Ingestion Gate:', JSON.stringify({ input: items.length, kept: kept.length, drops }));
return kept;
```

- [ ] **Step 2: Prepare — map url→hashtag + лимит 30**

В `Prepare-Instagram-Reel-Transcript-Input.js`:
- после сбора `rows` добавить построение карты (источник тега: `inputUrl` вида
  `…/explore/tags/<tag>/` либо поле `queryTag`/`hashtag` итема):

```javascript
function sourceTag(raw) {
  const explore = String(raw.inputUrl || '').match(/explore\/tags\/([^/?#]+)/i);
  if (explore) return decodeURIComponent(explore[1]);
  return String(raw.queryTag || raw.hashtag || '').replace(/^#/, '');
}
const tagByUrl = {};
for (const raw of rows) {
  const url = reelUrl(raw);
  const tag = sourceTag(raw);
  if (url && tag && !tagByUrl[url]) tagByUrl[url] = tag;
}
```

- заменить `const urls = …slice(0, 10);` на `…slice(0, 30);`
- в `return` добавить поле `tag_by_url: tagByUrl,`.

- [ ] **Step 3: Normalize — source_query из карты**

В `Normalize-Instagram-Raw-Rows.js` заменить

```javascript
const sourceQuery = $('Prepare Instagram Reel Transcript Input').first().json.source_query || $('Normalize Instagram Hashtags').first().json.source_query || '';
```

на

```javascript
const tagByUrl = $('Prepare Instagram Reel Transcript Input').first().json.tag_by_url || {};
```

и в объекте строки заменить `source_query: sourceQuery,` на

```javascript
      source_query: tagByUrl[url] ? 'hashtag:#' + tagByUrl[url] : 'unknown',
```

(переменная `url` уже вычислена выше в map-колбэке).

- [ ] **Step 4: workflow.json — узел и связи**

Добавить узел `Ingestion Gate IG` (code, typeVersion 2, id `cf-ingestion-gate-ig`,
позиция = Reels Raw Fetch + [220, 0]). Связи: `Apify Instagram Reels Raw Fetch` →
`Ingestion Gate IG` → `Prepare Instagram Reel Transcript Input` (заменить прежнюю
прямую связь).

- [ ] **Step 5: Push + проба** — `cf push-n8n n8n/cf01-instagram`, запуск через webhook
  `dashboard.workflows.raw[1]`, проверка: свежие IG-строки имеют `source_query=hashtag:#…`.
- [ ] **Step 6: Commit** — `git commit -m "feat(n8n): CF 01 Instagram — per-reel атрибуция и гейт"`

---

### Task 9: Бэкфилл истории + первый реальный source-stats

- [ ] **Step 1:**
```bash
.venv/Scripts/python -m cf backfill-attribution raw_tiktok --dry-run   # санити: ~1400+ строк
.venv/Scripts/python -m cf backfill-attribution raw_tiktok
.venv/Scripts/python -m cf backfill-attribution raw_instagram          # IG почти весь unknown — ок
```
- [ ] **Step 2:**
```bash
.venv/Scripts/python -m cf source-stats raw_tiktok
.venv/Scripts/python -m cf source-stats raw_instagram
```
Ожидание: таблица по ~24 TikTok-источникам с target_yield; отчёты в
`agent-runtime/source-stats/`. Итог показать оператору в ответе.
- [ ] **Step 3: Commit не нужен** (артефакты в agent-runtime не версионируются).

---

### Task 10: Вкладка CF Seeds + `cf export-seeds`

**Files:**
- Modify: `cf.config.json` (tabs: `"seeds": "CF Seeds"`)
- Modify: `src/cf/sheets.py` (метод `ensure_tab`)
- Modify: `tests/fakes.py` (ensure_tab в FakeSheets)
- Modify: `src/cf/cli.py`
- Test: `tests/test_cli_seeds.py`

- [ ] **Step 1: Падающий тест**

```python
import argparse
import json

from cf.cli import cmd_export_seeds

from tests.fakes import FakeSheets


def test_export_seeds_appends_new_urls_only(tmp_path, capsys):
    formula = {"name": "f1", "niche": "мужские-образы",
               "evidence": {"source_urls": [
                   "https://www.tiktok.com/@a/video/1",
                   "https://www.instagram.com/reel/X/",   # не tiktok — пропустить
                   "https://www.tiktok.com/@b/video/2",
               ]}}
    fpath = tmp_path / "f1.json"
    fpath.write_text(json.dumps(formula, ensure_ascii=False), encoding="utf-8")
    index = {"approved": [{"name": "f1", "niche": "мужские-образы",
                           "path": str(fpath).replace("\\", "/"), "version": 1}]}
    ipath = tmp_path / "index.json"
    ipath.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    sheets = FakeSheets({"seeds": [
        {"seed_url": "https://www.tiktok.com/@a/video/1", "active": "TRUE"},
    ]})
    args = argparse.Namespace(index=str(ipath))
    assert cmd_export_seeds(sheets, args) == 0
    urls = [r["seed_url"] for r in sheets.tables["seeds"]]
    assert urls == ["https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@b/video/2"]
    assert sheets.tables["seeds"][1]["niche"] == "мужские-образы"
```

- [ ] **Step 2: Падает.**

- [ ] **Step 3: Реализация**

`Sheets.ensure_tab` (в sheets.py):

```python
    def ensure_tab(self, tab_key, headers):
        """Создать вкладку с заголовками, если её нет. Возвращает True, если создали."""
        title = self.config["tabs"][tab_key]

        def op():
            book = self.client.open_by_key(self.config["spreadsheet_id"])
            if title in [ws.title for ws in book.worksheets()]:
                return False
            ws = book.add_worksheet(title=title, rows=200, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")
            return True

        return with_retry(op, base_delay=self.retry_delay, label=f"ensure {tab_key}")
```

FakeSheets:

```python
    def ensure_tab(self, tab_key, headers):
        if tab_key in self.tables:
            return False
        self.tables[tab_key] = []
        return True
```

cli.py:

```python
def cmd_export_seeds(sheets, args):
    index = read_json(args.index)
    sheets.ensure_tab("seeds", ["seed_url", "seed_type", "niche", "added_at", "active"])
    existing = {str(r.get("seed_url", "")).strip() for r in sheets.read_rows("seeds")}
    added = 0
    for entry in index.get("approved", []):
        formula = read_json(entry["path"])
        for url in formula.get("evidence", {}).get("source_urls", []):
            url = str(url).strip()
            if "tiktok.com" in url and url not in existing:
                sheets.append_row("seeds", {
                    "seed_url": url, "seed_type": "winner",
                    "niche": formula.get("niche", ""),
                    "added_at": now_iso(), "active": "TRUE",
                })
                existing.add(url)
                added += 1
    print(f"seeds: добавлено {added}, всего {len(existing)}")
    return 0
```

Сабпарсер:

```python
    sp = sub.add_parser("export-seeds", help="Выгрузить winner-URL из approved формул в CF Seeds")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_export_seeds)
```

cf.config.json: в `tabs` добавить `"seeds": "CF Seeds"`.

- [ ] **Step 4: Тесты зелёные, полный прогон зелёный.**
- [ ] **Step 5: Реальный запуск** — `.venv/Scripts/python -m cf export-seeds`
  (создаст вкладку, добавит 7 URL формулы short-styling-idea-reel v2, из них tiktok — часть).
- [ ] **Step 6: Commit** — `git commit -m "feat: CF Seeds + cf export-seeds"`

---

### Task 11: Воркфлоу CF 01b Snowball TikTok

**Files:**
- Create: `n8n/cf01b-snowball/workflow.json`
- Create: `n8n/cf01b-snowball/code/Build-Snowball-Input.js`
- Create: `n8n/cf01b-snowball/code/*.js` — копии Normalize/Gate/Attach из cf01-tiktok

Сборка: скопировать `n8n/cf01-tiktok/workflow.json` как основу и переделать:

- [ ] **Step 1: Узлы**
1. `Weekly Snowball Sunday 09:00` (scheduleTrigger, cron `0 9 * * 0`) + `Manual Start`.
2. `Read CF Seeds` (googleSheets, operation read; documentId = spreadsheet_id из
   cf.config.json, sheetName `CF Seeds`; блок `credentials` скопировать из узла
   `Write CF Raw TikTok` в `n8n/cf01-tiktok/workflow.json`).
3. `Build Snowball Input` (code):

```javascript
// Один item на активную затравку → по Apify-вызову на seed (атрибуция по seed).
const seeds = $input.all()
  .map((item) => item.json)
  .filter((row) => String(row.active).toUpperCase() === 'TRUE'
    && /tiktok\.com/i.test(String(row.seed_url || '')));
return seeds.map((row) => ({
  json: {
    platform: 'tiktok',
    seed_url: String(row.seed_url).trim(),
    seed_niche: String(row.niche || ''),
    collected_at: new Date().toISOString(),
  },
}));
```

4. `Apify Snowball Fetch` (httpRequest, как Apify TikTok Raw Fetch, но body:
   `postURLs = [{{ $json.seed_url }}]`, `scrapeRelatedVideos = true`,
   `resultsPerPage = 15`, без hashtags/searchQueries/searchSection/date-фильтров;
   креденшл Bearer тот же).
5. `Normalize Snowball Rows` (code) — копия `Normalize-TikTok-Raw-Rows.js` c одной
   правкой source_query:

```javascript
      source_query: 'snowball:' + ($('Build Snowball Input').item.json.seed_url || 'unknown'),
```

6. `Ingestion Gate` — тот же код, что в cf01-tiktok (файл скопировать).
7. Цепочка субтитров и `Write CF Raw TikTok` — скопировать из cf01-tiktok as-is.

- [ ] **Step 2: Создать** — `.venv/Scripts/python -m cf push-n8n n8n/cf01b-snowball --create`
  (id запишется в workflow.json), затем в UI n8n проверить и активировать
  (API create создаёт неактивным — активацию сделать через UI или POST …/activate).
- [ ] **Step 3: Проба** — ручной запуск в n8n UI; проверить в Sheets строки с
  `source_query=snowball:…`, прошедшие гейт.
- [ ] **Step 4: Commit** — `git commit -m "feat(n8n): CF 01b Snowball — related videos от winners"`

---

### Task 12: Схема source-proposal + `cf apply-sources`

**Files:**
- Create: `schemas/source-proposal.schema.json`
- Modify: `src/cf/validate.py` — в словарь `SCHEMAS` добавить строку
  `"source-proposal": "schemas/source-proposal.schema.json",`
- Modify: `src/cf/cli.py` — в сабпарсере `validate` расширить choices:
  `choices=["patterns", "formula", "brief", "proposal", "source-proposal"]`
- Modify: `src/cf/cli.py` (валидатор choices + cmd_apply_sources)
- Create: `src/cf/sourcepatch.py` (правка SOURCES-блока — чистая функция)
- Test: `tests/test_sourcepatch.py`

- [ ] **Step 1: Схема**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "source-proposal",
  "type": "object",
  "required": ["platform", "generated_at", "status", "remove", "add"],
  "properties": {
    "platform": {"enum": ["tiktok", "instagram"]},
    "generated_at": {"type": "string"},
    "status": {"enum": ["pending", "approved", "rejected"]},
    "applied_at": {"type": "string"},
    "remove": {"type": "array", "items": {
      "type": "object", "required": ["source", "reason", "stats"],
      "properties": {
        "source": {"type": "string"},
        "reason": {"type": "string", "minLength": 10},
        "stats": {"type": "object"}
      }}},
    "add": {"type": "array", "items": {
      "type": "object", "required": ["source", "kind", "evidence"],
      "properties": {
        "source": {"type": "string"},
        "kind": {"enum": ["hashtag", "query"]},
        "evidence": {"type": "string", "minLength": 10}
      }}}
  }
}
```

- [ ] **Step 2: Падающий тест sourcepatch**

```python
import pytest

from cf.sourcepatch import apply_proposal_to_sources, parse_sources_block

JS = '''const x = 1;
// SOURCES:BEGIN — правится только через cf apply-sources (proposal-цикл)
const SOURCES = {"hashtags": ["a", "b"], "searchQueries": ["q1"]};
// SOURCES:END
const hashtags = SOURCES.hashtags;'''


def test_parse_sources_block():
    assert parse_sources_block(JS) == {"hashtags": ["a", "b"], "searchQueries": ["q1"]}


def test_apply_remove_and_add():
    proposal = {"remove": [{"source": "hashtag:#b"}, {"source": "query:q1"}],
                "add": [{"source": "hashtag:#new", "kind": "hashtag"},
                        {"source": "query:новый запрос", "kind": "query"}]}
    out = apply_proposal_to_sources(JS, proposal)
    assert parse_sources_block(out) == {
        "hashtags": ["a", "new"], "searchQueries": ["новый запрос"]}
    assert out.startswith("const x = 1;")          # код вокруг блока не тронут
    assert "const hashtags = SOURCES.hashtags;" in out


def test_missing_markers_raise():
    with pytest.raises(ValueError):
        parse_sources_block("const SOURCES = {};")
```

- [ ] **Step 3: Реализация `src/cf/sourcepatch.py`**

```python
import json
import re

BLOCK = re.compile(r"(// SOURCES:BEGIN[^\n]*\nconst SOURCES = )(\{.*?\})(;\n// SOURCES:END)",
                   re.DOTALL)


def parse_sources_block(js_text):
    m = BLOCK.search(js_text)
    if not m:
        raise ValueError("SOURCES:BEGIN/END markers not found")
    return json.loads(m.group(2))


def _strip(source):
    """'hashtag:#x' -> 'x'; 'query:q' -> 'q'."""
    kind, _, value = source.partition(":")
    return value.lstrip("#") if kind == "hashtag" else value


def apply_proposal_to_sources(js_text, proposal):
    sources = parse_sources_block(js_text)
    removed_tags = {_strip(r["source"]) for r in proposal.get("remove", [])
                    if r["source"].startswith("hashtag:")}
    removed_queries = {_strip(r["source"]) for r in proposal.get("remove", [])
                       if r["source"].startswith("query:")}
    hashtags = [h for h in sources.get("hashtags", []) if h not in removed_tags]
    queries = [q for q in sources.get("searchQueries", []) if q not in removed_queries]
    for a in proposal.get("add", []):
        value = _strip(a["source"])
        target = hashtags if a["kind"] == "hashtag" else queries
        if value not in target:
            target.append(value)
    new_block = json.dumps({"hashtags": hashtags, "searchQueries": queries},
                           ensure_ascii=False)
    return BLOCK.sub(lambda m: m.group(1) + new_block + m.group(3), js_text)
```

- [ ] **Step 4: cmd_apply_sources в cli.py**

```python
def cmd_apply_sources(sheets, args):
    from cf.sourcepatch import apply_proposal_to_sources
    from cf.validate import validate_json_file
    errors = validate_json_file("source-proposal", args.proposal)
    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    proposal = read_json(args.proposal)
    if proposal["status"] != "approved":
        print(f"proposal не approved (status={proposal['status']}) — сначала ревью оператора",
              file=sys.stderr)
        return 1
    js_path = Path({"tiktok": "n8n/cf01-tiktok/code/Build-TikTok-Actor-Input.js",
                    "instagram": "n8n/cf01-instagram/code/Build-Instagram-Search-Input.js"
                    }[proposal["platform"]])
    js_path.write_text(apply_proposal_to_sources(js_path.read_text(encoding="utf-8"),
                                                 proposal),
                       encoding="utf-8", newline="\n")
    proposal["applied_at"] = now_iso()
    write_json_atomic(args.proposal, proposal)
    print(f"SOURCES обновлён в {js_path}")
    print("дальше: cf push-n8n n8n/cf01-" + proposal["platform"] + " и git commit")
    return 0
```

Сабпарсер:

```python
    sp = sub.add_parser("apply-sources", help="Применить approved source-proposal к n8n-спискам")
    sp.add_argument("proposal", help="JSON-файл proposal (status=approved)")
    sp.set_defaults(func=cmd_apply_sources)
```

Примечание: IG Build-Input сейчас без SOURCES-маркеров — добавить их туда же в этой
задаче (обернуть seedHashtags/searchQueries аналогично Task 7 Step 1, ключи JSON:
`hashtags` ← seedHashtags, `searchQueries`), и запушить `cf push-n8n n8n/cf01-instagram`.

- [ ] **Step 5: Тесты + полный прогон зелёные.**
- [ ] **Step 6: Commit** — `git commit -m "feat: source-proposal схема + cf apply-sources"`

---

### Task 13: Агент `/cf-tune-sources`

**Files:**
- Create: `prompts/agents/source-tuner.md`
- Create: `.claude/commands/cf-tune-sources.md`

- [ ] **Step 1: prompts/agents/source-tuner.md**

```markdown
# Source Tuner

Роль: еженедельный скоринг источников сбора и proposal на обновление списков
хэштегов/запросов. Списки НЕ меняешь сам — только proposal (железное правило №3).

## Порядок
1. `.venv/Scripts/python -m cf source-stats raw_tiktok` и `raw_instagram`
   (свежие отчёты в agent-runtime/source-stats/).
2. Кандидаты на удаление: источники с rows >= 20 и target_yield < 0.1 —
   каждый с цифрами (rows, target_niche_rows, views_1000_plus).
3. Кандидаты на добавление: candidate_hashtags с count >= 3, которых нет в текущем
   SOURCES-блоке (n8n/cf01-*/code/Build-*.js) и которые по смыслу — целевая ниша
   (не бренд-мусор, не generic #fyp-подобные).
4. Если менять нечего (все yield >= 0.1, кандидатов < 2) — честно: proposal не
   создавать, написать «insufficient_evidence».
5. Proposal → `proposals/YYYY-MM-DD-sources-<platform>.json` по
   `schemas/source-proposal.schema.json`, status=pending.
   Валидация: `python -m cf validate source-proposal <файл>`.
6. Показать оператору таблицу «удалить/добавить/почему» и путь к proposal.

## После одобрения оператором (не твоя зона — оператор запускает сам)
`cf apply-sources <proposal>` → `cf push-n8n n8n/cf01-<platform>` → git commit.

## Правила
- Evidence-first: каждый remove/add обязан ссылаться на цифры source-stats.
- Не удалять источник, у которого есть хоть один winner в паттернах/формулах.
- Максимум 5 удалений и 5 добавлений за один proposal — маленькие шаги.
```

- [ ] **Step 2: .claude/commands/cf-tune-sources.md**

```markdown
---
description: Source Tuner - скоринг источников сбора, proposal на обновление списков
---
Следуй промпту prompts/agents/source-tuner.md. Аргументы: $ARGUMENTS
(по умолчанию обе вкладки). Запуск залогируй: python -m cf log-run --agent source-tuner ...
```

- [ ] **Step 3: Commit** — `git commit -m "feat: агент /cf-tune-sources"`

---

### Task 14: Документация и закрытие

**Files:**
- Modify: `docs/n8n-integration.md`, `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-07-15-smart-ingestion-design.md` (наблюдаемость гейта)
- Create: `.claude/memory/decisions/2026-07-15-smart-ingestion.md` + строка в `.claude/memory/MEMORY.md`

- [ ] **Step 1: docs/n8n-integration.md** — добавить раздел:
  воркфлоу CF 01/01b версионируются в `n8n/` (pull-n8n/push-n8n; правки в UI не делать
  либо сразу забирать pull'ом); гейт и его константы; ритм-таблицу дополнить строками
  «Snowball | n8n CF 01b | еженедельно (вс)» и «Тюнинг источников | /cf-tune-sources |
  еженедельно».
- [ ] **Step 2: CLAUDE.md** — в список подкоманд CLI добавить: backfill-attribution,
  source-stats, pull-n8n, push-n8n, export-seeds, apply-sources; в slash-команды —
  /cf-tune-sources.
- [ ] **Step 3: Спека** — фразу про «счётчики в webhook-ответ дашборда» заменить на
  «счётчики дропов в execution-логе n8n (console.log узла Ingestion Gate)».
- [ ] **Step 4: Память** — решение с датой: что включено (гейт/атрибуция/воронка/
  snowball/петля), пороги гейта, договорённость «n8n правится только через репозиторий».
- [ ] **Step 5: Финальная проверка** — `pytest -q` зелёный; `cf status` работает;
  повторный `cf profile raw_tiktok` через сутки после первого сбора — контроль эффекта.
- [ ] **Step 6: Commit** — `git commit -m "docs: smart ingestion — интеграция, команды, решение"`
