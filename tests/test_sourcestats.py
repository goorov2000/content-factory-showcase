from cf.sourcestats import build_source_stats


def row(source, niche="мужские-образы", views=5000, transcript="слова", caption=""):
    return {"source_query": source, "niche": niche, "views": views,
            "transcript_text": transcript, "caption": caption,
            "source_url": f"https://t/{source}/{views}"}


def test_per_source_aggregates():
    rows = [
        row("hashtag:#menswear"),
        row("hashtag:#menswear", niche="other", views=100, transcript=""),
        row("query:pov парень", niche="женская-мода"),
    ]
    stats = build_source_stats(rows, ["мужские-образы"])
    by = {s["source"]: s for s in stats["sources"]}
    m = by["hashtag:#menswear"]
    assert (m["rows"], m["target_niche_rows"], m["views_1000_plus"], m["with_transcript"]) == (2, 1, 1, 1)
    assert m["target_yield"] == 0.5
    assert by["query:pov парень"]["target_yield"] == 0.0


def test_sources_sorted_by_yield_then_rows():
    rows = [row("query:слабый", niche="other")] + [row("hashtag:#сильный")] * 3
    stats = build_source_stats(rows, ["мужские-образы"])
    assert [s["source"] for s in stats["sources"]] == ["hashtag:#сильный", "query:слабый"]


def test_candidate_hashtags_mined_from_target_captions():
    rows = [
        row("hashtag:#a", caption="лук дня #мужскойстиль #капсула", views=2000),
        row("hashtag:#a", caption="#капсула снова", views=3000),
        row("hashtag:#b", niche="other", caption="#мусорныйтег" * 3, views=9000),
        row("hashtag:#a", caption="#редкий", views=50),  # < 1000 views не участвует
    ]
    stats = build_source_stats(rows, ["мужские-образы"])
    tags = {c["hashtag"]: c["count"] for c in stats["candidate_hashtags"]}
    assert tags["#капсула"] == 2
    assert "#мусорныйтег" not in tags and "#редкий" not in tags
