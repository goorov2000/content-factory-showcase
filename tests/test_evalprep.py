from cf.evalprep import _verdict_gate, build_eval_dataset


def _ab_fixture(version_dates, formula="f-1"):
    """version_dates: {label: [published-даты]}. По одному замеру на reel,
    published_at = дата. label == 'unknown' -> бриф/замер БЕЗ prompt_version (версия не
    резолвится). Возвращает (perf, briefs, reels)."""
    perf, briefs, reels = [], [], []
    for label, dates in version_dates.items():
        for i, pub in enumerate(dates):
            rid, bid = f"{label}r{i}", f"{label}b{i}"
            reels.append({"reel_id": rid, "brief_id": bid, "published_at": pub})
            brief = {"brief_id": bid, "formula_id": formula, "review_status": "approved"}
            meas = {"reel_id": rid, "brief_id": bid, "views": "1000", "er": "0.05",
                    "measured_at": pub}
            if label != "unknown":
                brief["prompt_version"] = label
                meas["prompt_version"] = label
            briefs.append(brief)
            perf.append(meas)
    return perf, briefs, reels


def _pair(group, a, b):
    """comparable-пара {a,b} из группы (порядок версий в паре не важен) или None."""
    want = {a, b}
    return next((p for p in group["comparable_pairs"] if set(p["versions"]) == want), None)

PERFORMANCE = [
    {"reel_id": "r1", "brief_id": "b-1", "prompt_version": "v1", "views": "10000", "er": "0.05"},
    {"reel_id": "r2", "brief_id": "b-2", "prompt_version": "v1", "views": "30000", "er": "0.07"},
    {"reel_id": "r3", "brief_id": "b-3", "prompt_version": "v2", "views": "5000", "er": "0.02"},
]
BRIEFS = [
    {"brief_id": "b-1", "formula_id": "f-1", "prompt_version": "v1", "review_status": "approved"},
    {"brief_id": "b-2", "formula_id": "f-1", "prompt_version": "v1", "review_status": "approved"},
    {"brief_id": "b-3", "formula_id": "f-2", "prompt_version": "v2", "review_status": "rejected"},
]
VERSIONS = [
    {"prompt_id": "brief-pets", "version": "v2", "github_path": "prompts/briefs/pets/x.md", "active": "TRUE"},
    {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md", "active": "FALSE"},
]


def test_aggregates_by_prompt_version():
    # Ключ ведра — «промпт + версия» (пространство имён по formula_id, пока в строках
    # нет prompt_id): голая 'v1' одна на все темы, см. test_same_version_*.
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert ds["by_prompt_version"]["f-1:v1"] == {
        "reels": 2, "avg_views": 20000.0, "avg_er": 0.06, "median_views": 20000.0}
    assert ds["by_prompt_version"]["f-2:v2"]["reels"] == 1


def test_median_in_aggregates_heavy_tail():
    # median устойчив к тяжёлому хвосту: 3 обычных ролика + 1 вирусный
    perf = [
        {"reel_id": "a", "brief_id": "b", "prompt_version": "v1", "views": "1000", "er": "0.01"},
        {"reel_id": "c", "brief_id": "b", "prompt_version": "v1", "views": "2000", "er": "0.01"},
        {"reel_id": "d", "brief_id": "b", "prompt_version": "v1", "views": "3000", "er": "0.01"},
        {"reel_id": "e", "brief_id": "b", "prompt_version": "v1", "views": "900000", "er": "0.01"},
    ]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    agg = build_eval_dataset(perf, briefs, [])["by_prompt_version"]["v1"]
    assert agg["median_views"] == 2500.0        # (2000+3000)/2 — хвост не сносит
    assert agg["avg_views"] == 226500.0         # avg — сносит


def test_rows_carry_formula_link():
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert ds["rows"][0]["formula_id"] == "f-1"
    assert ds["rows"][0]["brief_found"] is True
    assert ds["rows"][0]["prompt_version"] == "v1"


def test_reviewer_pass_rate():
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert ds["reviewer_pass_rate"] == 2 / 3


def test_unknown_brief_falls_back():
    perf = [{"reel_id": "rX", "brief_id": "ghost", "prompt_version": "", "views": "1", "er": ""}]
    ds = build_eval_dataset(perf, [], [])
    assert ds["rows"][0]["prompt_version"] == "unknown"
    assert ds["rows"][0]["brief_found"] is False
    assert ds["reviewer_pass_rate"] is None


def test_active_versions_listed():
    ds = build_eval_dataset([], [], VERSIONS)
    assert ds["active_prompt_versions"] == [
        {"prompt_id": "brief-pets", "version": "v2", "github_path": "prompts/briefs/pets/x.md"}
    ]


# --- P5.8: дедуп замеров одного reel_id ---------------------------------------

def test_ten_measurements_one_reel_dedup_to_closest_to_7_days():
    # 10 ежедневных замеров одного ролика -> ровно 1 строка, ближайшая к 7-му дню
    # после публикации (published_at 07-01 -> цель 07-08).
    perf = [
        {"reel_id": "R1", "brief_id": "b-1", "views": str(day * 1000), "er": "0.01",
         "measured_at": f"2026-07-{day:02d}"}
        for day in range(2, 12)  # 07-02 .. 07-11
    ]
    reels = [{"reel_id": "R1", "published_at": "2026-07-01"}]
    briefs = [{"brief_id": "b-1", "prompt_version": "v1", "review_status": "approved"}]
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    assert len(ds["rows"]) == 1
    assert ds["rows"][0]["measured_at"] == "2026-07-08"   # 07-01 + 7 дней
    assert ds["rows"][0]["views"] == 8000.0
    assert ds["rows"][0]["days_after_publish"] == 7
    assert ds["by_prompt_version"]["v1"]["reels"] == 1


def test_published_at_datetime_with_time_still_parses():
    # published_at несёт время (штамп mark_published) — берётся только дата.
    perf = [
        {"reel_id": "R1", "brief_id": "b", "views": "100", "er": "0.01", "measured_at": "2026-07-05"},
        {"reel_id": "R1", "brief_id": "b", "views": "700", "er": "0.01", "measured_at": "2026-07-08"},
    ]
    reels = [{"reel_id": "R1", "published_at": "2026-07-01T09:00:00+00:00"}]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    assert len(ds["rows"]) == 1
    assert ds["rows"][0]["measured_at"] == "2026-07-08"   # цель 07-08


def test_no_published_at_falls_back_to_latest_measured_at():
    # published_at нет ни в reels, ни на замере -> последний по measured_at.
    perf = [
        {"reel_id": "R1", "brief_id": "b", "views": "100", "er": "0.01", "measured_at": "2026-07-03"},
        {"reel_id": "R1", "brief_id": "b", "views": "500", "er": "0.01", "measured_at": "2026-07-10"},
        {"reel_id": "R1", "brief_id": "b", "views": "300", "er": "0.01", "measured_at": "2026-07-06"},
    ]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [])   # reels не передан
    assert len(ds["rows"]) == 1
    assert ds["rows"][0]["views"] == 500.0       # самый свежий замер 07-10
    assert ds["rows"][0]["days_after_publish"] is None   # published_at неизвестен


def test_empty_published_at_string_uses_fallback():
    # published_at в reels — пустая строка (не заполнено оператором) -> fallback.
    perf = [
        {"reel_id": "R1", "brief_id": "b", "views": "100", "er": "0.01", "measured_at": "2026-07-03"},
        {"reel_id": "R1", "brief_id": "b", "views": "500", "er": "0.01", "measured_at": "2026-07-10"},
    ]
    reels = [{"reel_id": "R1", "published_at": ""}]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    assert len(ds["rows"]) == 1
    assert ds["rows"][0]["views"] == 500.0               # последний measured_at
    assert ds["rows"][0]["days_after_publish"] is None


def test_since_applied_after_dedup_drops_old_reel():
    # Старый рил: канонический 7-дневный замер 06-08 вне окна since, плюс поздний
    # 44-дневный замер 07-15 в окне. Дедуп выбирает 06-08 -> рил выпадает целиком.
    # Новый рил: 7-дневный замер 07-15 в окне -> остаётся.
    perf = [
        {"reel_id": "OLD", "brief_id": "b", "views": "100", "er": "0.01", "measured_at": "2026-06-08"},
        {"reel_id": "OLD", "brief_id": "b", "views": "999", "er": "0.01", "measured_at": "2026-07-15"},
        {"reel_id": "NEW", "brief_id": "b", "views": "500", "er": "0.01", "measured_at": "2026-07-15"},
    ]
    reels = [
        {"reel_id": "OLD", "published_at": "2026-06-01"},
        {"reel_id": "NEW", "published_at": "2026-07-08"},
    ]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [], reels=reels, since="2026-07-14")
    assert [r["reel_id"] for r in ds["rows"]] == ["NEW"]  # старый честно выпал
    assert ds["rows"][0]["views"] == 500.0                # не 999 от 44-дневного замера
    assert ds["rows"][0]["days_after_publish"] == 7
    assert ds["by_prompt_version"]["v1"]["reels"] == 1


def test_multiple_measurements_same_day_dedup_to_one():
    perf = [
        {"reel_id": "R1", "brief_id": "b", "views": "100", "er": "0.01", "measured_at": "2026-07-08"},
        {"reel_id": "R1", "brief_id": "b", "views": "200", "er": "0.02", "measured_at": "2026-07-08"},
    ]
    reels = [{"reel_id": "R1", "published_at": "2026-07-01"}]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    assert len(ds["rows"]) == 1
    assert ds["by_prompt_version"]["v1"]["reels"] == 1


def test_distinct_reels_are_not_merged():
    perf = [
        {"reel_id": "R1", "brief_id": "b", "views": "100", "er": "0.01", "measured_at": "2026-07-08"},
        {"reel_id": "R2", "brief_id": "b", "views": "200", "er": "0.02", "measured_at": "2026-07-08"},
    ]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [])
    assert len(ds["rows"]) == 2


# --- P5.8: join_health и порог unknown 20% ------------------------------------

def test_unknown_share_over_20pct_is_insufficient_data():
    perf, briefs = [], []
    for i in range(7):                       # 7 роликов со связкой -> known
        perf.append({"reel_id": f"R{i}", "brief_id": f"b{i}", "views": "1000", "er": "0.01"})
        briefs.append({"brief_id": f"b{i}", "prompt_version": "v1", "review_status": "approved"})
    for i in range(7, 10):                   # 3 ролика без брифа -> unknown (30%)
        perf.append({"reel_id": f"R{i}", "brief_id": "ghost", "views": "1000", "er": "0.01"})
    ds = build_eval_dataset(perf, briefs, [])
    assert ds["join_health"]["unknown_version_share"] == 0.3
    assert ds["join_health"]["briefs_not_found"] == 3
    assert ds["status"] == "insufficient_data"
    assert ds["warnings"]                     # предупреждение выставлено


def test_unknown_share_exactly_20pct_is_not_insufficient():
    perf, briefs = [], []
    for i in range(8):
        perf.append({"reel_id": f"R{i}", "brief_id": f"b{i}", "views": "1000", "er": "0.01"})
        # formula_id как в реальном брифе (схема требует) — иначе ведро версии
        # осталось бы безымянным и датасет честно предупредил бы об этом
        briefs.append({"brief_id": f"b{i}", "prompt_version": "v1", "formula_id": "f-1"})
    for i in range(8, 10):                    # 2/10 = ровно 20% -> НЕ insufficient
        perf.append({"reel_id": f"R{i}", "brief_id": "ghost", "views": "1000", "er": "0.01"})
    ds = build_eval_dataset(perf, briefs, [])
    assert ds["join_health"]["unknown_version_share"] == 0.2
    assert ds["status"] == "ok"
    assert ds["warnings"] == []


def test_briefs_not_found_counts_missing_join():
    perf = [
        {"reel_id": "R1", "brief_id": "b1", "views": "1", "er": "0.01"},
        {"reel_id": "R2", "brief_id": "ghost", "views": "1", "er": "0.01"},
    ]
    briefs = [{"brief_id": "b1", "prompt_version": "v1"}]
    jh = build_eval_dataset(perf, briefs, [])["join_health"]
    assert jh["briefs_not_found"] == 1
    assert jh["unknown_version_share"] == 0.5


# --- P5.8: строгие сравнения статусов как в дашборде --------------------------

def test_review_status_normalized_like_dashboard():
    # 'Approved ' (пробел + регистр) и 'REJECTED' учитываются как approved/rejected.
    briefs = [
        {"brief_id": "b1", "review_status": "Approved "},
        {"brief_id": "b2", "review_status": " REJECTED"},
        {"brief_id": "b3", "review_status": "Revised"},
    ]
    ds = build_eval_dataset([], briefs, [])
    assert ds["reviewed_briefs"] == 3
    assert ds["reviewer_pass_rate"] == 1 / 3   # только b1 approved


def test_active_tokens_normalized():
    versions = [
        {"prompt_id": "p", "version": "v3", "github_path": "x", "active": " yes "},
        {"prompt_id": "p", "version": "v2", "github_path": "x", "active": "1"},
        {"prompt_id": "p", "version": "v1", "github_path": "x", "active": "false"},
    ]
    ds = build_eval_dataset([], [], versions)
    active = {v["version"] for v in ds["active_prompt_versions"]}
    assert active == {"v3", "v2"}


# --- P5.8: пустой датасет ------------------------------------------------------

def test_empty_dataset_is_graceful():
    ds = build_eval_dataset([], [], [])
    assert ds["rows"] == []
    assert ds["by_prompt_version"] == {}
    assert ds["join_health"] == {"unknown_version_share": 0.0, "briefs_not_found": 0}
    assert ds["reviewer_pass_rate"] is None
    assert ds["status"] == "ok"
    assert ds["warnings"] == []
    assert ds["cohorts_by_formula"] == {}


# --- P5.13: параллельные когорты + verdict-гейт --------------------------------

def test_verdict_gate_4_vs_6_is_insufficient():
    # Приёмка P5.13: 4 vs 6 замеренных reels -> insufficient_data (порог P5.9: >=5 у каждой).
    status, insufficient = _verdict_gate({"v1": 6, "v2": 4})
    assert status == "insufficient_data"
    assert insufficient == ["v2"]


def test_verdict_gate_5_vs_6_is_ok():
    assert _verdict_gate({"v1": 6, "v2": 5}) == ("ok", [])


def test_verdict_gate_single_version_insufficient():
    # Одна версия в когорте — сравнивать не с чем: insufficient_data.
    assert _verdict_gate({"v1": 9}) == ("insufficient_data", [])


def test_rows_carry_cohort_field():
    ds = build_eval_dataset(PERFORMANCE, BRIEFS, VERSIONS)
    assert "cohort" in ds["rows"][0]


def test_interleaved_versions_form_one_parallel_cohort():
    # Interleaving: v1 и v2 одной формулы публикуются в пересекающихся окнах ->
    # одна параллельная когорта; у обеих по 6 замеренных reels -> пара сравнима (ok).
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 1, 2, 2, 3, 3)]
    v2_dates = [f"2026-07-{d:02d}" for d in (1, 2, 2, 3, 3, 4)]
    perf, briefs, reels = _ab_fixture({"v1": v1_dates, "v2": v2_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    groups = ds["cohorts_by_formula"]["f-1"]["parallel_groups"]
    assert len(groups) == 1
    assert set(groups[0]["versions"]) == {"v1", "v2"}
    assert _pair(groups[0], "v1", "v2")["verdict_gate"] == "ok"
    # обе строки версии несут один cohort-ключ
    cohorts = {r["prompt_version"]: r["cohort"] for r in ds["rows"]}
    assert cohorts["v1"] == cohorts["v2"] == groups[0]["cohort"]


def test_before_after_windows_are_separate_cohorts():
    # before/after: v1 (неделя 1) заменён v2 (неделя 3) — окна не пересекаются ->
    # две отдельные когорты, ни одной сравнимой пары.
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 2, 3, 4, 5, 6)]
    v2_dates = [f"2026-07-{d:02d}" for d in (20, 21, 22, 23, 24, 25)]
    perf, briefs, reels = _ab_fixture({"v1": v1_dates, "v2": v2_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    groups = ds["cohorts_by_formula"]["f-1"]["parallel_groups"]
    assert len(groups) == 2
    assert all(g["comparable_pairs"] == [] for g in groups)


def test_unknown_version_does_not_bridge_before_after():
    # Дефект 1: unknown-строки с окном, накрывающим оба периода, НЕ мостят before/after.
    # v1 (01-05) и v2 (20-24) не пересекаются; 5 unknown с окном 03-21 не должны их склеить.
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 2, 3, 4, 5)]
    v2_dates = [f"2026-07-{d:02d}" for d in (20, 21, 22, 23, 24)]
    unknown_dates = [f"2026-07-{d:02d}" for d in (3, 8, 12, 17, 21)]
    perf, briefs, reels = _ab_fixture(
        {"v1": v1_dates, "v2": v2_dates, "unknown": unknown_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    groups = ds["cohorts_by_formula"]["f-1"]["parallel_groups"]
    version_of = {v: g["cohort"] for g in groups for v in g["versions"]}
    # v1 и v2 в РАЗНЫХ группах; unknown не участвует в когортах вовсе
    assert version_of["v1"] != version_of["v2"]
    assert "unknown" not in version_of
    assert all("unknown" not in g["versions"] for g in groups)
    # unknown-строки не несут cohort
    unknown_cohorts = {r["cohort"] for r in ds["rows"] if r["prompt_version"] == "unknown"}
    assert unknown_cohorts == {""}


def test_chain_windows_compare_only_overlapping_pairs():
    # Дефект 2: цепочка окон v1(01-10) v2(08-20) v3(18-25): v1∩v2 и v2∩v3 пересекаются,
    # v1∩v3 = пусто. Сравнимы только пары (v1,v2) и (v2,v3), не (v1,v3).
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 3, 5, 7, 9, 10)]
    v2_dates = [f"2026-07-{d:02d}" for d in (8, 10, 12, 14, 18, 20)]
    v3_dates = [f"2026-07-{d:02d}" for d in (18, 19, 21, 23, 24, 25)]
    perf, briefs, reels = _ab_fixture({"v1": v1_dates, "v2": v2_dates, "v3": v3_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    groups = ds["cohorts_by_formula"]["f-1"]["parallel_groups"]
    assert len(groups) == 1                                  # транзитивная связная группа
    group = groups[0]
    assert set(group["versions"]) == {"v1", "v2", "v3"}
    assert _pair(group, "v1", "v2")["verdict_gate"] == "ok"
    assert _pair(group, "v2", "v3")["verdict_gate"] == "ok"
    assert _pair(group, "v1", "v3") is None                  # окна не пересекаются -> не пара


def test_adjacent_windows_touching_one_date_are_parallel():
    # Граница: окна касаются одной датой (v1 01-05, v2 05-10) -> пересечение -> параллельны.
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 2, 3, 4, 5, 5)]
    v2_dates = [f"2026-07-{d:02d}" for d in (5, 5, 6, 7, 8, 10)]
    perf, briefs, reels = _ab_fixture({"v1": v1_dates, "v2": v2_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    groups = ds["cohorts_by_formula"]["f-1"]["parallel_groups"]
    assert len(groups) == 1
    assert _pair(groups[0], "v1", "v2")["verdict_gate"] == "ok"


def test_parallel_cohort_4_vs_6_pair_is_insufficient_in_dataset():
    # Приёмка P5.13 на уровне датасета: параллельная пара 4 vs 6 -> insufficient_data.
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 1, 2, 2, 3, 3)]   # 6
    v2_dates = [f"2026-07-{d:02d}" for d in (1, 2, 2, 3)]         # 4
    perf, briefs, reels = _ab_fixture({"v1": v1_dates, "v2": v2_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    group = ds["cohorts_by_formula"]["f-1"]["parallel_groups"][0]
    assert group["versions"]["v1"]["reels"] == 6
    assert group["versions"]["v2"]["reels"] == 4
    pair = _pair(group, "v1", "v2")
    assert pair["verdict_gate"] == "insufficient_data"
    assert pair["insufficient_versions"] == ["v2"]


# ── H16 (аудит 2026-07-24): сбойный замер не вытесняет честный ────────────────

FAILED_NOTES = ("n8n_collected_metrics_only; "
                "collection_status=metrics_empty_or_unavailable; post_url=x")


def test_failed_measurement_skipped_when_valid_exists():
    perf = [
        {"reel_id": "r1", "brief_id": "b", "prompt_version": "v1", "views": "1200",
         "er": "0.05", "measured_at": "2026-07-06", "eval_notes": ""},
        # ближе к 7-му дню, но это упавший Apify-ран — не замер
        {"reel_id": "r1", "brief_id": "b", "prompt_version": "v1", "views": "0",
         "er": "0", "measured_at": "2026-07-08", "eval_notes": FAILED_NOTES},
    ]
    reels = [{"reel_id": "r1", "published_at": "2026-07-01"}]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    assert ds["by_prompt_version"]["v1"]["avg_views"] == 1200.0
    assert ds["by_prompt_version"]["v1"]["median_views"] == 1200.0


def test_all_measurements_failed_falls_back_to_failed_row():
    perf = [{"reel_id": "r1", "brief_id": "b", "prompt_version": "v1", "views": "0",
             "er": "0", "measured_at": "2026-07-08", "eval_notes": FAILED_NOTES}]
    briefs = [{"brief_id": "b", "prompt_version": "v1"}]
    ds = build_eval_dataset(perf, briefs, [])
    assert len(ds["rows"]) == 1                    # рил не исчез из датасета
    assert ds["rows"][0]["views"] == 0.0           # даунстрим-фильтры views>0 решают


# ── Gap#4: ключ агрегации различает промпты, а не только версию ───────────────
# Бриф пишет prompt_version как 'v1'/'v2' без имени промпта (schemas/brief.schema.json),
# и mark_published копирует это в строку рила. В CF Creative Briefs на 26.07.2026 лежат
# 26 строк 'v2' (мужские-образы) и 16 строк 'v1' (бренды-магазины) — с голым ключом их
# performance слился бы в одно ведро при первой же публикации обеих тем.

def _row(reel, brief, version, views, **brief_fields):
    """(замер, бриф) одной строки датасета: версия на замере И на брифе, как в жизни."""
    return ({"reel_id": reel, "brief_id": brief, "prompt_version": version,
             "views": views, "er": "0.05"},
            {"brief_id": brief, "prompt_version": version, **brief_fields})


def test_same_version_of_two_themes_does_not_merge():
    # Две темы, у обеих 'v2', рецепты разные -> два ведра, а не одно среднее.
    m1, b1 = _row("r1", "b1", "v2", "10000", formula_id="short-styling-idea-reel")
    m2, b2 = _row("r2", "b2", "v2", "200", formula_id="mass-brand-hot-take")
    ds = build_eval_dataset([m1, m2], [b1, b2], [])
    agg = ds["by_prompt_version"]
    assert "v2" not in agg                                   # голого ведра больше нет
    assert agg["short-styling-idea-reel:v2"]["reels"] == 1
    assert agg["short-styling-idea-reel:v2"]["avg_views"] == 10000.0
    assert agg["mass-brand-hot-take:v2"]["avg_views"] == 200.0


def test_prompt_id_is_preferred_namespace_and_unites_formulas():
    # Когда строка знает prompt_id (колонка появится у оператора/генератора), ведро
    # собирает ВСЕ рецепты одной темы — это истинная ось A/B версий промпта.
    m1, b1 = _row("r1", "b1", "v2", "1000", formula_id="f-1",
                  prompt_id="brief-мужские-образы-reel")
    m2, b2 = _row("r2", "b2", "v2", "3000", formula_id="f-2",
                  prompt_id="brief-мужские-образы-reel")
    ds = build_eval_dataset([m1, m2], [b1, b2], [])
    assert ds["by_prompt_version"]["brief-мужские-образы-reel:v2"]["reels"] == 2
    assert ds["rows"][0]["prompt_id"] == "brief-мужские-образы-reel"
    assert ds["rows"][0]["prompt_key"] == "brief-мужские-образы-reel:v2"


def test_legacy_rows_without_prompt_id_do_not_merge_into_new_bucket():
    # Обратная совместимость: старая строка (только formula_id) не падает и не
    # склеивается с новой (prompt_id) — вёдра разные, обе видны.
    m_old, b_old = _row("r1", "b1", "v2", "1000", formula_id="f-1")
    m_new, b_new = _row("r2", "b2", "v2", "3000", formula_id="f-1",
                        prompt_id="brief-мужские-образы-reel")
    ds = build_eval_dataset([m_old, m_new], [b_old, b_new], [])
    agg = ds["by_prompt_version"]
    assert agg["f-1:v2"]["reels"] == 1
    assert agg["brief-мужские-образы-reel:v2"]["reels"] == 1


def test_row_without_brief_keeps_unknown_bucket():
    # Замер без брифа: версия не резолвится -> ключ 'unknown' (не 'unknown:...'),
    # доля таких строк по-прежнему сторожится join_health.
    perf = [{"reel_id": "rX", "brief_id": "ghost", "views": "10", "er": "0.01"}]
    ds = build_eval_dataset(perf, [], [])
    assert list(ds["by_prompt_version"]) == ["unknown"]
    assert ds["rows"][0]["prompt_key"] == "unknown"
    assert ds["join_health"]["unknown_version_share"] == 1.0


def test_version_without_namespace_warns_not_silent():
    # Версия есть, а промпта/формулы нет (ручная строка без formula_id): ведро может
    # смешать разные промпты — датасет говорит об этом вслух (правило №2).
    m, b = _row("r1", "b1", "v2", "1000")
    ds = build_eval_dataset([m], [b], [])
    assert ds["by_prompt_version"]["v2"]["reels"] == 1
    assert any("версии без промпта/формулы" in w for w in ds["warnings"])


def test_cohort_versions_stay_bare_labels():
    # Внутри формулы пространство имён уже задано ею самой: вердикт-пары
    # cohorts_by_formula продолжают говорить 'v1'/'v2', а не 'f-1:v1'.
    v1_dates = [f"2026-07-{d:02d}" for d in (1, 1, 2, 2, 3)]
    v2_dates = [f"2026-07-{d:02d}" for d in (1, 2, 2, 3, 3)]
    perf, briefs, reels = _ab_fixture({"v1": v1_dates, "v2": v2_dates})
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    group = ds["cohorts_by_formula"]["f-1"]["parallel_groups"][0]
    assert sorted(group["versions"]) == ["v1", "v2"]
    assert _pair(group, "v1", "v2")["verdict_gate"] == "ok"
    # а плоский агрегат в это время namespace'ит ключи формулой
    assert ds["by_prompt_version"]["f-1:v1"]["reels"] == 5


# ── Тикет 05 (UTM-контур): деньги в датасете ──────────────────────────────────
# utm/orders опциональны по образцу reels: без них датасет ПРЕЖНЕЙ формы (без
# новых ключей и новых предупреждений — совместимость со старыми вызовами); с
# ними строки несут clicks_exact/clicks_estimated/orders/revenue, а агрегаты по
# версиям и когорты — те же поля суммами. Период атрибуции — окно всего
# датасета (границы не передаются), не месяц.

def _money_fixture():
    perf = [{"reel_id": "R1", "brief_id": "b1", "prompt_version": "v1",
             "views": "1000", "er": "0.05", "measured_at": "2026-07-08"},
            {"reel_id": "R2", "brief_id": "b2", "prompt_version": "v1",
             "views": "3000", "er": "0.05", "measured_at": "2026-07-08"}]
    briefs = [{"brief_id": "b1", "prompt_version": "v1", "formula_id": "f-1",
               "review_status": "approved"},
              {"brief_id": "b2", "prompt_version": "v1", "formula_id": "f-1",
               "review_status": "approved"}]
    reels = [{"reel_id": "R1", "brief_id": "b1", "published_at": "2026-07-01",
              "account": "acc-1"},
             {"reel_id": "R2", "brief_id": "b2", "published_at": "2026-07-01",
              "account": "acc-1"}]
    utm = [
        # метка ролика -> точные клики R1
        {"row_kind": "daily", "date": "2026-07-03", "account": "acc-1",
         "utm_campaign": "acc-1", "utm_content": "R1", "reel_id": "R1",
         "visits": 5, "users": 4},
        # остаток дня 10 делится по последним просмотрам: R1 1000, R2 3000
        {"row_kind": "daily", "date": "2026-07-03", "account": "acc-1",
         "utm_campaign": "acc-1", "utm_content": "", "reel_id": "",
         "visits": 10, "users": 8},
    ]
    orders = [{"order_id": "o1", "status": "confirmed",
               "order_date": "2026-07-05", "reel_id": "R1", "utm_content": "R1",
               "account": "acc-1", "revenue": "1990"}]
    return perf, briefs, reels, utm, orders


def test_rows_enriched_with_clicks_and_orders():
    perf, briefs, reels, utm, orders = _money_fixture()
    ds = build_eval_dataset(perf, briefs, [], reels=reels, utm=utm, orders=orders)
    by_reel = {r["reel_id"]: r for r in ds["rows"]}
    assert by_reel["R1"]["clicks_exact"] == 5
    assert by_reel["R1"]["clicks_estimated"] == 2.5      # 10 × 1000/4000
    assert by_reel["R2"]["clicks_estimated"] == 7.5      # 10 × 3000/4000
    assert by_reel["R1"]["orders"] == 1
    assert by_reel["R1"]["revenue"] == 1990
    assert by_reel["R2"]["clicks_exact"] == 0 and by_reel["R2"]["orders"] == 0
    # обе вкладки с данными — денежных предупреждений нет
    assert ds["warnings"] == []


def test_clicks_estimated_rounded_to_2_digits():
    perf, briefs, reels, utm, orders = _money_fixture()
    perf[1]["views"] = "2000"                            # веса 1000/2000 -> трети
    ds = build_eval_dataset(perf, briefs, [], reels=reels, utm=utm, orders=orders)
    by_reel = {r["reel_id"]: r for r in ds["rows"]}
    assert by_reel["R1"]["clicks_estimated"] == 3.33     # 10/3 округлено до 2 знаков
    assert by_reel["R2"]["clicks_estimated"] == 6.67


def test_aggregates_and_cohorts_carry_money_sums():
    perf, briefs, reels, utm, orders = _money_fixture()
    ds = build_eval_dataset(perf, briefs, [], reels=reels, utm=utm, orders=orders)
    agg = ds["by_prompt_version"]["f-1:v1"]
    assert agg["clicks_exact"] == 5
    assert agg["clicks_estimated"] == 10.0               # 2.5 + 7.5
    assert agg["orders"] == 1 and agg["revenue"] == 1990
    group = ds["cohorts_by_formula"]["f-1"]["parallel_groups"][0]
    assert group["versions"]["v1"]["clicks_exact"] == 5
    assert group["versions"]["v1"]["clicks_estimated"] == 10.0
    assert group["versions"]["v1"]["orders"] == 1
    assert group["versions"]["v1"]["revenue"] == 1990


def test_without_utm_orders_dataset_keeps_prior_shape():
    perf, briefs, reels, _, _ = _money_fixture()
    ds = build_eval_dataset(perf, briefs, [], reels=reels)
    assert "clicks_exact" not in ds["rows"][0]
    assert "orders" not in ds["rows"][0]
    assert "clicks_exact" not in ds["by_prompt_version"]["f-1:v1"]
    group = ds["cohorts_by_formula"]["f-1"]["parallel_groups"][0]
    assert "clicks_exact" not in group["versions"]["v1"]
    assert ds["warnings"] == []                          # и без новых предупреждений


def test_empty_money_tabs_warn_but_do_not_break_status():
    perf, briefs, reels, _, _ = _money_fixture()
    ds = build_eval_dataset(perf, briefs, [], reels=reels, utm=[], orders=[])
    assert ds["status"] == "ok"                          # гейт статуса не тронут
    row = ds["rows"][0]
    assert row["clicks_exact"] == 0 and row["clicks_estimated"] == 0
    assert row["orders"] == 0 and row["revenue"] == 0
    assert any("CF UTM Traffic" in w for w in ds["warnings"])
    assert any("CF Orders" in w for w in ds["warnings"])


def test_one_money_tab_present_other_warns():
    perf, briefs, reels, utm, _ = _money_fixture()
    ds = build_eval_dataset(perf, briefs, [], reels=reels, utm=utm, orders=None)
    by_reel = {r["reel_id"]: r for r in ds["rows"]}
    assert by_reel["R1"]["clicks_exact"] == 5            # переходы посчитаны
    assert by_reel["R1"]["orders"] == 0                  # заказов нет — нули
    assert not any("CF UTM Traffic" in w for w in ds["warnings"])
    assert any("CF Orders" in w for w in ds["warnings"])
