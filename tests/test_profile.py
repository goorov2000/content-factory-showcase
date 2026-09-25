from cf.profile import CRITICAL_FIELDS, profile_rows

from tests.fakes import make_raw_row


def test_happy_path_ready():
    report = profile_rows([make_raw_row(i) for i in range(25)], min_rows=20)
    assert report["ready_for_analysis"] is True
    assert report["clean_rows"] == 25
    assert report["issues_list"] == []


def test_dedupes_by_source_url():
    report = profile_rows([make_raw_row(1), make_raw_row(1), make_raw_row(2)], min_rows=1)
    assert report["duplicates_removed"] == 1
    assert report["clean_rows"] == 2


def test_missing_critical_fields_lead_to_insufficient_data():
    report = profile_rows([make_raw_row(i, niche="") for i in range(30)], min_rows=20)
    assert report["ready_for_analysis"] is False
    assert report["rows_missing_critical_fields"] == 30
    assert any("insufficient_data" in issue for issue in report["issues_list"])


def test_duplicate_rate_warning():
    # NOTE: task spec had range(5) here, giving 4 dups / 33 rows = 12.1%, which never
    # exceeds the 20% dup_threshold regardless of formula variant. Bumped to range(10)
    # (9 dups / 38 rows = 23.7%) so this test actually exercises the warning branch.
    rows = [make_raw_row(1) for _ in range(10)] + [make_raw_row(i) for i in range(2, 30)]
    report = profile_rows(rows, min_rows=20)
    assert any("duplicate" in issue for issue in report["issues_list"])
