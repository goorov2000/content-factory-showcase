from cf.trace import build_trace, published_brief_ids

BRIEFS = [{"brief_id": "b-1", "source_pattern_ids": "p-1,p-2", "formula_id": "f-1",
           "prompt_version": "v1"}]
REELS = [{"reel_id": "r-1", "brief_id": "b-1", "platform": "tiktok",
          "post_url": "https://tiktok.com/@x/video/9"}]
PERF = [{"reel_id": "r-1", "brief_id": "b-1", "views": "50000", "er": "0.07",
         "measured_at": "2026-07-08"}]


def test_full_chain_no_missing_links():
    trace = build_trace("b-1", BRIEFS, REELS, PERF)
    assert trace["found"] is True
    assert trace["formula_id"] == "f-1"
    assert trace["reels"][0]["reel_id"] == "r-1"
    assert trace["performance"][0]["views"] == "50000"
    assert trace["missing_links"] == []


def test_reports_missing_links():
    trace = build_trace("b-1", [{"brief_id": "b-1", "source_pattern_ids": "",
                                 "formula_id": "f-1", "prompt_version": "v1"}], [], [])
    assert set(trace["missing_links"]) == {"source_pattern_ids", "published_reels"}


def test_unknown_brief():
    trace = build_trace("ghost", BRIEFS, REELS, PERF)
    assert trace["found"] is False and trace["missing_links"] == ["brief"]


def test_published_brief_ids_join_by_brief_id():
    reels = [{"brief_id": "b-1"}, {"brief_id": "b-2"}, {"brief_id": ""},
             {"reel_id": "orphan"}]
    assert published_brief_ids(reels) == {"b-1", "b-2"}
