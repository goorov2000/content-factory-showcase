import json
import re

BLOCK = re.compile(r"(// SOURCES:BEGIN[^\n]*\nconst SOURCES = )(\{.*?\})(;\n// SOURCES:END)",
                   re.DOTALL)


def parse_sources_block(js_text):
    m = BLOCK.search(js_text)
    if not m:
        raise ValueError("SOURCES:BEGIN/END markers not found")
    return json.loads(m.group(2))


def _strip(source):
    """'hashtag:#x' -> 'x'; 'query:q' -> 'q'."""
    kind, _, value = source.partition(":")
    return value.lstrip("#") if kind == "hashtag" else value


def apply_proposal_to_registry(records, proposal, today):
    """Применить approved source-proposal к реестру sources/<platform>.json (C3.3).

    remove -> status=retired (история сохраняется, в отличие от вычёркивания из
    JS-списка) с retired_at/retired_reason; add -> новая запись active/tuner либо
    реактивация существующей retired/paused. Ниша новых записей наследуется от
    первой active-записи (proposal её не несёт). Возвращает (новый список,
    сводка {added, retired, reactivated, missing})."""
    records = [dict(r) for r in records]
    by_key = {(r.get("kind"), r.get("query")): r for r in records}
    summary = {"added": 0, "retired": 0, "reactivated": 0, "missing": []}

    def registry_kind(source_or_kind):
        # proposal-грамматика 'query' -> реестровый 'search'
        return "search" if source_or_kind == "query" else source_or_kind

    for entry in proposal.get("remove", []):
        kind, _, _ = entry["source"].partition(":")
        rec = by_key.get((registry_kind(kind), _strip(entry["source"])))
        if rec is None:
            summary["missing"].append(entry["source"])
            continue
        rec["status"] = "retired"
        rec["retired_at"] = today
        rec["retired_reason"] = entry.get("reason", "")
        summary["retired"] += 1

    default_niche = next((r.get("niche") for r in records
                          if r.get("status") == "active" and r.get("niche")), "other")
    for entry in proposal.get("add", []):
        kind = registry_kind(entry["kind"])
        value = _strip(entry["source"])
        rec = by_key.get((kind, value))
        if rec is not None:
            # retired/paused -> реактивация; candidate -> повышение (C4.3):
            # approve promote-proposal переводит кандидата exploration в active
            if rec.get("status") in ("retired", "paused", "candidate"):
                rec["status"] = "active"
                summary["reactivated"] += 1
            continue
        new = {"query": value, "kind": kind,
               "niche": entry.get("niche") or default_niche,
               "status": "active", "origin": "tuner", "added_at": today}
        records.append(new)
        by_key[(kind, value)] = new
        summary["added"] += 1
    return records, summary


def apply_proposal_to_sources(js_text, proposal):
    sources = parse_sources_block(js_text)
    removed_tags = {_strip(r["source"]) for r in proposal.get("remove", [])
                    if r["source"].startswith("hashtag:")}
    removed_queries = {_strip(r["source"]) for r in proposal.get("remove", [])
                       if r["source"].startswith("query:")}
    hashtags = [h for h in sources.get("hashtags", []) if h not in removed_tags]
    queries = [q for q in sources.get("searchQueries", []) if q not in removed_queries]
    for a in proposal.get("add", []):
        value = _strip(a["source"])
        target = hashtags if a["kind"] == "hashtag" else queries
        if value not in target:
            target.append(value)
    new_block = json.dumps({"hashtags": hashtags, "searchQueries": queries},
                           ensure_ascii=False)
    return BLOCK.sub(lambda m: m.group(1) + new_block + m.group(3), js_text)
