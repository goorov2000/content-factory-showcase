# C1.7 — реестр sources/*.json + шардер батчей (cf.collect.sources).
# Порт применимых инвариантов n8n/__tests__/batching.test.js; JS-эталоны:
# Build-TikTok-Actor-Input.js, Build-Instagram-Search-Input.js,
# Normalize-Instagram-Hashtags.js, Prepare-Instagram-Reel-Transcript-Input.js.
#
# Маппинг «JS test → pytest» (batching.test.js):
#   'TikTok Build: >1 батча'                        → test_real_tiktok_registry_shards_into_multiple_batches
#   'TikTok Build: в каждом батче <=4 источника'    → test_each_batch_carries_1_to_4_sources
#   'TikTok Build: покрывают ВСЕ источники ровно 1' → test_full_coverage_without_losses_or_dupes
#   'TikTok Build: batch_index/batch_total'         → test_batch_index_sequential_batch_total_uniform
#   'TikTok Build: batch_sources hashtag:/query:'   → test_batch_sources_markers_by_kind
#   'IG Hashtags: >4 тегов -> несколько батчей'     → test_seven_sources_two_batches_in_order
#   'IG Hashtags: source_query = ПОЛНЫЙ список'     → test_source_query_full_list_on_every_batch
#   'IG Hashtags: ровно 4 -> 1 батч'                → test_exactly_four_sources_one_batch
#   'IG Hashtags: 25 -> 7 батчей, хвост 1'          → test_25_sources_7_batches_tail_1
#   'IG Hashtags: пустой список -> 0 item'          → test_empty_sources_zero_batches
#   'Prepare reel: пустой вход -> []'               → test_empty_sources_zero_batches (граница шардера;
#                                                     стадийная часть — reelUrl/dedup — C2.2)
#   'Prepare reel: 1 URL -> 1 батч из 1'            → test_single_source_one_batch_size6
#   'Prepare reel: ровно 6 -> 1 батч'               → test_exactly_six_sources_one_batch_size6
#   'Prepare reel: 7 -> 2 батча (6+1)'              → test_seven_sources_batch6_split_6_1
#   'Prepare reel: 30 -> 5 батчей + метаданные'     → test_30_sources_5_batches_size6_metadata
#   'Prepare reel: >30 -> усечение до 30'           → test_limit_truncates_over_30_to_30
# Не портируются здесь:
#   'IG Hashtags: <=4 тегов -> один батч, старый контракт' (matchesNiche/дедуп
#     discovered), tag_by_code / 'reel:'-маркеры / dedup URL — стадии mix/prepare → C2.2;
#   'TikTok Build: actor-id из одного места', 'IG Build: id трёх акторов' — конфиг
#     apify.actors → C2.1/C3.1; 'Snowball Build' (оба) → C2.3.
import json
from pathlib import Path

import pytest

from cf.collect.sources import (
    BATCH_SIZE,
    REEL_BATCH_SIZE,
    RESULTS_LIMIT,
    RESULTS_PER_PAGE,
    SEARCH_LIMIT_PER_QUERY,
    active_sources,
    load_registry,
    make_batches,
)
# Тот же регексп, что parseSources в batching.test.js (и sourcepatch.BLOCK):
# тест переживёт будущие правки списков через cf apply-sources.
from cf.sourcepatch import parse_sources_block

ROOT = Path(__file__).resolve().parents[1]
TT_BUILD = ROOT / "n8n/cf01-tiktok/code/Build-TikTok-Actor-Input.js"
IG_BUILD = ROOT / "n8n/cf01-instagram/code/Build-Instagram-Search-Input.js"
# Витринная копия: n8n/ (JS-эталон списанного сбора) и боевые sources/*.json не
# публикуются; load_registry читает sources/*.example.json (conftest). Тесты о
# JS-эталоне и о РАЗМЕРЕ реального реестра пропускаются, пока их нет.
N8N_PRESENT = TT_BUILD.is_file() and IG_BUILD.is_file()
REAL_REGISTRY = (ROOT / "sources" / "tiktok.json").is_file()
needs_n8n = pytest.mark.skipif(not N8N_PRESENT, reason="n8n/ (JS-эталон) отсутствует в копии")
needs_real_registry = pytest.mark.skipif(
    not REAL_REGISTRY, reason="боевой sources/tiktok.json не публикуется")


def js_sources(path):
    return parse_sources_block(path.read_text(encoding="utf-8"))


def tagged(hashtags=(), queries=()):
    return {"hashtags": list(hashtags), "search_queries": list(queries)}


# --- реестры sources/*.json: ровно те же источники, что SOURCES-блоки JS ---

def test_registry_records_shape():
    # Базовые ключи обязательны; счётчики exploration (mark_explored) и
    # retired_at/retired_reason (age_candidates, apply_proposal_to_registry)
    # появляются по мере боевых прогонов и применения source-proposal'ов.
    base = {"query", "kind", "niche", "status", "origin", "added_at"}
    optional = {"runs_count", "rows_passed_gate", "last_run_at",
                "retired_at", "retired_reason"}
    for platform in ("tiktok", "instagram"):
        registry = load_registry(platform)
        assert registry, platform
        for rec in registry:
            assert base <= set(rec), rec
            assert set(rec) - base <= optional, rec
            assert rec["kind"] in ("hashtag", "search")
            assert rec["status"] in ("active", "candidate", "paused", "retired")
            assert rec["origin"] in ("operator", "tuner", "harvest", "discovery")
            assert rec["niche"] == "мужские-образы"
            assert rec["query"].strip()


def _subsequence(part, whole):
    """part — подпоследовательность whole (порядок сохранён, допущены пропуски)."""
    it = iter(whole)
    return all(item in it for item in part)


def _assert_port_fidelity(platform, js_path):
    """Паритет ПОРТА: ни один источник из замороженного JS не потерялся при
    переносе, и активный список не переставлен.

    До 2026-07-26 тесты требовали равенства active == JS 1:1. Это был контракт
    времени миграции; n8n списан 24.07 (CLAUDE.md: n8n/ — замороженный архив), и
    реестр стал живым источником правды, который эволюционирует через proposal +
    cf apply-sources (правило №4). Первый же ретайр — proposal 2026-07-26 на пять
    пустых search-запросов TikTok — сделал равенство ложным по замыслу.

    Сторожим то, что осталось настоящим инвариантом:
      * каждый источник JS присутствует в реестре КАКОЙ-ТО записью (потеря при
        переносе — баг; сознательный ретайр — нет, у него есть retired_reason);
      * активный список — упорядоченная подпоследовательность JS: источники
        только выбывают, порядок батчей не перетасовывается.
    """
    js = js_sources(js_path)
    registry = load_registry(platform)
    act = active_sources(registry)
    known = {(r["kind"], r["query"]) for r in registry}
    for tag in (h for h in js["hashtags"] if h):
        assert ("hashtag", tag) in known, f"{platform}: хэштег {tag!r} потерян при переносе"
    for query in (q for q in js["searchQueries"] if q):
        assert ("search", query) in known, f"{platform}: запрос {query!r} потерян при переносе"
    assert _subsequence(act["hashtags"], [h for h in js["hashtags"] if h]), \
        f"{platform}: активные хэштеги переставлены относительно JS-эталона"
    assert _subsequence(act["search_queries"], [q for q in js["searchQueries"] if q]), \
        f"{platform}: активные запросы переставлены относительно JS-эталона"
    # выбывшие обязаны нести причину — иначе это молчаливая пропажа, а не решение
    for rec in registry:
        if rec.get("status") in ("retired", "paused"):
            assert str(rec.get("retired_reason", "")).strip(), \
                f"{platform}: {rec['query']!r} выключен без retired_reason"


@needs_n8n
@needs_real_registry
def test_tiktok_registry_matches_js_sources():
    _assert_port_fidelity("tiktok", TT_BUILD)


@needs_n8n
@needs_real_registry
def test_instagram_registry_matches_js_sources():
    _assert_port_fidelity("instagram", IG_BUILD)


def test_load_registry_custom_root_and_active_filter(tmp_path):
    (tmp_path / "sources").mkdir()
    records = [
        {"query": "a", "kind": "hashtag", "niche": "n", "status": "active",
         "origin": "operator", "added_at": "2026-07-23"},
        {"query": "b", "kind": "hashtag", "niche": "n", "status": "paused",
         "origin": "operator", "added_at": "2026-07-23"},
        {"query": "c", "kind": "search", "niche": "n", "status": "candidate",
         "origin": "harvest", "added_at": "2026-07-23"},
        {"query": "d", "kind": "search", "niche": "n", "status": "active",
         "origin": "operator", "added_at": "2026-07-23"},
        {"query": "e", "kind": "hashtag", "niche": "n", "status": "retired",
         "origin": "tuner", "added_at": "2026-07-23"},
    ]
    (tmp_path / "sources" / "tiktok.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")
    act = active_sources(load_registry("tiktok", root=tmp_path))
    assert act == {"hashtags": ["a"], "search_queries": ["d"]}


# --- константы акторов: значения из замороженных JS Build-файлов ---

def test_actor_constants_match_js():
    assert RESULTS_PER_PAGE == 20
    assert SEARCH_LIMIT_PER_QUERY == 4
    assert RESULTS_LIMIT == 10
    assert BATCH_SIZE == 4
    assert REEL_BATCH_SIZE == 6


# --- шардер: инварианты Build-TikTok-Actor-Input на живом реестре ---

@needs_real_registry
def test_real_tiktok_registry_shards_into_multiple_batches():
    batches = make_batches(active_sources(load_registry("tiktok")))
    assert len(batches) > 1


def test_each_batch_carries_1_to_4_sources():
    for batch in make_batches(active_sources(load_registry("tiktok"))):
        n = len(batch["hashtags"]) + len(batch["search_queries"])
        assert 1 <= n <= BATCH_SIZE
        assert batch["batch_size"] == n
        assert len(batch["batch_sources"]) == n


def test_full_coverage_without_losses_or_dupes():
    # Инвариант шардера, а не состава: батчи покрывают АКТИВНЫЙ список ровно один
    # раз. Эталон — сам реестр (он живой и меняется через proposal), а не
    # замороженный JS: фидельность переноса сторожит _assert_port_fidelity.
    act = active_sources(load_registry("tiktok"))
    batches = make_batches(act)
    tags = [t for b in batches for t in b["hashtags"]]
    queries = [q for b in batches for q in b["search_queries"]]
    assert tags == act["hashtags"]            # порядок и состав без потерь
    assert queries == act["search_queries"]
    assert len(tags) == len(set(tags))        # и без дублей
    assert len(queries) == len(set(queries))


def test_batch_index_sequential_batch_total_uniform():
    batches = make_batches(active_sources(load_registry("tiktok")))
    for i, batch in enumerate(batches):
        assert batch["batch_index"] == i
        assert batch["batch_total"] == len(batches)


def test_batch_sources_markers_by_kind():
    # Грамматика маркеров и их число — по АКТИВНОМУ реестру (см. коммент выше).
    act = active_sources(load_registry("tiktok"))
    markers = [m for b in make_batches(act) for m in b["batch_sources"]]
    assert len([m for m in markers if m.startswith("hashtag:#")]) == len(act["hashtags"])
    assert len([m for m in markers if m.startswith("query:")]) == len(act["search_queries"])
    # точная форма маркеров и порядок «хэштеги, потом запросы» (tagged в JS);
    # с 2026-08-10 (тикет 02) kind'ы не делят батч — запрос уходит отдельным
    assert [b["batch_sources"] for b in make_batches(tagged(["a", "b"], ["мужской стиль"]))] \
        == [["hashtag:#a", "hashtag:#b"], ["query:мужской стиль"]]


def test_batches_never_mix_kinds():
    # Тикет 02 (2026-08-10): hashtag-батчи уходят clockworks~tiktok-hashtag-scraper,
    # search-батчи — основному актору. Смешанный батч не мог бы уйти ни одному из
    # них целиком, поэтому kind режет батчи ДО шардинга по batch_size.
    out = make_batches(tagged(["h1", "h2", "h3", "h4", "h5"], ["q1", "q2"]))
    for batch in out:
        assert not (batch["hashtags"] and batch["search_queries"]), batch
    # граница kind'а даёт неполный батч, а не домешивание чужого kind'а
    assert [(len(b["hashtags"]), len(b["search_queries"])) for b in out] \
        == [(4, 0), (1, 0), (0, 2)]
    # инварианты шардера уцелели: полное покрытие без потерь, сквозная нумерация
    assert [t for b in out for t in b["hashtags"]] == ["h1", "h2", "h3", "h4", "h5"]
    assert [q for b in out for q in b["search_queries"]] == ["q1", "q2"]
    assert [b["batch_index"] for b in out] == [0, 1, 2]
    assert all(b["batch_total"] == 3 for b in out)
    # source_query — по-прежнему ПОЛНЫЙ список обоих kind (атрибуция downstream)
    assert all(b["source_query"] == "h1,h2,h3,h4,h5,q1,q2" for b in out)


# --- шардер: границы batch_size=4 (IG Hashtags в batching.test.js) ---

def test_seven_sources_two_batches_in_order():
    seven = ["a1", "b2", "c3", "d4", "e5", "f6", "g7"]
    out = make_batches(tagged(seven))
    assert len(out) == 2
    for batch in out:
        assert len(batch["hashtags"]) <= BATCH_SIZE
        assert batch["batch_size"] == len(batch["hashtags"])
    assert [t for b in out for t in b["hashtags"]] == seven  # порядок без потерь


def test_source_query_full_list_on_every_batch():
    five = ["menswear", "menstyle", "menfashion", "outfitideas", "streetwearmen"]
    out = make_batches(tagged(five))
    assert len(out) == 2
    for batch in out:
        assert batch["source_query"] == ",".join(five)  # не «резать» по батчу


def test_exactly_four_sources_one_batch():
    out = make_batches(tagged(["menswear", "menstyle", "menfashion", "outfitideas"]))
    assert len(out) == 1
    assert out[0]["batch_size"] == 4
    assert out[0]["batch_total"] == 1


def test_25_sources_7_batches_tail_1():
    out = make_batches(tagged(["style%d" % i for i in range(25)]))
    assert len(out) == 7  # ceil(25/4)
    flat = [t for b in out for t in b["hashtags"]]
    assert len(flat) == 25
    assert len(set(flat)) == 25
    assert out[-1]["batch_size"] == 1  # 25 mod 4


def test_empty_sources_zero_batches():
    assert make_batches(tagged()) == []
    # filter(Boolean) как в JS: пустые значения не образуют батчей
    assert make_batches(tagged(["", ""], [""])) == []


# --- шардер: границы batch_size=6 (Prepare reel в batching.test.js) ---

def test_single_source_one_batch_size6():
    out = make_batches(tagged(["u0"]), batch_size=REEL_BATCH_SIZE)
    assert len(out) == 1
    assert out[0]["batch_size"] == 1
    assert out[0]["batch_total"] == 1


def test_exactly_six_sources_one_batch_size6():
    out = make_batches(tagged(["u%d" % i for i in range(6)]), batch_size=REEL_BATCH_SIZE)
    assert len(out) == 1
    assert out[0]["batch_size"] == 6


def test_seven_sources_batch6_split_6_1():
    out = make_batches(tagged(["u%d" % i for i in range(7)]), batch_size=REEL_BATCH_SIZE)
    assert [b["batch_size"] for b in out] == [6, 1]


def test_30_sources_5_batches_size6_metadata():
    thirty = ["u%d" % i for i in range(30)]
    out = make_batches(tagged(thirty), batch_size=REEL_BATCH_SIZE)
    assert len(out) == 5
    flat = [t for b in out for t in b["hashtags"]]
    assert len(flat) == 30
    assert len(set(flat)) == 30
    for i, batch in enumerate(out):
        assert batch["batch_size"] <= REEL_BATCH_SIZE
        assert batch["batch_index"] == i
        assert batch["batch_total"] == 5
        assert batch["source_query"] == ",".join(thirty)
        assert len(batch["batch_sources"]) == batch["batch_size"]


def test_limit_truncates_over_30_to_30():
    out = make_batches(tagged(["u%d" % i for i in range(35)]),
                       batch_size=REEL_BATCH_SIZE, limit=30)
    assert len(out) == 5
    assert sum(b["batch_size"] for b in out) == 30


# --- promote-proposal: имя по составу, решение человека неприкосновенно ---
# (разбор 2026-07-27) Баг 9: имя пары файлов строилось из ОДНОЙ даты, а файл
# писался безусловно со status:"pending". Отсюда два дефекта на проде: (1) второй
# прогон суток возвращал в pending уже применённый proposal (cf apply-sources
# пишет applied_at в тот же файл) — решение человека стиралось; (2) каждый прогон
# плодил дубль того же решения (26.07 и 27.07 — байт в байт #mensfashion,
# #fashion), а дашборд держал на каждый вечную карточку «ждёт применения».

NOW_27 = "2026-07-27T04:30:05.513Z"


def promotable(*queries, rows=15, er=0.0916):
    """Вход save_promote_proposal: форма из age_candidates (record + цифры)."""
    return [{"record": {"kind": "hashtag", "query": q, "runs_count": 2},
             "median_er": er, "active_median_er": 0.0745,
             "rows_passed_gate": rows} for q in queries]


def _proposals(root):
    return sorted(p.name for p in (root / "proposals").glob("*.json"))


def test_promote_stem_hashes_composition_not_date():
    from cf.collect.sources import promote_stem

    same = promote_stem("tiktok", promotable("mensfashion", "fashion"), NOW_27)
    # порядок кандидатов у age_candidates зависит от порядка реестра — имя от
    # него зависеть не должно, иначе перестановка выпишет «новое» решение
    assert same == promote_stem("tiktok", promotable("fashion", "mensfashion"),
                                NOW_27)
    # суффикс имени сохранён: по нему файлы ищут глобом и глазами
    assert same.startswith("2026-07-27-") and same.endswith("-sources-tiktok-promote")
    other = promote_stem("tiktok", promotable("menstyle"), NOW_27)
    assert other != same          # разный состав — разные файлы в один день


def test_promote_proposal_does_not_revive_applied_decision(tmp_path):
    """Применённый proposal обратно в pending не возвращается (баг 9а)."""
    from cf.collect.sources import (build_promote_proposal, promote_stem,
                                    save_promote_proposal)

    people = promotable("mensfashion", "fashion")
    stem = promote_stem("tiktok", people, NOW_27)
    (tmp_path / "proposals").mkdir(parents=True)
    decided = build_promote_proposal("tiktok", people, NOW_27)
    decided["status"] = "approved"
    decided["applied_at"] = "2026-07-27T05:00:00Z"
    target = tmp_path / "proposals" / f"{stem}.json"
    target.write_text(json.dumps(decided, ensure_ascii=False), encoding="utf-8")

    path = save_promote_proposal("tiktok", people, NOW_27, root=tmp_path)

    assert path == target
    kept = json.loads(target.read_text(encoding="utf-8"))
    assert kept["status"] == "approved" and kept["applied_at"]
    # и обоснование поверх чужого решения тоже не выписываем
    assert not (tmp_path / "proposals" / f"{stem}.md").exists()


def test_promote_proposal_not_reissued_while_twin_awaits_human(tmp_path):
    """Тот же состав, другие сутки — дубля не будет (баг 9б)."""
    from cf.collect.sources import build_promote_proposal, save_promote_proposal

    people = promotable("mensfashion", "fashion")
    (tmp_path / "proposals").mkdir(parents=True)
    twin = tmp_path / "proposals" / "2026-07-26-sources-tiktok-promote.json"
    twin.write_text(json.dumps(build_promote_proposal(
        "tiktok", people, "2026-07-26T22:15:02.515Z"), ensure_ascii=False),
        encoding="utf-8")

    path = save_promote_proposal("tiktok", people, NOW_27, root=tmp_path)

    assert path == twin                       # звену сбора отвечаем «вот этот»
    assert _proposals(tmp_path) == [twin.name]
    assert not list((tmp_path / "proposals").glob("*.md"))


def test_promote_proposal_written_for_new_composition(tmp_path):
    """Сверка состава не должна глушить НОВОЕ решение (иначе промоут умер)."""
    from cf.collect.sources import save_promote_proposal

    (tmp_path / "proposals").mkdir(parents=True)
    # чужой pending-ретайр (add пустой) промоуту не помеха
    (tmp_path / "proposals" / "2026-07-26-retire-tiktok.json").write_text(
        json.dumps({"platform": "tiktok", "generated_at": "2026-07-26",
                    "status": "pending", "remove": [], "add": []}),
        encoding="utf-8")
    first = save_promote_proposal("tiktok", promotable("mensfashion"), NOW_27,
                                  root=tmp_path)
    second = save_promote_proposal("tiktok", promotable("mensfashion", "fashion"),
                                   NOW_27, root=tmp_path)

    assert first != second                    # два решения одних суток уцелели
    assert len(_proposals(tmp_path)) == 3
    got = json.loads(first.read_text(encoding="utf-8"))
    assert [a["source"] for a in got["add"]] == ["hashtag:#mensfashion"]


def test_promote_rationale_has_frontmatter_and_points_at_its_json(tmp_path):
    """Без frontmatter `cf status` печатал «не попал в сводку» на каждый прогон."""
    from cf.cli import _proposal_status
    from cf.collect.sources import save_promote_proposal

    path = save_promote_proposal("tiktok", promotable("mensfashion"), NOW_27,
                                 root=tmp_path)

    doc = json.loads(path.read_text(encoding="utf-8"))["rationale_doc"]
    text = (tmp_path / doc).read_text(encoding="utf-8")
    assert _proposal_status(text) == "proposed"     # тот же читатель, что у cf status
    head = text.split("---")[1]
    assert "kind: source-proposal" in head
    assert "platform: tiktok" in head
    assert "target: sources/tiktok.json" in head
    # ссылка на машиночитаемую часть ведёт в реальный файл, а не в имя-по-дате
    assert f"machine_readable: proposals/{path.name}" in head
    assert "created: 2026-07-27" in head
    assert f"apply-sources proposals/{path.name}" in text
