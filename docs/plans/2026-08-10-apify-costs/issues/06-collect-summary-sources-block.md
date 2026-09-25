# 06 — Сводка сбора рассказывает про источники и exploration

**What to build:** Обещание §5 спеки бота: владелец видит в Telegram-сводке
сбора не только «собрано N строк», но и что завод делает с источниками —
сколько exploration-кандидатов проверено этим прогоном, что предложено к
промоушену (появился proposal), что ретирнуто по возрасту/бесплодию. Блок
строится из данных, которые сбор уже знает (exploration-батчи,
promote-proposal, aging) — ЕСЛИ каких-то цифр в результате сбора нет, сначала
проверить, что сборщики их возвращают, и честно опустить недоступное, а не
выдумывать. Формулировки — в словаре сообщений бота, как все остальные тексты;
лимит Telegram держит существующая обрезка. Пустой прогон без exploration не
добавляет пустой блок-шум.

**Blocked by:** 02 (стабильный контур сборщика TikTok после гибрида).

**Status:** done

- [x] После прогона сбора с exploration-активностью сводка содержит блок про
  источники: проверено / предложено к промоушену / ретирнуто — по фактам
  прогона.
- [x] Прогон без exploration-активности не получает блока (ни пустых строк, ни
  нулей-шума).
- [x] Формулировки живут в словаре сообщений, тесты фиксируют текст блока.
- [x] Сводка с блоком укладывается в лимит Telegram (обрезка работает).
- [x] Полный тестовый набор зелёный.

## Комментарии

**Что сборщики УЖЕ возвращали наружу: ничего из нужного.** Все три факта
вычислялись внутри `collect` обеих платформ, но уезжали только строкой в Run
Log (`aged_note`/`probe_note`), в возвращаемом summary их не было:
- зонды этого прогона — `counted` (tiktok.py, только успешные
  exploration-батчи) и `ran` (instagram.py, зонд честен по `candidate_probes`);
- ретирнутые — `retired` из `age_candidates`;
- предложенные к промоушену — `promotable` + путь `save_promote_proposal`.

**Что вынесено (минимально, без новых вычислений):** оба сборщика кладут в
результат `summary["exploration"] = {"explored": [...], "retired": [...],
"promoted": [...]}` — списки маркеров канонической грамматики source_query
(`hashtag:#x` / `query:q`), ровно из тех переменных и под теми же условиями,
под которыми факты пишутся в реестр/proposal. Поэтому наружу идёт только
записанное: dry-run, упавший/голодный зонд и пропущенный из-за деградации
aging (IG) фактов не дают — тесты это фиксируют.

**Честно опущено:**
- имя файла proposal — человеку адресован не путь в репо, а раздел
  «Источники» пульта (ссылка в блоке);
- starved-зонды IG («рилсы не доехали» — голодание раздачи слотов) — это
  дефект конвейера, а не факт про источник; остаются в Run Log (`probe_note`);
- прогон, который успешен БЕЗ предшествующей поломки, сообщения не шлёт
  вовсе — политика отправки/приглушения (`cf.alerts`) не менялась, блок едет
  в тех сообщениях, которые сбор и так шлёт: 🟡/🔴-сводка (`collect_alert`) и
  «снова работает» (`collect_recovered`, теперь принимает summary
  починившегося прогона — проводка одной строкой в `cli._notify_collect`).

**Текст блока** (`cf.messages.collect_sources_block`, перевод маркеров —
`_source_name`: `hashtag:#x` → `#x`, `query:q` → `«q»`):

```
🌱 Завод заодно проверил 2 пробных источника: #мужскойстиль, «мужская мода».

#mensfashion принёс ролики не хуже постоянных источников — готово предложение
добавить его насовсем. Решение за тобой: https://cf.example.com/sources

#пустой больше не проверяется: за несколько проверок он почти ничего не принёс.
```

Пустые списки → пустая строка: ни заголовка, ни нулей. Длинный блок режет
существующий `notify.fit` (лимит 4096) — отдельный тест.

**Тесты** (12 новых; полный набор 2090 passed, 0 failed):
- `tests/test_messages.py`:
  `test_sources_block_reports_probes_promotions_and_retirements`,
  `test_sources_block_declines_the_singular`,
  `test_sources_block_plural_promotion_and_retirement`,
  `test_run_without_exploration_activity_gets_no_block`,
  `test_promotion_without_public_origin_points_at_the_dashboard_section`,
  `test_recovered_message_carries_the_sources_block`,
  `test_long_sources_block_is_cut_by_the_existing_telegram_limit`;
- `tests/test_collect_tiktok.py`:
  `test_summary_exposes_exploration_facts_for_the_owner`,
  `test_summary_exploration_empty_without_candidates`,
  `test_dry_run_reports_no_exploration_facts`;
- `tests/test_collect_instagram.py`:
  `test_summary_exposes_exploration_facts_for_the_owner`,
  `test_summary_exploration_empty_without_candidates`,
  `test_degraded_run_reports_probes_but_no_verdicts`;
- `tests/test_cli_collect.py`:
  `test_collect_summary_with_exploration_reaches_telegram`,
  `test_recovered_message_carries_exploration_of_the_healing_run`.

Снапшот-тесты payload и e2e тикетов 01–04 не тронуты: форма результата collect
только расширена новым ключом, существующие ключи не менялись.
