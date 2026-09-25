# C4.1 — exploration-квота (спека §2.1): ежедневный сбор добавляет 1 батч (<=4)
# кандидатов рядом с проверенными. Детерминированно, без LLM: ротация по
# (runs_count, last_run_at, added_at, query) с квотой дозревающим (Д4);
# счётчики runs_count/last_run_at/rows_passed_gate пишет сам пайплайн в реестр.
import json
from datetime import datetime, timedelta, timezone

from cf.collect.apify import RunResult
from cf.collect.sources import (EXPLORATION_DEFAULTS, candidate_room,
                                exploration_pick, harvest_candidates,
                                load_registry, mark_explored)
from cf.collect import tiktok, instagram

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


def cand(query, added="2026-07-01", runs=0, kind="hashtag", **over):
    r = {"query": query, "kind": kind, "niche": "мужские-образы",
         "status": "candidate", "origin": "harvest", "added_at": added,
         "runs_count": runs}
    r.update(over)
    return r


def active(query, kind="hashtag"):
    return {"query": query, "kind": kind, "niche": "мужские-образы",
            "status": "active", "origin": "operator", "added_at": "2026-06-01"}


# --- ротация ---

def test_pick_least_runs_then_oldest():
    reg = [cand("новее", added="2026-07-10"), cand("старее", added="2026-07-01"),
           cand("гонялся", runs=2), active("акт")]
    picked = exploration_pick(reg, limit=2)
    assert [r["query"] for r in picked] == ["старее", "новее"]


def test_rotation_two_consecutive_runs_differ():
    reg = [cand(f"к{i}", added=f"2026-07-0{i + 1}") for i in range(6)]
    first = [r["query"] for r in exploration_pick(reg)]
    assert first == ["к0", "к1", "к2", "к3"]
    mark_explored(reg, first, NOW)
    second = [r["query"] for r in exploration_pick(reg)]
    # Д4: половина батча зарезервирована дозревающим (к0,к1 ждут второго зонда),
    # остаток — новичкам. До правки новички забирали батч целиком, и когорта
    # runs_count=1 второго зонда не получала никогда.
    assert second == ["к0", "к1", "к4", "к5"]
    assert set(second) != set(first)


def test_no_candidates_returns_empty():
    assert exploration_pick([active("а")]) == []


# --- Д4: квота дозревающим ---

def test_maturing_cohort_gets_half_the_batch():
    reg = [cand(f"зр{i}", runs=1, last_run_at=f"2026-07-1{i}") for i in range(5)]
    reg += [cand(f"нов{i}", added=f"2026-07-2{i}") for i in range(5)]
    picked = [r["query"] for r in exploration_pick(reg)]
    assert picked == ["зр0", "зр1", "нов0", "нов1"]


def test_maturing_cohort_fifo_by_last_run_at():
    # дольше всех ждавший зондируется первым, added_at роли не играет
    reg = [cand("свежепрогнанный", added="2026-07-01", runs=1,
                last_run_at="2026-07-20"),
           cand("забытый", added="2026-07-15", runs=1, last_run_at="2026-07-05")]
    assert [r["query"] for r in exploration_pick(reg, limit=1)] == ["забытый"]


def test_cold_start_gives_whole_batch_to_newcomers():
    # когорта дозревающих пуста -> поведение как до Д4
    reg = [cand(f"к{i}", added=f"2026-07-0{i + 1}") for i in range(6)]
    assert [r["query"] for r in exploration_pick(reg)] == ["к0", "к1", "к2", "к3"]


def test_maturing_alone_fills_whole_batch():
    # новичков нет -> квота не держит слоты пустыми
    reg = [cand(f"зр{i}", runs=1, last_run_at=f"2026-07-1{i}") for i in range(5)]
    assert [r["query"] for r in exploration_pick(reg)] == ["зр0", "зр1", "зр2", "зр3"]


def test_judged_candidates_go_last():
    # добравшие min_runs ждут вердикта age_candidates, а не зондов
    reg = [cand("судимый", runs=2, last_run_at="2026-07-01"),
           cand("дозревает", runs=1, last_run_at="2026-07-02"),
           cand("новичок")]
    assert [r["query"] for r in exploration_pick(reg)] == ["дозревает", "новичок",
                                                          "судимый"]


# --- Д4: сходимость контура (пул растёт быстрее, чем зондируется) ---

BASE = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)


def run_at(i):
    # сбор идёт раз в сутки (cf-collect-tiktok.timer, 08:00) — прогон = день
    return (BASE + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def legacy_pick(registry, cfg=None):
    """Порядок до Д4: строго «сначала наименьший runs_count»."""
    cands = [r for r in registry if r.get("status") == "candidate"]
    cands.sort(key=lambda r: (int(r.get("runs_count") or 0),
                              str(r.get("added_at") or ""), str(r.get("query"))))
    return cands[:4]


def simulate(pick, runs, cfg):
    """Модель конвейера: за прогон зондируется батч кандидатов, а harvest
    доливает из капшенов до harvest_top_n новых. Приток (5) больше расхода (4) —
    ровно та ситуация, что убила контур на бою."""
    reg = [active("акт")]
    for i in range(runs):
        picked = pick(reg, cfg=cfg)
        mark_explored(reg, [r["query"] for r in picked], run_at(i),
                      {r["query"]: 1 for r in picked})
        harvest_candidates(reg, [{"caption": " ".join(f"#тег{i}_{j}"
                                                      for j in range(5)),
                                  "engagement_rate": 0.1, "views": 1000}],
                           today=run_at(i)[:10], cfg=cfg)
    return reg


def cohorts(registry, min_runs=2):
    cands = [r for r in registry if r.get("status") == "candidate"]
    return {"всего": len(cands),
            "очередь": sum(1 for r in cands
                           if int(r.get("runs_count") or 0) < min_runs),
            "зрелые": sum(1 for r in cands
                          if int(r.get("runs_count") or 0) >= min_runs)}


def test_legacy_order_never_matured_any_candidate():
    # как было: 40 прогонов, ни одного кандидата с runs_count >= min_runs,
    # то есть age_candidates не может вынести ни одного вердикта
    reg = simulate(legacy_pick, 40, {**EXPLORATION_DEFAULTS, "pool_cap": 0})
    assert cohorts(reg) == {"всего": 200, "очередь": 200, "зрелые": 0}


def test_candidates_mature_even_when_pool_grows_faster():
    # как стало: та же модель, тот же приток — вердикты идут с 3-го прогона,
    # очередь зондирования упирается в pool_cap и не растёт
    reg = simulate(exploration_pick, 40, EXPLORATION_DEFAULTS)
    got = cohorts(reg)
    assert got["зрелые"] >= 70            # ~2 вердикта за прогон после разгона
    assert got["очередь"] <= EXPLORATION_DEFAULTS["pool_cap"]
    # кандидаты самого первого прогона дозрели, а не утонули под новичками
    first_run = [r for r in reg if str(r.get("query", "")).startswith("тег0_")]
    assert first_run and all(int(r["runs_count"]) >= 2 for r in first_run)


def test_first_cohort_matures_within_three_runs():
    reg = simulate(exploration_pick, 3, EXPLORATION_DEFAULTS)
    assert cohorts(reg)["зрелые"] >= 2


# --- Д4: потолок очереди зондирования ---

def test_harvest_stops_at_pool_cap():
    reg = [active("а")] + [cand(f"к{i}") for i in range(3)]
    rows = [{"caption": " ".join(f"#нов{i}" for i in range(5)),
             "engagement_rate": 0.1, "views": 1000}]
    new = harvest_candidates(reg, rows, today="2026-07-26",
                             cfg={"pool_cap": 5, "harvest_top_n": 5})
    assert [r["query"] for r in new] == ["нов0", "нов1"]   # 5 - 3 занятых
    assert harvest_candidates(reg, rows, today="2026-07-26",
                              cfg={"pool_cap": 5}) == []


def test_pool_cap_counts_only_candidates_without_verdict():
    # добравшие min_runs ждут оператора (promotable) и очередь не удлиняют —
    # иначе застрявшая пачка заперла бы приток навсегда
    reg = ([active("а")] + [cand(f"суд{i}", runs=2) for i in range(5)]
           + [cand("в очереди", runs=1)])
    assert candidate_room(reg, {"pool_cap": 4}) == 3
    assert candidate_room(reg, {"pool_cap": 0}) > 100      # 0 — без потолка


def test_mark_explored_updates_counters():
    reg = [cand("к1"), cand("к2")]
    mark_explored(reg, ["к1"], NOW, rows_by_query={"к1": 3})
    assert reg[0]["runs_count"] == 1
    assert reg[0]["last_run_at"] == NOW
    assert reg[0]["rows_passed_gate"] == 3
    assert reg[1]["runs_count"] == 0


# --- интеграция с пайплайнами ---

def registry_file(tmp_path, platform, records):
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    (d / f"{platform}.json").write_text(json.dumps(records, ensure_ascii=False),
                                        encoding="utf-8")
    return tmp_path


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run_actor(self, actor_path, payload):
        self.calls.append((actor_path, payload))
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def tiktok_item(vid, tag, text=None):
    return {"id": vid, "text": text or f"видео {vid}", "createTimeISO": FRESH,
            "playCount": 1000, "webVideoUrl": f"https://tt/{vid}",
            "transcript": "текст", "searchHashtag": {"name": tag}}


class TiktokStub:
    """Отвечает по содержимому батча, а не по порядку вызовов: батчи уходят в
    пул потоков параллельно, позиционный стаб на них ненадёжен."""

    def __init__(self, texts=None):
        self.texts = texts or {}
        self.payloads = []

    def run_actor(self, actor_path, payload):
        self.payloads.append(payload)
        return RunResult("SUCCEEDED", items=[
            tiktok_item(f"v_{tag}", tag, text=self.texts.get(tag))
            for tag in payload.get("hashtags", [])])


def test_tiktok_exploration_batch_and_counters(tmp_path):
    root = registry_file(tmp_path, "tiktok",
                         [active("акт1"), active("акт2"), cand("канд1"), cand("канд2")])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([
        RunResult("SUCCEEDED", items=[tiktok_item("v1", "акт1")]),      # active-батч
        RunResult("SUCCEEDED", items=[tiktok_item("v2", "канд1")]),     # exploration
    ])
    summary = tiktok.collect(sheets, client, now_iso=NOW, registry_root=root)
    assert summary["status"] == "success"
    assert len(client.calls) == 2
    # exploration-батч нёс только кандидатов
    assert client.calls[1][1]["hashtags"] == ["канд1", "канд2"]
    reg = load_registry("tiktok", root=root)
    by_q = {r["query"]: r for r in reg}
    assert by_q["канд1"]["runs_count"] == 1
    assert by_q["канд1"]["rows_passed_gate"] == 1  # ряд v2 прошёл гейт с его атрибуцией
    assert by_q["канд2"]["runs_count"] == 1
    assert by_q["канд2"].get("rows_passed_gate", 0) == 0
    assert "runs_count" not in by_q["акт1"]  # active не считаем


def test_tiktok_no_candidates_only_active_batches(tmp_path):
    root = registry_file(tmp_path, "tiktok", [active("акт1")])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[tiktok_item("v1", "акт1")])])
    tiktok.collect(sheets, client, now_iso=NOW, registry_root=root)
    assert len(client.calls) == 1


def test_tiktok_lost_exploration_batch_not_counted(tmp_path):
    # сбой exploration-рана — не «прогон»: кандидат не получает сигнала
    root = registry_file(tmp_path, "tiktok", [active("акт1"), cand("канд1")])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[tiktok_item("v1", "акт1")]),
                         RuntimeError("exploration died")])
    tiktok.collect(sheets, client, now_iso=NOW, registry_root=root)
    reg = load_registry("tiktok", root=root)
    assert {r["query"]: r for r in reg}["канд1"].get("runs_count", 0) == 0


def test_tiktok_dry_run_does_not_write_registry(tmp_path):
    root = registry_file(tmp_path, "tiktok", [active("акт1"), cand("канд1")])
    before = (tmp_path / "sources" / "tiktok.json").read_text(encoding="utf-8")
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[tiktok_item("v1", "акт1")]),
                         RunResult("SUCCEEDED", items=[tiktok_item("v2", "канд1")])])
    tiktok.collect(sheets, client, now_iso=NOW, registry_root=root, dry_run=True)
    assert (tmp_path / "sources" / "tiktok.json").read_text(encoding="utf-8") == before


def ig_reel(code, tag):
    return {"url": f"https://www.instagram.com/reel/{code}/", "hashtag": tag,
            "ownerUsername": "acc", "caption": f"пост {code}",
            "timestamp": FRESH, "videoViewCount": 1000}


def ig_item(code, tag="канд1"):
    return {"url": f"https://www.instagram.com/reel/{code}/", "shortCode": code,
            "ownerUsername": "acc", "caption": "пост", "timestamp": FRESH,
            "videoViewCount": 1000, "transcript": "текст"}


def test_instagram_exploration_tags_and_counters(tmp_path):
    root = registry_file(tmp_path, "instagram",
                         [active("акттег"), cand("канд1")])
    sheets = FakeSheets(tables={"raw_instagram": [], "run_log": []})
    client = StubClient([
        # hashtag-батч: active + exploration-тег вместе (<=4 в батче)
        RunResult("SUCCEEDED", items=[ig_reel("C1", "акттег"), ig_reel("C2", "канд1")]),
        RunResult("SUCCEEDED", items=[ig_item("C1", "акттег"), ig_item("C2", "канд1")]),
    ])
    summary = instagram.collect(sheets, client, now_iso=NOW, registry_root=root)
    assert summary["status"] == "success"
    hashtag_call = client.calls[0][1]
    assert "канд1" in hashtag_call["hashtags"]
    reg = load_registry("instagram", root=root)
    by_q = {r["query"]: r for r in reg}
    assert by_q["канд1"]["runs_count"] == 1
    assert by_q["канд1"]["rows_passed_gate"] == 1


# --- Д4: пороги из cf.config.json доезжают до pick/harvest/притока ---

def test_tiktok_takes_batch_and_harvest_top_n_from_config(tmp_path):
    root = registry_file(tmp_path, "tiktok", [active("акт1"), cand("к1"),
                                              cand("к2"), cand("к3")])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = TiktokStub(texts={"акт1": "лук #нов1 #нов2 #нов3"})
    config = {"sources": {"exploration": {"batch": 2, "harvest_top_n": 1}}}
    tiktok.collect(sheets, client, config=config, now_iso=NOW, registry_root=root)
    explore = [p["hashtags"] for p in client.payloads
               if "акт1" not in p.get("hashtags", [])]
    assert explore == [["к1", "к2"]]          # batch=2 доехал до exploration_pick
    reg = load_registry("tiktok", root=root)
    harvested = [r["query"] for r in reg if str(r["query"]).startswith("нов")]
    assert harvested == ["нов1"]              # harvest_top_n=1 доехал до harvest


class InstagramStub:
    """Отвечает по актору: hashtag- и reel-батчи тоже идут параллельно."""

    def __init__(self, discovery=(), reels=(), items=()):
        self.discovery, self.reels, self.items = (list(discovery), list(reels),
                                                  list(items))

    def run_actor(self, actor_path, payload):
        if "search-scraper" in actor_path:
            return RunResult("SUCCEEDED", items=list(self.discovery))
        if "hashtag-scraper" in actor_path:
            return RunResult("SUCCEEDED", items=list(self.reels))
        return RunResult("SUCCEEDED", items=list(self.items))


def ig_discovery_run(tmp_path, pool_cap):
    root = registry_file(tmp_path, "instagram",
                         [active("акттег"), active("мужской стиль", kind="search"),
                          cand("канд1"), cand("канд2")])
    sheets = FakeSheets(tables={"raw_instagram": [], "run_log": []})
    client = InstagramStub(discovery=[{"hashtag": "menstyle"}],
                           reels=[ig_reel("C1", "акттег")],
                           items=[ig_item("C1", "акттег")])
    instagram.collect(sheets, client, config={"sources": {"exploration":
                                                          {"pool_cap": pool_cap}}},
                      now_iso=NOW, registry_root=root)
    return {r["query"]: r for r in load_registry("instagram", root=root)}


def test_instagram_discovery_inflow_stops_at_pool_cap(tmp_path):
    # второй канал притока (нишевые теги от discovery-актора) под тем же
    # потолком, что harvest: очередь уже занята двумя кандидатами
    by_q = ig_discovery_run(tmp_path, pool_cap=2)
    assert "menstyle" not in by_q
    assert by_q["канд1"]["runs_count"] == 1   # зондирование при этом идёт


def test_instagram_discovery_inflow_works_when_pool_has_room(tmp_path):
    by_q = ig_discovery_run(tmp_path, pool_cap=10)
    assert by_q["menstyle"]["origin"] == "discovery"
    assert by_q["menstyle"]["status"] == "candidate"


def test_promote_proposal_ships_with_a_rationale_doc(tmp_path):
    """Машинный JSON без человеческого .md — evidence, которую никто не ревьюил.

    Прогон 27.07 01:17: починенный накануне контур эксплорейшна впервые выписал
    promote-proposal — и сразу нарушил инвариант репозитория (правило №1)."""
    from cf.collect.sources import save_promote_proposal
    promotable = [{"record": {"kind": "hashtag", "query": "mensfashion",
                              "runs_count": 2},
                   "median_er": 0.0916, "active_median_er": 0.0745,
                   "rows_passed_gate": 15}]

    path = save_promote_proposal("tiktok", promotable, "2026-07-27T00:00:00Z",
                                 root=tmp_path)

    proposal = json.loads(path.read_text(encoding="utf-8"))
    doc = proposal.get("rationale_doc")
    assert doc, "promote-proposal без rationale_doc"
    text = (tmp_path / doc).read_text(encoding="utf-8")
    assert "hashtag:#mensfashion" in text          # что именно продвигается
    assert "0.0916" in text and "0.0745" in text  # и по каким цифрам
    assert "apply-sources" in text                # и что с этим делать человеку
