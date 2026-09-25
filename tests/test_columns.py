from cf.columns import apply_column_aliases

ALIASES = {"source_url": "url", "account": "author", "posted_at": "created_at"}


def test_adds_canonical_fields_from_actual():
    rows = [{"url": "https://a", "author": "acc", "created_at": "2026-07-01", "views": "10"}]
    out = apply_column_aliases(rows, ALIASES)
    assert out[0]["source_url"] == "https://a"
    assert out[0]["account"] == "acc"
    assert out[0]["posted_at"] == "2026-07-01"
    assert out[0]["url"] == "https://a"  # исходное поле сохраняется


def test_existing_canonical_field_not_overwritten():
    rows = [{"source_url": "https://canon", "url": "https://raw"}]
    out = apply_column_aliases(rows, ALIASES)
    assert out[0]["source_url"] == "https://canon"


def test_missing_actual_field_ignored():
    rows = [{"views": "10"}]
    out = apply_column_aliases(rows, ALIASES)
    assert "source_url" not in out[0]


def test_empty_aliases_noop():
    rows = [{"url": "https://a"}]
    assert apply_column_aliases(rows, {}) == [{"url": "https://a"}]
