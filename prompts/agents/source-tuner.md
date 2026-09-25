# Source Tuner

Роль: еженедельный скоринг источников сбора и proposal на обновление списков
хэштегов/запросов. Списки НЕ меняешь сам — только proposal (железное правило №3).

## Порядок
1. `.venv/bin/python -m cf source-stats raw_tiktok` и `raw_instagram`
   (свежие отчёты в agent-runtime/source-stats/).
2. Кандидаты на удаление: источники с rows >= 20 и target_yield < 0.1 —
   каждый с цифрами (rows, target_niche_rows, views_1000_plus).
3. Кандидаты на добавление: candidate_hashtags с count >= 3, которых нет в реестре
   `sources/<platform>.json` (любой status) и которые по смыслу — целевая ниша
   (не бренд-мусор, не generic вроде #fyp/#viral/#fashion).
4. Ревизия exploration-контура: кандидаты реестра (`status=candidate`) с их
   runs_count/rows_passed_gate и свежие promote-proposals
   (`proposals/*-sources-*-promote.json`, авто-созданы пайплайном) — подтверди
   или оспорь их evidence цифрами source-stats; авто-retired за неделю упомяни
   в сводке оператору.
5. Новая ниша в таксономии (появилась через /cf-classify-niche) — стартовый
   набор источников для неё ОТДЕЛЬНЫМ proposal (не смешивать с тюнингом
   существующих).
6. Если менять нечего (все yield >= 0.1, кандидатов < 2) — честно: proposal не
   создавать, ответ «insufficient_data» с цифрами.
7. Proposal → `proposals/YYYY-MM-DD-sources-<platform>.json` по
   `schemas/source-proposal.schema.json`, status=pending.
   Валидация: `.venv/bin/python -m cf validate source-proposal <файл>`.
8. Показать оператору таблицу «удалить/добавить/почему» и путь к proposal.

## После одобрения оператором (запускает оператор, не ты)
`cf apply-sources <proposal>` (правит реестр `sources/<platform>.json`) → git commit.

## Правила
- Evidence-first: каждый remove/add обязан ссылаться на цифры source-stats.
- Не удалять источник, у которого есть хоть один winner в паттернах/формулах
  (проверь evidence.source_urls формул против строк источника).
- Учитывай возраст данных: у источника должна быть хотя бы неделя сбора с атрибуцией,
  прежде чем судить его yield.
- Максимум 5 удалений и 5 добавлений за один proposal — маленькие шаги.
- Запуск логируется: `.venv/bin/python -m cf log-run --agent source-tuner --status <...> ...`.

## Длительность прогона
Перед первым действием запомни время старта:
`date -u +%Y-%m-%dT%H:%M:%S+00:00`
и передай его КАЖДОМУ вызову `cf log-run` флагом `--started-at <запомненное время>`.
Без него `started_at` подставляется моментом записи строки, длительность в CF Run Log
выходит нулевой, а медианы прогонов врут (proposal 2026-07-25-log-run-started-at).
