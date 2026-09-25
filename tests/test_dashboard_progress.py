"""Чистая математика прогресса ленты конвейера (Фаза 2б)."""

import math

from cf.dashboard.progress import (COLLECT_PHASES, DEFAULT_ETA_SEC, GATE_STEPS,
                                    GLOBAL_CAP, PHASE_PLANS,
                                    PIPELINE_STEPS, PRODUCING_STEPS, SEGMENT_CAP,
                                    SIM_CAP, bar_ceiling, bar_fraction,
                                    build_timeline, inner_fraction,
                                    median_duration, phase_label, phase_plan,
                                    phase_tau, simulated_fraction)


def test_pipeline_steps_shape():
    # Порядок ленты 2026-07-26: два операторских решения (рецепт, промпт темы)
    # стали ВИДИМЫМИ строками-воротами между «Черновиками рецептов» и «Сценариями».
    # Раньше их не было вовсе, и цикл шёл «Анализ → Сценарии» встык.
    # Вечером того же дня оба решения стали машинными: перед каждыми воротами
    # появилась строка работы («Проверка рецептов», «Включение промпта темы»), а
    # сами ворота стали очередью исключений — того, что машина решать отказалась.
    # «Доработка сценариев» стоит ДО ревью (27.07): один прогон ревьюера покрывает
    # и новые сценарии, и переписанные. До неё вердикт «доработка» был тупиком.
    keys = [s["key"] for s in PIPELINE_STEPS]
    assert keys == ["collect", "classify", "analyze", "formulas", "formula-review",
                    "gate-recipes", "prompt-draft", "prompt-apply", "gate-prompt",
                    "briefs", "fix", "review", "gate-briefs", "publish", "stats"]
    assert PRODUCING_STEPS == ["collect", "classify", "analyze", "formulas",
                               "formula-review", "prompt-draft", "prompt-apply",
                               "briefs", "fix", "review"]
    assert GATE_STEPS == ["gate-recipes", "gate-prompt", "gate-briefs"]


def test_every_step_declares_its_kind():
    # kind — то, по чему шаблон отличает автоматику от ворот и ручного этапа.
    assert {s["kind"] for s in PIPELINE_STEPS} == {"auto", "gate", "manual"}
    for step in PIPELINE_STEPS:
        assert (step["kind"] == "gate") == bool(step.get("gate")), step["key"]


def test_simulated_fraction_monotone_and_capped():
    eta = 60.0
    assert simulated_fraction(0, eta) == 0.0
    early = simulated_fraction(10, eta)
    late = simulated_fraction(120, eta)
    assert 0 < early < late                        # растёт со временем
    assert late <= SIM_CAP                          # но не добегает до 100% сам
    # даже на огромном elapsed — кэп держит
    assert simulated_fraction(10_000, eta) == SIM_CAP


def test_simulated_fraction_invalid_inputs_are_zero():
    assert simulated_fraction(None, 60) == 0.0
    assert simulated_fraction(-5, 60) == 0.0
    assert simulated_fraction(30, 0) == 0.0
    assert simulated_fraction(30, None) == 0.0


def test_simulated_fraction_matches_asymptote():
    eta = 45.0
    assert math.isclose(simulated_fraction(45, eta), 1 - math.exp(-1), rel_tol=1e-9)


def test_bar_fraction_done_is_full():
    assert bar_fraction({"status": "done", "done": 1, "total": 4}) == 1.0


def test_bar_fraction_real_done_total():
    assert bar_fraction({"status": "running", "done": 3, "total": 4}) == 0.75
    # прогресс v2: 100% только по подтверждению завершения, битые данные (done>total)
    # упираются в GLOBAL_CAP, а не рисуют «готово» раньше факта
    assert bar_fraction({"status": "running", "done": 9, "total": 4}) == GLOBAL_CAP


def test_bar_fraction_simulated_when_no_total():
    frac = bar_fraction({"status": "running", "eta_median": 60}, elapsed=60)
    assert 0 < frac <= SIM_CAP


def test_bar_fraction_idle_is_zero():
    assert bar_fraction({"status": "idle"}) == 0.0
    assert bar_fraction(None) == 0.0


def test_median_duration_from_run_log():
    rows = [
        {"agent": "brief-generator", "status": "success",
         "started_at": "2026-07-14T12:00:00+00:00",
         "completed_at": "2026-07-14T12:01:00+00:00"},   # 60s
        {"agent": "brief-generator", "status": "success",
         "started_at": "2026-07-14T13:00:00+00:00",
         "completed_at": "2026-07-14T13:02:00+00:00"},   # 120s
        {"agent": "brief-generator", "status": "success",
         "started_at": "2026-07-14T14:00:00+00:00",
         "completed_at": "2026-07-14T14:03:00+00:00"},   # 180s
        {"agent": "other", "status": "success",
         "started_at": "2026-07-14T14:00:00+00:00",
         "completed_at": "2026-07-14T14:30:00+00:00"},   # чужой агент — не в счёт
    ]
    assert median_duration(rows, "brief-generator") == 120.0


def test_median_duration_none_without_history():
    assert median_duration([], "brief-generator") is None
    assert median_duration([{"agent": "x", "status": "failed"}], "x") is None
    # запись без completed_at не роняет расчёт
    assert median_duration([{"agent": "brief-generator", "status": "success",
                             "started_at": "2026-07-14T12:00:00+00:00"}],
                           "brief-generator") is None


def test_median_duration_default_eta_is_sane():
    assert DEFAULT_ETA_SEC > 0


# ── build_timeline: строки ленты для шаблона ─────────────────────────────────


def _rows_by_key(rows):
    return {r["key"]: r for r in rows}


def test_build_timeline_producing_and_downstream_split():
    run_progress = {
        "collect": {"status": "done", "done": 2, "total": 2, "count": 0,
                    "produced": 118, "produced_new": 24},
        "classify": {"status": "done", "done": 2, "total": 2, "produced": 107,
                     "left": 0},
        "analyze": {"status": "running", "done": 1, "total": 3, "count": 4,
                    "started_at": None, "eta_median": 90},
        "briefs": {"status": "idle"},
        "review": {"status": "idle"},
    }
    metrics = {"briefs_pending": 3, "reels_published": 5, "awaiting_stats": 2}
    rows = build_timeline(run_progress, metrics, now=None)
    by = _rows_by_key(rows)
    # производящие шаги считают добычу, а не единицы обхода машины
    assert by["analyze"]["status"] == "running"
    assert by["analyze"]["count_label"] == "1 из 3 тем · 4 рецепта"
    assert by["collect"]["count_label"] == "118 роликов · 24 новых"
    assert by["classify"]["count_label"] == "107 роликов размечено"
    # downstream — живой бэклог из metrics
    assert by["publish"]["count_label"] == "5 роликов вышло"
    assert by["stats"]["count_label"] == "2 ждут данных"


def test_classify_label_never_claims_a_remainder_it_could_not_count():
    # Правило №2 CLAUDE.md: одна raw-вкладка прочиталась, вторая нет. Назвать
    # сумму «без темы» нельзя — «107 размечено · 0 без темы» читается как
    # «работа кончилась», пока в непрочитанной вкладке лежат десятки строк.
    rows = build_timeline({"classify": {"status": "done", "produced": 107,
                                        "left": None, "left_unknown": True}},
                          {}, now=None)
    label = next(r for r in rows if r["key"] == "classify")["count_label"]
    assert label == "107 роликов размечено · остаток неизвестен"
    # ничего не разметили и сосчитать не смогли — это НЕ «размечать было нечего»
    rows = build_timeline({"classify": {"status": "done", "produced": 0,
                                        "left": None, "left_unknown": True}},
                          {}, now=None)
    assert next(r for r in rows if r["key"] == "classify")["count_label"] \
        == "не удалось посчитать"


def test_build_timeline_marks_stage_entry_for_run_button():
    rows = _rows_by_key(build_timeline({}, {}, now=None))
    # ▶ рисуется на первом шаге звена: collect(raw), classify(factory), stats, publish
    assert rows["collect"]["stage_entry"] and rows["collect"]["stage"] == "raw"
    assert rows["classify"]["stage_entry"] and rows["classify"]["stage"] == "factory"
    assert not rows["analyze"]["stage_entry"]      # analyze — часть factory, своей ▶ нет
    assert not rows["briefs"]["stage_entry"]
    assert rows["stats"]["stage_entry"] and rows["publish"]["stage_entry"]


def test_gate_rows_carry_state_and_count_from_gates():
    gates = {"recipes": {"blocking": True, "count": 2,
                         "rows": [{"niche": "а"}, {"niche": "б"}],
                         "informational": []},
             "prompt": {"blocking": False, "count": 0, "rows": [],
                        "informational": []},
             "briefs": {"blocking": False, "count": 3, "rows": [],
                        "informational": []}}
    rows = _rows_by_key(build_timeline({}, {}, now=None, gates=gates))
    assert rows["gate-recipes"]["status"] == "blocked"
    assert rows["gate-recipes"]["count_label"] == "2 темы ждут вас"
    # Закрытые ворота не молчат: пустая строка посреди ленты читается как «сломано».
    assert rows["gate-prompt"]["status"] == "passed"
    assert rows["gate-prompt"]["count_label"] == "правила сценариев включены"
    # Ворота сценариев машину не держат — счётчик есть, красного нет.
    assert rows["gate-briefs"]["status"] == "passed"
    assert rows["gate-briefs"]["count_label"] == "3 ждут решения"


def test_gate_row_says_unknown_when_gates_not_computed():
    # Правило №2: сбой чтения формул/Sheets не должен выдаваться за «всё разобрано».
    rows = _rows_by_key(build_timeline({}, {}, now=None, gates=None))
    assert rows["gate-recipes"]["count_label"] == "—"
    assert rows["gate-recipes"]["note"] == "состояние ворот не посчитано"


# ── подпись актора: кто на самом деле закрывает шаг (2026-07-28, элемент 17в) ──

def test_gate_row_takes_its_actor_from_the_gate_not_from_the_constant():
    # Жёстко зашитого актора у строки-ворот не остаётся: подпись приезжает из
    # самих ворот, а те выводят её из политики (queues.gate_actor).
    gates = {"recipes": {"blocking": False, "count": 0, "rows": [],
                         "informational": [], "actor": "CLAUDE CODE"},
             "prompt": {"blocking": False, "count": 0, "rows": [],
                        "informational": [], "actor": "вы"}}
    rows = _rows_by_key(build_timeline({}, {}, now=None, gates=gates))
    assert rows["gate-recipes"]["actor"] == "CLAUDE CODE"
    assert rows["gate-prompt"]["actor"] == "вы"


def test_gate_row_falls_back_to_human_when_gates_not_computed():
    # Правило №2: пока ворота не посчитаны, обещать «за вас это сделает машина»
    # нельзя — безопасная сторона это «вы».
    rows = _rows_by_key(build_timeline({}, {}, now=None, gates=None))
    assert rows["gate-recipes"]["actor"] == "вы"
    assert rows["gate-prompt"]["actor"] == "вы"


def test_shooting_step_is_signed_you_not_third_person():
    # «ПРОДЮСЕР» звучал как третье лицо, хотя это и есть тот, кто смотрит на экран.
    rows = _rows_by_key(build_timeline({}, {}, now=None))
    assert rows["publish"]["actor"] == "вы"
    assert "ПРОДЮСЕР" not in {s["actor"] for s in PIPELINE_STEPS}


def test_prompt_steps_are_named_in_producer_language():
    # «Промпт темы» непонятен непогружённому: это правила, по которым пишутся
    # сценарии темы. Слово меняется во всех трёх строках сразу.
    labels = {s["key"]: s["label"] for s in PIPELINE_STEPS}
    assert labels["prompt-draft"] == "Черновик правил сценариев темы"
    assert labels["prompt-apply"] == "Включение правил сценариев темы"
    assert not [k for k, v in labels.items() if "промпт" in v.lower()]


def test_idle_reason_replaces_note_for_skipped_worker():
    # Пустая очередь -> платный вызов не делается, и полоска обязана объяснить,
    # почему она пустая, иначе шаг читается как зависший.
    progress = {"briefs": {"status": "idle",
                           "idle_reason": "не запускается: 3 темы ждут включения промпта"}}
    row = _rows_by_key(build_timeline(progress, {}, now=None))["briefs"]
    assert row["note"] == "не запускается: 3 темы ждут включения промпта"


def test_build_timeline_running_bar_carries_ticker_data():
    # Шаблон гейтит тикер по status == "running" (partials/stages.html) и читает
    # только frac/ceil/tau/run. Отдельного «это симуляция» в строке нет — два
    # источника правды заставляли бы диффать шаблон, чтобы понять, кто главный.
    run_progress = {"briefs": {"status": "running", "eta_median": 60,
                               "started_at": 100.0}}
    briefs = _rows_by_key(build_timeline(run_progress, {}, now=130.0))["briefs"]
    assert 0 < briefs["fraction"] <= briefs["ceiling"] <= GLOBAL_CAP
    assert briefs["tau"] > 0
    assert "is_sim" not in briefs and "eta_median" not in briefs


def test_build_timeline_tolerates_empty_inputs():
    rows = build_timeline(None, None, now=None)
    assert len(rows) == len(PIPELINE_STEPS)
    assert _rows_by_key(rows)["publish"]["count_label"] == "—"  # нет метрик → прочерк


# ── Прогресс v2: факты как якоря, кривая как интерполяция (спека §7) ──────────


def test_phase_plan_resolves_from_state_then_step():
    assert phase_plan("collect") == COLLECT_PHASES
    ig = phase_plan("collect", {"phase_plan": "instagram"})
    assert [p[0] for p in ig] == ["prepare", "fetch", "gate", "reels", "write"]
    assert phase_plan("briefs") == ()          # claude-шаг фаз не имеет
    assert sum(w for _, w, _ in COLLECT_PHASES) == 1.0   # веса — ровно единица


def test_phase_plans_cover_only_platforms_the_dashboard_actually_runs():
    # Планы фаз существуют для единиц работы, которые ведёт звено с полоской:
    # это raw-сбор (tiktok/instagram). У «Статистики» producing-шага нет, snowball
    # звенья не запускают вовсе — план для них выглядел бы работающей проводкой.
    assert set(PHASE_PLANS) == {"tiktok", "instagram"}


def test_collect_starts_at_prepare_weight_not_zero():
    # «быстрые первые 15–25%»: подготовка реально закончена (реестр прочитан,
    # батчи собраны) — бар честно стоит на её весе, а не на нуле
    state = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
             "phase": "fetch", "phase_done": 0, "phase_total": 8}
    frac = bar_fraction(state, 0, "collect")
    assert 0.07 < frac < 0.08          # 0.15 веса prepare / 2 платформы


def test_collect_fetch_uses_real_batches_as_anchor():
    base = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
            "phase": "fetch", "phase_total": 8}
    fracs = [bar_fraction(dict(base, phase_done=k), 0, "collect") for k in range(9)]
    assert fracs == sorted(fracs)                      # монотонно по фактам
    # 8/8 батчей = конец фазы fetch внутри первой платформы: (0.15+0.60)/2
    assert abs(fracs[-1] - 0.375) < 1e-6
    assert fracs[4] > fracs[3]                         # каждый батч виден


def test_progress_never_goes_backwards_across_phases_and_units():
    seq = [
        {"phase": "prepare"},
        {"phase": "fetch", "phase_done": 3, "phase_total": 8},
        {"phase": "fetch", "phase_done": 8, "phase_total": 8},
        {"phase": "gate"},
        {"phase": "write"},
    ]
    prev = 0.0
    for extra in seq:                                  # первая платформа
        state = {"status": "running", "done": 0, "total": 2,
                 "phase_plan": "tiktok", **extra}
        frac = bar_fraction(state, 30, "collect")
        assert frac >= prev, (extra, frac, prev)
        prev = frac
    # переход к следующей платформе: факт (done=1) выше потолка предыдущей единицы
    nxt = bar_fraction({"status": "running", "done": 1, "total": 2,
                        "phase_plan": "tiktok", "phase": "prepare"}, 0, "collect")
    assert nxt >= prev


def test_bar_never_reaches_full_until_server_confirms():
    almost = {"status": "running", "done": 1, "total": 2, "phase_plan": "tiktok",
              "phase": "write", "phase_started_at": 0}
    assert bar_fraction(almost, 10_000, "collect") <= GLOBAL_CAP
    # подтверждение завершения — и только тогда 100%
    assert bar_fraction(dict(almost, status="done"), 10_000, "collect") == 1.0


def test_bar_ceiling_is_the_next_anchor_not_the_end_of_phase():
    # Между батчами сервер стоит на месте: если целиться в конец фазы, клиентская
    # кривая убегает вперёд, и каждый опрос htmx тянет полоску назад (жалоба
    # оператора «её как будто отталкивает», 2026-07-25). Потолок = следующий батч.
    state = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
             "phase": "fetch", "phase_done": 1, "phase_total": 8}
    ceil = bar_ceiling(state, "collect")
    assert ceil >= bar_fraction(state, 5, "collect")   # потолок не ниже текущего
    # ровно там, где сервер окажется на следующем факте — ни выше, ни ниже
    assert abs(ceil - bar_fraction(dict(state, phase_done=2), 5, "collect")) < 1e-9
    assert ceil < 0.375                                # конец fetch — дальше потолка
    # последний батч фазы: дальше конца фазы потолок не уезжает
    last = dict(state, phase_done=8)
    assert abs(bar_ceiling(last, "collect") - 0.375) < 1e-9
    # у шага без фаз потолок — конец единицы по SEGMENT_CAP
    assert bar_ceiling({"status": "running", "eta_median": 60}, "briefs") == SEGMENT_CAP


def test_bar_ceiling_of_curve_phase_matches_server_limit():
    # У фазы без якорей кэп сервера — SEGMENT_CAP; потолок клиента обязан совпасть,
    # иначе тикер целится выше, чем сервер способен показать, и расходится с ним.
    state = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
             "phase": "prepare", "eta_median": 90}
    assert abs(bar_ceiling(state, "collect")
               - bar_fraction(state, 10_000, "collect")) < 1e-9


def test_phase_tau_scales_with_phase_weight():
    state = {"status": "running", "phase_plan": "tiktok", "phase": "fetch",
             "eta_median": 100}
    assert phase_tau("collect", state) == 60.0         # вес fetch 0.6
    assert phase_tau("collect", dict(state, phase="write")) == 10.0
    assert phase_tau("briefs", {"eta_median": 100}) == 100.0
    # в фазе с якорями отрезок кривой — ОДИН батч, значит и τ делится на их число
    assert phase_tau("collect", dict(state, phase_done=1, phase_total=8)) == 7.5
    assert phase_tau("collect", dict(state, phase_done=1, phase_total=1000)) == 1.0


def test_phase_tau_is_scoped_to_one_unit_of_work():
    # eta_median измерен по ВСЕМУ шагу (обе платформы сбора), а кривая живёт
    # внутри ОДНОЙ единицы: без деления на их число полоска ползёт в `total` раз
    # медленнее фазы — тот самый «шаг выглядит замёрзшим», ради которого делали v2.
    state = {"status": "running", "total": 2, "phase_plan": "tiktok",
             "phase": "gate", "eta_median": 1080}
    assert phase_tau("collect", state) == 81.0                      # 1080·0.15/2
    assert phase_tau("collect", dict(state, total=None)) == 162.0    # единица одна
    # фаза с якорями: отрезок кривой — один батч ОДНОЙ платформы
    assert phase_tau("collect", dict(state, phase="fetch", phase_done=1,
                                     phase_total=8)) == 40.5        # 1080·0.6/2/8


def test_curve_inside_unit_advances_at_the_pace_of_the_phase():
    # за длительность самой фазы (81 с) кривая обязана пройти больше половины
    # своего сегмента; со старой τ=162 она проходила меньше
    state = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
             "phase": "gate", "eta_median": 1080}
    assert inner_fraction("collect", state, 81) > 0.75 + 0.15 * 0.5


def test_ticker_curve_reaches_next_anchor_without_overshooting():
    # Контракт с тикером: за время τ·N кривая подходит к следующему якорю снизу.
    # Проверяем математику клиента (та же формула) на серверных числах.
    state = {"status": "running", "done": 1, "total": 2, "phase_plan": "instagram",
             "phase": "fetch", "phase_done": 1, "phase_total": 5, "eta_median": 90}
    frac = bar_fraction(state, 40, "collect")
    ceil, tau = bar_ceiling(state, "collect"), phase_tau("collect", state)
    prev = frac
    for dt in (1, 5, 10, 30, 120):
        shown = ceil - (ceil - frac) * math.exp(-dt / tau)
        # монотонно и без обгона. Строгое «<» держится, пока экспонента различима
        # в double: τ теперь считается внутри ОДНОЙ единицы (≈3 с на батч), и к
        # dt=120 (≈38τ) остаток уходит под ulp — кривая садится ровно на якорь.
        # Это и есть асимптота, а не обгон: якорь — то, что сервер подтвердит.
        assert prev <= shown <= ceil
        if dt <= 30:
            assert shown < ceil
        prev = shown
    # следующий факт подтверждает то, к чему кривая шла, и не отматывает назад
    assert bar_fraction(dict(state, phase_done=2), 0, "collect") >= prev


def test_phase_label_says_what_is_happening_now():
    state = {"status": "running", "unit_label": "TikTok", "phase_plan": "tiktok",
             "phase": "fetch", "phase_done": 3, "phase_total": 8}
    assert phase_label("collect", state) == "TikTok · Собираем ролики · 3/8 батчей"
    assert phase_label("collect", dict(state, phase="gate", phase_done=None,
                                       phase_total=None)) \
        == "TikTok · Отбираем годные"
    ig = {"status": "running", "unit_label": "Instagram", "phase_plan": "instagram",
          "phase": "reels", "phase_done": 2, "phase_total": 5}
    assert phase_label("collect", ig) == "Instagram · Забираем ролики · 2/5 батчей"
    assert phase_label("briefs", {"status": "running"}) == "Пишем сценарии"
    assert phase_label("briefs", {"status": "idle"}) == ""   # не бежит — не пишем


def test_unknown_phase_marker_does_not_break_or_move_bar():
    state = {"status": "running", "done": 0, "total": 2, "phase_plan": "tiktok",
             "phase": "чего-то-новое"}
    assert bar_fraction(state, 30, "collect") == 0.0
    assert phase_label("collect", state) == ""


def test_build_timeline_exposes_ticker_contract():
    run_progress = {"collect": {"status": "running", "done": 0, "total": 2,
                                "phase_plan": "tiktok", "phase": "fetch",
                                "phase_done": 2, "phase_total": 8,
                                "started_at": 100.0, "phase_started_at": 110.0,
                                "eta_median": 120, "unit_label": "TikTok"}}
    row = _rows_by_key(build_timeline(run_progress, {}, now=140.0))["collect"]
    assert row["status"] == "running"        # тикер включает шаблон по статусу
    assert 0 < row["fraction"] <= row["ceiling"] <= GLOBAL_CAP
    assert row["tau"] > 0
    assert row["elapsed"] == 40.0                      # «идёт 0:40» — от старта шага
    assert row["phase_label"] == "TikTok · Собираем ролики · 2/8 батчей"
    assert row["run_token"] == 100.0                   # метка запуска шага


def test_run_token_marks_a_new_bar_for_the_client():
    # Тикер копит показанную ширину по шагу и назад её не отдаёт. Отличить новый
    # прогон от «сервер стоит между фактами» он может только по метке запуска:
    # та же полоска — та же метка, другой запуск — другая.
    state = {"status": "running", "done": 0, "total": 2, "started_at": 100.0}
    first = _rows_by_key(build_timeline({"collect": state}, {}, now=140.0))["collect"]
    same = _rows_by_key(build_timeline({"collect": dict(state, done=1)}, {},
                                       now=200.0))["collect"]
    again = _rows_by_key(build_timeline({"collect": dict(state, started_at=900.0)},
                                        {}, now=940.0))["collect"]
    assert first["run_token"] == same["run_token"] != again["run_token"]
    # не бегущие шаги метки не имеют — там нечего копить
    assert _rows_by_key(build_timeline({}, {}, now=1.0))["publish"]["run_token"] == ""


# ── Счёт в единицах результата (правка оператора 2026-07-25) ─────────────────


def test_count_labels_speak_in_results_not_machine_units():
    def label(key, state):
        rows = _rows_by_key(build_timeline({key: state}, {}, now=None))
        return rows[key]["count_label"]

    # Сбор: ролики и сколько из них новых
    assert label("collect", {"status": "done", "produced": 1, "produced_new": 1}) \
        == "1 ролик · 1 новый"
    assert label("collect", {"status": "done", "produced": 0}) == "новых роликов нет"
    assert label("collect", {"status": "running"}) == "идёт…"   # ещё нечего показать
    # Разметка: размечено / осталось без темы / нечего было размечать
    assert label("classify", {"status": "done", "produced": 3, "left": 2}) \
        == "3 ролика размечено · 2 без темы"
    assert label("classify", {"status": "done", "produced": 0, "left": 4}) \
        == "4 без темы — не размечено"
    assert label("classify", {"status": "done", "produced": 0, "left": 0}) \
        == "размечать было нечего"
    # Анализ: пустая очередь — это ответ, а не «0/0»
    assert label("analyze", {"status": "done", "done": 0, "total": 0}) \
        == "тем в очереди нет"
    assert label("analyze", {"status": "done", "done": 2, "total": 2, "count": 1}) \
        == "2 из 2 тем · 1 рецепт"
    # Сценарии и ревью
    assert label("briefs", {"status": "done", "produced": 5}) == "5 сценариев"
    assert label("briefs", {"status": "done", "produced": 0}) == "сценариев нет"
    assert label("review", {"status": "done", "produced": 1}) == "1 проверен"
    assert label("review", {"status": "done", "produced": 0}) == "проверять было нечего"


def test_finished_step_reports_how_long_it_ran():
    # Таймер не исчезает вместе с завершением: «сколько это заняло» спрашивают после.
    done = _rows_by_key(build_timeline(
        {"collect": {"status": "done", "duration": 1082.8, "started_at": None}},
        {}, now=None))["collect"]
    assert done["duration"] == 1082.8 and done["elapsed"] is None
    # мгновенный шаг («тем в очереди нет») не подписывается «шёл 0:00»
    instant = _rows_by_key(build_timeline(
        {"analyze": {"status": "done", "duration": 0.004}}, {}, now=None))["analyze"]
    assert instant["duration"] is None
    # у бегущего шага — живой таймер, итоговой длительности ещё нет
    running = _rows_by_key(build_timeline(
        {"collect": {"status": "running", "started_at": 100.0, "duration": 5.0}},
        {}, now=130.0))["collect"]
    assert running["duration"] is None and running["elapsed"] == 30.0
