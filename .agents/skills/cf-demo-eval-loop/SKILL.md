---
name: cf-demo-eval-loop
description: Прогнать eval-петлю Content Factory на фиктивных данных — симуляция выкладки роликов и сбора статистики, слепая проверка выводов системы по заранее известному ключу ответов, затем полное стирание. Использовать при просьбах про «фиктивную петлю», «демо-данные», «симулировать выкладку/статистику», «проверить eval», «подготовить eval к бою», «показать обратную связь завода».
user-invocable: true
---

# Фиктивная eval-петля CF

Задача целиком описана в [docs/plans/2026-07-27-remaining-two.md](../../../docs/plans/2026-07-27-remaining-two.md) — раздел «Задача 1».
Технические факты (колонки, пороги, места в коде) — в [references/eval-loop-map.md](references/eval-loop-map.md).
Состав демо-данных и шаблон ключа ответов — в [references/demo-dataset.md](references/demo-dataset.md).

## Зачем это делается

Отдела съёмки и выкладки ещё нет: CF Published Reels и CF Performance пусты, и
вся обратная связь завода (eval-агент, атрибуция провалов, `own_performance`,
performance-гвардия, вердикт A/B) **ни разу не выполнялась на данных**. Мы
подставляем данные сами, чтобы увидеть, правильные ли выводы делает система,
пока цена ошибки нулевая.

Это **не** «нарисовать красивые цифры для показа». Это приёмочное испытание с
заранее известным правильным ответом. Красивая витрина — побочный продукт.

## Железные инварианты

1. **Ключ ответов пишется ДО прогона.** Цифры проектируются от нужного вывода, а
   не подгоняются под полученный. `agent-runtime/demo/answer-key.md` создаётся
   раньше, чем `cf eval-prep`.
2. **Eval-агент работает вслепую.** Запускать его подагентом (Agent tool) в
   чистом контексте, не передавая ни плана, ни ключа. Иначе испытание
   бессмысленно: агент воспроизведёт подсказку, а не выведет из данных.
3. **Каждая фиктивная строка помечена.** `DEMO-` в начале `reel_id` и
   `performance_id`, `status = "demo"` в строке ролика. Без этого стирание
   становится угадыванием, а сбор статистики в 08:40 уходит в Apify за деньги.
4. **Расхождение с ключом — результат, а не повод править данные.** Если система
   выдала не тот вывод, это находка: записать её, разобраться в причине.
   Переделывать план можно только если он сам собран неверно (например, окна
   публикации не пересеклись) — и это фиксируется в отчёте.
5. **Данные стираются в той же сессии.** Не «потом когда-нибудь». Если сессия
   прерывается — стереть до выхода; при следующем заходе всё заново дешевле, чем
   забытые демо-строки в боевой таблице.

## Порядок работы

### 0. Предусловия (не пропускать)

```bash
cd /home/<user>/projects/CF
date -u +%Y-%m-%dT%H:%M:%S+00:00        # запомнить как STARTED_AT (правило №6)
.venv/bin/python -m pytest -q            # база: 1594 passed
systemctl list-timers 'cf-*' --no-pager
git status --porcelain formulas/         # ДОЛЖНО быть пусто: иначе откат затрёт чужую работу
.venv/bin/python -m cf backup            # снимок всех вкладок ДО инъекции
.venv/bin/python -c "
from cf.sheets import Sheets; s=Sheets()
print('reels', s.count_rows('reels'), '| performance', s.count_rows('performance'))"
```

Прочитать [references/eval-loop-map.md](references/eval-loop-map.md) и
`prompts/agents/eval-agent.md`. Если `reels`/`performance` уже не пусты —
остановиться и разобраться, чьи это строки, прежде чем что-либо писать.

### 1. Заморозить автоматику

Ночные таймеры трогают ровно то, что мы собираемся показывать. Просьба
оператору (sudo с паролем — только он):

```bash
sudo systemctl stop cf-backup.timer cf-cycle.timer cf-collect-performance.timer
```

Если оператор недоступен — уложиться в один день до 07:00 и обязательно
поставить `status = "demo"` (это единственное, что удерживает сбор статистики от
платных запросов в Apify).

### 2. Сеялка: код и тесты (TDD)

Написать `src/cf/demoseed.py` и команды `cf demo-seed` / `cf demo-wipe`.
Требования — в [references/eval-loop-map.md](references/eval-loop-map.md), раздел
«Сеялка». Коротко:

- `DEMO_PREFIX = "DEMO-"`, `DEMO_STATUS = "demo"` — константы модуля;
- `demo-seed --plan <файл>` читает план, проверяет, что каждый `brief_id`
  существует в CF Creative Briefs, строит строки роликов и замеров, пишет
  батчем через `sheets.append_rows`;
- `demo-wipe` читает вкладку, оставляет строки БЕЗ префикса `DEMO-` и
  переписывает вкладку через `sheets.replace_rows` — так чужая строка,
  появившаяся между инъекцией и стиранием, переживёт уборку;
- обе команды пишут в Run Log через `runlog.log_run(..., started_at=...)`,
  `input_summary` начинается с `DEMO:`;
- `tests/test_demo_seed.py` на `FakeSheets` с ЯВНО заданными живыми
  заголовками вкладок (образец приёма — `tests/test_publish.py`): состав полей,
  префиксы, `status="demo"`, повторный seed не плодит дублей, wipe не трогает
  не-DEMO строки.

Тест зелёный **до** первого касания боевой таблицы.

### 3. План инъекции и ключ ответов

Собрать `agent-runtime/demo/plan.json` по составу из
[references/demo-dataset.md](references/demo-dataset.md): реальные `brief_id`
утверждённых сценариев из `agent-runtime/backups/<дата>-briefs.jsonl`, даты
публикации, версии промпта, просмотры.

Тут же, до всякого прогона, написать `agent-runtime/demo/answer-key.md`: какой
вывод система обязана сделать по каждому пункту и какой обязана НЕ делать
(отрицательные контроли). Формулировки — проверяемые: «рецепт X снят с
производства», а не «система заметит проблему».

### 4. Сухой прогон без Sheets

Прогнать `build_eval_dataset` на плане локально (FakeSheets или прямой вызов из
`cf.evalprep`) и убедиться, что датасет получается ожидаемый: обе когорты по 5
замеров, `verdict_gate == "ok"`, окна пересеклись, `unknown_version_share == 0`.
Здесь ловятся все ошибки плана — бесплатно и без следов в боевой таблице.

### 5. Инъекция

```bash
.venv/bin/python -m cf demo-seed --plan agent-runtime/demo/plan.json --started-at $STARTED_AT
.venv/bin/python -c "
from cf.sheets import Sheets; s=Sheets()
print('reels', s.count_rows('reels'), '| performance', s.count_rows('performance'))"
```

### 6. Датасет и его честность

```bash
.venv/bin/python -m cf eval-prep --since 2026-06-29     # 28-дневное окно, не недельное
```

Проверить в свежем `agent-runtime/evals/*-eval-dataset.json`: `status`,
`join_health`, число замеров по каждому ключу `by_prompt_version`,
`cohorts_by_formula[...].parallel_groups[*].comparable_pairs`, `warnings`.
Красные флаги и что они значат — в
[references/eval-loop-map.md](references/eval-loop-map.md), раздел «Диагностика
датасета». Пока датасет не тот, eval-агента не запускать.

### 7. Слепой прогон eval-агента

Запустить подагентом с чистым контекстом. В задании — только команда и промпт
агента, **ни слова** о плане, ключе и ожидаемых выводах:

> Следуй `prompts/agents/eval-agent.md` и `.claude/commands/cf-eval.md`.
> Датасет уже собран: `agent-runtime/evals/<дата>-eval-dataset.json`.
> Построй weekly-eval, покажи вердикты по версиям, атрибуцию провалов и
> success_criteria. Ничего не додумывай: чего нет в данных — того нет.

### 8. Сверка с ключом — главный шаг

Сопоставить отчёт агента с `answer-key.md` построчно. Для каждого пункта:
совпало / не совпало / система промолчала. Результат — таблица в отчёте сессии.
Отдельно проверить отрицательные контроли: вердикт по тонкой когорте обязан быть
`insufficient_data`, `color-upgrade-ladder` обязан остаться в производстве.

Любое расхождение разобрать до причины: ошибка агента, дыра в пороге, дефект
кода или неверно собранный план. Это и есть продукт всей работы.

### 9. Витрины и гвардия

```bash
.venv/bin/python -m cf formula-perf
.venv/bin/python -m cf formula-guard
git --no-pager diff --stat formulas/     # зафиксировать, что изменилось — для отката
```

Ожидается: `own_performance` появился у рецептов с замерами; на паузе ровно
`store-native-skit` с причиной, содержащей «окно 28 дн.»; `color-upgrade-ladder`
не тронут; в `formulas/_decisions.jsonl` строка с `actor=auto`.

### 10. Замкнуть петлю до proposal

`/cf-propose-update` по теме с A/B-результатом → proposal в `proposals/` →
`.venv/bin/python -m cf validate proposal <файл>`. **Остановиться на этом.**
Применять нельзя: evidence синтетический (правило №3). В proposal — явная строка
«данные DEMO, применение блокировано».

### 11. Снять доказательства

До стирания: скриншоты `/overview`, `/performance`, `/lab`, вывод
`cf trace <brief_id>` по одному демо-ролику (цепочка без пропусков),
`cf formula-status`. Складывать в `agent-runtime/demo/shots/`. Дашборд держит
кэш вкладок ~3 минуты — нажать «Обновить данные», прежде чем снимать.

### 12. Стирание

```bash
.venv/bin/python -m cf demo-wipe --started-at $STARTED_AT
git --no-pager diff --stat formulas/     # посмотреть ГЛАЗАМИ, что там только следы демо
git checkout -- formulas/                # снять авто-паузу и own_performance
git status --porcelain formulas/         # пусто
.venv/bin/python -c "
from cf.sheets import Sheets; s=Sheets()
print('reels', s.count_rows('reels'), '| performance', s.count_rows('performance'))"
```

Если за время работы наступило 07:00 — удалить демо-строки из свежего снимка
`agent-runtime/backups/<дата>-{reels,performance}.jsonl` (иначе месячный снимок
врёт). Попросить оператора вернуть таймеры:
`sudo systemctl start cf-backup.timer cf-cycle.timer cf-collect-performance.timer`.

### 13. Лог, память, отчёт

```bash
.venv/bin/python -m cf log-run --agent demo-eval-loop --status success \
  --started-at $STARTED_AT --input "DEMO: <N> роликов / <M> замеров, <K> рецептов" \
  --outputs agent-runtime/evals/<дата>-weekly-eval.json
```

Записать `.claude/memory/decisions/<дата>-demo-eval-loop.md`: что показала петля,
где система ошиблась, что из этого стало задачей. Обновить строку в
`.claude/memory/MEMORY.md`. Инсайты про НИШИ из демо-данных в
`.claude/memory/patterns/**` **не** переносить — они выдуманы.

Отчёт владельцу: таблица «ключ ответов × что сказала система», список находок,
подтверждение, что данные стёрты.

## Ловушки

1. **08:40, `cf-collect-performance.timer`.** Строка ролика без `status = "demo"`
   считается опубликованной (в коде пустой статус трактуется как `published`) и
   уходит в Apify: деньги, падения на несуществующих URL, алерт оператору ночью.
2. **07:30, `cf-cycle.timer`** сам запустит `formula-guard` и `formula-perf` — и
   поставит на паузу настоящий рецепт по демо-цифрам, без вашего ведома.
3. **07:00, `cf-backup.timer`** запечёт демо-строки в месячный снимок.
4. **Ложный «зелёный» A/B.** Если все замеры окажутся одной версии или окна
   публикации не пересекутся, вердикта не будет вовсе — а это легко принять за
   «система не справилась». Проверяется на шаге 4, до инъекции.
5. **Дедуп замеров.** Два замера одного ролика дают в датасете ОДНУ строку
   (ближайшую к 7-му дню). «Замеров 37, а в датасете 35» — это работающий
   инвариант, а не потеря данных.
6. **`git checkout -- formulas/`** снесёт и чужие несохранённые правки. Поэтому
   `git status --porcelain formulas/` на шаге 0 и `diff --stat` глазами на шаге 12.
7. **Разрешения харнесса.** `formula-perf`, `formula-guard`, `backup`, `restore`
   и новые `demo-*` не в allow-листе — в интерактивной сессии будет запрос
   подтверждения. Править `.claude/settings*.json` агенту запрещено; список
   расширяет оператор.

## Правила проекта, которые задача обязана соблюсти

- **№1 evidence-first** — каждый вывод со ссылкой на `reel_id`/цифру; при этом
  вся evidence помечена как DEMO и не выдаётся за знание о нише.
- **№2 insufficient_data** — если порог не набрался, честный `insufficient_data`.
  Отрицательные контроли существуют именно чтобы это проверить.
- **№3 промпты** — proposal да, применение нет. Прямая запись в `prompts/**`
  запрещена deny-правилом харнесса.
- **№4 runtime** — план, ключ, датасет, отчёты, скриншоты только в
  `agent-runtime/**`. В git уходят `src/cf/demoseed.py`, `tests/test_demo_seed.py`,
  правки `cli.py` и proposal.
- **№6 log-run** — `--started-at` с самого начала, след на любом исходе.
- **auto-push запрещён** — коммит без `git push`.
