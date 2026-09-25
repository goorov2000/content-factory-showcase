# C2.1 — e2e TikTok-пайплайна на моках: источники -> батчи -> Apify -> normalize
# -> gate -> субтитры -> upsert(coalesce) -> run_log.
import json

from cf.collect.apify import RunResult
from cf.collect.tiktok import (build_apidojo_payload, build_hashtag_payload,
                               build_search_payload, collect)

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


def raw_item(vid="v1", **over):
    item = {
        "id": vid,
        "text": f"видео {vid} #стиль",
        "createTimeISO": FRESH,
        "playCount": 1000, "diggCount": 100, "commentCount": 10,
        "shareCount": 5, "collectCount": 3,
        "webVideoUrl": f"https://www.tiktok.com/@acc/video/{vid}",
        "authorMeta": {"name": "acc"},
        "transcript": "готовый транскрипт",
        "searchHashtag": {"name": "мужскаяодежда"},
    }
    item.update(over)
    return item


def make_fake(raw_rows=()):
    return FakeSheets(tables={"raw_tiktok": list(raw_rows), "run_log": []})


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


def registry(tmp_path, n_hashtags=2):
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    records = [{"query": f"тег{i}", "kind": "hashtag", "niche": "мужские-образы",
                "status": "active", "origin": "operator", "added_at": "2026-07-01"}
               for i in range(n_hashtags)]
    (d / "tiktok.json").write_text(json.dumps(records, ensure_ascii=False),
                                   encoding="utf-8")
    return tmp_path


def test_build_hashtag_payload_snapshot_exact_schema_fields():
    # Снапшот 1:1 — дрейф полей payload виден на ревью, а не на счёте Apify.
    # Схема clockworks~tiktok-hashtag-scraper (билд latest, сверка 10.08):
    # РОВНО эти поля, REQUIRED [hashtags]; proxyCountryCode и searchQueries в
    # схеме отсутствуют — не шлём полей, которых нет в схеме. Субтитры —
    # бесплатный вариант enum downloadSubtitlesOptions.
    assert build_hashtag_payload({"hashtags": ["a", "b"], "search_queries": []}) == {
        "hashtags": ["a", "b"],
        "resultsPerPage": 20,
        "shouldDownloadCovers": True,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadVideos": False,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
    }


def test_build_search_payload_snapshot_no_dead_fields_no_hashtags():
    # Search-ветка остаётся на основном акторе (hashtag-scraper поиска не умеет).
    # videoSearchSorting/videoSearchDateFilter («UNDER MAINTENANCE» у вендора,
    # $2.90/мес в никуда — тикет 01) не отправляются; поля hashtags нет ВООБЩЕ —
    # хэштеги ушли hashtag-scraper'у, пустой список был бы мусором в платном
    # запросе. leastDiggs НЕ шлётся: поле есть в схеме, но живой ран 10.08 его
    # молча игнорирует (0 событий фильтра, likes от 2 в выдаче) — no-op поле,
    # которое может однажды молча проснуться с тарификацией, против US5.
    assert build_search_payload({"hashtags": [], "search_queries": ["b"]}) == {
        "searchQueries": ["b"],
        "searchSection": "/video",
        "resultsPerPage": 20,
        "excludePinnedPosts": False,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": True,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
        "shouldDownloadSlideshowImages": False,
        "scrapeRelatedVideos": False,
        "proxyCountryCode": "None",
    }


def test_build_apidojo_payload_hashtag_snapshot():
    # Тикет 04: apidojo~tiktok-scraper — кандидат за конфиг-флагом. Payload
    # РОВНО по схеме билда latest 0.0.1055 (сверка по API 10.08, REQUIRED []):
    # хэштеги подаются keywords-путём «#тег», maxItems соразмерен клоку
    # (RESULTS_PER_PAGE на источник). sortType/dateRange/location не шлём:
    # enum-значения в схеме есть, но НЕдефолтное значение — смена семантики
    # сбора без evidence, а дефолт явным полем — мусор в запросе.
    assert build_apidojo_payload({"hashtags": ["тег0", "тег1"],
                                  "search_queries": []}) == {
        "keywords": ["#тег0", "#тег1"],
        "maxItems": 40,
    }


def test_build_apidojo_payload_search_snapshot():
    # Search-батч тем же актором: запросы уходят keywords как есть (без «#»).
    # Батчи однородны по kind (make_batches), поэтому один билдер обслуживает
    # обе ветки — какая из них ходит в apidojo, решает только конфиг-ключ.
    assert build_apidojo_payload({"hashtags": [],
                                  "search_queries": ["мужская мода"]}) == {
        "keywords": ["мужская мода"],
        "maxItems": 20,
    }


def registry_mixed(tmp_path, n_hashtags=2, n_search=1):
    """Реестр обоих kind: гибрид акторов режет батчи по kind (тикет 02)."""
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    records = [{"query": f"тег{i}", "kind": "hashtag", "niche": "мужские-образы",
                "status": "active", "origin": "operator", "added_at": "2026-07-01"}
               for i in range(n_hashtags)]
    records += [{"query": f"запрос{i}", "kind": "search", "niche": "мужские-образы",
                 "status": "active", "origin": "operator", "added_at": "2026-07-01"}
                for i in range(n_search)]
    (d / "tiktok.json").write_text(json.dumps(records, ensure_ascii=False),
                                   encoding="utf-8")
    return tmp_path


class RecordingClient:
    """Отвечает всем батчам одинаково и пишет (actor, payload): батчи уходят в
    пул потоков, позиционный стаб на них ненадёжен (см. TiktokStub в
    test_collect_exploration)."""

    def __init__(self, items_by_actor=None):
        self.items_by_actor = items_by_actor or {}
        self.calls = []

    def run_actor(self, actor_path, payload):
        self.calls.append((actor_path, payload))
        return RunResult("SUCCEEDED", items=list(self.items_by_actor.get(actor_path, [])))


def test_hashtag_and_search_batches_routed_to_their_actors(tmp_path):
    # Гибрид (тикет 02): hashtag-батч уходит clockworks~tiktok-hashtag-scraper,
    # search-батч — прежнему актору; kind'ы не делят один платный ран.
    root = registry_mixed(tmp_path, n_hashtags=2, n_search=1)
    sheets = make_fake()
    client = RecordingClient({
        "clockworks~tiktok-hashtag-scraper": [raw_item("v1")],
        "clockworks~tiktok-scraper": [raw_item("v2", searchHashtag=None,
                                               searchQuery="запрос0")],
    })
    summary = collect(sheets, client, now_iso=NOW, registry_root=root,
                      explore=False)
    assert summary["status"] == "success"
    by_actor = {actor: payload for actor, payload in client.calls}
    assert set(by_actor) == {"clockworks~tiktok-hashtag-scraper",
                             "clockworks~tiktok-scraper"}
    assert by_actor["clockworks~tiktok-hashtag-scraper"]["hashtags"] == ["тег0", "тег1"]
    assert "searchQueries" not in by_actor["clockworks~tiktok-hashtag-scraper"]
    assert by_actor["clockworks~tiktok-scraper"]["searchQueries"] == ["запрос0"]
    assert "hashtags" not in by_actor["clockworks~tiktok-scraper"]
    # строки обеих веток доехали до Sheets в одном формате
    ids = {r["raw_id"] for r in sheets.tables["raw_tiktok"]}
    assert ids == {"tiktok_v1", "tiktok_v2"}


def test_actor_keys_overridden_by_config(tmp_path):
    # Реестр акторов: apify.actors.tiktok_hashtag / .tiktok перекрывают кодовые
    # дефолты — откат миграции строкой конфига, без правки кода (спека).
    root = registry_mixed(tmp_path, n_hashtags=1, n_search=1)
    sheets = make_fake()
    client = RecordingClient({"vendor~hashtag-x": [raw_item("v1")],
                              "vendor~main-y": [raw_item("v2")]})
    config = {"apify": {"actors": {"tiktok": "vendor~main-y",
                                   "tiktok_hashtag": "vendor~hashtag-x"}}}
    collect(sheets, client, config=config, now_iso=NOW, registry_root=root,
            explore=False)
    assert {actor for actor, _ in client.calls} == {"vendor~hashtag-x",
                                                    "vendor~main-y"}


def apidojo_item(vid="a1", **over):
    """Item формата apidojo (README «Example Output Object», тикет 04);
    живых ответов до 19.08 нет — формат подтверждается в runbook смоука."""
    item = {
        "id": vid,
        "title": f"видео {vid} #тег0",
        "views": 1000, "likes": 100, "comments": 10, "shares": 5, "bookmarks": 3,
        "hashtags": ["тег0"],
        "channel": {"username": "acc", "name": "Акк"},
        "uploadedAtFormatted": FRESH,
        "video": {"duration": 20, "url": "https://v/1.mp4",
                  "cover": "https://c/1.jpg"},
        "subtitleInformation": [{"lang": "rus-RU", "language_code": "ru",
                                 "is_auto_generated": True,
                                 "url": "https://sub/a1.vtt"}],
        "postPage": f"https://www.tiktok.com/@acc/video/{vid}",
    }
    item.update(over)
    return item


def test_apidojo_hashtag_actor_switched_by_config_only(tmp_path):
    # Тикет 04: включение apidojo — ТОЛЬКО значением конфиг-ключа (кодовый
    # дефолт остаётся clockworks, см. тесты выше). Hashtag-ветка на apidojo:
    # payload его схемы, выдача через адаптер нормализации, субтитры-ссылки
    # уезжают в существующий контур скачивания. Search-ветка не тронута.
    root = registry_mixed(tmp_path, n_hashtags=2, n_search=1)
    sheets = make_fake()
    client = RecordingClient({
        "apidojo~tiktok-scraper": [apidojo_item("a1")],
        "clockworks~tiktok-scraper": [raw_item("v2", searchHashtag=None,
                                               searchQuery="запрос0")],
    })
    config = {"apify": {"actors": {"tiktok_hashtag": "apidojo~tiktok-scraper"}}}
    downloaded = []

    def downloader(url, client=None):
        downloaded.append(url)
        return True, "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nтекст apidojo\n"

    summary = collect(sheets, client, config=config, now_iso=NOW,
                      registry_root=root, explore=False, downloader=downloader)
    assert summary["status"] == "success"
    by_actor = {actor: payload for actor, payload in client.calls}
    # снапшот 1:1: у apidojo-батча — поля его схемы, ничего от clockworks
    assert by_actor["apidojo~tiktok-scraper"] == {
        "keywords": ["#тег0", "#тег1"], "maxItems": 40}
    assert by_actor["clockworks~tiktok-scraper"]["searchQueries"] == ["запрос0"]
    rows = {r["raw_id"]: r for r in sheets.tables["raw_tiktok"]}
    assert set(rows) == {"tiktok_a1", "tiktok_v2"}
    assert rows["tiktok_a1"]["source_actor"] == "apidojo/tiktok-scraper"
    assert rows["tiktok_a1"]["source_query"] == "hashtag:#тег0"
    assert rows["tiktok_a1"]["url"] == "https://www.tiktok.com/@acc/video/a1"
    # субтитры apidojo подхватил существующий контур скачивания
    assert downloaded == ["https://sub/a1.vtt"]
    assert rows["tiktok_a1"]["transcript_text"] == "текст apidojo"
    assert rows["tiktok_a1"]["processing_status"] == "raw_saved_actor_transcript"
    # search-ветка осталась прежней — clockworks-формат ряда
    assert rows["tiktok_v2"]["source_actor"] == "clockworks/tiktok-scraper"
    assert rows["tiktok_v2"]["source_query"] == "query:запрос0"


def test_apidojo_search_actor_switched_by_config_only(tmp_path):
    # Флаг на search-ветке независим от hashtag-ветки: keywords без «#»,
    # атрибуция из единственного источника батча, hashtag-батч — прежний
    # clockworks-hashtag-scraper с его payload.
    root = registry_mixed(tmp_path, n_hashtags=1, n_search=1)
    sheets = make_fake()
    client = RecordingClient({
        "apidojo~tiktok-scraper": [apidojo_item("a2", hashtags=[])],
        "clockworks~tiktok-hashtag-scraper": [raw_item("v1")],
    })
    config = {"apify": {"actors": {"tiktok": "apidojo~tiktok-scraper"}}}
    summary = collect(sheets, client, config=config, now_iso=NOW,
                      registry_root=root, explore=False)
    assert summary["status"] == "success"
    by_actor = {actor: payload for actor, payload in client.calls}
    assert by_actor["apidojo~tiktok-scraper"] == {
        "keywords": ["запрос0"], "maxItems": 20}
    assert by_actor["clockworks~tiktok-hashtag-scraper"]["hashtags"] == ["тег0"]
    rows = {r["raw_id"]: r for r in sheets.tables["raw_tiktok"]}
    assert rows["tiktok_a2"]["source_query"] == "query:запрос0"
    assert rows["tiktok_v1"]["source_actor"] == "clockworks/tiktok-scraper"


def test_apidojo_unrecognized_output_degrades_run_with_reason(tmp_path):
    # Неузнанный формат ответа apidojo — громкий провал батча с причиной в
    # Run Log (механика потерь __apify_error), не молчаливые кривые строки.
    root = registry(tmp_path, n_hashtags=2)
    sheets = make_fake()
    client = RecordingClient({
        "apidojo~tiktok-scraper": [apidojo_item("a1"),
                                   {"itemType": "чужой формат без url"}],
    })
    config = {"apify": {"actors": {"tiktok_hashtag": "apidojo~tiktok-scraper"}}}
    summary = collect(sheets, client, config=config, now_iso=NOW,
                      registry_root=root, explore=False)
    assert summary["status"] == "insufficient_data"
    assert summary["batches_failed"] == 1
    assert any("формат выхода не распознан" in r for r in summary["lost_reasons"])
    log = sheets.tables["run_log"][0]
    assert log["status"] == "insufficient_data"
    assert "формат выхода не распознан" in log["input_summary"]
    assert any("формат выхода не распознан" in e for e in json.loads(log["errors"]))
    # распознанный item того же батча при этом доехал
    assert [r["raw_id"] for r in sheets.tables["raw_tiktok"]] == ["tiktok_a1"]


class KindStub:
    """Отвечает по содержимому payload (батчи параллельны — позиция ненадёжна):
    на каждый хэштег/запрос — одно видео с его атрибуцией. fail_search_probe
    роняет ран exploration-зонда search-kind."""

    def __init__(self, fail_search_probe=False):
        self.calls = []
        self.fail_search_probe = fail_search_probe

    def run_actor(self, actor_path, payload):
        self.calls.append((actor_path, payload))
        if self.fail_search_probe and payload.get("searchQueries") == ["зонд1"]:
            raise RuntimeError("probe died")
        items = [raw_item(f"v_{t}", searchHashtag={"name": t})
                 for t in payload.get("hashtags", [])]
        items += [raw_item(f"v_{q}", searchHashtag=None, searchQuery=q)
                  for q in payload.get("searchQueries", [])]
        return RunResult("SUCCEEDED", items=items)


def cand(query, kind):
    return {"query": query, "kind": kind, "niche": "мужские-образы",
            "status": "candidate", "origin": "harvest", "added_at": "2026-07-10",
            "runs_count": 0}


def registry_with_candidates(tmp_path):
    root = registry_mixed(tmp_path, n_hashtags=1, n_search=0)
    path = root / "sources" / "tiktok.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    records += [cand("канд1", "hashtag"), cand("зонд1", "search")]
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return root


def test_exploration_candidates_go_to_actors_of_their_kind(tmp_path):
    # Тикет 02: exploration-зонды не смешиваются в один батч — search-кандидат
    # не должен уехать hashtag-актору (там он молча пропал бы из платного рана).
    from cf.collect.sources import load_registry

    root = registry_with_candidates(tmp_path)
    sheets = make_fake()
    client = KindStub()
    summary = collect(sheets, client, now_iso=NOW, registry_root=root)
    assert summary["status"] == "success"
    for actor, payload in client.calls:
        if "канд1" in payload.get("hashtags", []):
            assert actor == "clockworks~tiktok-hashtag-scraper"
        if payload.get("searchQueries"):
            assert actor == "clockworks~tiktok-scraper"
            assert payload["searchQueries"] == ["зонд1"]
    by_q = {r["query"]: r for r in load_registry("tiktok", root=root)}
    assert by_q["канд1"]["runs_count"] == 1
    assert by_q["канд1"]["rows_passed_gate"] == 1
    assert by_q["зонд1"]["runs_count"] == 1
    assert by_q["зонд1"]["rows_passed_gate"] == 1


def test_lost_search_probe_does_not_steal_hashtag_probe_run(tmp_path):
    # Каждый exploration-батч судит только СВОИХ кандидатов: сбой search-зонда
    # не крадёт прогон у hashtag-зондов (и сам сигнала не получает).
    from cf.collect.sources import load_registry

    root = registry_with_candidates(tmp_path)
    sheets = make_fake()
    client = KindStub(fail_search_probe=True)
    collect(sheets, client, now_iso=NOW, registry_root=root)
    by_q = {r["query"]: r for r in load_registry("tiktok", root=root)}
    assert by_q["канд1"]["runs_count"] == 1
    assert by_q["зонд1"].get("runs_count", 0) == 0


def test_happy_path_writes_rows_and_runlog(tmp_path):
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1"), raw_item("v2")])])
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "success"
    rows = sheets.tables["raw_tiktok"]
    assert [r["raw_id"] for r in rows] == ["tiktok_v1", "tiktok_v2"]
    assert rows[0]["source_query"] == "hashtag:#мужскаяодежда"
    assert rows[0]["transcript_text"] == "готовый транскрипт"
    assert rows[0]["processing_status"] == "raw_saved_actor_transcript"
    log = sheets.tables["run_log"][0]
    assert log["agent"] == "collect-tiktok"
    assert log["status"] == "success"


def test_broken_batch_isolated_and_counted(tmp_path):
    # 2 источника x батч<=4 -> 1 батч; форсируем 2 батча через 5 источников
    root = registry(tmp_path, n_hashtags=5)
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")]),
                         RuntimeError("actor exploded")])
    summary = collect(sheets, client, now_iso=NOW, registry_root=root)
    # Разбор 2026-07-27: потерянный батч — это НЕ «Успех». Ночью так прошёл
    # сбор с 7 потерянными источниками, и Telegram промолчал (cli.py шлёт алерт
    # только на failed/insufficient_data).
    assert summary["status"] == "insufficient_data"
    assert summary["batches_failed"] == 1
    assert summary["sources_lost"] == 1  # во втором батче 1 источник
    assert len(sheets.tables["raw_tiktok"]) == 1
    assert "batches_failed=1" in sheets.tables["run_log"][0]["input_summary"]
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"


def test_degraded_run_summary_and_errors_name_the_cause(tmp_path):
    # Разбор 2026-07-27: в Run Log за ночь лежали только имена источников
    # («query:menswear», ...) — причину («потолок трат Apify») пришлось доставать
    # руками из чужого API. Теперь причина и в сводке, и в errors, и в error
    # (его cli.py отправляет в Telegram вместо заглушки «строк не привезено»).
    root = registry(tmp_path, n_hashtags=5)
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")]),
                         RuntimeError("apify run FAILED (monthly usage hard limit)")])
    lines = []
    summary = collect(sheets, client, now_iso=NOW, registry_root=root,
                      log=lines.append)
    log = sheets.tables["run_log"][0]
    assert "monthly usage hard limit" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert any(e.startswith("hashtag:") for e in errors), "имена источников остались"
    assert any("monthly usage hard limit" in e for e in errors)
    assert "monthly usage hard limit" in summary["error"]
    assert any("деградация" in l for l in lines)
    # Ревью 2026-07-27: cli печатает «источников не опрошено» по ключу
    # lost_sources — одного счётчика sources_lost ему мало, и частичная
    # потеря уходила к оператору безымянной.
    assert summary["lost_sources"] == ["hashtag:#тег4"]


def test_total_failure_is_failed_status(tmp_path):
    sheets = make_fake()
    client = StubClient([RuntimeError("all dead")])
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "failed"
    assert sheets.tables["raw_tiktok"] == []
    assert sheets.tables["run_log"][0]["status"] == "failed"


def test_total_failure_logs_lost_sources_and_reason(tmp_path):
    # Разбор 2026-07-27: полный провал шёл в Run Log одной фразой «0 реальных
    # рядов при N упавших батчах» — без имён и без причины (raise минует _result).
    sheets = make_fake()
    client = StubClient([RuntimeError("apify poll timeout after 900s")])
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    log = sheets.tables["run_log"][0]
    assert "apify poll timeout after 900s" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert any(e.startswith("hashtag:") for e in errors)
    assert any("apify poll timeout after 900s" in e for e in errors)
    assert "apify poll timeout after 900s" in summary["error"]


def test_repeat_collection_coalesces(tmp_path):
    existing = {"raw_id": "tiktok_v1", "collected_at": "2026-07-01T00:00:00.000Z",
                "source_query": "query:старый запрос",
                "transcript_text": "старый готовый транскрипт", "views": 5}
    sheets = make_fake([existing])
    # повторный сбор того же видео без транскрипта и без субтитров
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1", transcript="")])])
    collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    row = sheets.tables["raw_tiktok"][0]
    assert row["transcript_text"] == "старый готовый транскрипт"
    assert row["source_query"] == "query:старый запрос"
    assert row["collected_at"] == "2026-07-01T00:00:00.000Z"
    assert row["views"] == 1000  # метрика свежая


def test_subtitles_downloaded_for_pending(tmp_path):
    sheets = make_fake()
    item = raw_item("v1", transcript="",
                    subtitleLinks=[{"downloadLink": "https://sub/v1.vtt",
                                    "language": "rus"}])
    client = StubClient([RunResult("SUCCEEDED", items=[item])])
    downloaded = []

    def downloader(url, client=None):
        downloaded.append(url)
        return True, "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nтекст из субтитров\n"

    collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path),
            downloader=downloader)
    assert downloaded == ["https://sub/v1.vtt"]
    row = sheets.tables["raw_tiktok"][0]
    assert row["transcript_text"] == "текст из субтитров"
    assert row["processing_status"] == "raw_saved_actor_transcript"


def test_dry_run_returns_rows_without_writes(tmp_path):
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path),
                      dry_run=True)
    assert sheets.tables["raw_tiktok"] == []
    assert [r["raw_id"] for r in summary["rows"]] == ["tiktok_v1"]
    assert sheets.tables["run_log"][0]["trigger_type"] == "dry-run"


def test_limit_batches_caps_apify_calls(tmp_path):
    root = registry(tmp_path, n_hashtags=9)  # 3 батча
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    collect(sheets, client, now_iso=NOW, registry_root=root, limit_batches=1)
    assert len(client.calls) == 1


def test_empty_registry_insufficient_data(tmp_path):
    root = registry(tmp_path, n_hashtags=0)
    sheets = make_fake()
    client = StubClient([])
    summary = collect(sheets, client, now_iso=NOW, registry_root=root)
    assert summary["status"] == "insufficient_data"
    assert client.calls == []
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"


def test_collect_emits_progress_markers_for_dashboard(tmp_path):
    # §7 timeline: сбор рассказывает о себе по ходу — фазы и готовые батчи Apify.
    # Без этого дашборд знал только «платформа началась / кончилась» и бар прыгал.
    from cf.collect import progress as marker

    root = registry(tmp_path, n_hashtags=5)              # -> 2 батча
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")]),
                         RunResult("SUCCEEDED", items=[raw_item("v2")])])
    lines = []
    summary = collect(sheets, client, now_iso=NOW, registry_root=root,
                      log=lines.append)
    markers = [marker.parse(l) for l in lines if marker.is_marker(l)]
    phases = [m["phase"] for m in markers]
    assert phases[0] == "prepare"
    assert "fetch" in phases and phases.index("gate") > phases.index("fetch")
    assert phases[-1] == "write"                        # порядок фаз — как в плане
    fetch = [m for m in markers if m["phase"] == "fetch"]
    assert fetch[0]["done"] == 0 and fetch[-1]["done"] == fetch[-1]["total"]
    assert fetch[-1]["total"] >= 2                      # батчи, а не платформы
    # сводка сбора маркерами не подменяется — она по-прежнему в выводе
    assert any("kept=" in l for l in lines)
    # итог единицы работы приезжает числом: лента показывает ролики, а не платформы
    assert markers[-1]["phase"] == "write"
    assert markers[-1]["rows"] == len(summary["rows"])
    assert markers[-1]["new"] == summary["upsert"]["appended"]


# ── Тикет 06 (план 2026-08-10-apify-costs): факты про источники в сводке ─────
# Сборщик уже вычисляет, что прогон сделал с exploration-кандидатами (зонды,
# ретайр, promote-proposal), но наружу отдавал только строку Run Log — блоку
# в Telegram-сводке взять факты было неоткуда. Возвращаем их маркерами
# (каноническое имя источника), тексты собирает cf.messages.


class ByContentClient:
    """Отвечает по содержимому батча (батчи параллельны, порядок ненадёжен)."""

    def run_actor(self, actor_path, payload):
        return RunResult("SUCCEEDED", items=[
            raw_item(f"v_{tag}", searchHashtag={"name": tag})
            for tag in payload.get("hashtags", [])])


def _cand(query, runs, rows):
    return {"query": query, "kind": "hashtag", "niche": "мужские-образы",
            "status": "candidate", "origin": "harvest", "added_at": "2026-07-01",
            "runs_count": runs, "rows_passed_gate": rows}


def registry_for_aging(tmp_path):
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    records = [
        {"query": "тег0", "kind": "hashtag", "niche": "мужские-образы",
         "status": "active", "origin": "operator", "added_at": "2026-07-01"},
        _cand("пробный", runs=0, rows=0),      # только зондируется
        _cand("мёртвый", runs=3, rows=0),      # дозрел и бесплоден -> retired
        _cand("зрелый", runs=2, rows=20),      # дозрел и урожаен -> proposal
    ]
    (d / "tiktok.json").write_text(json.dumps(records, ensure_ascii=False),
                                   encoding="utf-8")
    return tmp_path


def test_summary_exposes_exploration_facts_for_the_owner(tmp_path):
    root = registry_for_aging(tmp_path)
    # свежие строки зрелого кандидата с высоким ER — почва для promote
    raw_rows = [{"raw_id": f"tiktok_x{i}", "source_query": "hashtag:#зрелый",
                 "engagement_rate": 0.2, "collected_at": FRESH}
                for i in range(3)]
    sheets = make_fake(raw_rows)
    summary = collect(sheets, ByContentClient(), now_iso=NOW, registry_root=root)
    assert summary["status"] == "success"
    expl = summary["exploration"]
    assert sorted(expl["explored"]) == ["hashtag:#зрелый", "hashtag:#мёртвый",
                                        "hashtag:#пробный"]
    assert expl["retired"] == ["hashtag:#мёртвый"]
    assert expl["promoted"] == ["hashtag:#зрелый"]


def test_summary_exploration_empty_without_candidates(tmp_path):
    # прогон без exploration-активности отдаёт пустые списки — блок в сводке
    # не появится (ни заголовка, ни нулей)
    sheets = make_fake()
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["exploration"] == {"explored": [], "retired": [],
                                      "promoted": []}


def test_dry_run_reports_no_exploration_facts(tmp_path):
    # dry-run реестр не пишет и proposal не создаёт — фактов «по прогону» нет,
    # и врать про них в сводке нельзя
    root = registry_for_aging(tmp_path)
    sheets = make_fake()
    summary = collect(sheets, ByContentClient(), now_iso=NOW, registry_root=root,
                      dry_run=True)
    assert summary["exploration"] == {"explored": [], "retired": [],
                                      "promoted": []}
