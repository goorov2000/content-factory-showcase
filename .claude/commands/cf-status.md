---
description: Content Factory - запуски, pending-ревью, proposals, активные промпты
---
Выполни `.venv/bin/python -m cf status --limit 10` и покажи оператору результат.

Дальше подскажи следующий шаг:
- есть pending briefs → предложи /cf-review-brief --pending;
- есть proposed proposals → напомни, что они ждут решения (см. proposals/);
- в Recent runs есть failed/insufficient_data → покажи, какие агенты и почему (input_summary).

Если команда падает с ошибкой про cf.config.json или ~/.cf/secrets/service-account.json —
объясни оператору, что настроить (spreadsheet_id; JSON-ключ сервисного аккаунта;
таблица расшарена на его email).
