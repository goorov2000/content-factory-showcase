import argparse

from cf.cli import (
    build_parser,
    cmd_hiring_check,
    cmd_hiring_status,
    cmd_hiring_sync,
)
from cf.hiring.spec import TRACKER_TABS


class _Tab:
    def __init__(self, title):
        self.title = title


class _Book:
    def __init__(self, titles=TRACKER_TABS):
        self.titles = titles

    def worksheets(self):
        return [_Tab(title) for title in self.titles]


class _Store:
    def __init__(self, resources=None, titles=TRACKER_TABS):
        self.resources = resources or {}
        self.book = _Book(titles)

    def config_values(self):
        return dict(self.resources)


def test_parser_wires_hiring_commands():
    check = build_parser().parse_args(["hiring-check"])
    sync = build_parser().parse_args(["hiring-sync", "--dry-run", "--no-agent"])
    status = build_parser().parse_args(["hiring-status"])

    assert check.func is cmd_hiring_check
    assert sync.func is cmd_hiring_sync
    assert sync.dry_run is True and sync.no_agent is True
    assert status.func is cmd_hiring_status


def test_hiring_check_requires_new_and_archived_tracker_tabs(
    monkeypatch, capsys
):
    store = _Store(titles=[title for title in TRACKER_TABS if title != "Assessments"])
    monkeypatch.setattr("cf.config.load_config", lambda: {"hiring": {}})
    monkeypatch.setattr(
        "cf.hiring.google.HiringSheet.from_config",
        classmethod(lambda cls, config: store),
    )

    assert cmd_hiring_check(None, None) == 1
    assert "Assessments" in capsys.readouterr().out


def test_hiring_check_reports_single_form_bootstrap(monkeypatch, capsys):
    store = _Store({"APPLICATION_FORM_ID": "PENDING_OWNER_SETUP"})
    monkeypatch.setattr("cf.config.load_config", lambda: {"hiring": {}})
    monkeypatch.setattr(
        "cf.hiring.google.HiringSheet.from_config",
        classmethod(lambda cls, config: store),
    )

    assert cmd_hiring_check(None, None) == 2
    output = capsys.readouterr().out
    assert "одной human-owned Google Form" in output
    assert "APPLICATION_FORM_ID" in output


def test_hiring_check_reads_exactly_one_form(monkeypatch, capsys):
    store = _Store({"APPLICATION_FORM_ID": "form-1"})
    calls = []

    class FakeForms:
        @classmethod
        def from_config(cls, config):
            return cls()

        def get_form(self, form_id):
            calls.append(form_id)
            return {"info": {"title": "Единая анкета"}}

    monkeypatch.setattr("cf.config.load_config", lambda: {"hiring": {}})
    monkeypatch.setattr(
        "cf.hiring.google.HiringSheet.from_config",
        classmethod(lambda cls, config: store),
    )
    monkeypatch.setattr("cf.hiring.google.FormsClient", FakeForms)

    assert cmd_hiring_check(None, None) == 0
    assert calls == ["form-1"]
    assert "Единая анкета" in capsys.readouterr().out


def test_hiring_sync_logs_counts_without_candidate_data(monkeypatch, capsys):
    store = _Store()
    logged = []

    class FakeForms:
        @classmethod
        def from_config(cls, config):
            return cls()

    class FakeWorkflow:
        def __init__(self, *args, **kwargs):
            assert "application_rule" in kwargs
            assert "application_judge" in kwargs

        def sync(self, *, dry_run):
            return {
                "dry_run": dry_run,
                "counts": {"application_created": 2, "outbox": 1},
                "warnings": [],
                "ruleset": "v2",
            }

    monkeypatch.setattr(
        "cf.config.load_config",
        lambda: {"hiring": {"spreadsheet_id": "sheet-1"}},
    )
    monkeypatch.setattr(
        "cf.hiring.google.HiringSheet.from_config",
        classmethod(lambda cls, config: store),
    )
    monkeypatch.setattr("cf.hiring.google.FormsClient", FakeForms)
    monkeypatch.setattr("cf.hiring.workflow.HiringWorkflow", FakeWorkflow)
    monkeypatch.setattr("cf.cli.runlog.log_run", lambda *a, **kw: logged.append(kw))

    args = argparse.Namespace(
        dry_run=True, no_agent=True, run_started_at="2026-07-31T12:00:00+00:00"
    )
    assert cmd_hiring_sync(object(), args) == 0

    assert logged[0]["status"] == "success"
    assert logged[0]["input_summary"] == "dry_run=True agent=False responses=2"
    assert "@" not in logged[0]["input_summary"]
    assert '"application_created": 2' in capsys.readouterr().out


def test_hiring_status_prints_assessments_and_single_link(monkeypatch, capsys):
    store = _Store()
    monkeypatch.setattr("cf.config.load_config", lambda: {"hiring": {}})
    monkeypatch.setattr(
        "cf.hiring.google.HiringSheet.from_config",
        classmethod(lambda cls, config: store),
    )
    monkeypatch.setattr(
        "cf.hiring.workflow.status_summary",
        lambda value: {
            "candidates": 3,
            "candidate_statuses": {
                "application_review": 1,
                "defense_invite": 2,
            },
            "assessments": 3,
            "assessment_statuses": {"defense_invite": 2},
            "outbox": {"pending": 2},
            "resources": {
                "TRACKER_URL": "https://tracker",
                "APPLICATION_FORM_URL": "https://application",
            },
        },
    )

    assert cmd_hiring_status(None, None) == 0
    output = capsys.readouterr().out
    assert '"assessments": 3' in output
    assert "tracker: https://tracker" in output
    assert "application: https://application" in output
    assert "\ntest:" not in output
