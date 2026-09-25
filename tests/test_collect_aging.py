# C4.3 — взросление и отсев кандидатов (спека §2.1): ниже yield-порога ->
# retired автоматически; выше promote-порогов -> proposal на перевод в active
# (сам реестр не меняется — гейт оператора); между — ждёт прогонов.
import json

from cf.collect.apify import RunResult
from cf.collect.sources import age_candidates, build_promote_proposal, load_registry
from cf.collect import tiktok
from cf.sourcepatch import apply_proposal_to_registry
from cf.validate import validate_json_file

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


def active(query, **over):
    r = {"query": query, "kind": "hashtag", "niche": "мужские-образы",
         "status": "active", "origin": "operator", "added_at": "2026-06-01"}
    r.update(over)
    return r


def cand(query, runs=2, rows=0, **over):
    r = {"query": query, "kind": "hashtag", "niche": "мужские-образы",
         "status": "candidate", "origin": "harvest", "added_at": "2026-07-01",
         "runs_count": runs, "rows_passed_gate": rows}
    r.update(over)
    return r


def raw(source_query, er, collected="2026-07-20T00:00:00.000Z"):
    return {"source_query": source_query, "engagement_rate": er,
            "collected_at": collected}


def test_retire_below_yield():
    reg = [active("а"), cand("мёртвый", runs=2, rows=1)]
    retired, promotable = age_candidates(reg, [], NOW)
    assert [r["query"] for r in retired] == ["мёртвый"]
    assert reg[1]["status"] == "retired"
    assert "rows_passed_gate=1" in reg[1]["retired_reason"]
    assert "2 прогонов" in reg[1]["retired_reason"]
    assert promotable == []


def test_single_run_untouched():
    reg = [cand("новичок", runs=1, rows=0)]
    retired, promotable = age_candidates(reg, [], NOW)
    assert retired == [] and promotable == []
    assert reg[0]["status"] == "candidate"


def test_promote_needs_er_vs_active_median():
    raws = [raw("hashtag:#а", 0.10), raw("hashtag:#а", 0.10),   # active median 0.10
            raw("hashtag:#силён", 0.09), raw("hashtag:#силён", 0.09),
            raw("hashtag:#слаб", 0.01), raw("hashtag:#слаб", 0.01)]
    reg = [active("а"), cand("силён", rows=10), cand("слаб", rows=10)]
    retired, promotable = age_candidates(reg, raws, NOW)
    names = [p["record"]["query"] for p in promotable]
    assert names == ["силён"]  # 0.09 >= 0.8*0.10; 0.01 — нет
    assert promotable[0]["rows_passed_gate"] == 10
    # promote не меняет статус в реестре — гейт оператора
    assert reg[1]["status"] == "candidate"


def test_middle_zone_waits():
    reg = [active("а"), cand("середина", rows=5)]
    retired, promotable = age_candidates(reg, [], NOW)
    assert retired == [] and promotable == []


def test_window_excludes_old_rows():
    old = "2026-06-01T00:00:00.000Z"  # старше 14 дней от NOW
    raws = [raw("hashtag:#а", 0.5, collected=old),
            raw("hashtag:#кан", 0.01), raw("hashtag:#кан", 0.01)]
    reg = [active("а"), cand("кан", rows=10)]
    _, promotable = age_candidates(reg, raws, NOW)
    # активная медиана из окна пуста (старая строка выпала) -> кандидат с
    # ненулевым ER проходит (не с кем сравнивать)
    assert [p["record"]["query"] for p in promotable] == ["кан"]


def test_promote_proposal_valid_and_applies(tmp_path):
    reg = [active("а"), cand("силён", rows=10)]
    promotable = [{"record": reg[1], "median_er": 0.09, "active_median_er": 0.1,
                   "rows_passed_gate": 10}]
    proposal = build_promote_proposal("tiktok", promotable, NOW)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
    assert validate_json_file("source-proposal", path) == []
    # approve оператором -> apply переводит кандидата в active
    proposal["status"] = "approved"
    records, summary = apply_proposal_to_registry(reg, proposal, today="2026-07-24")
    assert summary["reactivated"] == 1
    assert {r["query"]: r["status"] for r in records}["силён"] == "active"


def test_tiktok_collect_ages_and_writes_proposal(tmp_path):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "tiktok.json").write_text(json.dumps(
        [active("акт1"), cand("мёртвый", runs=3, rows=0),
         cand("зрелый", runs=2, rows=20)], ensure_ascii=False), encoding="utf-8")
    item = {"id": "v1", "text": "видео", "createTimeISO": FRESH, "playCount": 1000,
            "diggCount": 100, "webVideoUrl": "https://tt/v1", "transcript": "т",
            "searchHashtag": {"name": "акт1"}}
    # raw-вкладка уже несёт свежие строки зрелого кандидата с высоким ER
    raw_rows = [{"raw_id": f"tiktok_x{i}", "source_query": "hashtag:#зрелый",
                 "engagement_rate": 0.2, "collected_at": FRESH} for i in range(3)]
    sheets = FakeSheets(tables={"raw_tiktok": raw_rows, "run_log": []})

    class Client:
        def run_actor(self, actor_path, payload):
            return RunResult("SUCCEEDED", items=[item])

    summary = tiktok.collect(sheets, Client(), now_iso=NOW, registry_root=tmp_path)
    reg = load_registry("tiktok", root=tmp_path)
    by_q = {r["query"]: r for r in reg}
    assert by_q["мёртвый"]["status"] == "retired"
    proposals = list((tmp_path / "proposals").glob("*-sources-tiktok-promote.json"))
    assert len(proposals) == 1
    prop = json.loads(proposals[0].read_text(encoding="utf-8"))
    assert prop["add"][0]["source"] == "hashtag:#зрелый"
    assert "retired=1" in sheets.tables["run_log"][0]["input_summary"]
