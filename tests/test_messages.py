"""Тексты уведомлений — то, что владелец читает утром с телефона.

Образец ошибки взят из боевого прогона 2026-08-09
(agent-runtime/reports/stage-reports.json, звено raw): именно он приходил в
чат стеной на 2615 знаков, из-за которой Telegram предлагал владельцу
перевести собственного бота на русский.
"""
from cf.messages import (collect_alert, collect_crash_alert, cycle_degraded,
                         cycle_summary, dashboard_link)

CONFIG = {"dashboard": {"public_origin": "https://cf.example.com"}}

# Ровно то, что лежало в summary["error"] утром 09.08 (6 упавших батчей).
REAL_403 = (
    "Ingestion Gate: 0 реальных рядов при 6 упавших батчах — весь сбор "
    "провалился, тихий зелёный ран не пропускаем | причины: "
    + "; ".join(
        f"батч {i}: Client error '403 Forbidden' for url "
        f"'https://api.apify.com/v2/acts/clockworks~tiktok-scraper/runs'\n"
        f"For more information check: "
        f"https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"
        for i in range(6)))


def test_real_403_becomes_human_text():
    text = collect_alert("tiktok", {"status": "failed", "error": REAL_403},
                         CONFIG)
    # ничего из машинного словаря наружу не попало
    for junk in ("403", "Forbidden", "Client error", "developer.mozilla.org",
                 "Ingestion Gate", "батч", "url"):
        assert junk not in text, f"в сообщении осталось техническое «{junk}»"
    # зато есть причина, действие и последствие
    assert "Сбор роликов из TikTok не работает" in text
    assert "кончились деньги" in text
    assert "console.apify.com/billing" in text
    assert "Завод продолжает делать сценарии" in text


def test_real_403_is_short_enough_to_read():
    # было 2615 знаков — примерно три экрана телефона
    text = collect_alert("tiktok", {"status": "failed", "error": REAL_403},
                         CONFIG)
    assert len(text) < 450, f"сообщение снова разрослось: {len(text)} знаков"


def test_empty_batch_is_not_an_alarm():
    # 0 строк — не поломка, и выглядеть как поломка не должно
    text = collect_alert("tiktok", {"status": "insufficient_data"}, CONFIG)
    assert text.startswith("🟡")
    assert "не поломка" in text
    assert "cf.example.com/sources" in text


def test_impact_depends_on_which_collector_died():
    # владельцу нужно понимать, насколько волноваться
    err = {"status": "failed", "error": REAL_403}
    assert "На сегодняшние сценарии это не влияет" in collect_alert(
        "snowball", err, CONFIG)
    assert "цифры продаж" in collect_alert("metrika", err, CONFIG)


def test_crash_names_the_missing_key():
    text = collect_crash_alert(
        "tiktok", FileNotFoundError("~/.cf/secrets/apify-token.txt"), CONFIG)
    assert "не запустился" in text
    assert "Что делать:" in text


def test_cycle_summary_leads_with_the_owners_only_job():
    text = cycle_summary(formulas=0, pending=9, config=CONFIG)
    assert text.startswith("📋 9 сценариев ждут твоего решения")
    assert "https://cf.example.com/briefs" in text


def test_cycle_summary_declines_the_count_correctly():
    assert "1 сценарий ждёт" in cycle_summary(0, 1, CONFIG)
    assert "3 сценария ждут" in cycle_summary(0, 3, CONFIG)
    assert "9 сценариев ждут" in cycle_summary(0, 9, CONFIG)


def test_nothing_to_decide_does_not_send_the_owner_to_the_dashboard():
    text = cycle_summary(formulas=0, pending=0, config=CONFIG)
    assert "Новых сценариев на одобрение нет" in text
    assert "http" not in text          # звать некуда — и не зовём


def test_unreadable_briefs_do_not_invent_a_number():
    # pending=None — счётчик не прочитался; выдумывать цифру нельзя (правило №2)
    text = cycle_summary(formulas=2, pending=None, config=CONFIG)
    assert "Завод отработал за ночь" in text
    assert "сценари" not in text
    assert "2 рецепта" in text


def test_degraded_cycle_points_at_the_red_message():
    text = cycle_degraded(["Собранные ролики", "Контент-завод"], CONFIG)
    assert "работал не полностью" in text
    assert "Собранные ролики, Контент-завод" in text
    assert "красное сообщение" in text


def test_works_without_a_configured_public_origin():
    # адрес пульта не настроен — сообщение всё равно осмысленное, без «None»
    assert dashboard_link({}) == ""
    text = cycle_summary(formulas=0, pending=5, config={})
    assert "Открой пульт" in text
    assert "None" not in text


def test_repeat_says_which_day_it_is():
    # пятое одинаковое утро — главная причина, по которой бота перестают читать
    err = {"status": "failed", "error": REAL_403}
    assert "6-й день" in collect_alert("tiktok", err, CONFIG, day=6)
    # в первый день счётчика нет («деньги» в тексте причины — не он)
    assert "-й день" not in collect_alert("tiktok", err, CONFIG, day=1)


def test_repair_closes_the_topic():
    from cf.messages import collect_recovered
    assert collect_recovered("tiktok", CONFIG) == \
        "✅ Сбор роликов из TikTok снова работает."


def test_failed_service_is_named_in_human_words():
    from cf.messages import unit_failed
    text = unit_failed("cf-backup.service", CONFIG)
    assert "резервная копия таблиц" in text
    assert "cf-backup.service" not in text        # имя юнита человеку не нужно
    assert "защита от потери таблиц" in text      # насколько волноваться


def test_unknown_service_still_produces_a_message():
    from cf.messages import unit_failed
    text = unit_failed("cf-что-то-новое.service", CONFIG)
    assert "cf-что-то-новое.service" in text      # хоть так, но не молчим


def test_watchdog_reports_a_silent_factory():
    from cf.messages import cycle_missing
    text = cycle_missing(31.4, CONFIG)
    assert "Завод не отработал" in text
    assert "31 часов" in text
    assert "cf.example.com/runs" in text


def test_watchdog_admits_when_it_cannot_tell():
    from cf.messages import cycle_missing
    assert "не читается" in cycle_missing(None, CONFIG)


def test_paused_formula_speaks_russian():
    from cf.messages import formulas_paused
    text = formulas_paused([("style-rules-skit",
                             "авто-пауза: 3 reject из последних 5")], CONFIG)
    assert "reject" not in text and "авто-пауза" not in text
    assert "3 из последних 5 сценариев забракованы" in text
    assert "Это страховка" in text


# ── Тикет 06 (план 2026-08-10-apify-costs): блок про источники и exploration ──
# Обещание §5 спеки бота: владелец видит не только исход сбора, но и что завод
# делает с источниками — по фактам прогона, которые возвращают сборщики
# (summary["exploration"]: маркеры грамматики source_query).


def test_sources_block_reports_probes_promotions_and_retirements():
    summary = {"status": "insufficient_data", "exploration": {
        "explored": ["hashtag:#мужскойстиль", "query:мужская мода"],
        "promoted": ["hashtag:#mensfashion"],
        "retired": ["hashtag:#пустой"]}}
    text = collect_alert("tiktok", summary, CONFIG)
    # проверено этим прогоном — сколько и что именно
    assert "🌱 Завод заодно проверил 2 пробных источника: " \
           "#мужскойстиль, «мужская мода»." in text
    # предложено к промоушену — что значит и куда нажать
    assert ("#mensfashion принёс ролики не хуже постоянных источников — "
            "готово предложение добавить его насовсем.") in text
    assert "cf.example.com/sources" in text
    # ретирнуто — и почему это не страшно
    assert "#пустой больше не проверяется: за несколько проверок " \
           "он почти ничего не принёс." in text
    # машинная грамматика маркеров наружу не попала
    assert "hashtag:" not in text and "query:" not in text


def test_sources_block_declines_the_singular():
    summary = {"status": "insufficient_data",
               "exploration": {"explored": ["hashtag:#один"]}}
    assert "проверил 1 пробный источник: #один." in \
        collect_alert("tiktok", summary, CONFIG)


def test_sources_block_plural_promotion_and_retirement():
    summary = {"status": "insufficient_data", "exploration": {
        "promoted": ["hashtag:#а", "hashtag:#б"],
        "retired": ["hashtag:#в", "hashtag:#г"]}}
    text = collect_alert("tiktok", summary, CONFIG)
    assert "#а, #б принесли ролики не хуже постоянных источников — " \
           "готово предложение добавить их насовсем." in text
    assert "#в, #г больше не проверяются: за несколько проверок " \
           "они почти ничего не принесли." in text


def test_run_without_exploration_activity_gets_no_block():
    # пустой прогон — ни заголовка блока, ни нулей-шума
    for summary in ({"status": "insufficient_data"},
                    {"status": "insufficient_data",
                     "exploration": {"explored": [], "promoted": [],
                                     "retired": []}}):
        text = collect_alert("tiktok", summary, CONFIG)
        assert "🌱" not in text
        assert "пробн" not in text


def test_promotion_without_public_origin_points_at_the_dashboard_section():
    summary = {"status": "insufficient_data",
               "exploration": {"promoted": ["hashtag:#находка"]}}
    text = collect_alert("tiktok", summary, config={})
    assert "Решение за тобой: раздел «Источники» в пульте" in text
    assert "None" not in text


def test_recovered_message_carries_the_sources_block():
    # «снова работает» — единственное сообщение УСПЕШНОГО прогона; факты про
    # источники едут в нём, когда они есть, и не меняют его без них.
    from cf.messages import collect_recovered
    assert collect_recovered("tiktok", CONFIG) == \
        "✅ Сбор роликов из TikTok снова работает."
    summary = {"status": "success",
               "exploration": {"explored": ["hashtag:#новый"]}}
    text = collect_recovered("tiktok", CONFIG, summary=summary)
    assert text.startswith("✅ Сбор роликов из TikTok снова работает.")
    assert "проверил 1 пробный источник: #новый." in text


def test_long_sources_block_is_cut_by_the_existing_telegram_limit():
    from cf.notify import TELEGRAM_LIMIT, fit
    summary = {"status": "insufficient_data", "exploration": {
        "explored": [f"hashtag:#оченьдлинныйтег{i}" for i in range(300)]}}
    text = collect_alert("tiktok", summary, CONFIG)
    assert len(text) > TELEGRAM_LIMIT          # обрезке есть что резать
    cut = fit(text)
    assert len(cut) <= TELEGRAM_LIMIT
    assert cut.endswith("…остальное — в пульте, раздел «Отчёты этапов».")
