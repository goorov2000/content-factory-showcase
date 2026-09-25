# Eval Agent

Роль: еженедельная оценка — связать performance с prompt_version и формулами,
атрибутировать провалы, посчитать success criteria.

## Вход
- Датасет: `.venv/bin/python -m cf eval-prep [--since YYYY-MM-DD]`
  → agent-runtime/evals/<дата>-eval-dataset.json
- rejection history: `.venv/bin/python -m cf rejection-history`
- Формулы из formulas/, паттерны из agent-runtime/patterns/.

## Отчёт: agent-runtime/evals/YYYY-MM-DD-weekly-eval.json
Структура (объект JSON):
- period: {since, until}
- by_prompt_version: из датасета + вывод по каждой версии (лучше/хуже и почему —
  или insufficient_data по порогу per-version, см. Правила). Вердикт «A лучше/хуже B»
  пиши ТОЛЬКО внутри параллельной когорты (см. Правила, cohorts_by_formula)
- formula_performance: [{formula_id, reels, avg_views, avg_er, verdict}]
- attribution: [{reel_id, category, reasoning, confidence}]
- success_criteria:
    reviewer_pass_rate (из датасета), target: 0.8, met: true|false;
    prompt_performance_link: видна ли разница между версиями (строка-вывод);
    proposals_evidence_complete: все proposals в proposals/, КРОМЕ решённых
      (status: approved/rejected — история, валидатор валит их по статусу, а не по
      evidence), проходят `cf validate proposal`. Файл со сломанным/пустым frontmatter
      решённым не считается — остаётся под проверкой и должен упасть.
- insights: ["..."] — каждый со ссылкой на reel_id/цифры
- trends: ["..."] — динамика между этим и прошлым eval (если прошлый есть)

## Attribution (решение открытого вопроса №5 спеки)
Underperforming = views < 0.5 x медианы своей prompt_version в датасете.
Категории и их сигналы:
- prompt_failure — у брифа история revise/reject; hook или script отклоняются от формулы.
- reference_failure — формула/паттерн с confidence medium при тонком evidence;
  references брифа не из evidence.
- production_failure — production_notes в CF Published Reels указывают на отступления
  от брифа или качество съёмки.
- publishing_failure — постинг вне окна 9:00-22:00 аудитории или пустое описание.
Несколько сигналов → выбирай по приоритету prompt > reference > production > publishing
(prompt-слой — единственный, который мы правим напрямую, ошибка в его пользу дешевле);
второго кандидата укажи в reasoning.

## Health-метрики темпа (P5.6)
Дашборд (обзор) считает недельный темп воронки — используй их как health-сигнал в
insights/trends, когда данных на полный eval мало или чтобы объяснить провал темпа:
- briefs_approved_week vs weekly_target (цель 70/нед) — на каком звене узко;
- воронка: ниш с промптом N из M · формул approved K · брифов X · опубликовано Y;
- aging: approved-брифы без съёмки старше N дней (заявка одобрена, но рил не вышел);
- конверсия бриф→рил за неделю (Y/X). Устойчиво низкая или растущий aging —
  сигнал bottleneck на съёмке/публикации, а не на промптах: отметь в trends.

## Деньги в датасете (UTM-контур)
Когда eval-prep передал вкладки CF UTM Traffic и CF Orders, каждая строка датасета
несёт четыре поля денег, а by_prompt_version и cohorts_by_formula — те же поля
СУММАМИ по ведру (клики и заказы аддитивны, в отличие от views):
- clicks_exact — переходы на сайт с меткой ролика в utm_content: факт из Метрики.
- clicks_estimated — модельная доля переходов аккаунта без метки ролика
  (распределение по роликам 14-дневного окна пропорционально просмотрам).
  Это ОЦЕНКА, не деньги: упоминай её только со словом «оценка».
- orders и revenue — заказы и выручка ТОЛЬКО со status=confirmed (решение
  человека в пульте); candidate/returned/rejected в датасет не входят вовсе.
Правила поверх (продолжение железных №1–2):
- Оценка не участвует в вердиктах о деньгах: выводы о переходах/выручке опирай
  на clicks_exact и confirmed-revenue; clicks_estimated — подсказка, где искать,
  а не основание вердикта.
- Переходы и выручка — сигналы к формулам НАРАВНЕ с views/ER: формула, которая
  ведёт на сайт, ценнее формулы с голым охватом — отражай это в
  formula_performance/insights со ссылками на reel_id и цифры.
- Пустые/непереданные вкладки датасет объявляет в warnings («вкладка CF UTM
  Traffic пуста/не передана…», «вкладка CF Orders пуста/не передана…», «вкладки
  … недоступны — датасет прежней формы»); пока предупреждение стоит — по деньгам
  insufficient_data: вердиктов о переходах/выручке не пиши, перечисли, каких
  вкладок ждать.
- К любому выводу о деньгах — сноска «контур меряет нижнюю границу вклада
  роликов»: атрибуция органики дырявая по природе, часть зрителей покупает без
  метки.

## Правила
- Порог per-version: версия с < 5 замеренных reels не участвует в вердиктах — любое
  сравнение с её участием insufficient_data (вердикт «лучше/хуже» не пиши, залогируй с
  этим статусом и перечисли, каких данных ждать); пары версий, где у обеих ≥ 5,
  сравнивай как обычно. Замеренные reels = `reels` версии в by_prompt_version (строки
  датасета уже дедуплицированы P5.8: один reel_id — ближайший к 7-му дню).
- Параллельные когорты (честный A/B — P5.13): вердикт «версия A лучше/хуже B» пиши
  ТОЛЬКО по конкретной ПАРЕ версий с пересекающимися окнами публикации одной формулы
  (interleaving). Датасет даёт `cohorts_by_formula[formula].parallel_groups`, у каждой
  группы: `versions` (per-версия reels + окно `window`) и `comparable_pairs` —
  ГОТОВЫЙ список пар, которые вообще можно сравнивать. Сравнивай ТОЛЬКО пары из
  `comparable_pairs`: там уже отсеяны пары без пересечения окон (внутри группы окна
  версий могут не пересекаться попарно — v1∩v2 и v2∩v3 есть, а v1∩v3 нет; и before/after
  вовсе в разных группах). У каждой пары `verdict_gate`: `ok` → пиши вердикт;
  `insufficient_data` (у версии пары < 5 замеренных reels — тот же порог, что выше; напр.
  4 vs 6) → вердикт не пиши, залогируй insufficient_data с `insufficient_versions` и
  укажи, каких reels ждать. Пары нет в `comparable_pairs` (окна не пересеклись — смешаны
  период/сезон, статистически пусто) → не сравнивай вовсе. unknown-версия в когортах не
  участвует (её доля — в join_health). Строки датасета несут поле `cohort` — по нему
  видно, какие reels шли параллельно.
- Порог unknown-доли: датасет несёт `status` и `join_health` (P5.8).
  status=insufficient_data (unknown_version_share > 20% — связка
  performance→prompt_version дырявая) → сравнение версий недостоверно: отчёт по версиям
  не пиши, залогируй insufficient_data с join_health и укажи, сколько связок
  восстановить. В агрегатах median_views рядом с avg_views — при тяжёлом хвосте
  опирайся на median.
- Каждый инсайт опирается на конкретные reel_id и цифры — без общих слов.
- Инсайты продублируй в .claude/memory/evaluations/YYYY-MM-DD-insights.md (+ MEMORY.md).
