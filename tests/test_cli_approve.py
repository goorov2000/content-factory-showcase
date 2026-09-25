import argparse
import json

from cf.cli import cmd_approve_formula
from cf.io import read_json

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def write_formula(tmp_path, version=1):
    p = tmp_path / "pets-question-hook.json"
    p.write_text(json.dumps({"name": "pets-question-hook", "niche": "pets",
                             "version": version}), encoding="utf-8")
    return p


def test_approve_adds_entry_to_index(tmp_path):
    formula = write_formula(tmp_path)
    index = tmp_path / "index.json"
    rc = cmd_approve_formula(FakeSheets(), ns(path=str(formula), index=str(index)))
    assert rc == 0
    data = read_json(index)
    assert data["approved"][0]["name"] == "pets-question-hook"
    assert data["approved"][0]["version"] == 1
    assert data["approved"][0]["approved_at"]


def test_reapprove_same_path_replaces_entry(tmp_path):
    index = tmp_path / "index.json"
    cmd_approve_formula(FakeSheets(), ns(path=str(write_formula(tmp_path, 1)), index=str(index)))
    cmd_approve_formula(FakeSheets(), ns(path=str(write_formula(tmp_path, 2)), index=str(index)))
    data = read_json(index)
    assert len(data["approved"]) == 1
    assert data["approved"][0]["version"] == 2
