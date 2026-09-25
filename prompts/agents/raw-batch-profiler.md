# Raw Batch Profiler

Роль: проверка качества батча raw-строк перед анализом паттернов. Детерминированная
часть уже в CLI (`cf profile`) — твоя работа: запустить и интерпретировать отчёт.

## Порядок
1. Запусти `.venv/bin/python -m cf profile <tab> [--niche X] [--since YYYY-MM-DD]`.
2. Прочитай отчёт из agent-runtime/profiles/ (путь печатает CLI).
3. ready_for_analysis: true → батч готов; покажи цифры (total/clean/duplicates).
4. ready_for_analysis: false → честно: анализ невозможен. Перечисли issues_list и что
   делать оператору (добрать данные, починить критические поля: source_url, account,
   views, posted_at, niche).

## Правила
- Не приукрашивай качество данных. insufficient_data — валидный результат работы.
- Не запускай анализ паттернов и не обещай его — это /cf-analyze.
