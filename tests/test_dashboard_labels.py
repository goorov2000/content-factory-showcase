"""Единый слой «система → пользователь» (§2 UX-спеки, Фаза 2)."""

import pytest

from cf.dashboard.labels import report_view


def test_report_view_translates_claude_limit_error_and_collapses_raw():
    # Англоязычный отказ Claude по лимиту → человеческая строка; сырой текст
    # остаётся, но сворачивается под <details>.
    for raw in ("You've hit your session limit. Try again later.",
                "You've reached your Fable 5 limit for this week.",
                "Error: usage limit exceeded"):
        view = report_view({"title": "/cf-analyze", "text": raw})
        assert view["banner"] == "Система была занята, повторит позже."
        assert view["collapse"] is True
        assert view["text"] == raw.strip()   # текст не потерян


def test_report_view_collapses_machine_dump_without_banner():
    dump = "batches=7 rows=519 kept=257\nNICHE_RESULT: {\"niche\": \"мужские-образы\"}"
    view = report_view({"title": "python -m cf collect tiktok", "text": dump})
    assert view["banner"] is None          # не отказ по лимиту — баннера нет
    assert view["collapse"] is True        # но машинный дамп сворачиваем


def test_report_view_keeps_plain_agent_question_visible():
    q = "Уточни: какой из двух хуков брать за основу — про осень или про трикотаж?"
    view = report_view({"title": "/cf-analyze", "text": q})
    assert view["banner"] is None
    assert view["collapse"] is False       # человеческий вопрос — на виду


def test_report_view_never_collapses_producer_dialog():
    # Реплики диалога с продюсером не сворачиваем, даже если текст похож на дамп.
    view = report_view({"title": "вопрос продюсера",
                        "text": "смотри agent-runtime/patterns/x.json"})
    assert view["collapse"] is False


def test_report_view_tolerates_empty_entry():
    view = report_view(None)
    assert view == {"banner": None, "text": "", "collapse": False}


# --- Фаза 3 UX-доводки: единые форматы + словарь значений (§2 спеки) ----------

from cf.dashboard.labels import (NBSP, agent_label, confidence_label, fmt_date,
                                 fmt_datetime, fmt_pct, fmt_views, link_label,
                                 niche_label, run_summary, run_view,
                                 trigger_label)


def test_fmt_views_groups_thousands_and_guards_garbage():
    assert fmt_views(705906) == f"705{NBSP}906"
    assert fmt_views("125000") == f"125{NBSP}000"
    assert fmt_views(0) == "0"
    for junk in (None, "", "—", "не число"):
        assert fmt_views(junk) == "—"        # мусор в колонке метрик читался бы как сбой


def test_fmt_pct_renders_fraction_as_russian_percent():
    assert fmt_pct(0.078) == "7,8%"          # ER хранится долей, проценты — на выводе
    assert fmt_pct(0.16) == "16,0%"
    # рукоправка в Sheets («4.5» вместо 0.045) не должна давать «450%»
    assert fmt_pct("4.5") == "4,5%"
    assert fmt_pct(None) == "—"
    assert fmt_pct(None, "нет данных") == "нет данных"


def test_fmt_date_and_datetime_are_ru_and_tolerant():
    assert fmt_date("2026-07-24T22:25:00+00:00") == "24.07.2026"
    assert fmt_datetime("2026-07-24T22:25:00+00:00") == "24.07.2026 22:25"
    assert fmt_date("") == "—"
    assert fmt_date("позавчера") == "позавчера"   # не-ISO не переформатируем наугад


def test_niche_and_confidence_labels():
    assert niche_label("мужские-образы") == "мужские образы"
    assert niche_label("other") == "без категории"
    assert niche_label("") == "без категории"
    assert confidence_label("medium") == "средняя"
    assert confidence_label("странное") == "странное"


def test_agent_label_translates_known_and_keeps_unknown():
    assert agent_label("collect-tiktok") == "Сбор роликов из TikTok"
    assert agent_label("brief-reviewer") == "Проверка сценариев"
    assert agent_label("dashboard-factory") == "Этап «Контент-завод»"
    # новый агент не переводится наугад — честнее показать слаг, чем выдумать имя
    assert agent_label("совсем-новый-агент") == "совсем-новый-агент"
    assert agent_label("") == "—"


def test_trigger_label_translates_known_values():
    assert trigger_label("dashboard") == "с дашборда"
    assert trigger_label("cli") == "по расписанию"
    assert trigger_label("dry-run") == "тест"
    assert trigger_label("невиданный") == "невиданный"


def test_run_summary_extracts_human_facts_from_machine_dump():
    summary = run_summary("batches=7 rows=519 kept=257 drops=12 batches_failed=0")
    assert "просмотрено 519 роликов" in summary
    assert "отобрано 257" in summary
    assert "batches=" not in summary
    # профиль ниши: тема выводится словами, слаг-дефис уходит
    assert "тема «уход грумминг»" in run_summary(
        "raw_tiktok niche=уход-грумминг rows=61 passed=38")


def test_run_summary_names_the_stage_that_lost_instagram_batches():
    """Потери Instagram видны на пульте — и названы по стадии.

    До разбора 2026-07-27 правило искало \\bbatches_failed=, а IG печатает
    hashtag_/reel_batches_failed: \\b между «_» и «b» не срабатывает, оба
    word-символы. Прогон 27.07 01:21 с reel_batches_failed=2 и reels_lost=12
    показывался чисто зелёным — деградацию сбора не видел никто.
    """
    summary = run_summary(
        "hashtags=18 (discovery_failed=False) gate kept=73/180 "
        "hashtag_batches_failed=0 reel kept=28/30 reel_batches_failed=2 "
        "reels_lost=12 upsert updated=18 appended=10")
    assert "неудачных батчей по рилсам 2" in summary
    assert "потеряно рилсов 12" in summary
    # нулевую стадию не поминаем, и общее правило её потерь себе не приписывает
    assert "по хэштегам" not in summary
    assert "неудачных батчей 2" not in summary

    tt = run_summary("batches=6 rows=282 kept=92 drops=190 batches_failed=2 "
                     "sources_lost=7 upsert updated=80 appended=4")
    assert "неудачных батчей 2" in tt and "потеряно источников 7" in tt


def test_apify_spend_limit_is_not_shown_as_a_busy_system():
    """Отказ Apify по деньгам ≠ «система была занята, повторит позже».

    С разбора 2026-07-27 причина отказа Apify доезжает до input_summary. Её текст
    («monthly usage limit exceeded») попадал под маркеры лимитов Claude-сессии, и
    пульт обещал продюсеру, что прогон повторится сам. Не повторится: сбор стоит,
    пока владелец не поднимет потолок трат — обещание «само пройдёт» стоило бы
    ещё одних суток.
    """
    dump = ("batches=6 rows=282 kept=92 batches_failed=2 sources_lost=7 "
            "деградация: батч 4: apify start failed: monthly usage limit exceeded")
    summary = run_summary(dump)
    assert summary != "Система была занята, повторит позже."
    assert "неудачных батчей 2" in summary
    view = report_view({"title": "collect tiktok", "text": dump})
    assert view["banner"] is None
    assert view["collapse"] is True          # сырой текст по-прежнему под катом

    # сообщение самого агента про лимит Claude трактуется как прежде
    agent_msg = "Claude AI usage limit reached, try again later"
    assert run_summary(agent_msg) == "Система была занята, повторит позже."
    assert report_view({"title": "анализ", "text": agent_msg})["banner"] == \
        "Система была занята, повторит позже."


def test_run_summary_passes_through_human_text_and_gives_up_honestly():
    human = "конвейер занят (звено raw)"
    assert run_summary(human) == human          # уже человеческая строка
    assert run_summary("") == ""
    # непонятный дамп — пустая сводка (шаблон покажет имя задачи, сырое под катом)
    assert run_summary("zzz=1 qqq=2") == ""


def test_run_view_translates_row_and_marks_dry_run():
    view = run_view({"agent": "collect-tiktok", "trigger_type": "dry-run",
                     "status": "success", "errors": "[]",
                     "input_summary": "batches=1 rows=80 kept=17",
                     "completed_at": "2026-07-24T22:25:00+00:00"})
    assert view["task"] == "Сбор роликов из TikTok"
    assert view["agent_id"] == "collect-tiktok"   # слаг не выброшен
    assert view["dry_run"] is True                # тест ничего не сохранил
    assert view["trigger_label"] == "тест"
    assert "отобрано 17" in view["summary"]
    assert view["raw_summary"] == "batches=1 rows=80 kept=17"   # сырое под катом
    assert view["when"] == "24.07.2026 22:25"


def test_run_view_success_with_errors_is_not_plain_success():
    # «Успех» зелёным над красной ошибкой внутри строки — дезинформация продюсеру
    view = run_view({"agent": "brief-reviewer", "status": "success",
                     "errors": '["ревью брифов не удалась"]',
                     "completed_at": "2026-07-24T10:00:00+00:00"})
    assert view["badge_label"] == "Успех с ошибками"
    assert view["badge_class"] != "success"
    assert view["errors_list"] == ["ревью брифов не удалась"]


def test_run_view_keeps_unknown_status_and_clean_success():
    ok = run_view({"agent": "backup", "status": "success", "errors": "[]"})
    assert (ok["badge_class"], ok["badge_label"]) == ("success", "Успех")
    weird = run_view({"agent": "backup", "status": "странно"})
    assert weird["badge_label"] == "странно"


def test_link_label_shortens_reference_urls():
    assert link_label("https://www.tiktok.com/@dancox_7/video/7654496523778002184") \
        == "TikTok · @dancox_7"
    assert link_label("https://instagram.com/reel/ABC") == "Instagram"
    assert link_label("https://example.com/a") == "example.com/a"
    assert link_label("") == "—"


def test_run_summary_translates_statuses_and_hides_latin_identifiers():
    # статусы решения переводим прямо в готовой фразе (§2 словарь)
    assert run_summary("short-styling-idea-reel: approved") == "утверждено"
    # латинский идентификатор в сводке — признак машинной строки: она уходит под кат,
    # а не показывается продюсеру как «человеческая»
    assert run_summary("own_performance обновлён у 0/2 формул") == ""


def test_run_summary_translates_claude_limit_refusal():
    # «Что произошло» в истории задач не должно быть английской строкой отказа:
    # тот же случай уже переведён в отчётах этапов.
    row = run_view({"agent": "brief-reviewer", "status": "failed",
                    "input_summary": "You've reached your Fable 5 limit. "
                                     "Run /usage-credits to continue."})
    assert row["summary"] == "Система была занята, повторит позже."
    assert "Fable 5 limit" in row["raw_summary"]      # сырой текст остаётся под катом


def test_every_loss_counter_of_the_collectors_has_a_human_label():
    """Счётчик потерь без правила в словаре = невидимая деградация.

    Ровно так и вышло с Instagram: сборщик печатал hashtag_/reel_batches_failed,
    а правило искало голый batches_failed — ночь 27.07 висела на пульте зелёной
    (разбор 2026-07-27). Точечных проверок тут мало: следующая стадия сбора
    заведёт свой префикс, и подпись снова забудут. Поэтому сверяем не память, а
    исходники сборщиков: каждый счётчик, доезжающий до сводки, обязан иметь
    фразу для оператора.
    """
    import re as _re
    from pathlib import Path

    import cf.collect

    # Счётчик попадает в сводку только f-строкой «ключ={…}»: по ней и ищем, не
    # путая с ключами словарей ("batches_failed": …) и локальными переменными.
    key_re = _re.compile(r"([a-z_]*(?:batches_failed|sources_lost|reels_lost|"
                         r"runs_failed))=\{")
    keys = set()
    for path in sorted(Path(cf.collect.__file__).parent.glob("*.py")):
        keys.update(key_re.findall(path.read_text(encoding="utf-8")))
    # сканер жив: три известные формы на месте (иначе тест зелен на пустом месте)
    assert {"batches_failed", "hashtag_batches_failed",
            "reel_batches_failed"} <= keys
    for key in sorted(keys):
        phrase = run_summary(f"{key}=3")
        assert phrase and key not in phrase, \
            f"счётчик {key} не переведён — добавь правило в _SUMMARY_RULES"


# ── «Темп недели»: три полукольца (2026-07-28, элемент 9) ────────────────────

def test_tempo_rings_order_follows_the_conveyor():
    from cf.dashboard.labels import tempo_rings
    rings = tempo_rings({"weekly_target": 70, "briefs_approved_week": 54,
                         "assigned_week": 12, "reels_published_week": 3})
    assert [r["key"] for r in rings] == ["approved", "assigned", "published"]
    assert [r["label"] for r in rings] == ["одобрено", "назначен исполнитель",
                                           "выложено"]
    assert [r["value"] for r in rings] == [54, 12, 3]
    assert all(r["target"] == 70 for r in rings)
    # вложенность: внешнее кольцо шире среднего, среднее — внутреннего
    assert rings[0]["radius"] > rings[1]["radius"] > rings[2]["radius"]


def test_tempo_ring_dash_is_proportional_to_the_share_of_the_goal():
    from cf.dashboard.labels import tempo_rings
    ring = tempo_rings({"weekly_target": 70, "briefs_approved_week": 35})[0]
    drawn, total = (float(x) for x in ring["dash"].split())
    assert drawn == pytest.approx(total / 2, rel=1e-6)      # 35 из 70 — половина


def test_tempo_ring_above_the_goal_stops_at_the_full_arc():
    # Первый же разогнавшийся завод даст перелёт цели: дуга обязана остановиться
    # на полном полукольце, а не уехать за габарит плитки.
    from cf.dashboard.labels import tempo_rings
    ring = tempo_rings({"weekly_target": 70, "briefs_approved_week": 210})[0]
    drawn, total = (float(x) for x in ring["dash"].split())
    assert drawn == pytest.approx(total)
    assert ring["over"] is True
    assert ring["value"] == 210                            # само число не врём


def test_tempo_rings_survive_missing_and_dirty_numbers():
    from cf.dashboard.labels import tempo_rings
    for metrics in ({}, {"weekly_target": 0}, {"weekly_target": 70},
                    {"weekly_target": 70, "briefs_approved_week": "мусор"},
                    {"weekly_target": 70, "briefs_approved_week": -5}):
        for ring in tempo_rings(metrics):
            drawn, total = (float(x) for x in ring["dash"].split())
            assert 0 <= drawn <= total


def test_ring_path_is_a_semicircle_of_the_given_radius():
    from cf.dashboard.labels import RING_CENTER, ring_path
    cx, cy = RING_CENTER
    assert ring_path(52) == f"M {cx - 52} {cy} A 52 52 0 0 1 {cx + 52} {cy}"
