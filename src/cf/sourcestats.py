import re
from collections import Counter


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_source_stats(rows, target_niches, candidate_limit=25):
    """Детерминированная агрегация по source_query. Evidence для /cf-tune-sources."""
    targets = {n.lower() for n in target_niches}
    per_source, tag_counter, tag_examples = {}, Counter(), {}
    for r in rows:
        source = str(r.get("source_query", "")).strip() or "unknown"
        s = per_source.setdefault(source, {
            "source": source, "rows": 0, "target_niche_rows": 0,
            "views_1000_plus": 0, "with_transcript": 0,
        })
        s["rows"] += 1
        views = _num(r.get("views"))
        on_target = str(r.get("niche", "")).strip().lower() in targets
        if on_target:
            s["target_niche_rows"] += 1
        if views >= 1000:
            s["views_1000_plus"] += 1
        if str(r.get("transcript_text", "")).strip():
            s["with_transcript"] += 1
        if on_target and views >= 1000:
            for tag in re.findall(r"#[\w\d_]+", str(r.get("caption", "")), re.UNICODE):
                tag = tag.lower()
                tag_counter[tag] += 1
                tag_examples.setdefault(tag, str(r.get("source_url", "")))
    for s in per_source.values():
        s["target_yield"] = round(s["target_niche_rows"] / s["rows"], 4) if s["rows"] else 0.0
    return {
        "sources": sorted(per_source.values(),
                          key=lambda s: (-s["target_yield"], -s["rows"], s["source"])),
        "candidate_hashtags": [
            {"hashtag": tag, "count": count, "example_url": tag_examples[tag]}
            for tag, count in tag_counter.most_common(candidate_limit) if count >= 2
        ],
        "target_niches": sorted(targets),
        "total_rows": len(rows),
    }
