import pytest

from cf.sourcepatch import apply_proposal_to_sources, parse_sources_block

JS = '''const x = 1;
// SOURCES:BEGIN — правится только через cf apply-sources (proposal-цикл)
const SOURCES = {"hashtags": ["a", "b"], "searchQueries": ["q1"]};
// SOURCES:END
const hashtags = SOURCES.hashtags;'''


def test_parse_sources_block():
    assert parse_sources_block(JS) == {"hashtags": ["a", "b"], "searchQueries": ["q1"]}


def test_apply_remove_and_add():
    proposal = {"remove": [{"source": "hashtag:#b"}, {"source": "query:q1"}],
                "add": [{"source": "hashtag:#new", "kind": "hashtag"},
                        {"source": "query:новый запрос", "kind": "query"}]}
    out = apply_proposal_to_sources(JS, proposal)
    assert parse_sources_block(out) == {
        "hashtags": ["a", "new"], "searchQueries": ["новый запрос"]}
    assert out.startswith("const x = 1;")          # код вокруг блока не тронут
    assert "const hashtags = SOURCES.hashtags;" in out


def test_apply_is_idempotent():
    proposal = {"remove": [{"source": "hashtag:#b"}],
                "add": [{"source": "hashtag:#new", "kind": "hashtag"}]}
    once = apply_proposal_to_sources(JS, proposal)
    twice = apply_proposal_to_sources(once, proposal)
    assert once == twice


def test_missing_markers_raise():
    with pytest.raises(ValueError):
        parse_sources_block("const SOURCES = {};")
