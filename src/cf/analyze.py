"""Метод v2 pattern-analyzer в коде: дедуп, пороги, квартили, same-account.

Агент получает готовые числа и делает только смысловую часть.
"""

ENGAGE_FIELDS = ("likes", "comments", "shares", "saves")
# visual_facts (тикет 07 визуального контура): winners/losers несут визуальные
# факты агенту-анализатору. Пустое значение = «медиа не разобрано» — код его
# не синтезирует и не подставляет, отличать «не разобрано» от «фактов нет»
# обязан промпт анализатора (тикет 09).
ROW_FIELDS = ("source_url", "account", "hook_text", "caption", "transcript_text",
              "duration_sec", "collected_at", "niche", "visual_facts")


def _num(value):
    # Боевой путь читает UNFORMATTED_VALUE, но запятая-разделитель тысяч
    # уже один раз ломала пайплайн — защита на случай CSV/JSON-фикстур.
    try:
        return float(str(value).strip().replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def engagement_rate(row):
    views = _num(row.get("views"))
    if views <= 0:
        return None
    return sum(_num(row.get(f)) for f in ENGAGE_FIELDS) / views


def dedupe_rows(rows):
    """Дубли по source_url: побеждает строка с транскриптом, при равенстве — свежая."""
    best = {}
    for row in rows:
        url = str(row.get("source_url", "")).strip()
        if not url:
            continue
        cur = best.get(url)
        if cur is None:
            best[url] = row
            continue
        key = (bool(str(row.get("transcript_text", "")).strip()),
               str(row.get("collected_at", "")))
        cur_key = (bool(str(cur.get("transcript_text", "")).strip()),
                   str(cur.get("collected_at", "")))
        if key > cur_key:
            best[url] = row
    return list(best.values())


def _slim(row, er):
    out = {f: row.get(f, "") for f in ROW_FIELDS}
    out["views"] = _num(row.get("views"))
    out["er"] = round(er, 5)
    return out


def _quartile_cut(values):
    """Индекс отсечения четверти списка (минимум 1 элемент)."""
    return max(1, len(values) // 4)


def analyze_rows(rows, min_rows=12, min_views=1000):
    rows = dedupe_rows(rows)
    scored, anomalies = [], 0
    for row in rows:
        er = engagement_rate(row)
        if er is None:
            anomalies += 1
            continue
        scored.append((row, er))
    passed = [(r, er) for r, er in scored if _num(r.get("views")) >= min_views]
    report = {
        "total_rows": len(rows), "anomalies_zero_views": anomalies,
        "passed_threshold": len(passed), "min_rows": min_rows, "min_views": min_views,
    }
    if len(passed) < min_rows:
        report["status"] = "insufficient_data"
        report["missing_rows"] = min_rows - len(passed)
        return report
    views_sorted = sorted(_num(r.get("views")) for r, _ in passed)
    median_views = views_sorted[len(views_sorted) // 2]
    by_er = sorted(passed, key=lambda p: p[1])
    losers = by_er[:_quartile_cut(by_er)]
    high_views = [(r, er) for r, er in by_er if _num(r.get("views")) >= median_views]
    winners = high_views[-_quartile_cut(high_views):]
    winner_accounts = {str(r.get("account", "")) for r, _ in winners}
    same_account = []
    for row, er in losers:
        acc = str(row.get("account", ""))
        if acc and acc in winner_accounts:
            same_account.append({"account": acc, "loser": _slim(row, er)})
    report.update({
        "status": "ok", "median_views": median_views,
        "winners": [_slim(r, er) for r, er in winners],
        "losers": [_slim(r, er) for r, er in losers],
        "same_account": same_account,
    })
    return report
