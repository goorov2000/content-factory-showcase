def published_brief_ids(reels):
    """Множество brief_id, у которых есть хотя бы один опубликованный рил.

    Тот же join по brief_id, что в build_trace (str-сравнение), но для всего листа
    сразу — дашборд отличает «одобрен, ждёт съёмки» от «вышел» одним проходом.
    Рилы без brief_id (сирота-строка) в множество не попадают."""
    return {str(r.get("brief_id")) for r in reels
            if str(r.get("brief_id", "")).strip()}


def build_trace(brief_id, briefs, reels, performance):
    brief = next((b for b in briefs if str(b.get("brief_id")) == str(brief_id)), None)
    if brief is None:
        return {"brief_id": str(brief_id), "found": False, "missing_links": ["brief"]}

    linked_reels = [r for r in reels if str(r.get("brief_id")) == str(brief_id)]
    perf = [p for p in performance if str(p.get("brief_id")) == str(brief_id)]

    missing = []
    for field in ("source_pattern_ids", "formula_id", "prompt_version"):
        if not str(brief.get(field, "")).strip():
            missing.append(field)
    if not linked_reels:
        missing.append("published_reels")
    if linked_reels and not perf:
        missing.append("performance_rows")

    return {
        "brief_id": str(brief_id),
        "found": True,
        "source_pattern_ids": brief.get("source_pattern_ids", ""),
        "formula_id": brief.get("formula_id", ""),
        "prompt_version": brief.get("prompt_version", ""),
        "reels": [{"reel_id": r.get("reel_id"), "platform": r.get("platform"),
                   "post_url": r.get("post_url")} for r in linked_reels],
        "performance": [{"reel_id": p.get("reel_id"), "views": p.get("views"),
                         "er": p.get("er"), "measured_at": p.get("measured_at")}
                        for p in perf],
        "missing_links": missing,
    }
