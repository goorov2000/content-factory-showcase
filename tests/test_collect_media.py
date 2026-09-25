# Тикет 03 плана 2026-08-14-visual-contour: медиа-контур сборщика.
# Скоропортящиеся ссылки перестают быть точкой невозврата: обложка и кадры
# скачиваются в момент сбора, сырец удаляется сразу после нарезки, строка
# несёт честный статус. Тесты — через верхний шов collect() и через
# attach_media (инжектируемые скачивальщик/нарезчик/замерщик, реальные сеть,
# ffmpeg и ffprobe не зовутся; tmp-каталог вместо agent-runtime/).
import json
from pathlib import Path

from cf.collect.apify import RunResult
from cf.collect.media import FRAME_CAP, attach_media, cut_plan
from cf.collect import snowball, tiktok

from tests.fakes import FakeSheets

NOW = "2026-07-23T12:00:00.000Z"
FRESH = "2026-07-22T12:00:00.000Z"


def make_downloader(fail=(), expired=()):
    """(ok, bytes, status) по URL; счёт вызовов в .calls."""
    def dl(url, client=None):
        dl.calls.append(url)
        if url in expired:
            return False, b"", 403
        if url in fail:
            return False, b"", 0
        return True, b"JPEGDATA", 200
    dl.calls = []
    return dl


def make_cutter(n=3, error=None):
    """Пишет n фейковых кадров; error — исключение вместо нарезки.
    Фиксирует сырец в момент вызова (.saw_video) и планы нарезки (.plans)."""
    def cut(video_path, frames_dir, plan):
        cut.saw_video = Path(video_path).exists()
        cut.plans.append(plan)
        if error is not None:
            raise error
        frames_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            (frames_dir / f"{i:03d}.jpg").write_bytes(b"F")
        return n
    cut.saw_video = None
    cut.plans = []
    return cut


def make_prober(duration=45.0, error=None):
    """Фейковый замерщик длительности; error — исключение вместо замера."""
    def probe(video_path):
        probe.calls.append(str(video_path))
        if error is not None:
            raise error
        return duration
    probe.calls = []
    return probe


def media_row(vid="v1", **over):
    row = {"raw_id": f"tiktok_{vid}",
           "thumbnail_url": f"https://cdn/{vid}-cover.jpg",
           "video_url": f"https://cdn/{vid}.mp4"}
    row.update(over)
    return row


# --- attach_media: статусы, файлы, сырец, идемпотентность ---


def test_saved_files_manifest_and_raw_deleted(tmp_path):
    cutter = make_cutter(n=2)
    prober = make_prober(duration=45.0)
    rows, stats = attach_media([media_row()], "tiktok", root=tmp_path,
                               downloader=make_downloader(), cutter=cutter,
                               prober=prober)
    d = tmp_path / "tiktok" / "tiktok_v1"
    assert (d / "cover.jpg").read_bytes() == b"JPEGDATA"
    assert sorted(p.name for p in (d / "frames").glob("*.jpg")) == ["001.jpg",
                                                                   "002.jpg"]
    assert cutter.saw_video is True            # сырец был на диске при нарезке
    assert not list(d.glob("*.tmp"))           # и удалён сразу после неё
    assert rows[0]["media_status"] == "saved"
    manifest = json.loads(rows[0]["media_manifest"])
    assert manifest == {"dir": "tiktok/tiktok_v1", "cover": "cover.jpg",
                        "frames_dir": "frames", "frame_count": 2,
                        "duration_sec": 45.0, "cut_mode": "per-second",
                        "raw_deleted": True}
    assert json.loads((d / "manifest.json").read_text())["status"] == "saved"
    assert stats["saved"] == 1 and stats["frames"] == 2
    # нарезчик получил план от замерщика: посекундный режим, потолок 120
    assert cutter.plans == [cut_plan(45.0)]


def test_video_failure_is_partial_with_expired_counter(tmp_path):
    rows, stats = attach_media(
        [media_row()], "tiktok", root=tmp_path,
        downloader=make_downloader(expired=["https://cdn/v1.mp4"]),
        cutter=make_cutter())
    assert rows[0]["media_status"] == "partial"
    manifest = json.loads(rows[0]["media_manifest"])
    assert manifest["cover"] == "cover.jpg"
    assert manifest["frame_count"] == 0
    assert stats == {"saved": 0, "partial": 1, "failed": 0, "reused": 0,
                     "covers": 1, "videos": 0, "frames": 0, "expired": 1}


def test_nothing_downloaded_is_failed_without_manifest(tmp_path):
    urls = ["https://cdn/v1-cover.jpg", "https://cdn/v1.mp4"]
    rows, stats = attach_media([media_row()], "tiktok", root=tmp_path,
                               downloader=make_downloader(fail=urls),
                               cutter=make_cutter())
    assert rows[0]["media_status"] == "failed"
    assert rows[0]["media_manifest"] == ""
    # манифеста нет — следующий сбор честно ретраит свежие ссылки
    assert not (tmp_path / "tiktok" / "tiktok_v1" / "manifest.json").exists()
    assert stats["failed"] == 1


def test_cut_error_still_deletes_raw_video(tmp_path):
    # Решение гриля №3: сырец не живёт дольше нарезки — и при её ошибке.
    cutter = make_cutter(error=RuntimeError("ffmpeg exploded"))
    rows, stats = attach_media([media_row()], "tiktok", root=tmp_path,
                               downloader=make_downloader(), cutter=cutter,
                               prober=make_prober())
    d = tmp_path / "tiktok" / "tiktok_v1"
    assert not list(d.glob("*.tmp"))
    assert rows[0]["media_status"] == "partial"   # обложка есть, кадров нет
    assert json.loads(rows[0]["media_manifest"])["frame_count"] == 0
    assert stats["partial"] == 1


def test_recollect_with_manifest_does_not_redownload(tmp_path):
    dl = make_downloader()
    attach_media([media_row()], "tiktok", root=tmp_path, downloader=dl,
                 cutter=make_cutter(), prober=make_prober())
    first_calls = len(dl.calls)
    rows, stats = attach_media([media_row()], "tiktok", root=tmp_path,
                               downloader=dl, cutter=make_cutter(),
                               prober=make_prober())
    assert len(dl.calls) == first_calls        # ни одного нового скачивания
    assert stats["reused"] == 1
    # строка самодостаточна и без вкладки (dry-run/JSONL живёт этим)
    assert rows[0]["media_status"] == "saved"
    assert json.loads(rows[0]["media_manifest"])["frame_count"] == 3


def test_row_without_urls_failed_row_without_raw_id_untouched(tmp_path):
    rows, stats = attach_media(
        [media_row(thumbnail_url="", video_url=""), {"caption": "без raw_id"}],
        "tiktok", root=tmp_path, downloader=make_downloader(),
        cutter=make_cutter())
    assert rows[0]["media_status"] == "failed"
    assert rows[1] == {"caption": "без raw_id"}
    assert stats["failed"] == 1


def test_downloader_exception_isolated_to_row(tmp_path):
    def dl(url, client=None):
        if "v1" in url:
            raise RuntimeError("disk full")
        return True, b"JPEGDATA", 200

    rows, stats = attach_media([media_row("v1"), media_row("v2")], "tiktok",
                               root=tmp_path, downloader=dl,
                               cutter=make_cutter(), prober=make_prober())
    assert rows[0]["media_status"] == "failed"
    assert rows[1]["media_status"] == "saved"
    assert stats["failed"] == 1 and stats["saved"] == 1


# --- Тикет 01 этапа 2 (кадры): план нарезки 120 + растяжка, замерщик ---


def test_cut_plan_is_pure_and_deterministic():
    # ≤120 с — честная 1 кадр/сек; длиннее — ровно 120 кадров растяжкой.
    for duration, mode, expected in [(1, "per-second", 1),
                                     (45.0, "per-second", 45),
                                     (90.5, "per-second", 91),
                                     (120.0, "per-second", 120),   # граница
                                     (120.5, "stretch", FRAME_CAP),
                                     (300.0, "stretch", FRAME_CAP),
                                     (3600.0, "stretch", FRAME_CAP)]:
        plan = cut_plan(duration)
        assert plan["mode"] == mode, duration
        assert plan["expected_frames"] == expected, duration
        assert plan["cap"] == FRAME_CAP
        assert plan == cut_plan(duration)          # детерминизм
    # растяжка покрывает всю длительность: fps = 120/длительность
    assert cut_plan(300.0)["fps"] == FRAME_CAP / 300.0
    assert cut_plan(45.0)["fps"] == 1.0


def test_cut_plan_degrades_without_duration():
    # Отказ замера — не отказ медиа: прежнее посекундное правило, потолок 120.
    for bad in (None, 0, -3, "мусор"):
        plan = cut_plan(bad)
        assert plan["mode"] == "per-second"
        assert plan["cap"] == FRAME_CAP
        assert plan["duration_sec"] is None
        assert plan["expected_frames"] is None


def test_long_video_gets_stretch_plan_and_manifest_fields(tmp_path):
    cutter = make_cutter(n=120)
    rows, _ = attach_media([media_row()], "tiktok", root=tmp_path,
                           downloader=make_downloader(), cutter=cutter,
                           prober=make_prober(duration=300.0))
    assert cutter.plans == [cut_plan(300.0)]
    manifest = json.loads(rows[0]["media_manifest"])
    assert manifest["duration_sec"] == 300.0
    assert manifest["cut_mode"] == "stretch"
    assert manifest["frame_count"] == 120


def test_prober_failure_degrades_to_per_second_without_falling(tmp_path):
    # Правило №2 через статусы: замер упал — нарезка идёт прежним посекундным
    # правилом, манифест честно без длительности, сбор не падает.
    for i, prober in enumerate([make_prober(error=RuntimeError("ffprobe упал")),
                                make_prober(duration=None)]):
        cutter = make_cutter(n=2)
        rows, stats = attach_media([media_row()], "tiktok",
                                   root=tmp_path / f"case{i}",
                                   downloader=make_downloader(), cutter=cutter,
                                   prober=prober)
        assert stats["saved"] == 1
        assert rows[0]["media_status"] == "saved"
        assert cutter.plans[0]["mode"] == "per-second"
        assert cutter.plans[0]["cap"] == FRAME_CAP
        manifest = json.loads(rows[0]["media_manifest"])
        assert "duration_sec" not in manifest
        assert manifest["cut_mode"] == "per-second"


# --- e2e через шов collect(): TikTok и snowball ---


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
        "covers": {"default": f"https://cdn/{vid}-cover.jpg"},
        "videoUrl": f"https://cdn/{vid}.mp4",
    }
    item.update(over)
    return item


class StubClient:
    token = "tok"

    def __init__(self, responses):
        self.responses = list(responses)

    def run_actor(self, actor_path, payload):
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def registry(tmp_path):
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    (d / "tiktok.json").write_text(json.dumps(
        [{"query": "тег0", "kind": "hashtag", "niche": "мужские-образы",
          "status": "active", "origin": "operator", "added_at": "2026-07-01"}],
        ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_e2e_tiktok_rows_carry_media_and_runlog_counts(tmp_path):
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    media_root = tmp_path / "media"
    summary = tiktok.collect(sheets, client, now_iso=NOW,
                             registry_root=registry(tmp_path),
                             media_root=media_root,
                             media_downloader=make_downloader(),
                             frame_cutter=make_cutter(),
                             duration_prober=make_prober())
    assert summary["status"] == "success"
    row = sheets.tables["raw_tiktok"][0]
    assert row["media_status"] == "saved"
    assert json.loads(row["media_manifest"])["frame_count"] == 3
    assert (media_root / "tiktok" / "tiktok_v1" / "cover.jpg").exists()
    assert summary["media"]["saved"] == 1
    # счётчики — в сводке Run Log (правило №2 действует через них)
    assert "media saved=1" in sheets.tables["run_log"][0]["input_summary"]


def test_e2e_media_failure_does_not_degrade_run(tmp_path):
    # Медиа — обогащение, не условие сбора: полный отказ скачивания оставляет
    # статус success, счётчики честно в сводке.
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    dl = make_downloader(fail=["https://cdn/v1-cover.jpg", "https://cdn/v1.mp4"])
    summary = tiktok.collect(sheets, client, now_iso=NOW,
                             registry_root=registry(tmp_path),
                             media_root=tmp_path / "media",
                             media_downloader=dl, frame_cutter=make_cutter())
    assert summary["status"] == "success"
    assert sheets.tables["raw_tiktok"][0]["media_status"] == "failed"
    log = sheets.tables["run_log"][0]
    assert log["status"] == "success"
    assert "media saved=0 partial=0 failed=1" in log["input_summary"]


def test_e2e_dry_run_downloads_media_but_writes_nothing(tmp_path):
    # Механизм пилота (решение гриля №5): dry-run качает и режет боевым
    # режимом, строки с media-полями идут только в JSONL (их вернёт summary).
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    media_root = tmp_path / "media"
    summary = tiktok.collect(sheets, client, now_iso=NOW,
                             registry_root=registry(tmp_path), dry_run=True,
                             media_root=media_root,
                             media_downloader=make_downloader(),
                             frame_cutter=make_cutter(),
                             duration_prober=make_prober())
    assert sheets.tables["raw_tiktok"] == []
    assert (media_root / "tiktok" / "tiktok_v1" / "cover.jpg").exists()
    assert summary["rows"][0]["media_status"] == "saved"


def test_e2e_recollect_keeps_visual_fields_and_media(tmp_path):
    # Пересбор не затирает добытое: media-поля переезжают из манифеста на
    # диске (повторного скачивания нет), а visual_status/visual_facts (их
    # пишет только cf vision, сбор их не несёт) бережёт coalesce на upsert.
    media_root = tmp_path / "media"
    dl = make_downloader()
    first = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    tiktok.collect(first, StubClient([RunResult("SUCCEEDED",
                                                items=[raw_item("v1")])]),
                   now_iso=NOW, registry_root=registry(tmp_path),
                   media_root=media_root, media_downloader=dl,
                   frame_cutter=make_cutter(), duration_prober=make_prober())
    stored = dict(first.tables["raw_tiktok"][0])
    stored["visual_status"] = "done"
    stored["visual_facts"] = '{"observed_media":"cover"}'
    sheets = FakeSheets(tables={"raw_tiktok": [stored], "run_log": []})
    calls_before = len(dl.calls)
    summary = tiktok.collect(sheets, StubClient([RunResult("SUCCEEDED",
                                                 items=[raw_item("v1")])]),
                             now_iso=NOW, registry_root=registry(tmp_path),
                             media_root=media_root, media_downloader=dl,
                             frame_cutter=make_cutter(),
                             duration_prober=make_prober())
    assert len(dl.calls) == calls_before       # идемпотентность пересбора
    assert summary["media"]["reused"] == 1
    row = sheets.tables["raw_tiktok"][0]
    assert row["media_status"] == "saved"
    assert row["visual_status"] == "done"
    assert row["visual_facts"] == '{"observed_media":"cover"}'


def test_e2e_snowball_same_media_tract(tmp_path):
    seeds = [{"seed_url": "https://www.tiktok.com/@a/video/s1", "active": "TRUE",
              "niche": "мужские-образы"}]
    sheets = FakeSheets(tables={"seeds": seeds, "raw_tiktok": [], "run_log": []})
    client = StubClient([RunResult("SUCCEEDED", items=[raw_item("v1")])])
    media_root = tmp_path / "media"
    summary = snowball.collect(sheets, client, now_iso=NOW,
                               media_root=media_root,
                               media_downloader=make_downloader(),
                               frame_cutter=make_cutter(),
                               duration_prober=make_prober())
    assert summary["status"] == "success"
    assert summary["media"]["saved"] == 1
    row = sheets.tables["raw_tiktok"][0]
    assert row["media_status"] == "saved"
    assert (media_root / "tiktok" / "tiktok_v1" / "frames" / "001.jpg").exists()
    assert "media saved=1" in sheets.tables["run_log"][0]["input_summary"]


def test_media_columns_registered_for_live_sheet_health():
    # Реестр ожидаемых колонок: без колонок в живом листе боевой upsert молча
    # выбросит media-поля — дрейф обязан быть виден в cf status до включения
    # сборщиков (колонки добавляет оператор, тикет 13).
    from cf.sheets import EXPECTED_COLUMNS
    assert "media_status" in EXPECTED_COLUMNS["raw_tiktok"]
    assert "media_manifest" in EXPECTED_COLUMNS["raw_tiktok"]


# --- Тикет 04: тот же медиа-контур на четырёхстадийном Instagram-тракте ---


def ig_reel_item(code, transcript="текст"):
    return {"url": f"https://www.instagram.com/reel/{code}/", "shortCode": code,
            "ownerUsername": "acc", "caption": f"пост {code}",
            "timestamp": FRESH, "videoViewCount": 1000, "likesCount": 100,
            "transcript": transcript,
            "displayUrl": f"https://cdn/{code}-cover.jpg",
            "videoUrl": f"https://cdn/{code}.mp4"}


def ig_hashtag_reel(code, tag="мужтег0"):
    return {"url": f"https://www.instagram.com/reel/{code}/", "hashtag": tag,
            "ownerUsername": "acc", "caption": f"пост {code}",
            "timestamp": FRESH, "videoViewCount": 1000}


class IgStubClient:
    token = "tok"

    def __init__(self, hashtag=None, reel=None, transcripts=None):
        self.responses = {"hashtag": list(hashtag or []), "reel": list(reel or []),
                          "transcripts": list(transcripts or [])}
        self.calls = []

    def run_actor(self, actor_path, payload):
        kind = ("transcripts" if "transcript" in actor_path
                else "hashtag" if "hashtag" in actor_path else "reel")
        self.calls.append(kind)
        out = self.responses[kind].pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def ig_registry(tmp_path):
    # только hashtag-источники: discovery-стадии нет — стабу меньше ответов
    d = tmp_path / "sources"
    d.mkdir(exist_ok=True)
    (d / "instagram.json").write_text(json.dumps(
        [{"query": "мужтег0", "kind": "hashtag", "niche": "мужские-образы",
          "status": "active", "origin": "operator", "added_at": "2026-07-01"}],
        ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_e2e_instagram_rows_carry_media_and_runlog_counts(tmp_path):
    from cf.collect import instagram
    sheets = FakeSheets(tables={"raw_instagram": [], "run_log": []})
    client = IgStubClient(
        hashtag=[RunResult("SUCCEEDED", items=[ig_hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[ig_reel_item("C1")])])
    media_root = tmp_path / "media"
    summary = instagram.collect(sheets, client, now_iso=NOW,
                                registry_root=ig_registry(tmp_path),
                                media_root=media_root,
                                media_downloader=make_downloader(),
                                frame_cutter=make_cutter(),
                                duration_prober=make_prober())
    assert summary["status"] == "success"
    row = sheets.tables["raw_instagram"][0]
    assert row["media_status"] == "saved"
    assert json.loads(row["media_manifest"])["dir"] == "instagram/instagram_C1"
    assert (media_root / "instagram" / "instagram_C1" / "cover.jpg").exists()
    assert summary["media"]["saved"] == 1
    assert "media saved=1" in sheets.tables["run_log"][0]["input_summary"]


def test_e2e_instagram_media_failure_does_not_degrade(tmp_path):
    from cf.collect import instagram
    sheets = FakeSheets(tables={"raw_instagram": [], "run_log": []})
    client = IgStubClient(
        hashtag=[RunResult("SUCCEEDED", items=[ig_hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[ig_reel_item("C1")])])
    dl = make_downloader(fail=["https://cdn/C1-cover.jpg", "https://cdn/C1.mp4"])
    summary = instagram.collect(sheets, client, now_iso=NOW,
                                registry_root=ig_registry(tmp_path),
                                media_root=tmp_path / "media",
                                media_downloader=dl, frame_cutter=make_cutter())
    assert summary["status"] == "success"
    assert sheets.tables["raw_instagram"][0]["media_status"] == "failed"
    assert sheets.tables["run_log"][0]["status"] == "success"


def test_e2e_instagram_dry_run_media_only_in_rows(tmp_path):
    from cf.collect import instagram
    sheets = FakeSheets(tables={"raw_instagram": [], "run_log": []})
    client = IgStubClient(
        hashtag=[RunResult("SUCCEEDED", items=[ig_hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[ig_reel_item("C1")])])
    media_root = tmp_path / "media"
    summary = instagram.collect(sheets, client, now_iso=NOW,
                                registry_root=ig_registry(tmp_path),
                                dry_run=True, media_root=media_root,
                                media_downloader=make_downloader(),
                                frame_cutter=make_cutter(),
                                duration_prober=make_prober())
    assert sheets.tables["raw_instagram"] == []
    assert (media_root / "instagram" / "instagram_C1" / "cover.jpg").exists()
    assert summary["rows"][0]["media_status"] == "saved"


def test_e2e_instagram_recollect_keeps_media_and_visual_fields(tmp_path):
    # Правило coalesce тикета 01 распространено на media/visual-поля:
    # пересбор IG-строки не затирает добытое (медиа реюзается из манифеста,
    # visual_* бережёт coalesce на upsert).
    from cf.collect import instagram
    media_root = tmp_path / "media"
    dl = make_downloader()
    first = FakeSheets(tables={"raw_instagram": [], "run_log": []})
    instagram.collect(first, IgStubClient(
        hashtag=[RunResult("SUCCEEDED", items=[ig_hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[ig_reel_item("C1")])]),
        now_iso=NOW, registry_root=ig_registry(tmp_path),
        media_root=media_root, media_downloader=dl, frame_cutter=make_cutter(),
        duration_prober=make_prober())
    stored = dict(first.tables["raw_instagram"][0])
    stored["visual_status"] = "done"
    stored["visual_facts"] = '{"observed_media":"cover"}'
    sheets = FakeSheets(tables={"raw_instagram": [stored], "run_log": []})
    calls_before = len(dl.calls)
    summary = instagram.collect(sheets, IgStubClient(
        hashtag=[RunResult("SUCCEEDED", items=[ig_hashtag_reel("C1")])],
        reel=[RunResult("SUCCEEDED", items=[ig_reel_item("C1")])]),
        now_iso=NOW, registry_root=ig_registry(tmp_path),
        media_root=media_root, media_downloader=dl, frame_cutter=make_cutter(),
        duration_prober=make_prober())
    assert len(dl.calls) == calls_before
    assert summary["media"]["reused"] == 1
    row = sheets.tables["raw_instagram"][0]
    assert row["media_status"] == "saved"
    assert row["visual_status"] == "done"
    assert row["visual_facts"] == '{"observed_media":"cover"}'


def test_ig_media_columns_registered_for_live_sheet_health():
    from cf.sheets import EXPECTED_COLUMNS
    assert "media_status" in EXPECTED_COLUMNS["raw_instagram"]
    assert "media_manifest" in EXPECTED_COLUMNS["raw_instagram"]


# --- ревью 14.09.2026: обрыв записи не хоронит строку и не оставляет сырец ---


def test_unreadable_manifest_is_treated_as_absent_and_media_refetched(tmp_path):
    # Усечённый manifest.json (обрыв процесса на записи) раньше давал failed + reused,
    # и строка больше не перекачивалась никогда.
    d = tmp_path / "tiktok" / "tiktok_v1"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text('{"status": "sa', encoding="utf-8")
    dl = make_downloader()
    rows, stats = attach_media([media_row()], "tiktok", root=tmp_path, downloader=dl,
                               cutter=make_cutter(n=2), prober=make_prober())
    assert rows[0]["media_status"] == "saved"
    assert stats["reused"] == 0 and len(dl.calls) == 2
    assert json.loads((d / "manifest.json").read_text())["status"] == "saved"


def test_raw_video_removed_when_writing_it_fails(tmp_path, monkeypatch):
    real_write = Path.write_bytes

    def write_bytes(self, data):
        if self.name.endswith(".tmp"):
            real_write(self, data[:3])
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    attach_media([media_row()], "tiktok", root=tmp_path, downloader=make_downloader(),
                 cutter=make_cutter(n=2), prober=make_prober())
    assert not list((tmp_path / "tiktok" / "tiktok_v1").glob("*.tmp"))
