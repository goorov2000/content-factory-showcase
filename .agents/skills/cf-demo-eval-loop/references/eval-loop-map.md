# Карта eval-петли CF (сверено с кодом и живыми листами 2026-07-27)

Всё ниже проверено чтением кода и заголовков боевой таблицы. Номера строк
устаревают — сверяйся с именами функций.

## 1. Цепочка данных

```
сценарий (CF Creative Briefs, brief_id, formula_id, prompt_version)
   └─ выложили → CF Published Reels (published_id = reel_id, brief_id, prompt_version, status)
        └─ замерили → CF Performance (performance_id, published_id, views, engagement_rate, …)
             └─ cf eval-prep → agent-runtime/evals/<дата>-eval-dataset.json
                  ├─ eval-агент → weekly-eval.json → insights → proposal
                  ├─ cf formula-perf  → own_performance в formulas/_approved/index.json
                  └─ cf formula-guard → авто-пауза рецепта (P5.10) + formulas/_decisions.jsonl
```

## 2. Живые колонки (боевой лист, 2026-07-27)

**CF Published Reels** — 0 строк:
`published_id, brief_id, platform, post_url, published_at, creator, content_owner, prompt_version, status, notes`

**CF Performance** — 0 строк:
`performance_id, published_id, brief_id, prompt_version, platform, measured_at, hours_since_publish, views, likes, comments, shares, saves, engagement_rate, views_per_hour, result_label, result_reason, eval_notes`

**Канонические имена ≠ имена колонок.** `cf.config.json → column_aliases`
переводит их автоматически в обе стороны (`Sheets._to_actual` при записи,
`apply_column_aliases` при чтении). Пишем каноническими:

| вкладка | канонический ключ | фактическая колонка |
|---|---|---|
| reels | `reel_id` | `published_id` |
| reels | `production_notes` | `notes` |
| performance | `reel_id` | `published_id` |
| performance | `er` | `engagement_rate` |

Ключ, которого нет в заголовках, **молча выбрасывается** с предупреждением в лог
(`Sheets.append_rows`). Поэтому тесты сеялки обязаны задавать FakeSheets реальные
заголовки — иначе потеря поля будет невидимой.

## 3. Пороги (константы, менять нельзя — на них держатся выводы)

| Константа | Значение | Где | Что делает |
|---|---|---|---|
| `UNKNOWN_VERSION_THRESHOLD` | 0.20 | `src/cf/evalprep.py` | доля замеров без версии выше → весь датасет `insufficient_data` |
| `COHORT_MIN_REELS` | 5 | `src/cf/evalprep.py` | версия с меньшим числом замеров не участвует в A/B-вердикте |
| `PERF_WINDOW_DAYS` | 28 | `src/cf/cli.py` | окно замеров для гвардии и витрины |
| `PERF_MIN_REELS` | 3 | `src/cf/cli.py` | меньше замеров у рецепта → правило P5.10 не применяется |
| `PERF_MEDIAN_RATIO` | 0.5 | `src/cf/cli.py` | медиана рецепта < 0.5 × общей медианы → авто-пауза |

Правило гвардии по ревью (независимое): 3 reject из последних 5 сценариев
рецепта, либо 4 «reject или доработка» из 5.

## 4. Как определяется версия промпта у замера

`evalprep._resolve_version`: сначала `prompt_version` **строки замера**, потом
`prompt_version` брифа, иначе `unknown`.

Отсюда главный приём симуляции: чтобы получить вторую ветку A/B, достаточно
написать другую версию в строку CF Performance — сценарии в CF Creative Briefs
трогать не нужно вообще.

Ключ агрегата — не голая версия, а `«пространство:версия»`
(`evalprep._prompt_key`). Колонки `prompt_id` в живых листах нет, поэтому
пространством служит `formula_id`: ключи выглядят как
`short-styling-idea-reel:v2`. Замер без брифа даёт голую версию и предупреждение
в `warnings` — вердикт по такому ведру не выносится.

## 5. Параллельные когорты (ось A/B)

`evalprep._parallel_groups` объединяет версии, у которых **пересекаются окна
публикации** (`[min published_at, max published_at]`). Чередование дат → одна
группа → сравнение честное. Если v2 выходила 8–12-го, а v4 13–19-го, это
«до/после», группы разные и вердикт не выносится вовсе.

`evalprep._verdict_gate`: вердикт возможен, только если версий ≥ 2 и у **каждой**
≥ 5 замеров. Иначе `insufficient_data` со списком недобравших версий.

## 6. Дедуп замеров (P5.8)

Один замер на ролик — ближайший к `published_at + 7 дней`; при ничье
предпочитается замер на/после 7-го дня. Нет валидного `published_at` — последний
по `measured_at`. `--since` применяется ПОСЛЕ дедупа к выбранному замеру и
сравнивается как строка, поэтому `measured_at` обязан быть ISO (`YYYY-MM-DD…`).

## 7. Что пишет боевой сбор статистики (эталон формы строки)

`cf collect performance` (`src/cf/collect/performance.py`):

- `build_metric_requests` берёт строку ролика **только если**
  `status == "published"` (пустой статус трактуется как `published`!), есть
  `post_url`, распознана площадка, есть `brief_id` и `prompt_version`;
- `normalize_performance_row` пишет `performance_id`, `published_id`, `brief_id`,
  `prompt_version`, `platform`, `measured_at`, `hours_since_publish`, `views`,
  `likes`, `comments`, `shares`, `saves`, `engagement_rate`, `views_per_hour`,
  `result_label = "unknown"`, `result_reason`, `eval_notes` со строкой
  `collection_status=metrics_collected|metrics_zero|metrics_empty_or_unavailable`.

Маркер `metrics_empty_or_unavailable` означает «замера не было» — eval вытесняет
такие строки. Демо-замеры обязаны нести `collection_status=metrics_collected`.

`cf mark-published` (`src/cf/publish.py`) пишет строку ролика: `reel_id`
разбирается из URL, `prompt_version` подтягивается **из брифа**, `status` не
пишется вовсе. Для демо он не годится (не даст второй ветки A/B и засорит Run Log
по строке на ролик), но его форма строки — эталон.

## 8. Сеялка: требования к `src/cf/demoseed.py`

```python
DEMO_PREFIX = "DEMO-"
DEMO_STATUS = "demo"

def build_rows(briefs, plan) -> (reel_rows, perf_rows)
def seed(sheets, plan) -> dict          # append_rows батчем, без mark_published
def wipe(sheets) -> (n_reels, n_perf)   # replace_rows(tab, [строки без DEMO_PREFIX])
```

Форма строки ролика (канонические ключи):

```python
{"reel_id": "DEMO-<slug>", "brief_id": <настоящий>, "platform": "tiktok",
 "post_url": "https://demo.local/r/DEMO-<slug>", "published_at": "2026-07-08T09:00:00+00:00",
 "creator": "", "content_owner": "", "prompt_version": "v2",
 "status": "demo", "production_notes": "DEMO eval-loop <дата>"}
```

Форма строки замера:

```python
{"performance_id": "DEMO-<slug>-<YYYYMMDDHHMM>", "reel_id": "DEMO-<slug>",
 "brief_id": <настоящий>, "prompt_version": "v2", "platform": "tiktok",
 "measured_at": "2026-07-15T09:00:00+00:00", "hours_since_publish": 168,
 "views": 5400, "likes": 430, "comments": 25, "shares": 40, "saves": 60,
 "er": 0.1027, "views_per_hour": 32.14,
 "result_label": "unknown", "result_reason": "demo_seed",
 "eval_notes": "collection_status=metrics_collected; DEMO"}
```

Проверки сеялки, без которых она опасна:

- каждый `brief_id` из плана существует в CF Creative Briefs — иначе отказ до
  единой записи (иначе `brief_found = False`, `formula_id` пуст, гвардия и
  когорты не сработают, и демо провалится молча);
- `reel_id` и `performance_id` обязаны начинаться с `DEMO_PREFIX` — иначе отказ;
- `status` каждой строки ролика равен `DEMO_STATUS`;
- повторный `seed` не плодит дублей (сверка по `reel_id`);
- `wipe` НИКОГДА не вызывает `replace_rows(tab, [])` вслепую — только со списком
  сохранённых не-DEMO строк.

## 9. Диагностика датасета

| Что видно | Что значит | Что делать |
|---|---|---|
| `status == "insufficient_data"` | доля `unknown` версий > 20% | у замеров пустой `prompt_version` — чинить план |
| `join_health.briefs_not_found > 0` | `brief_id` не найден в CF Creative Briefs | взяты выдуманные id вместо настоящих |
| `comparable_pairs == []` | окна публикации версий не пересеклись | чередовать даты версий, а не блоками |
| `verdict_gate == "insufficient_data"` | у версии < 5 замеров | добрать замеры (или это задуманный контроль) |
| `warnings` про «версии без промпта/формулы» | замер без брифа | тот же случай `briefs_not_found` |
| замеров меньше, чем строк в плане | сработал дедуп P5.8 | это норма, если дубли заложены сознательно |

## 10. Откат

| Что изменено | Чем возвращается |
|---|---|
| строки CF Published Reels / CF Performance | `cf demo-wipe` (фильтр по `DEMO-`) |
| `formulas/_approved/index.json`, `_decisions.jsonl` | `git checkout -- formulas/` после `git diff --stat` глазами |
| снимок `agent-runtime/backups/<дата>-{reels,performance}.jsonl` | удалить файл, если он снят при живых демо-строках |
| строки CF Run Log | **не откатываются** (только ротация `cf archive`) — поэтому `input_summary` начинается с `DEMO:` |

`cf restore` для reels/performance не поможет: он отказывается восстанавливать из
пустого снимка (восстановление «ничего» затёрло бы вкладку). Отсюда требование к
`demo-wipe` быть точным фильтром.
