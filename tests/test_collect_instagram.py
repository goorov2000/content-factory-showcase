# C2.2 — Instagram 3 стадии: порт Hashtags-инвариантов instagram.test.js
# (matchesNiche, dedupeHashtags) и Prepare-инвариантов batching.test.js
# (уникальные reel-URL <=30, tag_by_code, батчи <=6) + e2e пайплайна.
import json

from cf.collect.apify import RunResult
from cf.collect.instagram import (
    build_reel_payload,
    build_transcript_payload,
    collect,
    dedupe_hashtags,
    hashtag_batches,
    matches_niche,
    merge_hashtags,
    prepare_reel_batches,
)
from cf.collect.sources import load_registry

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


# JS: «matchesNiche: 'men' не матчит 'women' (граница слова)»
def test_matches_niche_word_boundary():
    assert matches_niche("menstyle") is True
    assert matches_niche("men") is True
    assert matches_niche("women") is False
    assert matches_niche("womenfashionista") is True  # fashion — подстрока, как в JS
    assert matches_niche("мужскиеобразы") is True
    assert matches_niche("котики") is False


# JS: «dedupeHashtags: регистронезависимый, первая форма побеждает»
def test_dedupe_hashtags_case_insensitive():
    assert dedupe_hashtags(["MenStyle", "menstyle", "MENSTYLE", "other"]) == \
        ["MenStyle", "other"]
    assert dedupe_hashtags(["", "a", "A"]) == ["a"]


# JS e2e: seed + discovered, фильтр ниши, лимиты 24/28
def test_merge_hashtags_seed_first_filter_and_limits():
    discovery = [[{"hashtag": "#menswear"}, {"name": "women"},
                  {"title": "стильмужской"}, {"hashtag": "кулинария"}]]
    out = merge_hashtags(["мужскаяодежда", "menswear"], discovery)
    # women и кулинария отфильтрованы; menswear не задублирован (seed-форма побеждает)
    assert out == ["мужскаяодежда", "menswear", "стильмужской"]


def test_merge_hashtags_discovered_capped_24_total_28():
    discovery = [[{"hashtag": f"menstyle{i}"} for i in range(40)]]
    seed = [f"seedmen{i}" for i in range(10)]
    out = merge_hashtags(seed, discovery)
    assert len(out) == 28  # 10 seed + 24 discovered, обрезано до 28
    assert out[:10] == seed


def test_hashtag_batches_full_source_query():
    batches = hashtag_batches([f"тег{i}" for i in range(7)])
    assert len(batches) == 2
    assert batches[0]["batch_size"] == 4
    assert batches[1]["batch_size"] == 3
    full = ",".join(f"тег{i}" for i in range(7))
    assert all(b["source_query"] == full for b in batches)
    assert batches[1]["batch_sources"] == ["hashtag:#тег4", "hashtag:#тег5", "hashtag:#тег6"]


# JS Prepare: уникальные URL, <=30, tag_by_code, батчи <=6, reel:-маркеры
def test_prepare_reel_batches():
    reels = [{"url": f"https://www.instagram.com/reel/C{i}/", "hashtag": "menstyle"}
             for i in range(8)]
    reels.append({"url": "https://www.instagram.com/reel/C0/?igsh=1"})  # дубль C0
    batches, tag_by_code = prepare_reel_batches(reels, source_query="a,b")
    assert len(batches) == 2  # 8 уникальных -> 6+2
    assert batches[0]["batch_size"] == 6
    assert batches[1]["batch_size"] == 2
    assert batches[0]["batch_sources"][0] == "reel:https://www.instagram.com/reel/C0/"
    assert tag_by_code["C0"] == "menstyle"
    assert all(b["tag_by_code"] == tag_by_code for b in batches)


def test_prepare_reel_batches_cap_30():
    reels = [{"url": f"https://www.instagram.com/reel/D{i}/"} for i in range(45)]
    batches, _ = prepare_reel_batches(reels)
    assert sum(b["batch_size"] for b in batches) == 30


def test_prepare_reel_batches_empty():
    batches, tag_by_code = prepare_reel_batches([{"caption": "без url"}])
    assert batches == []


# --- payload-снапшоты (дрейф полей виден на ревью, а не на счёте Apify) ---

def test_reel_payload_snapshot_no_transcript_addon():
    # Снапшот 1:1 — тикет 03 плана 2026-08-10-apify-costs: includeTranscript
    # снят, аддон стоил $0.041/начатую минуту за КАЖДЫЙ собранный ролик (40%
    # счёта Apify). Расшифровка — отдельным этапом ПОСЛЕ гейта (apple_yang).
    assert build_reel_payload({"reel_urls": ["https://www.instagram.com/reel/C1/"]}) == {
        "username": ["https://www.instagram.com/reel/C1/"],
        "resultsLimit": 1,
        "includeDownloadedVideo": False,
        "includeSharesCount": False,
        "skipPinnedPosts": True,
        "skipTrialReels": True,
    }


def test_transcript_payload_snapshot_bulk_urls():
    # Схема билда apple_yang: единственное поле bulkUrls (сверено 10.08).
    assert build_transcript_payload(["https://www.instagram.com/reel/C1/",
                                     "https://www.instagram.com/reel/C2/"]) == {
        "bulkUrls": ["https://www.instagram.com/reel/C1/",
                     "https://www.instagram.com/reel/C2/"],
    }


# --- e2e пайплайна ---

def registry(tmp_path, hashtags=2, searches=1):
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    records = [{"query": f"мужтег{i}", "kind": "hashtag", "niche": "мужские-образы",
                "status": "active", "origin": "operator", "added_at": "2026-07-01"}
               for i in range(hashtags)]
    records += [{"query": f"запрос {i}", "kind": "search", "niche": "мужские-образы",
                 "status": "active", "origin": "operator", "added_at": "2026-07-01"}
                for i in range(searches)]
    (d / "instagram.json").write_text(json.dumps(records, ensure_ascii=False),
                                      encoding="utf-8")
    return tmp_path


def hashtag_reel(code, tag="мужтег0", views=1000):
    return {"url": f"https://www.instagram.com/reel/{code}/", "hashtag": tag,
            "ownerUsername": "acc", "caption": f"пост {code}",
            "timestamp": FRESH, "videoViewCount": views}


def reel_item(code, transcript="текст"):
    return {"url": f"https://www.instagram.com/reel/{code}/", "shortCode": code,
            "ownerUsername": "acc", "caption": f"пост {code}",
            "timestamp": FRESH, "videoViewCount": 1000, "likesCount": 100,
            "transcript": transcript}


class StubClient:
    """Ответы по префиксу actor_path: discovery/hashtag/reel/transcripts."""

    def __init__(self, discovery=None, hashtag=None, reel=None, transcripts=None):
        self.responses = {"search": list(discovery or []),
                          "hashtag": list(hashtag or []), "reel": list(reel or []),
                          "transcripts": list(transcripts or [])}
        self.calls = []
        self.actor_paths = []   # (kind, actor_path) — сверка ключей реестра акторов

    def run_actor(self, actor_path, payload):
        kind = ("transcripts" if "transcript" in actor_path
                else "search" if "search" in actor_path
                else "hashtag" if "hashtag" in actor_path else "reel")
        self.calls.append((kind, payload))
        self.actor_paths.append((kind, actor_path))
        out = self.responses[kind].pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def make_fake():
    return FakeSheets(tables={"raw_instagram": [], "run_log": []})


def test_e2e_happy_path_three_stages(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[{"hashtag": "menswearlook"}])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1"), hashtag_reel("C2")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1"), reel_item("C2")])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "success"
    rows = sheets.tables["raw_instagram"]
    assert [r["raw_id"] for r in rows] == ["instagram_C1", "instagram_C2"]
    assert rows[0]["source_query"] == "hashtag:#мужтег0"
    assert rows[0]["transcript_text"] == "текст"
    assert sheets.tables["run_log"][0]["agent"] == "collect-instagram"
    # discovery-тег доехал до hashtag-батча
    hashtag_call = next(p for k, p in client.calls if k == "hashtag")
    assert "menswearlook" in hashtag_call["hashtags"]


def test_e2e_discovery_failure_degrades_to_seed(tmp_path):
    client = StubClient(
        discovery=[RuntimeError("apify start 402 monthly usage hard limit")],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1")])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    # деградация до seed-тегов — штатный путь (в n8n onError: continue), статус
    # не портим; но (разбор 2026-07-27) текст отказа обязан пережить except:
    # 402 «кончились деньги», 401 «протух токен» и таймаут лечатся по-разному.
    assert summary["status"] == "success"
    assert summary["discovery_failed"] is True
    assert "402" in summary["discovery_error"]
    log = sheets.tables["run_log"][0]
    assert "402" in log["input_summary"]
    assert any(e.startswith("discovery: ") and "402" in e
               for e in json.loads(log["errors"]))
    hashtag_call = next(p for k, p in client.calls if k == "hashtag")
    assert hashtag_call["hashtags"] == ["мужтег0", "мужтег1"]  # только seed


def test_e2e_partial_reel_failure_degrades_with_reason(tmp_path):
    # 8 рилсов -> 2 reel-батча (6+2); второй падает -> reels_lost, без throw.
    # (разбор 2026-07-27) Раньше это был «success» с молчащим Telegram и без
    # единого слова о причине потери дорогой стадии — контракт: потерян батч,
    # значит insufficient_data (правило №2), причина в сводке и в errors.
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED",
                           items=[hashtag_reel(f"C{i}") for i in range(8)])],
        reel=[RunResult("SUCCEEDED", items=[reel_item(f"C{i}") for i in range(6)]),
              RuntimeError("reel batch died")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path),
                      max_workers=1)   # порядок ответов стаба = порядок батчей
    assert summary["status"] == "insufficient_data"
    assert summary["reel"]["reels_lost"] == 2
    assert summary["reel"]["batches_failed"] == 1
    assert len(sheets.tables["raw_instagram"]) == 6
    assert "reel batch died" in summary["error"]
    log = sheets.tables["run_log"][0]
    assert log["status"] == "insufficient_data"
    assert "деградация" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert "reel:https://www.instagram.com/reel/C6/" in errors
    assert any("батч 1:" in e and "reel batch died" in e for e in errors)


def test_e2e_partial_hashtag_failure_degrades_with_reason(tmp_path):
    # 5 тегов -> 2 hashtag-батча (4+1); второй не стартовал (ночь 27.07: потолок
    # трат Apify). Строки приехали, но семи источников в них нет — не «Успех».
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")]),
                 RuntimeError("apify: monthly usage hard limit exceeded")],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1")])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, max_workers=1,
                      registry_root=registry(tmp_path, hashtags=5, searches=0))
    assert summary["status"] == "insufficient_data"
    assert len(sheets.tables["raw_instagram"]) == 1
    assert "monthly usage" in summary["error"]
    log = sheets.tables["run_log"][0]
    assert log["status"] == "insufficient_data"
    assert "деградация" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert "hashtag:#мужтег4" in errors
    assert any("батч 1:" in e and "monthly usage" in e for e in errors)


def test_e2e_gate_kept_nothing_still_names_lost_batches(tmp_path):
    # Уцелевший батч привёз только протухшее (гейт всё выбросил), второй не
    # стартовал: статус тот же, а причины разные — оператору нужна вторая.
    old = hashtag_reel("C1")
    old["timestamp"] = "2026-05-01T12:00:00.000Z"      # старше 30 дней -> too_old
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[old]),
                 RuntimeError("apify: monthly usage hard limit exceeded")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, max_workers=1,
                      registry_root=registry(tmp_path, hashtags=5, searches=0))
    assert summary["status"] == "insufficient_data"
    assert "monthly usage" in summary["error"]
    log = sheets.tables["run_log"][0]
    errors = json.loads(log["errors"])
    assert "hashtag:#мужтег4" in errors
    assert any("батч 1:" in e and "monthly usage" in e for e in errors)


def test_e2e_total_reel_failure_is_failed(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RuntimeError("reel stage dead")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "failed"
    assert sheets.tables["raw_instagram"] == []
    assert sheets.tables["run_log"][0]["status"] == "failed"


def test_e2e_all_hashtag_batches_failed_is_failed(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RuntimeError("apify: monthly usage hard limit exceeded")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "failed"
    log = sheets.tables["run_log"][0]
    assert log["status"] == "failed"
    # (разбор 2026-07-27) Самый громкий отказ был самым бессодержательным:
    # ни имён потерянных тегов, ни причины — их пришлось доставать из API Apify.
    assert "monthly usage" in log["input_summary"]
    errors = json.loads(log["errors"])
    assert "hashtag:#мужтег0" in errors
    assert any("батч 0:" in e and "monthly usage" in e for e in errors)
    assert summary["lost_reasons"] and summary["lost_sources"]


def test_e2e_empty_registry_and_discovery_insufficient(tmp_path):
    root = registry(tmp_path, hashtags=0, searches=1)
    client = StubClient(discovery=[RunResult("SUCCEEDED", items=[])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=root)
    assert summary["status"] == "insufficient_data"
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"


def test_e2e_dry_run_no_writes(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1")])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path),
                      dry_run=True)
    assert sheets.tables["raw_instagram"] == []
    assert [r["raw_id"] for r in summary["rows"]] == ["instagram_C1"]
    assert sheets.tables["run_log"][0]["trigger_type"] == "dry-run"


# ── тикет 03 (план 2026-08-10-apify-costs): транскрипты apple_yang ПОСЛЕ гейта ──
# Маппинг ответа (transcript → text → segments[].text; url/inputUrl/shortCode) —
# ДОПУЩЕНИЕ из описания актора, подтверждение — runbook 19.08 (тикет 07).

def bare_reel(code):
    """Ответ reel-scraper БЕЗ транскрипта — мир после снятия includeTranscript."""
    return {"url": f"https://www.instagram.com/reel/{code}/", "shortCode": code,
            "ownerUsername": "acc", "caption": f"пост {code}",
            "timestamp": FRESH, "videoViewCount": 1000, "likesCount": 100}


def test_e2e_transcripts_one_bulk_call_after_gate(tmp_path):
    # Три формы ответа apple_yang в одном прогоне: url+transcript,
    # inputUrl+segments[].text (склейка), shortCode+text.
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1"), hashtag_reel("C2"),
                                               hashtag_reel("C3")])],
        reel=[RunResult("SUCCEEDED",
                        items=[bare_reel("C1"), bare_reel("C2"), bare_reel("C3")])],
        transcripts=[RunResult("SUCCEEDED", items=[
            {"url": "https://www.instagram.com/reel/C1/", "transcript": "первый текст"},
            {"inputUrl": "https://www.instagram.com/reel/C2/",
             "segments": [{"text": "второй"}, {"text": "текст"}]},
            {"shortCode": "C3", "text": "третий текст"},
            # дубль C1 в выдаче: первый результат побеждает и не двоит счётчик
            {"url": "https://www.instagram.com/reel/C1/", "transcript": "дубль"},
        ])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "success"
    # ровно ОДИН bulk-вызов, в нём только прошедшие гейт строки без транскрипта
    assert [p for k, p in client.calls if k == "transcripts"] == [
        {"bulkUrls": ["https://www.instagram.com/reel/C1/",
                      "https://www.instagram.com/reel/C2/",
                      "https://www.instagram.com/reel/C3/"]}]
    # актор — из кодового дефолта реестра
    assert ("transcripts", "apple_yang~instagram-transcripts-scraper") \
        in client.actor_paths
    rows = {r["raw_id"]: r for r in sheets.tables["raw_instagram"]}
    assert rows["instagram_C1"]["transcript_text"] == "первый текст"
    assert rows["instagram_C2"]["transcript_text"] == "второй текст"
    assert rows["instagram_C3"]["transcript_text"] == "третий текст"
    # контракт processing_status — 1:1 с нынешним актор-транскриптом normalize
    assert all(r["processing_status"] == "raw_saved_actor_transcript"
               for r in rows.values())
    assert summary["transcripts"]["ok"] == 3
    assert summary["transcripts"]["requested"] == 3


def test_e2e_only_rows_without_transcript_go_to_bulk(tmp_path):
    # C1 пришёл из reel-scraper уже с текстом (актор может отдавать его и без
    # аддона) — повторно не расшифровываем; в bulkUrls только C2.
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1"), hashtag_reel("C2")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1", transcript="готовый"),
                                            bare_reel("C2")])],
        transcripts=[RunResult("SUCCEEDED", items=[
            {"url": "https://www.instagram.com/reel/C2/", "transcript": "новый"}])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert [p for k, p in client.calls if k == "transcripts"] == [
        {"bulkUrls": ["https://www.instagram.com/reel/C2/"]}]
    rows = {r["raw_id"]: r for r in sheets.tables["raw_instagram"]}
    assert rows["instagram_C1"]["transcript_text"] == "готовый"
    assert rows["instagram_C2"]["transcript_text"] == "новый"
    assert summary["transcripts"] == {"requested": 1, "reused": 0, "ok": 1,
                                      "failed": False, "error": ""}


def test_e2e_transcript_from_previous_run_reused_no_paid_call(tmp_path):
    # Вкладка держит текст C1 с прошлого сбора, прогон приносит C1 без текста.
    # Upsert IG без coalesce перезаписал бы готовый текст пустым, а bulk оплатил
    # бы расшифровку заново — переносим из вкладки; пустой pending = ноль
    # платных вызовов (acceptance: «уже расшифрованное не расшифровывается
    # повторно», «пустой список — вызова нет вообще»).
    sheets = FakeSheets(tables={
        "raw_instagram": [{"raw_id": "instagram_C1",
                           "transcript_text": "старый текст",
                           "processing_status": "raw_saved_actor_transcript"}],
        "run_log": []})
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[bare_reel("C1")])])
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["status"] == "success"
    assert not any(k == "transcripts" for k, _ in client.calls)
    row = next(r for r in sheets.tables["raw_instagram"]
               if r["raw_id"] == "instagram_C1")
    assert row["transcript_text"] == "старый текст"
    assert row["processing_status"] == "raw_saved_actor_transcript"
    assert summary["transcripts"] == {"requested": 0, "reused": 1, "ok": 0,
                                      "failed": False, "error": ""}


def test_e2e_transcript_actor_key_overridden_by_config(tmp_path):
    # Реестр акторов: значение apify.actors.instagram_transcripts перекрывает
    # кодовый дефолт — откат/замена актора строкой конфига, без правки кода.
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[bare_reel("C1")])],
        transcripts=[RunResult("SUCCEEDED", items=[
            {"url": "https://www.instagram.com/reel/C1/", "transcript": "текст"}])])
    sheets = make_fake()
    config = {"apify": {"actors": {"instagram_transcripts": "vendor~transcripts-x"}}}
    collect(sheets, client, config=config, now_iso=NOW,
            registry_root=registry(tmp_path))
    assert ("transcripts", "vendor~transcripts-x") in client.actor_paths
    assert ("transcripts", "apple_yang~instagram-transcripts-scraper") \
        not in client.actor_paths


def _assert_transcript_degraded(sheets, summary, reason_substr):
    """Контракт деградации этапа — по образцу discovery: сбор НЕ падает, строки
    уезжают без текста, причина — в сводке и errors Run Log."""
    assert summary["status"] == "success"
    assert summary["transcripts"]["failed"] is True
    assert reason_substr in summary["transcripts"]["error"]
    row = sheets.tables["raw_instagram"][0]
    assert row["transcript_text"] == ""
    assert row["processing_status"] == "raw_saved_no_actor_transcript"
    log = sheets.tables["run_log"][0]
    assert "сбой этапа" in log["input_summary"]
    assert reason_substr in log["input_summary"]
    assert any(e.startswith("transcripts: ") and reason_substr in e
               for e in json.loads(log["errors"]))


def test_e2e_transcript_stage_exception_degrades_not_fails(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[bare_reel("C1")])],
        transcripts=[RuntimeError("apify start 402 monthly usage hard limit")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    _assert_transcript_degraded(sheets, summary, "402")


def test_e2e_transcript_run_not_ok_degrades_with_reason(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[bare_reel("C1")])],
        transcripts=[RunResult("FAILED",
                               error="apify run FAILED (apple_yang~...)")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    _assert_transcript_degraded(sheets, summary, "apify run FAILED")


def test_e2e_transcript_unknown_format_loud_with_sample_keys(tmp_path):
    # Маппинг-допущение протухло: items есть, но ни сопоставления, ни текста.
    # Молчаливые пустые транскрипты запрещены — причина несёт ключи item.
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[bare_reel("C1")])],
        transcripts=[RunResult("SUCCEEDED", items=[
            {"videoId": "123", "transcription": "текст в чужом ключе"}])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    _assert_transcript_degraded(sheets, summary, "не распознан")
    assert "transcription" in summary["transcripts"]["error"]
    assert "videoId" in summary["transcripts"]["error"]


# ── H11 (аудит 2026-07-24): кандидатам гарантированы слоты в капе 30 ──────────

def _tag_reel(code, tag):
    return {"url": f"https://www.instagram.com/reel/{code}/",
            "inputUrl": f"https://www.instagram.com/explore/tags/{tag}"}


def test_candidate_reels_survive_30_url_cap():
    # 32 reel активных тегов + 4 кандидатских В КОНЦЕ (как в бою: кандидатские
    # батчи идут последними) — раньше кап 30 отрезал кандидатов целиком.
    reels = ([_tag_reel(f"a{i}", "active") for i in range(32)]
             + [_tag_reel(f"c{i}", "НовыйТег") for i in range(4)])
    batches, _ = prepare_reel_batches(reels, candidate_tags=["новыйтег"])
    urls = [u for b in batches for u in b["reel_urls"]]
    assert len(urls) == 30
    assert sum("/reel/c" in u for u in urls) == 4      # все кандидаты вошли


def test_candidate_slots_capped():
    reels = ([_tag_reel(f"a{i}", "active") for i in range(30)]
             + [_tag_reel(f"c{i}", "cand") for i in range(10)])
    batches, _ = prepare_reel_batches(reels, candidate_tags=["cand"],
                                      reserved_candidate_slots=6)
    urls = [u for b in batches for u in b["reel_urls"]]
    assert len(urls) == 30
    assert sum("/reel/c" in u for u in urls) == 6      # не больше квоты


def test_under_cap_no_reservation_needed():
    reels = ([_tag_reel(f"a{i}", "active") for i in range(10)]
             + [_tag_reel(f"c{i}", "cand") for i in range(4)])
    batches, _ = prepare_reel_batches(reels, candidate_tags=["cand"])
    urls = [u for b in batches for u in b["reel_urls"]]
    assert len(urls) == 14                             # всё влезло — порядок исходный


# ── разбор 2026-07-27: голодание кандидатов и потеря причин ──────────────────

def test_reserved_slots_split_between_candidates_round_robin():
    """26.07: тег 164494443 забрал 5 слотов из 6 резерва, а avocadostyle /
    mensfashion / mydubai не получили ни одного — и поехали к ретайру за
    «rows_passed_gate=0». Резерв делится по кандидатам, а не срезом первых k."""
    reels = ([_tag_reel(f"a{i}", "active") for i in range(30)]
             + [_tag_reel(f"f{i}", "жирный") for i in range(8)]
             + [_tag_reel(f"t{i}", "тонкий1") for i in range(2)]
             + [_tag_reel(f"u{i}", "тонкий2") for i in range(2)]
             + [_tag_reel(f"v{i}", "тонкий3") for i in range(2)])
    batches, _ = prepare_reel_batches(
        reels, candidate_tags=["жирный", "тонкий1", "тонкий2", "тонкий3"])
    urls = [u for b in batches for u in b["reel_urls"]]
    assert len(urls) == 30
    for prefix in ("f", "t", "u", "v"):
        assert sum(f"/reel/{prefix}" in u for u in urls) >= 1, prefix


def registry_records(tmp_path, records):
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    (d / "instagram.json").write_text(json.dumps(records, ensure_ascii=False),
                                      encoding="utf-8")
    return tmp_path


def active_rec(query="мужтег0"):
    return {"query": query, "kind": "hashtag", "niche": "мужские-образы",
            "status": "active", "origin": "operator", "added_at": "2026-07-01"}


def cand_rec(query="кандтег", **fields):
    rec = {"query": query, "kind": "hashtag", "niche": "мужские-образы",
           "status": "candidate", "origin": "harvest", "added_at": "2026-07-01"}
    rec.update(fields)
    return rec


def test_probe_not_counted_when_candidate_reels_never_arrived(tmp_path):
    """Зонда не было — значит и вердикта нет.

    6 рилсов активного тега + 2 кандидатских: кандидат целиком уезжает во
    второй reel-батч, а тот не стартует. Раньше runs_count начислялся по успеху
    hashtag-стадии, а судили кандидата по строкам ПОСЛЕ reel-стадии — два таких
    прогона давали необратимый retired «yield: rows_passed_gate=0»."""
    root = registry_records(tmp_path, [active_rec(), cand_rec()])
    client = StubClient(
        hashtag=[RunResult("SUCCEEDED",
                           items=[hashtag_reel(f"C{i}") for i in range(6)]
                           + [hashtag_reel(f"K{i}", "кандтег") for i in range(2)])],
        reel=[RunResult("SUCCEEDED", items=[reel_item(f"C{i}") for i in range(6)]),
              RuntimeError("apify: monthly usage hard limit exceeded")])
    sheets = make_fake()
    collect(sheets, client, now_iso=NOW, registry_root=root, max_workers=1)
    reg = {r["query"]: r for r in load_registry("instagram", root=root)}
    assert reg["кандтег"].get("runs_count", 0) == 0
    assert reg["кандтег"].get("rows_passed_gate", 0) == 0
    assert reg["кандтег"]["status"] == "candidate"
    assert "зонд не засчитан" in sheets.tables["run_log"][0]["input_summary"]


def test_probe_counted_when_candidate_tag_returned_nothing(tmp_path):
    """Граница обратная: тег отработал вхолостую САМ (ни одного рилса на входе
    reel-стадии) — вердикт полный, зонд засчитан. Иначе очередь кандидатов не
    разгребается никогда, pool_cap упирается в вечных кандидатов и приток
    новых источников встаёт — ровно дефект Д4, только с другой стороны."""
    root = registry_records(tmp_path, [active_rec(), cand_rec()])
    client = StubClient(
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1")])])
    sheets = make_fake()
    collect(sheets, client, now_iso=NOW, registry_root=root, max_workers=1)
    reg = {r["query"]: r for r in load_registry("instagram", root=root)}
    assert reg["кандтег"]["runs_count"] == 1


def test_aging_skipped_on_run_with_lost_batches(tmp_path):
    """Ретайр необратим, а прогон с потерянными батчами видел неполную выдачу:
    судить по нему кандидатов нельзя (правило №2 CLAUDE.md)."""
    root = registry_records(tmp_path, [
        active_rec(), cand_rec("старыйканд", runs_count=2, rows_passed_gate=0)])
    client = StubClient(
        hashtag=[RunResult("SUCCEEDED",
                           items=[hashtag_reel(f"C{i}") for i in range(8)])],
        reel=[RunResult("SUCCEEDED", items=[reel_item(f"C{i}") for i in range(6)]),
              RuntimeError("apify: monthly usage hard limit exceeded")])
    sheets = make_fake()
    collect(sheets, client, now_iso=NOW, registry_root=root, max_workers=1)
    reg = {r["query"]: r for r in load_registry("instagram", root=root)}
    assert reg["старыйканд"]["status"] == "candidate"
    assert "retired_reason" not in reg["старыйканд"]


# ── Тикет 06 (план 2026-08-10-apify-costs): факты про источники в сводке ─────
# Как у TikTok: зонды, ретайр и promote-proposal сборщик уже считал, но наружу
# отдавал только строку Run Log — сводке в Telegram взять факты было неоткуда.


def test_summary_exposes_exploration_facts_for_the_owner(tmp_path):
    root = registry_records(tmp_path, [
        active_rec(),
        cand_rec("пробтег"),                                   # только зондируется
        cand_rec("мёртвый", runs_count=2, rows_passed_gate=0),  # -> retired
        cand_rec("зрелая", runs_count=2, rows_passed_gate=20),  # -> proposal
    ])
    client = StubClient(
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1")])])
    # свежие строки зрелого кандидата с высоким ER — почва для promote
    raw_rows = [{"raw_id": f"instagram_x{i}", "source_query": "hashtag:#зрелая",
                 "engagement_rate": 0.2, "collected_at": FRESH}
                for i in range(3)]
    sheets = FakeSheets(tables={"raw_instagram": raw_rows, "run_log": []})
    summary = collect(sheets, client, now_iso=NOW, registry_root=root,
                      max_workers=1)
    assert summary["status"] == "success"
    expl = summary["exploration"]
    assert sorted(expl["explored"]) == ["hashtag:#зрелая", "hashtag:#мёртвый",
                                        "hashtag:#пробтег"]
    assert expl["retired"] == ["hashtag:#мёртвый"]
    assert expl["promoted"] == ["hashtag:#зрелая"]


def test_summary_exploration_empty_without_candidates(tmp_path):
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1")])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    assert summary["exploration"] == {"explored": [], "retired": [],
                                      "promoted": []}


def test_degraded_run_reports_probes_but_no_verdicts(tmp_path):
    # aging на прогоне с потерями пропущен — вердиктов нет и в сводке их нет;
    # засчитанные зонды при этом факты, и они видны
    root = registry_records(tmp_path, [
        active_rec(), cand_rec("старыйканд", runs_count=2, rows_passed_gate=0)])
    client = StubClient(
        hashtag=[RunResult("SUCCEEDED",
                           items=[hashtag_reel(f"C{i}") for i in range(8)])],
        reel=[RunResult("SUCCEEDED", items=[reel_item(f"C{i}") for i in range(6)]),
              RuntimeError("apify: monthly usage hard limit exceeded")])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=root,
                      max_workers=1)
    assert summary["exploration"] == {"explored": ["hashtag:#старыйканд"],
                                      "retired": [], "promoted": []}
    assert "aging пропущен" in sheets.tables["run_log"][0]["input_summary"]


# --- Тикет 01 визуального контура: coalesce на пересборе IG (префактор) ---


def test_e2e_repeat_collection_coalesces(tmp_path):
    # Пересбор той же IG-строки перестаёт затирать дорогие поля: то же правило
    # coalesce, что у TikTok/snowball (прообраз — test_repeat_collection_coalesces
    # в test_collect_tiktok.py). Без merge-правила upsert затирал source_query
    # (исходная атрибуция) и collected_at (first-seen — несущий инвариант
    # runner._new_rows_since), а медиа/visual-поля тикетов 03–05 получили бы
    # то же ружьё.
    existing = {"raw_id": "instagram_C1",
                "collected_at": "2026-07-01T00:00:00.000Z",
                "source_query": "hashtag:#старыйтег",
                "transcript_text": "старый готовый транскрипт", "views": 5}
    sheets = FakeSheets(tables={"raw_instagram": [existing], "run_log": []})
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED", items=[hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[reel_item("C1", transcript="")])])
    collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path))
    row = sheets.tables["raw_instagram"][0]
    assert row["transcript_text"] == "старый готовый транскрипт"
    assert row["source_query"] == "hashtag:#старыйтег"       # исходная атрибуция
    assert row["collected_at"] == "2026-07-01T00:00:00.000Z"  # first-seen
    assert row["views"] == 1000                               # метрика свежая
    # платного транскрипт-вызова не было: текст перенесён из вкладки (reuse)
    assert not [k for k, _ in client.calls if k == "transcripts"]


def test_e2e_duplicate_raw_id_in_one_run_merged_by_coalesce(tmp_path):
    # H12/M33: related-выдачи приносят один reel дважды за прогон. Дубль без
    # merge-правила побеждал «последним» и затирал непустой транскрипт первого
    # вхождения пустым — то же правило coalesce, что у дедупа TikTok.
    dup_without_text = reel_item("C0", transcript="")
    client = StubClient(
        discovery=[RunResult("SUCCEEDED", items=[])],
        hashtag=[RunResult("SUCCEEDED",
                           items=[hashtag_reel(f"C{i}") for i in range(7)])],
        reel=[RunResult("SUCCEEDED", items=[reel_item(f"C{i}") for i in range(6)]),
              RunResult("SUCCEEDED", items=[reel_item("C6"), dup_without_text])])
    sheets = make_fake()
    summary = collect(sheets, client, now_iso=NOW, registry_root=registry(tmp_path),
                      max_workers=1)
    assert summary["status"] == "success"
    rows = sheets.tables["raw_instagram"]
    assert [r["raw_id"] for r in rows] == [f"instagram_C{i}" for i in range(7)]
    assert rows[0]["transcript_text"] == "текст"  # непустое побеждает пустое
