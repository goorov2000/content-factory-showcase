# C3.4 — cf validate sources: схема реестра источников (спека §2.1).
import json

import pytest

from cf.validate import validate_json_file


def write_registry(tmp_path, records):
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return path


def full_record(**over):
    rec = {"query": "мужскаяодежда", "kind": "hashtag", "niche": "мужские-образы",
           "status": "active", "origin": "operator", "added_at": "2026-07-23"}
    rec.update(over)
    return rec


def test_valid_registry_ok(tmp_path):
    path = write_registry(tmp_path, [
        full_record(),
        full_record(query="стиль мужской", kind="search", status="candidate",
                    origin="harvest", runs_count=2, rows_passed_gate=5,
                    last_run_at="2026-07-23T08:00:00+00:00"),
    ])
    assert validate_json_file("sources", path) == []


@pytest.mark.parametrize("missing", ["kind", "status", "query", "origin"])
def test_missing_required_field_invalid(tmp_path, missing):
    rec = full_record()
    del rec[missing]
    errors = validate_json_file("sources", write_registry(tmp_path, [rec]))
    assert errors
    assert missing in " ".join(errors)


def test_bad_enum_invalid(tmp_path):
    errors = validate_json_file(
        "sources", write_registry(tmp_path, [full_record(status="enabled")]))
    assert errors


def test_unknown_field_invalid(tmp_path):
    # опечатка в имени поля не должна тонуть молча (additionalProperties: false)
    errors = validate_json_file(
        "sources", write_registry(tmp_path, [full_record(statuss="active")]))
    assert errors


def test_live_registries_valid():
    # боевые реестры репо всегда проходят собственную схему
    # витринная копия: роль боевых реестров играют sources/*.example.json (та же схема)
    import os
    for platform in ("tiktok", "instagram"):
        live = f"sources/{platform}.json"
        path = live if os.path.exists(live) else f"sources/{platform}.example.json"
        assert validate_json_file("sources", path) == [], path
