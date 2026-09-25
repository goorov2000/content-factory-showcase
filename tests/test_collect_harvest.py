# C4.2 — harvest кандидатов (спека §2.1): детерминированная выжимка хэштегов
# из капшенов строк, прошедших гейт с высоким сигналом (ER >= 0.05 ИЛИ
# views >= p75 прогона); минус известные и стоп-лист; топ-5 по частоте.
import json

from cf.collect.apify import RunResult
from cf.collect.sources import harvest_candidates, load_registry
from cf.collect import tiktok

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


def active(query):
    return {"query": query, "kind": "hashtag", "niche": "мужские-образы",
            "status": "active", "origin": "operator", "added_at": "2026-06-01"}


def row(caption, er=0.1, views=100):
    return {"caption": caption, "engagement_rate": er, "views": views}


def test_harvest_top_by_frequency_minus_known_and_stop():
    registry = [active("известный")]
    rows = [row("#новый1 #новый2 #известный #fyp"),
            row("#новый1 #новый3"),
            row("#новый1 #новый2")]
    new = harvest_candidates(registry, rows, today="2026-07-23")
    assert [r["query"] for r in new] == ["новый1", "новый2", "новый3"]
    assert all(r["status"] == "candidate" and r["origin"] == "harvest"
               and r["added_at"] == "2026-07-23" and r["kind"] == "hashtag"
               and r["niche"] == "мужские-образы" for r in new)
    assert len(registry) == 4  # добавлены в реестр


def test_harvest_top_n_capped():
    rows = [row(" ".join(f"#тег{i}" for i in range(10)))]
    new = harvest_candidates([active("а")], rows, today="2026-07-23", top_n=5)
    assert len(new) == 5


def test_harvest_low_signal_rows_ignored():
    # низкий ER и просмотры ниже p75 -> капшен не участвует
    rows = [row("#мусорный", er=0.01, views=10),
            row("#сильный", er=0.01, views=1000),   # views == p75 максимум
            row("#точныйер", er=0.05, views=10),
            row("без тегов", er=0.5, views=10)]
    new = harvest_candidates([active("а")], rows, today="2026-07-23")
    assert [r["query"] for r in new] == ["сильный", "точныйер"]


def test_harvest_idempotent():
    registry = [active("а")]
    rows = [row("#новый")]
    first = harvest_candidates(registry, rows, today="2026-07-23")
    second = harvest_candidates(registry, rows, today="2026-07-24")
    assert len(first) == 1
    assert second == []
    assert len(registry) == 2


def test_harvest_case_insensitive_known():
    registry = [active("MenStyle")]
    new = harvest_candidates(registry, [row("#menstyle #Другой")], today="2026-07-23")
    # M28: query хранится в каноническом lowercase — иначе mixed-case кандидат
    # никогда не получал атрибуции exploration (акторы отдают lowercase-теги)
    assert [r["query"] for r in new] == ["другой"]


# --- интеграция: tiktok collect добавляет harvest-кандидатов в реестр ---

def test_tiktok_collect_harvests_candidates(tmp_path):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "tiktok.json").write_text(json.dumps([active("акт1")], ensure_ascii=False),
                                   encoding="utf-8")
    item = {"id": "v1", "text": "лук #харвесттег #харвесттег2", "createTimeISO": FRESH,
            "playCount": 1000, "diggCount": 200,  # ER 0.2 — сильный сигнал
            "webVideoUrl": "https://tt/v1", "transcript": "текст",
            "searchHashtag": {"name": "акт1"}}
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})

    class Client:
        def run_actor(self, actor_path, payload):
            return RunResult("SUCCEEDED", items=[item])

    tiktok.collect(sheets, Client(), now_iso=NOW, registry_root=tmp_path)
    reg = load_registry("tiktok", root=tmp_path)
    harvested = [r for r in reg if r.get("origin") == "harvest"]
    assert {r["query"] for r in harvested} == {"харвесттег", "харвесттег2"}
    assert all(r["status"] == "candidate" for r in harvested)


def test_tiktok_dry_run_no_harvest_write(tmp_path):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "tiktok.json").write_text(json.dumps([active("акт1")], ensure_ascii=False),
                                   encoding="utf-8")
    before = (d / "tiktok.json").read_text(encoding="utf-8")
    item = {"id": "v1", "text": "#харвесттег", "createTimeISO": FRESH,
            "playCount": 1000, "diggCount": 200, "webVideoUrl": "https://tt/v1",
            "transcript": "т", "searchHashtag": {"name": "акт1"}}
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})

    class Client:
        def run_actor(self, actor_path, payload):
            return RunResult("SUCCEEDED", items=[item])

    tiktok.collect(sheets, Client(), now_iso=NOW, registry_root=tmp_path, dry_run=True)
    assert (d / "tiktok.json").read_text(encoding="utf-8") == before
