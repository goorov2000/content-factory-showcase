---
status: approved
applied: 2026-07-26
prompt_id: brief-prompt-writer
created: 2026-07-26
---
# Proposal: агентское звено «brief-промпт ниши» вместо кнопки-скаффолда

## Current version
Промпта-агента нет. Его роль сейчас исполняет кнопка «Создать промпт из шаблона»
на /lab (`cf.dashboard.decisions.scaffold_prompt`, P5.2), которая:

1. копирует `prompts/briefs/мужские-образы/reel.md` в `prompts/briefs/{niche}/reel.md`
   с заменой подстроки имени ниши и маркером `# TODO: требует правки под нишу`;
2. активирует `brief-{niche}-reel v1` в CF Prompt Versions;
3. коммитит файл в git.

## Proposed changes
Скаффолд удаляется целиком (кнопка, роут `POST /prompts/scaffold`,
`decisions.scaffold_prompt`, `_strip_heading_version`, `ScaffoldResult`,
`_TEMPLATE_NICHE`). Вместо него — агентское звено, запускаемое из «Лаборатории»
кнопкой ▶ у ниши, тем же путём, что ритуалы недели (`runner._fanout_claude`).
Агент отдаёт proposal, оператор ревьюит и применяет.

Код дашборда, раннера и шаблона в этом proposal не описан — он применён отдельным
коммитом и без новых файлов промптов не активируется. Здесь — два файла, которые
закрыты deny-правилами и требуют решения оператора.

### Новый файл `prompts/agents/brief-prompt-writer.md`

```markdown
# Brief Prompt Writer

Роль: написать brief-промпт ниши (`prompts/briefs/{niche}/reel.md`) по её
УТВЕРЖДЁННЫМ формулам и паттернам. Только proposal — прямых правок
`prompts/briefs/` не делаешь и версию промпта не активируешь. Решение за оператором
(железное правило №3).

Вход: имя ниши аргументом. Без аргумента — возьми первую нишу из списка «ждут
brief-промпта» (см. Гейты) и работай по ней.

## Зачем это звено
Ниша не производит брифы, пока нет `prompts/briefs/{niche}/reel.md` и активной
строки `brief-{niche}-reel` в CF Prompt Versions: brief-generator честно скипает
её со словами «ниша X ждёт brief-промпта». Промпт ниши — это перевод её
утверждённой формулы на язык задания генератору, поэтому он пишется ПОСЛЕ
одобрения формулы, а не до.

Копировать промпт другой ниши запрещено: правила мужских образов («3–5 образов по
2–4 секунды», «текст-плашка с идеей на каждом образе») не действуют в обуви,
груминге или спорте, а ссылка на чужую формулу указывает на несуществующий файл.

## Гейты (проверяй в этом порядке, до единой строки текста)
1. Имя ниши есть в `prompts/agents/niche-taxonomy.json`. Нет — `insufficient_data`,
   объясни, что нишу сначала принимают в таксономию.
2. Ниша НЕ в `dashboard.fanout.exclude_niches` (`cf.config.json`). В списке —
   `insufficient_data`: бренд по ней контент не выпускает, промпт не нужен.
3. `prompts/briefs/{niche}/reel.md` ещё нет. Есть — `insufficient_data`: изменение
   существующего промпта идёт через /cf-propose-update, не через это звено.
4. У ниши есть хотя бы одна формула в `formulas/_approved/index.json`. Нет —
   `insufficient_data` со списком: какие формулы ниши в статусе proposed и ждут
   решения оператора. Промпт по неутверждённой формуле не пишется.

## Источники (читаешь всё, что есть по нише)
- Утверждённые формулы: `formulas/_approved/index.json` → снапшоты
  `formulas/_approved/{niche}/*.json` (иммутабельные, ручной правке не подлежат).
- Паттерны: `agent-runtime/patterns/*-{niche}-patterns.json` — evidence, avg_views,
  avg_er, confidence.
- Схема брифа: `schemas/brief.schema.json` и `prompts/briefs/_shared/schema.md` —
  выход генератора обязан ей соответствовать.
- Образец ЖАНРА (структура разделов, тон, уровень конкретности), НЕ источник
  содержания: `prompts/briefs/мужские-образы/reel.md`.

## Что пишешь
Черновик файла `prompts/briefs/{niche}/reel.md` со структурой донора и содержанием
ИСКЛЮЧИТЕЛЬНО этой ниши:

- **Заголовок** — `# Промпт генерации брифов — {niche} / reel`.
- **Формула** — точное имя и путь утверждённой формулы ниши. Формул несколько —
  промпт покрывает их все и явно говорит, по какому признаку генератор выбирает
  (условия из `conditions` каждой формулы).
- **Вход** — поля формулы, которые читает генератор.
- **Задача** — поля брифа (hook, script, visual_direction, cta, references,
  source_pattern_ids, formula_id, prompt_version) с требованиями, выведенными из
  `hook_structure`, `solution_structure`, `visual_requirements`, `cta_type` формулы.
  Тайминги, число сцен и длительность бери из формулы и паттернов ниши, не из
  головы и не из промпта-донора.
- **Запреты** — из `prohibitions` утверждённых формул ниши, дословно по смыслу.
  Нарушение = reject ревьюером.
- **Выход** — строка в CF Creative Briefs (как у донора: human_status=pending,
  payload_json + плоские поля для eval и trace).

Каждое числовое или структурное требование должно прослеживаться до формулы или
паттерна. Требование без источника не пишешь.

## Выход — proposal, не файл промпта
Сохрани `proposals/YYYY-MM-DD-brief-{niche}-reel.md`. Frontmatter: `status: proposed`,
`prompt_id: brief-{niche}-reel`, `created: YYYY-MM-DD`. Заголовок —
«Proposal: brief-промпт ниши {niche}». Дальше секции второго уровня в том порядке,
который требует `cf validate proposal` (Current version, Proposed prompt, Evidence,
Confidence, Risks) плюс «Применение»:

- **Current version** — «Промпта нет: `prompts/briefs/{niche}/reel.md` отсутствует,
  активной строки `brief-{niche}-reel` в CF Prompt Versions нет — ниша не производит
  брифы».
- **Proposed prompt** — полный текст будущего `prompts/briefs/{niche}/reel.md` в
  блоке кода с меткой markdown.
- **Evidence** — формулы (`<name> v<N>`, путь снапшота в `formulas/_approved/{niche}/`,
  confidence, число `evidence.source_urls`, avg_views, avg_er); паттерны
  (`<pattern_id>` и файл в `agent-runtime/patterns/`) с указанием, что именно из
  паттерна попало в промпт; каждое требование промпта — строкой «требование ←
  источник». Без цифр и путей секция не пройдёт валидатор.
- **Confidence** — high|medium|low плюс одно предложение почему.
- **Risks** — что может пойти не так в первых брифах и по какой метрике это увидим
  (доля reject по коду причины в rejection_history, avg_er версии в следующем eval).
- **Применение (делает оператор)** — четыре шага: ревью текста; сохранить блок
  Proposed prompt в `prompts/briefs/{niche}/reel.md`; `.venv/bin/python -m cf
  log-prompt-version --prompt-id brief-{niche}-reel --version v1 --path
  prompts/briefs/{niche}/reel.md --changelog "<...>"`; коммит промпта вместе с
  proposal, переведённым в `status: approved`.

Перед сохранением прогони `.venv/bin/python -m cf validate proposal <путь>`.

## Правила
- Ни одной строки промпта без источника в формуле или паттерне ниши (правило №1).
- Не копируешь текст промпта другой ниши: заимствуется структура разделов, не
  содержание.
- Не пишешь в `prompts/briefs/`, не трогаешь CF Prompt Versions, не коммитишь
  (правило №3).
- Не хватает данных по любому гейту — `insufficient_data` с перечнем недостающего,
  proposal не создаётся.

## Лог
В конце обязательно (правило №6):
`.venv/bin/python -m cf log-run --agent brief-prompt-writer --status
<success|insufficient_data|failed> --started-at <ISO8601 старта>
--input "ниша {niche}" --outputs proposals/YYYY-MM-DD-brief-{niche}-reel.md`
```

### Новый файл `.claude/commands/cf-write-brief-prompt.md`

```markdown
---
description: Brief Prompt Writer - написать brief-промпт ниши по её утверждённым формулам (proposal)
---
Следуй промпту prompts/agents/brief-prompt-writer.md. Аргументы: $ARGUMENTS
(имя ниши; без аргумента — первая ниша, у которой есть утверждённая формула, но нет
prompts/briefs/{niche}/reel.md). В конце залогируй запуск:
.venv/bin/python -m cf log-run --agent brief-prompt-writer --status <success|insufficient_data|failed> ...
```

## Evidence

**Скаффолд производит промпт с чужими правилами и битой ссылкой.** Донор захардкожен
(`decisions.py:25`, `_TEMPLATE_NICHE = "мужские-образы"`), подстановка — замена
подстроки. Прогон трансформации для ниши «обувь» даёт:

```
# TODO: требует правки под нишу обувь
# Промпт генерации брифов — обувь / reel
Ты генерируешь бриф ... по утверждённой формуле
`short-styling-idea-reel` (formulas/обувь/short-styling-idea-reel.json).
```

`formulas/обувь/short-styling-idea-reel.json` не существует: формула
`short-styling-idea-reel` принадлежит нише мужские-образы. Дальше в теле — «3–5
образов по 2–4 секунды», «текст-плашка с идеей на каждом образе», запреты мужских
образов (`prompts/briefs/мужские-образы/reel.md`).

**Скаффолд гасит честную диагностику.** `prompts/agents/brief-generator.md:46-47`
требует: нет активного промпта ниши — «сообщи в сводке ЯВНО: „ниша X ждёт
brief-промпта“ (не молчаливый скип)». Скаффолд активирует версию сразу, поэтому
после нажатия генератор считает нишу готовой и печёт брифы по правилам чужой ниши.

**Скаффолд обходит правило №3.** Один клик пишет промпт, активирует версию и
коммитит без ревью содержания — при том, что `prompts/agents/**` и
`.claude/commands/**` закрыты deny-правилами именно ради этого ревью.

**Масштаб — не краевой случай.** В таксономии 12 ниш, в `exclude_niches` 3, значит в
производстве 9. Промпт есть у одной (`ls prompts/briefs/` → `_shared`,
`мужские-образы`). На /lab это давало 17 одинаковых кнопок на 7 ниш.

**Скаффолдом ни разу не пользовались.** `git log -- prompts/briefs/` содержит только
ручные коммиты (`e6ced45`, `cf261e9`, `99a340d`); коммита со словом «скаффолд» в
истории нет.

**Порядок «сначала формула, потом промпт» подтверждается содержанием донора.**
`prompts/briefs/мужские-образы/reel.md` начинается со ссылки на конкретную
утверждённую формулу и выводит из неё тайминги, число сцен и запреты. Значит
промпт нельзя написать раньше одобрения формулы — что и закреплено гейтом №4.

## Confidence
high — дефект воспроизводится прогоном самой трансформации на реальном доноре, а
требование «не молчаливый скип» и правило №3 зафиксированы в репозитории текстом.

## Risks
Пока proposal не применён, у ниш без промпта нет действия вообще: «Лаборатория»
показывает их списком с ▶, но команда `/cf-write-brief-prompt` не установлена —
раннер это распознаёт и пишет отчёт «команда не установлена», а не запускает claude
вслепую. Заметим по отчёту в «Отчётах звеньев» на /overview.

Второй риск — агент напишет промпт формально по формуле, но слабый по содержанию.
Метрика: доля reject брифов ниши по коду `weak_hook`/`prohibition_violation` в
`agent-runtime/reviews/rejection_history.json` и avg_er версии в следующем eval.
Смягчение: proposal читает оператор до активации версии, промпт без ревью в
производство не попадает.
