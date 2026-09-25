# Карта дашборда CF (сверено с кодом 2026-07-27)

Всё живёт в `src/cf/dashboard/`. Точка входа — `cmd_dashboard` в `src/cf/cli.py`
→ `build_production_app` → uvicorn на `127.0.0.1:8787`.

## 1. Маршруты

| Метод | Путь | Что это |
|---|---|---|
| GET | `/`, `/overview` | главная: метрики, лента шагов, отчёты, здоровье |
| POST | `/refresh` | сброс кэша вкладок (кэш держит данные ~3 мин) |
| GET | `/briefs` | сценарии: очередь на решение продюсера |
| GET | `/briefs/shooting-list` | список на съёмку |
| POST | `/briefs/{id}/review` | решение по сценарию (одобрить/отклонить) |
| POST | `/briefs/{id}/published` | отметить опубликованным |
| POST | `/formulas/decision` | ручное решение по рецепту |
| POST | `/niches/decision` | решение по теме |
| POST | `/prompts/write`, `/prompts/apply`, `/prompts/reject` | черновик промпта темы: написать / включить / отклонить |
| GET | `/lab` | «лаборатория»: рецепты, темы, промпты, ворота |
| GET | `/runs` | история прогонов |
| GET | `/performance` | результаты роликов |
| GET | `/sources` | источники сбора |
| GET | `/partials/stages`, `/partials/reports`, `/partials/health` | htmx-поллинг живых блоков |
| POST | `/stages/{stage}/run`, `/stages/{stage}/reply` | запуск шага, ответ агенту |
| POST | `/rituals/{ritual}/run` | ритуалы (в т.ч. eval — таймера у него нет) |
| POST | `/cycle/run` | полный цикл |

Шаблоны: `base.html`, `overview.html`, `briefs.html`, `shooting-list.html`,
`lab.html` (397 строк — самый тяжёлый), `performance.html`, `runs.html`,
`sources.html` + `partials/{briefs_review,health,reports,stages}.html`.

Фронт: серверный Jinja + htmx (`static/htmx.min.js`), плюс `static/timeline.js`.
Живые блоки обновляются поллингом htmx.

## 2. Ключевые модули и что за что отвечает

| Модуль | Роль |
|---|---|
| `app.py` | маршруты, CSRF-мидлварь (origin выводится из порта), компиляция шаблонов на старте, `auto_reload = False` |
| `queues.py` | **единый источник** глубин очередей и состояний ворот: `recipe_gate`, `prompt_gate`, `brief_gate`, `build_gates`, `gate_banner` |
| `autogate.py` | машинные ворота: `gate_policy` / `is_auto`, `formula_checks` (11 проверок), `verdict`, `read_ledger` (журнал `formulas/_decisions.jsonl`), `human_rejections` |
| `progress.py` | лента шагов конвейера (`PIPELINE_STEPS`), статусы, гейтовые строки |
| `sections.py` | сборка контекстов разделов |
| `labels.py` | человекочитаемые подписи, форматирование чисел/дат/длительностей, схлопывание технических дампов |
| `data.py` | кэш вкладок (`DataCache`, TTL ~180 с) |
| `runner.py` | StageRunner: шаги, воркеры фан-аута, автопродолжение |
| `decisions.py`, `actions.py`, `prompt_apply.py` | запись решений, применение промптов |
| `health.py` | фоновые проверки связности |

## 3. Дизайн-система

`static/style.css` (533 строки) начинается с токенов в `:root`:
`--primary`, `--ink`, `--accent-blue*`, `--gray*`, `--silver`, `--frost`,
`--press`, `--hairline-dark`, `--success*`, `--warning*`, `--error*`,
`--info-soft`, `--shadow-1/2`, `--focus-ring`, `--focus-ring-dark`,
`--font-display` (Aeroport → Montserrat), `--font-ui` (Montserrat),
`--font-mono` (Aeroport Mono), `--ease`.

Шрифты хостятся локально (`static/fonts/`), без CDN. Фокус реализован через
`box-shadow` + прозрачный `outline` — ради режима высокой контрастности Windows.

Описание ДС — `docs/design/dashboard-ds.md`. Макет — `docs/design/cf-dashboard.pen`
(открывается только инструментами Pencil, не Read/Grep). Продуктовые брифы —
`docs/design/cf-ui-design-brief.md`, `cf-ui-project-brief.md`.

## 4. Стартовый бэклог расхождений (проверено, но перепроверь перед работой)

1. **`queues.py` не знает о политике ворот.** Нет импорта `autogate`; `actor: "вы"`
   зашит в трёх местах; баннер говорит «конвейер стоит на воротах». При
   `gates.policy.recipes = "auto"` и `prompt = "auto"` (текущая боевая настройка)
   это прямая дезинформация.
2. **`lab.html` противоречит правилу №3.** Подпись очереди промптов: «Включение
   промпта остаётся за вами: файл и активная версия появляются только по нажатию
   (правило №3)». Действующая редакция — обратная: включает машина по зелёному
   чек-листу, человек держит вето и откат.
3. **Баннер с CLI-командой в лицо продюсеру** (`lab.html`, ветка `file_only`):
   `cf log-prompt-version --prompt-id … --path prompts/briefs/…/reel.md`.
4. **Причины машинного решения нет в интерфейсе.** `autogate.formula_checks`
   считает 11 проверок, `verdict` даёт вердикт судьи — ни то, ни другое не
   выводится; причина видна только в технической строке ленты.
5. **Журнала решений нет в интерфейсе.** `read_ledger` читает
   `formulas/_decisions.jsonl` (кто решил: `human`/`auto`) — в контекстах
   дашборда не используется.
6. **A/B почти не показан.** Активная версия темы мужские-образы — `v2`,
   кандидат — `v4`; в интерфейсе от этого только бейдж.
7. **Долг ДС:** переменная `--porcelain` используется в `.tl-kind-gate`, но в
   `:root` не объявлена (живёт на fallback-хексе).
8. **Пустые витрины.** Роликов и замеров ноль — как это выглядит и что
   объясняет, проверить в первую очередь (см. правило №2).

## 5. Инварианты в тестах (регресс-сеть, ломать осознанно)

`tests/test_dashboard_routes.py`:

- `test_every_section_opens_with_a_lead_line` — у раздела есть вводная строка;
- `test_producer_sections_have_no_system_jargon_in_visible_text` — в видимом
  тексте продюсерских разделов нет системного жаргона (расширить на `/lab`);
- `test_no_static_inline_styles_left_in_templates` — инлайн-стилей в шаблонах нет;
- `test_empty_state_split_into_three_meanings` — пустое состояние различает
  «нечего показывать», «ещё не считали» и «сломалось».

Смежные: `tests/test_queues.py`, `test_lab_template.py`,
`test_dashboard_progress.py`, `test_dashboard_labels.py`,
`test_dashboard_runner.py`. Всего в проекте 1594 теста.

## 6. Исследование 25.07

`docs/ux-research-2026-07-25.md` + `-findings.md` — 141 находка глазами нового
SMM-продюсера, с локациями и предложенными правками. Часть уже исправлена
ремонтом 25–26.07: **каждую находку сверять с текущим кодом**, прежде чем брать
в работу. Ценность файла не в списке правок, а в интонации: он показывает, как
выглядит интерфейс для человека, который не строил эту систему.
