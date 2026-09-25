CRITICAL_FIELDS = ["source_url", "account", "views", "posted_at", "niche"]


def profile_rows(rows, min_rows=20, dup_threshold=0.2):
    issues = []
    seen, deduped, duplicates = set(), [], 0
    for r in rows:
        key = str(r.get("source_url", "")).strip()
        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        deduped.append(r)

    clean, missing = [], []
    for r in deduped:
        if any(not str(r.get(f, "")).strip() for f in CRITICAL_FIELDS):
            missing.append(str(r.get("source_url") or "<no source_url>"))
        else:
            clean.append(r)

    if missing:
        issues.append(
            f"{len(missing)} rows missing critical fields "
            f"({', '.join(CRITICAL_FIELDS)}), например: {missing[:5]}"
        )
    if rows and duplicates / len(rows) > dup_threshold:
        issues.append(f"duplicate rate {duplicates}/{len(rows)} выше порога {dup_threshold:.0%}")
    ready = len(clean) >= min_rows
    if not ready:
        issues.append(f"insufficient_data: нужно >= {min_rows} чистых строк, есть {len(clean)}")

    return {
        "total_rows": len(rows),
        "duplicates_removed": duplicates,
        "rows_missing_critical_fields": len(missing),
        "clean_rows": len(clean),
        "issues_list": issues,
        "ready_for_analysis": ready,
    }
