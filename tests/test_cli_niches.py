import argparse
import json

from cf.cli import cmd_apply_niches

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def write_mapping(tmp_path, mapping):
    p = tmp_path / "niches.json"
    p.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return p


def test_apply_niches_updates_mapped_rows_keeps_others(tmp_path, capsys):
    sheets = FakeSheets({"raw_tiktok": [
        {"raw_id": "t1", "niche": ""},
        {"raw_id": "t2", "niche": ""},
        {"raw_id": "t3", "niche": "старая"},
    ]})
    mapping = write_mapping(tmp_path, {"t1": "мужская одежда", "t2": "питомцы"})
    rc = cmd_apply_niches(sheets, ns(mapping=str(mapping), tab="raw_tiktok", key="raw_id"))
    assert rc == 0
    rows = {r["raw_id"]: r["niche"] for r in sheets.read_rows("raw_tiktok")}
    assert rows == {"t1": "мужская одежда", "t2": "питомцы", "t3": "старая"}
    assert "2" in capsys.readouterr().out


def test_apply_niches_empty_mapping_returns_error(tmp_path):
    mapping = write_mapping(tmp_path, {})
    sheets = FakeSheets()
    rc = cmd_apply_niches(sheets, ns(mapping=str(mapping), tab="raw_tiktok", key="raw_id"))
    assert rc == 1
    # пустой mapping — ранний выход без записи, лога тоже нет
    assert sheets.read_rows("run_log") == []


def test_apply_niches_self_logs_run(tmp_path):
    # массовая перезапись niche всегда оставляет след в Run Log, даже если агент
    # пропустил шаг log-run в промпте
    sheets = FakeSheets({"raw_tiktok": [
        {"raw_id": "t1", "niche": ""},
        {"raw_id": "t2", "niche": ""},
        {"raw_id": "t3", "niche": "старая"},
    ]})
    mapping = write_mapping(tmp_path, {"t1": "мужская-одежда", "t2": "питомцы"})
    rc = cmd_apply_niches(sheets, ns(mapping=str(mapping), tab="raw_tiktok", key="raw_id"))
    assert rc == 0
    runs = sheets.read_rows("run_log")
    assert len(runs) == 1
    assert runs[0]["agent"] == "apply-niches"
    assert runs[0]["status"] == "success"
    # заметка содержит вкладку и число обновлённых строк
    assert "raw_tiktok" in runs[0]["input_summary"]
    assert "2" in runs[0]["input_summary"]
