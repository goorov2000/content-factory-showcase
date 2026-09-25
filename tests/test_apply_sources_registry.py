# C3.3 — apply-sources применяется к реестру sources/*.json, JS-эталоны не
# трогаются (n8n собирает по своим спискам до этапа 5; расхождение допустимо).
import argparse
import json
import shutil
from pathlib import Path

from cf.cli import cmd_apply_sources
from cf.sourcepatch import apply_proposal_to_registry

REPO = Path(__file__).resolve().parents[1]


def rec(query, kind="hashtag", status="active", **over):
    r = {"query": query, "kind": kind, "niche": "мужские-образы",
         "status": status, "origin": "operator", "added_at": "2026-07-01"}
    r.update(over)
    return r


def proposal(**over):
    p = {"platform": "tiktok", "generated_at": "2026-07-23", "status": "approved",
         "remove": [], "add": []}
    p.update(over)
    return p


def test_add_appends_active_tuner_records():
    records, summary = apply_proposal_to_registry(
        [rec("старый")],
        proposal(add=[
            {"source": "hashtag:#новыйтег", "kind": "hashtag", "evidence": "count=5 в harvest"},
            {"source": "query:мужской стиль осень", "kind": "query", "evidence": "yield 0.4"},
        ]),
        today="2026-07-23")
    assert summary == {"added": 2, "retired": 0, "reactivated": 0, "missing": []}
    added = records[1:]
    assert added[0]["query"] == "новыйтег"
    assert added[0]["kind"] == "hashtag"
    assert added[1]["query"] == "мужской стиль осень"
    assert added[1]["kind"] == "search"  # proposal-кайнд query -> реестровый search
    for a in added:
        assert a["status"] == "active"
        assert a["origin"] == "tuner"
        assert a["added_at"] == "2026-07-23"
        assert a["niche"] == "мужские-образы"  # унаследована от active-записей


def test_remove_retires_with_reason():
    records, summary = apply_proposal_to_registry(
        [rec("мёртвыйтег"), rec("живойтег")],
        proposal(remove=[{"source": "hashtag:#мёртвыйтег",
                          "reason": "rows=25 target_yield=0.02", "stats": {}}]),
        today="2026-07-23")
    assert summary["retired"] == 1
    dead = records[0]
    assert dead["status"] == "retired"
    assert dead["retired_at"] == "2026-07-23"
    assert dead["retired_reason"] == "rows=25 target_yield=0.02"
    assert records[1]["status"] == "active"


def test_remove_missing_source_reported_not_fatal():
    records, summary = apply_proposal_to_registry(
        [rec("есть")],
        proposal(remove=[{"source": "hashtag:#нету", "reason": "мимо реестра...", "stats": {}}]),
        today="2026-07-23")
    assert summary["missing"] == ["hashtag:#нету"]
    assert records[0]["status"] == "active"


def test_add_existing_retired_reactivates_without_duplicate():
    records, summary = apply_proposal_to_registry(
        [rec("вернулся", status="retired", retired_at="2026-07-10", retired_reason="x")],
        proposal(add=[{"source": "hashtag:#вернулся", "kind": "hashtag",
                       "evidence": "снова даёт yield"}]),
        today="2026-07-23")
    assert summary["reactivated"] == 1
    assert len(records) == 1
    assert records[0]["status"] == "active"


def test_add_existing_active_noop():
    records, summary = apply_proposal_to_registry(
        [rec("ужеесть")],
        proposal(add=[{"source": "hashtag:#ужеесть", "kind": "hashtag",
                       "evidence": "дубль в proposal"}]),
        today="2026-07-23")
    assert summary["added"] == 0
    assert len(records) == 1


def test_cmd_apply_sources_writes_registry_not_js(tmp_path, monkeypatch, capsys):
    # схемы нужны валидатору из cwd
    (tmp_path / "schemas").mkdir()
    for name in ("source-proposal.schema.json", "sources.schema.json"):
        shutil.copy(REPO / "schemas" / name, tmp_path / "schemas" / name)
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "tiktok.json").write_text(
        json.dumps([rec("старый")], ensure_ascii=False), encoding="utf-8")
    prop_path = tmp_path / "prop.json"
    prop_path.write_text(json.dumps(proposal(add=[
        {"source": "hashtag:#новый", "kind": "hashtag", "evidence": "count=4 harvest"}]),
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    code = cmd_apply_sources(None, argparse.Namespace(proposal=str(prop_path)))
    out = capsys.readouterr().out

    assert code == 0
    registry = json.loads((tmp_path / "sources" / "tiktok.json").read_text(encoding="utf-8"))
    assert [r["query"] for r in registry] == ["старый", "новый"]
    applied = json.loads(prop_path.read_text(encoding="utf-8"))
    assert applied["applied_at"]
    assert "git commit" in out
    assert "push-n8n" not in out  # шаг синка n8n из цикла исчез


def test_cmd_apply_sources_rejects_pending(tmp_path, monkeypatch):
    (tmp_path / "schemas").mkdir()
    for name in ("source-proposal.schema.json", "sources.schema.json"):
        shutil.copy(REPO / "schemas" / name, tmp_path / "schemas" / name)
    prop_path = tmp_path / "prop.json"
    prop_path.write_text(json.dumps(proposal(status="pending")), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cmd_apply_sources(None, argparse.Namespace(proposal=str(prop_path))) == 1


def _setup_repo(tmp_path):
    (tmp_path / "schemas").mkdir(exist_ok=True)
    for name in ("source-proposal.schema.json", "sources.schema.json"):
        shutil.copy(REPO / "schemas" / name, tmp_path / "schemas" / name)
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / "tiktok.json").write_text(
        json.dumps([rec("старый")], ensure_ascii=False), encoding="utf-8")


def test_apply_sources_registry_locked_returns_clear_error(tmp_path, monkeypatch, capsys):
    # M37: collect держит лок реестра весь прогон — apply-sources честно отказывает,
    # а не молча теряет правки под save_registry.
    from cf import pipeline_lock

    _setup_repo(tmp_path)
    prop_path = tmp_path / "prop.json"
    prop_path.write_text(json.dumps(proposal(add=[
        {"source": "hashtag:#новый", "kind": "hashtag", "evidence": "count=4 harvest"}]),
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    import cf.cli as cli_mod
    real_lock = pipeline_lock.registry_lock

    def short_lock(platform, locks_dir=None, timeout=10.0):
        return real_lock(platform, locks_dir=locks_dir, timeout=0.2)

    with real_lock("tiktok"):
        monkeypatch.setattr("cf.pipeline_lock.registry_lock", short_lock)
        code = cmd_apply_sources(None, argparse.Namespace(proposal=str(prop_path)))

    err = capsys.readouterr().err
    assert code == 1
    assert "занят сбором" in err
    registry = json.loads((tmp_path / "sources" / "tiktok.json").read_text(encoding="utf-8"))
    assert [r["query"] for r in registry] == ["старый"]     # файл не тронут
    assert "applied_at" not in json.loads(prop_path.read_text(encoding="utf-8"))


def test_apply_sources_invalid_result_leaves_registry_untouched(tmp_path, monkeypatch, capsys):
    # M38: валидация ДО записи — битый после применения реестр не попадает на диск.
    _setup_repo(tmp_path)
    before = (tmp_path / "sources" / "tiktok.json").read_text(encoding="utf-8")
    prop_path = tmp_path / "prop.json"
    # kind "hashtag", но source без решётки и с недопустимым для схемы мусором не
    # соберёшь легально — ломаем реестр моком применения
    prop_path.write_text(json.dumps(proposal(add=[
        {"source": "hashtag:#новый", "kind": "hashtag", "evidence": "count=4 harvest"}]),
        ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cf.sourcepatch.apply_proposal_to_registry",
                        lambda reg, prop, today: ([{"мусор": True}], {
                            "added": 1, "retired": 0, "reactivated": 0, "missing": []}))

    code = cmd_apply_sources(None, argparse.Namespace(proposal=str(prop_path)))

    err = capsys.readouterr().err
    assert code == 1
    assert "не прошёл бы схему" in err
    assert (tmp_path / "sources" / "tiktok.json").read_text(encoding="utf-8") == before
    assert "applied_at" not in json.loads(prop_path.read_text(encoding="utf-8"))
