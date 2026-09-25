# Секция «Почему этот бриф» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Статус: исполнено и влито в master** (merge a27d0f6, 2026-07-15). Чекбоксы в ходе исполнения не велись.

**Goal:** Карточка брифа показывает цепочку происхождения: формула (с «почему») → паттерны (формулировка, метрики, evidence) → референсы с пометками; референсы вне evidence и пропавшие паттерны видны честно.

**Architecture:** Чистая функция `brief_origin(selected, references, root)` в `src/cf/dashboard/sections.py` (читает `formulas/_approved/` и `agent-runtime/patterns/` через существующий `_load_json`); `app.py::_briefs_context` кладёт результат в контекст; `briefs.html` рендерит секцию вместо блока «РЕФЕРЕНСЫ». Данные, агентов и Sheets не трогаем.

**Tech Stack:** Python 3.13, FastAPI + Jinja2, pytest. Windows: тесты гонять из корня `<local-path>` командой `.venv\Scripts\python.exe -m pytest <файл> -q`.

**Спека:** `docs/superpowers/specs/2026-07-15-cf-brief-origin-design.md`

**Формат данных (проверено на живых файлах):**

- Бриф (строка Sheets): `formula_id` — строка (`short-styling-idea-reel`), `source_pattern_ids` — строка с запятыми (`"id1, id2"`), `references` — уже распарсены роутом в список URL.
- `formulas/_approved/index.json`: `{"approved": [{"name", "niche", "path", "version", "approved_at"}]}`; файл формулы по `path` (относительно корня репо): `problem_definition` (str), `confidence` (str), прочее не нужно.
- `agent-runtime/patterns/*.json`: `{"meta": {"source_tab": "raw_tiktok"|"raw_instagram", ...}, "patterns": [{"pattern_id", "description", "confidence", "evidence": {"source_urls": [...], "avg_views": float, "avg_er": float}}]}`. Имена файлов начинаются с даты — сортировка имён по убыванию даёт «свежайший файл побеждает».

---

## File Structure

- Modify: `src/cf/dashboard/sections.py` — `brief_origin` + приватные хелперы (единственное место логики).
- Modify: `src/cf/dashboard/app.py` — импорт + одна строка в `_briefs_context`.
- Modify: `src/cf/dashboard/templates/briefs.html` — секция вместо блока «РЕФЕРЕНСЫ».
- Modify: `tests/test_dashboard_sections.py` — юнит-тесты `brief_origin`.
- Modify: `tests/test_dashboard_routes.py` — рендер секции на странице.

---

### Task 1: `brief_origin` в sections.py

**Files:**
- Modify: `src/cf/dashboard/sections.py`
- Test: `tests/test_dashboard_sections.py`

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_dashboard_sections.py` (проверь импорты вверху файла: нужны `import json` и `from cf.dashboard.sections import brief_origin` — добавь к существующим импортам, не дублируя):

```python
def _origin_repo(tmp_path):
    """Мини-репо: формула + два файла паттернов (старый и свежий дубль)."""
    fdir = tmp_path / "formulas" / "_approved"
    fdir.mkdir(parents=True)
    (fdir / "index.json").write_text(json.dumps({"approved": [{
        "name": "short-styling-idea-reel", "niche": "мужские-образы",
        "path": "formulas/мужские-образы/short-styling-idea-reel.json",
        "version": 2, "approved_at": "2026-07-14T17:07:59+00:00"}]},
        ensure_ascii=False), encoding="utf-8")
    ffile = tmp_path / "formulas" / "мужские-образы"
    ffile.mkdir(parents=True)
    (ffile / "short-styling-idea-reel.json").write_text(json.dumps({
        "problem_definition": "Мега-вирусные форматы проигрывают личному контенту.",
        "confidence": "medium"}, ensure_ascii=False), encoding="utf-8")
    pdir = tmp_path / "agent-runtime" / "patterns"
    pdir.mkdir(parents=True)
    (pdir / "2026-07-10-старый.json").write_text(json.dumps({
        "meta": {"source_tab": "raw_tiktok"},
        "patterns": [{"pattern_id": "tt-01", "description": "УСТАРЕВШАЯ формулировка",
                      "confidence": "low",
                      "evidence": {"source_urls": ["https://t.tt/old"],
                                   "avg_views": 1.0, "avg_er": 0.01}}]},
        ensure_ascii=False), encoding="utf-8")
    (pdir / "2026-07-14-свежий.json").write_text(json.dumps({
        "meta": {"source_tab": "raw_tiktok"},
        "patterns": [{"pattern_id": "tt-01", "description": "личный образ автора",
                      "confidence": "medium",
                      "evidence": {"source_urls": ["https://t.tt/a/", "https://t.tt/b"],
                                   "avg_views": 108118.0, "avg_er": 0.16}}]},
        ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_brief_origin_full_chain(tmp_path):
    root = _origin_repo(tmp_path)
    selected = {"formula_id": "short-styling-idea-reel",
                "source_pattern_ids": "tt-01, ig-99"}
    origin = brief_origin(selected, ["https://t.tt/a", "https://x.com/чужой"], root=root)
    f = origin["formula"]
    assert f["name"] == "short-styling-idea-reel" and f["missing"] is False
    assert f["version"] == 2 and f["approved_at"] == "2026-07-14"
    assert f["problem"].startswith("Мега-вирусные")
    (p,) = origin["patterns"]
    assert p["description"] == "личный образ автора"      # свежий файл победил
    assert p["platform"] == "TikTok"
    assert p["avg_views"] == 108118.0
    # матчинг терпит трейлинг-слэш с обеих сторон
    assert p["evidence"] == [{"url": "https://t.tt/a/", "is_reference": True},
                             {"url": "https://t.tt/b", "is_reference": False}]
    assert origin["missing_pattern_ids"] == ["ig-99"]
    assert origin["unmatched_refs"] == ["https://x.com/чужой"]


def test_brief_origin_none_when_agent_wrote_nothing(tmp_path):
    assert brief_origin({"formula_id": "", "source_pattern_ids": " "},
                        [], root=tmp_path) is None
    assert brief_origin(None, [], root=tmp_path) is None


def test_brief_origin_missing_formula_file(tmp_path):
    origin = brief_origin({"formula_id": "нет-такой", "source_pattern_ids": ""},
                          [], root=tmp_path)
    assert origin["formula"] == {"name": "нет-такой", "missing": True}
    assert origin["patterns"] == [] and origin["missing_pattern_ids"] == []


def test_brief_origin_broken_pattern_json_does_not_crash(tmp_path):
    root = _origin_repo(tmp_path)
    (root / "agent-runtime" / "patterns" / "2026-07-15-битый.json").write_text(
        "{оборванный", encoding="utf-8")
    origin = brief_origin({"formula_id": "", "source_pattern_ids": "tt-01"},
                          [], root=root)
    assert origin["patterns"][0]["pattern_id"] == "tt-01"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_sections.py -q -k brief_origin`
Expected: 4 ошибки ImportError/FAIL (`brief_origin` не существует).

- [ ] **Step 3: Реализация в sections.py**

Добавить в конец `src/cf/dashboard/sections.py` (использует уже существующие `Path` и `_load_json`):

```python
PLATFORM_LABELS = {"raw_tiktok": "TikTok", "raw_instagram": "Instagram"}
ORIGIN_PROBLEM_LIMIT = 300


def _norm_url(url):
    return str(url or "").strip().rstrip("/")


def _shorten(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _origin_formula(formula_id, root):
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    entry = next((e for e in index.get("approved", [])
                  if str(e.get("name")) == formula_id), None)
    detail = _load_json(root / str(entry.get("path", ""))) if entry else None
    if detail is None:
        return {"name": formula_id, "missing": True}
    return {
        "name": formula_id,
        "missing": False,
        "version": entry.get("version", ""),
        "niche": entry.get("niche", ""),
        "approved_at": str(entry.get("approved_at", ""))[:10],
        "confidence": detail.get("confidence", ""),
        "problem": _shorten(detail.get("problem_definition", ""), ORIGIN_PROBLEM_LIMIT),
    }


def _origin_patterns(pattern_ids, refs_normed, root):
    found = {}
    patterns_dir = root / "agent-runtime" / "patterns"
    if patterns_dir.is_dir():
        # имена начинаются с даты: обратная сортировка = свежайший файл побеждает
        for path in sorted(patterns_dir.glob("*.json"), reverse=True):
            data = _load_json(path)
            if not data:
                continue
            tab = str((data.get("meta") or {}).get("source_tab", ""))
            for p in data.get("patterns", []) or []:
                pid = str(p.get("pattern_id", ""))
                if pid in pattern_ids and pid not in found:
                    ev = p.get("evidence", {}) or {}
                    found[pid] = {
                        "pattern_id": pid,
                        "description": p.get("description", ""),
                        "confidence": p.get("confidence", ""),
                        "platform": PLATFORM_LABELS.get(tab, tab),
                        "avg_views": ev.get("avg_views"),
                        "avg_er": ev.get("avg_er"),
                        "evidence": [
                            {"url": u, "is_reference": _norm_url(u) in refs_normed}
                            for u in ev.get("source_urls", []) or []],
                    }
    return ([found[pid] for pid in pattern_ids if pid in found],
            [pid for pid in pattern_ids if pid not in found])


def brief_origin(selected, references, root="."):
    """Цепочка происхождения брифа: формула → паттерны → evidence.

    None — агент не записал происхождение (старые брифы). Чтение файлов
    best-effort: битые/отсутствующие файлы не роняют страницу.
    """
    if not selected:
        return None
    formula_id = str(selected.get("formula_id") or "").strip()
    pattern_ids = [p.strip() for p in
                   str(selected.get("source_pattern_ids") or "").split(",")
                   if p.strip()]
    if not formula_id and not pattern_ids:
        return None
    root = Path(root)
    refs_normed = {_norm_url(u) for u in references or []}
    patterns, missing = _origin_patterns(pattern_ids, refs_normed, root)
    matched = {_norm_url(e["url"])
               for p in patterns for e in p["evidence"] if e["is_reference"]}
    return {
        "formula": _origin_formula(formula_id, root) if formula_id else None,
        "patterns": patterns,
        "missing_pattern_ids": missing,
        "unmatched_refs": [u for u in (references or [])
                           if _norm_url(u) not in matched],
    }
```

- [ ] **Step 4: Тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_sections.py -q`
Expected: все PASS (старые + 4 новых).

- [ ] **Step 5: Commit**

```bash
git add src/cf/dashboard/sections.py tests/test_dashboard_sections.py
git commit -m "dashboard: brief_origin — цепочка происхождения брифа из файлов репо"
```

(в конце сообщения, после пустой строки: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`)

---

### Task 2: Подключение к странице брифов + секция в шаблоне

**Files:**
- Modify: `src/cf/dashboard/app.py` (импорт, `_briefs_context`)
- Modify: `src/cf/dashboard/templates/briefs.html`
- Test: `tests/test_dashboard_routes.py`

- [ ] **Step 1: Написать падающий route-тест**

В `tests/test_dashboard_routes.py` — изучи, как существующие тесты страницы брифов строят клиент (есть фикстуры/хелперы с FakeSheets; брифы лежат во вкладке "briefs"). Добавь тест по этому же образцу; суть:

```python
def test_briefs_page_renders_origin_section(tmp_path, ...):  # сигнатура по образцу соседних тестов
    # 1) в FakeSheets кладём бриф с происхождением:
    #    {"brief_id": "b-1", "hook": "х", "script": "с", "review_status": "",
    #     "human_status": "pending", "formula_id": "short-styling-idea-reel",
    #     "source_pattern_ids": "tt-01",
    #     "references": "https://t.tt/a; https://x.com/чужой"}
    # 2) файлы формулы/паттернов — переиспользуй хелпер _origin_repo из
    #    tests/test_dashboard_sections.py (импортируй его), root=tmp_path
    # 3) create_app(..., lab_root=tmp_path)  — как в тестах /lab, если они есть
    html = client.get("/briefs?id=b-1").text
    assert "ПОЧЕМУ ЭТОТ БРИФ" in html
    assert "личный образ автора" in html          # формулировка паттерна
    assert "референс этого брифа" in html          # пометка совпавшего evidence
    assert "вне evidence" in html                  # https://x.com/чужой
    assert "short-styling-idea-reel" in html


def test_briefs_page_honest_when_origin_missing(...):
    # бриф без formula_id/source_pattern_ids
    html = client.get("/briefs?id=b-old").text
    assert "агент не записал происхождение" in html
```

Комментарии выше — инструкция по построению, итоговый тест должен быть полным кодом по образцу соседних (без «...» в реальном файле).

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_routes.py -q -k origin`
Expected: 2 FAIL (секции нет в HTML).

- [ ] **Step 3: app.py — прокинуть origin**

Импорт (строки 12-13) дополнить:

```python
from cf.dashboard.sections import (brief_origin, lab_context, performance_context,
                                   runs_context, sources_context)
```

В `_briefs_context` перед `return` (после вычисления `references`):

```python
        origin = brief_origin(selected, references, root=lab_root)
```

и добавить `"origin": origin` в возвращаемый словарь. В except-ветке роута `/briefs` (строки 107-108) добавить `"origin": None` в fallback-контекст. ВАЖНО: `_briefs_context` определён до строки `lab_root = Path(lab_root or ".")`? Нет — `lab_root` нормализуется на строке 40, а `_briefs_context` объявлен ниже (строка 78) и берёт его из замыкания: порядок уже корректный, ничего не двигать.

- [ ] **Step 4: briefs.html — секция вместо блока «РЕФЕРЕНСЫ»**

Заменить блок (строки 43-46):

```html
    <div class="field-label" style="margin-top: 16px;">РЕФЕРЕНСЫ</div>
    {% for url in references %}
    <div>{% if url.startswith("http://") or url.startswith("https://") %}<a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a>{% else %}{{ url }}{% endif %}</div>
    {% else %}<div class="empty-state">нет</div>{% endfor %}
```

на:

```html
    <div class="field-label" style="margin-top: 16px;">ПОЧЕМУ ЭТОТ БРИФ</div>
    {% if origin %}
    {% if origin.formula %}
    <div style="font: 600 13px var(--font-ui);">
      Формула: <span class="mono">{{ origin.formula.name }}</span>
      {% if origin.formula.missing %}<span class="badge insufficient">файл не найден</span>
      {% else %} · v{{ origin.formula.version }} · {{ origin.formula.niche }}
        · утверждена {{ origin.formula.approved_at }} · {{ origin.formula.confidence }}{% endif %}
    </div>
    {% if not origin.formula.missing and origin.formula.problem %}
    <div style="font: 400 12px/1.5 var(--font-ui); color: var(--gray-text); margin: 4px 0 10px;">{{ origin.formula.problem }}</div>
    {% endif %}
    {% endif %}
    {% for p in origin.patterns %}
    <div class="report" style="padding: 8px 0;">
      <div><span class="tag">{{ p.platform or "?" }}</span>
        <span style="font: 400 12.5px var(--font-ui);">{{ p.description }}</span></div>
      <div class="mono" style="font-size: 10.5px; color: var(--gray-text); margin: 4px 0;">
        {{ p.evidence | length }} роликов{% if p.avg_views %} · avg views {{ "{:,.0f}".format(p.avg_views).replace(",", " ") }}{% endif %}{% if p.avg_er %} · avg ER {{ "%.2f" | format(p.avg_er) }}{% endif %}</div>
      {% for e in p.evidence %}
      <div style="font-size: 12px;"><a href="{{ e.url }}" target="_blank" rel="noopener">{{ e.url }}</a>
        {% if e.is_reference %}<span class="badge approved">референс этого брифа</span>{% endif %}</div>
      {% endfor %}
    </div>
    {% endfor %}
    {% for pid in origin.missing_pattern_ids %}
    <div style="font-size: 12px;"><span class="mono">{{ pid }}</span> <span class="badge insufficient">файл паттерна не найден</span></div>
    {% endfor %}
    {% if origin.unmatched_refs %}
    <div class="report" style="padding: 8px 0;">
      <div><span class="badge pending">вне evidence</span>
        <span style="font-size: 11.5px; color: var(--gray-text);">референсы, которых нет в evidence паттернов — проверь перед одобрением</span></div>
      {% for url in origin.unmatched_refs %}
      <div style="font-size: 12px;"><a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
      {% endfor %}
    </div>
    {% endif %}
    {% else %}
    <div style="font-size: 12.5px; color: var(--gray-text);">агент не записал происхождение — у брифа нет formula_id и source_pattern_ids</div>
    {% if references %}
    <div class="field-label" style="margin-top: 12px;">РЕФЕРЕНСЫ</div>
    {% for url in references %}
    <div style="font-size: 12px;"><a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></div>
    {% endfor %}
    {% endif %}
    {% endif %}
```

(Классы `badge approved/pending/insufficient`, `tag`, `report`, `mono`, `field-label` уже существуют в style.css — новых стилей не добавлять. У брифов с происхождением отдельный список рефов не нужен: все рефы видны либо в evidence, либо в «вне evidence».)

- [ ] **Step 5: Тесты зелёные**

Run: `.venv\Scripts\python.exe -m pytest tests/test_dashboard_routes.py tests/test_dashboard_sections.py -q`
Expected: все PASS.

- [ ] **Step 6: Полный сьют**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: все PASS (~266+).

- [ ] **Step 7: Commit**

```bash
git add src/cf/dashboard/app.py src/cf/dashboard/templates/briefs.html tests/test_dashboard_routes.py
git commit -m "dashboard: секция «Почему этот бриф» — формула, паттерны, честные рефы"
```

(трейлер Co-Authored-By как в Task 1)

---

### Task 3: Живая проверка

**Files:** нет правок кода (кроме возможных доводок по итогам).

- [ ] **Step 1: Перезапустить дашборд**

```powershell
$conn = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
if ($conn) { Stop-Process -Id $conn[0].OwningProcess -Force -Confirm:$false }
# из корня <local-path>, фоном:
.venv\Scripts\python.exe -m cf dashboard
```

- [ ] **Step 2: Скриншот карточки summerbase**

Headless Edge: `http://127.0.0.1:8787/briefs?id=b-short-styling-idea-reel-20260715-summerbase`, окно 1440×1400. Проверить глазами: секция «ПОЧЕМУ ЭТОТ БРИФ», формула v2 с «проблемой», два паттерна TikTok/Instagram с формулировками и метриками, оба рефа помечены «референс этого брифа», блока «вне evidence» нет.

- [ ] **Step 3: Если правилось — финальный коммит**

```bash
git status --short
git add -A && git commit -m "dashboard: доводка секции происхождения по живой проверке"
```
