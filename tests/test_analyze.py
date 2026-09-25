from cf.analyze import analyze_rows, dedupe_rows, engagement_rate


def _row(url, views, likes=0, comments=0, shares=0, saves=0, account="a",
         transcript="есть текст", collected="2026-07-01", niche="тест"):
    return {"source_url": url, "views": views, "likes": likes, "comments": comments,
            "shares": shares, "saves": saves, "account": account,
            "transcript_text": transcript, "collected_at": collected, "niche": niche,
            "hook_text": "хук", "duration_sec": 15, "caption": "капшен"}


def test_dedupe_prefers_transcript_then_freshness():
    rows = [_row("u1", 100, transcript="", collected="2026-07-02"),
            _row("u1", 100, transcript="полный", collected="2026-07-01"),
            _row("u2", 100, collected="2026-07-01"),
            _row("u2", 100, collected="2026-07-03")]
    out = dedupe_rows(rows)
    assert len(out) == 2
    assert next(r for r in out if r["source_url"] == "u1")["transcript_text"] == "полный"
    assert next(r for r in out if r["source_url"] == "u2")["collected_at"] == "2026-07-03"


def test_er_zero_views_is_none():
    assert engagement_rate(_row("u", 0, likes=5)) is None
    assert engagement_rate(_row("u", 1000, likes=30, comments=10)) == 0.04


def test_insufficient_below_12_rows_over_threshold():
    rows = [_row(f"u{i}", 500) for i in range(20)]          # все ниже порога views
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert report["status"] == "insufficient_data"
    assert report["passed_threshold"] == 0


def test_boundary_exactly_12_rows_over_threshold():
    # Ровно min_rows строк над порогом: статус ok, квартиль = 12 // 4 = 3
    rows = [_row(f"u{i}", 2000, likes=i * 10) for i in range(12)]
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert report["status"] == "ok"
    assert len(report["winners"]) == 3
    assert len(report["losers"]) == 3


def test_winners_losers_and_same_account():
    # 16 строк над порогом: ER растёт с индексом, views одинаковые (медиана = views)
    rows = [_row(f"u{i}", 2000, likes=i * 10, account=f"acc{i % 4}") for i in range(16)]
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert report["status"] == "ok"
    w_urls = {w["source_url"] for w in report["winners"]}
    l_urls = {l["source_url"] for l in report["losers"]}
    assert w_urls == {"u12", "u13", "u14", "u15"}            # верхний квартиль ER
    assert l_urls == {"u0", "u1", "u2", "u3"}                # нижний квартиль ER
    # acc0 держит и winner (u12), и loser (u0) — пара для same-account сравнения
    pairs = {(p["account"]) for p in report["same_account"]}
    assert "acc0" in pairs


# --- Тикет 07 визуального контура: visual_facts в белом списке анализа ---


def test_visual_facts_reach_winners_and_losers():
    # Визуальные факты доезжают до артефакта анализа: раздел «Визуал»
    # паттернов опирается на наблюдаемое, а не на догадку по тексту.
    facts = '{"observed_media":"cover","visual_hook":"лицо крупно"}'
    rows = [_row(f"u{i}", 2000, likes=i * 10) for i in range(16)]
    for r in rows:
        r["visual_facts"] = facts
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert all(w["visual_facts"] == facts for w in report["winners"])
    assert all(l["visual_facts"] == facts for l in report["losers"])


def test_rows_without_vision_carry_empty_field_as_is():
    # «Медиа не разобрано» ≠ «фактов нет»: код ничего не синтезирует и не
    # подставляет — пустое поле уезжает пустым, различать их обязан промпт
    # анализатора (тикет 09), а вход анализа честно несёт факт отсутствия.
    rows = [_row(f"u{i}", 2000, likes=i * 10) for i in range(16)]
    report = analyze_rows(rows, min_rows=12, min_views=1000)
    assert all(w["visual_facts"] == "" for w in report["winners"])
    assert all(l["visual_facts"] == "" for l in report["losers"])


def test_visual_facts_not_hidden_by_heavy_projection():
    # Согласование с проекцией тяжёлых колонок (тикет 05): чтение строк для
    # анализа идёт read_rows без include_heavy, и попадание visual_facts в
    # HEAVY_COLUMNS молча спрятало бы факты от анализатора.
    from cf.sheets import HEAVY_COLUMNS
    assert "visual_facts" not in HEAVY_COLUMNS
