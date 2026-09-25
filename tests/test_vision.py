# Тикет 05 плана 2026-08-14-visual-contour: cf vision — разбор скачанного
# медиа движком за конфигом. Единственный новый шов системы — интерфейс
# движка «контекст строки + пути медиа (+ строка метаданных подачи) -> текст
# ответа»: тесты гоняют fake-движок, реальные claude/API не зовутся
# (Testing Decisions §3). Этап 2 (2026-08-14-frames-stage2) добавил нишевый
# фильтр и ручной ре-разбор (--redo) — их тесты в хвосте файла.
import json
from pathlib import Path

import pytest

from cf import vision
from cf.vision import VisionEngineError, media_paths_for, run, vision_cfg

from tests.fakes import FakeSheets

VALID_FACTS = {
    "observed_media": "cover",
    "visual_evidence": [{"fact": "лицо крупным планом", "media_ref": "cover"}],
    "visual_hook": "лицо + плашка",
    "screen_text": [],
    "shooting_format": "вертикаль",
    "main_emotion": "удивление",
    "formatting_pattern": "плашка сверху",
    "visual_hypotheses": [],
    "unsupported_visual_claims": ["монтаж по обложке не судим"],
    "engine": "fake",
    "model": "fake-model",
    "analyzed_at": "2026-08-14T12:00:00+00:00",
}

HEADERS = ["raw_id", "caption", "media_status", "media_manifest",
           "visual_status", "visual_facts", "views", "niche"]


def media_on_disk(tmp_path, raw_id="tiktok_v1", platform="tiktok"):
    d = tmp_path / platform / raw_id
    d.mkdir(parents=True)
    (d / "cover.jpg").write_bytes(b"JPEG")
    return json.dumps({"dir": f"{platform}/{raw_id}", "cover": "cover.jpg",
                       "frames_dir": "", "frame_count": 0, "raw_deleted": True})


def raw_row(raw_id="tiktok_v1", manifest="", **over):
    row = {"raw_id": raw_id, "caption": "пост", "media_status": "saved",
           "media_manifest": manifest, "visual_status": "", "visual_facts": "",
           "views": 100}
    row.update(over)
    return row


def make_sheets(rows_tiktok=(), rows_instagram=()):
    return FakeSheets(
        tables={"raw_tiktok": list(rows_tiktok),
                "raw_instagram": list(rows_instagram), "run_log": []},
        headers={"raw_tiktok": HEADERS, "raw_instagram": HEADERS})


def ok_engine(calls=None):
    def engine(cfg, context, paths, media_note=""):
        if calls is not None:
            calls.append((context, paths))
        return json.dumps(VALID_FACTS, ensure_ascii=False)
    return engine


def test_valid_answer_written_done_other_fields_untouched(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []
    summary = run(sheets, config={}, engine=ok_engine(calls),
                  media_root=tmp_path)
    assert summary["status"] == "success"
    assert summary["done"] == 1
    row = sheets.tables["raw_tiktok"][0]
    assert row["visual_status"] == "done"
    assert json.loads(row["visual_facts"]) == VALID_FACTS
    assert row["views"] == 100                       # прочие поля не тронуты
    assert row["media_status"] == "saved"
    # движок получил контекст строки и путь обложки
    context, paths = calls[0]
    assert context["caption"] == "пост"
    assert paths == [str(tmp_path / "tiktok" / "tiktok_v1" / "cover.jpg")]
    log = sheets.tables["run_log"][0]
    assert log["agent"] == "vision"
    assert "done=1" in log["input_summary"]


def test_invalid_answer_is_failed_no_garbage_in_sheet(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    summary = run(sheets, config={},
                  engine=lambda cfg, c, p, note="": '{"вайб": "динамично"}',
                  media_root=tmp_path)
    row = sheets.tables["raw_tiktok"][0]
    assert row["visual_status"] == "failed"
    assert row["visual_facts"] == ""                 # мусор в Sheets не попал
    assert summary["failed"] == 1
    assert summary["status"] == "insufficient_data"  # разборов не записано


def test_non_json_answer_is_failed(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    run(sheets, config={}, engine=lambda cfg, c, p, note="": "извините, не могу",
        media_root=tmp_path)
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == "failed"


def test_facts_json_extracted_from_surrounding_text(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    answer = "Вот факты:\n" + json.dumps(VALID_FACTS, ensure_ascii=False)
    run(sheets, config={}, engine=lambda cfg, c, p, note="": answer,
        media_root=tmp_path)
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == "done"


def test_missing_media_file_failed_without_engine_call(tmp_path):
    manifest = json.dumps({"dir": "tiktok/tiktok_gone", "cover": "cover.jpg"})
    sheets = make_sheets([raw_row(raw_id="tiktok_gone", manifest=manifest)])
    calls = []
    run(sheets, config={}, engine=ok_engine(calls), media_root=tmp_path)
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == "failed"
    assert calls == []                               # платного вызова не было


def test_empty_queue_no_paid_call_reason_logged(tmp_path):
    sheets = make_sheets([raw_row(visual_status="done"),
                          raw_row(raw_id="tiktok_v2", media_status="failed"),
                          raw_row(raw_id="tiktok_v3", media_status="")])
    calls = []
    summary = run(sheets, config={}, engine=ok_engine(calls),
                  media_root=tmp_path)
    assert calls == []
    assert summary["status"] == "insufficient_data"
    assert "очередь пуста" in summary["note"]
    assert "очередь пуста" in sheets.tables["run_log"][0]["input_summary"]


def test_partial_media_rows_are_queued(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(media_status="partial", manifest=manifest)])
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path)
    assert summary["done"] == 1


def test_batch_limit_from_arg_and_idempotent_rerun(tmp_path):
    manifests = [media_on_disk(tmp_path, f"tiktok_v{i}") for i in range(3)]
    sheets = make_sheets([raw_row(f"tiktok_v{i}", manifest=manifests[i])
                          for i in range(3)])
    run(sheets, config={}, engine=ok_engine(), media_root=tmp_path, limit=2)
    statuses = [r["visual_status"] for r in sheets.tables["raw_tiktok"]]
    assert statuses.count("done") == 2               # лимит партии
    calls = []
    run(sheets, config={}, engine=ok_engine(calls), media_root=tmp_path, limit=2)
    assert len(calls) == 1                           # разобранные не переразбираются
    assert all(r["visual_status"] == "done"
               for r in sheets.tables["raw_tiktok"])


def test_both_tabs_queued(tmp_path):
    m1 = media_on_disk(tmp_path, "tiktok_v1")
    m2 = media_on_disk(tmp_path, "instagram_C1", platform="instagram")
    sheets = make_sheets([raw_row(manifest=m1)],
                         [raw_row(raw_id="instagram_C1", manifest=m2)])
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path)
    assert summary["done"] == 2
    assert sheets.tables["raw_instagram"][0]["visual_status"] == "done"


def test_engine_infrastructure_error_stops_loudly_queue_intact(tmp_path):
    # Лимит подписки/нет CLI — останов, а не failed строк: failed из очереди
    # не возвращается, и лимитная ночь навсегда выкинула бы строки из разбора.
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])

    def dead_engine(cfg, context, paths, media_note=""):
        raise VisionEngineError("weekly limit reached")

    summary = run(sheets, config={}, engine=dead_engine, media_root=tmp_path)
    assert summary["status"] == "failed"
    assert "weekly limit" in summary["engine_error"]
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == ""   # цела
    log = sheets.tables["run_log"][0]
    assert log["status"] == "failed"
    assert "weekly limit" in log["input_summary"]


def test_missing_visual_columns_stop_loudly_with_runlog(tmp_path):
    # Колонок в живом листе нет (их добавляет оператор, тикет 13) — громкий
    # останов с следом в Run Log, а не молчаливая потеря разбора.
    manifest = media_on_disk(tmp_path)
    sheets = FakeSheets(
        tables={"raw_tiktok": [raw_row(manifest=manifest)],
                "raw_instagram": [], "run_log": []},
        headers={"raw_tiktok": ["raw_id", "caption", "media_status",
                                "media_manifest"],
                 "raw_instagram": []})
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path)
    assert summary["status"] == "failed"
    assert "visual" in summary["engine_error"]
    assert sheets.tables["run_log"][0]["status"] == "failed"


def test_engine_and_model_come_from_config(monkeypatch, tmp_path):
    # Выбор движка/модели — одна строка cf.config.json (решение гриля №4).
    seen = {}

    def engine_a(cfg, context, paths, media_note=""):
        seen["engine"] = "a"
        seen["model"] = cfg["model"]
        return json.dumps(VALID_FACTS)

    def engine_b(cfg, context, paths, media_note=""):
        seen["engine"] = "b"
        seen["model"] = cfg["model"]
        return json.dumps(VALID_FACTS)

    monkeypatch.setitem(vision.ENGINES, "engine-a", engine_a)
    monkeypatch.setitem(vision.ENGINES, "engine-b", engine_b)
    manifest = media_on_disk(tmp_path)
    config = {"vision": {"engine": "engine-b", "model": "модель-теста"}}
    sheets = make_sheets([raw_row(manifest=manifest)])
    run(sheets, config=config, media_root=tmp_path)
    assert seen == {"engine": "b", "model": "модель-теста"}


def test_unknown_engine_is_loud():
    with pytest.raises(VisionEngineError):
        vision.resolve_engine(vision_cfg({"vision": {"engine": "нет-такого"}}))


def test_config_defaults_and_overrides():
    cfg = vision_cfg({})
    assert cfg["engine"] == "headless-subscription"
    assert cfg["frames_enabled"] is False
    cfg = vision_cfg({"vision": {"batch_limit": 3}})
    assert cfg["batch_limit"] == 3
    assert cfg["engine"] == "headless-subscription"


def test_jsonl_pilot_mode_writes_sibling_not_sheets(tmp_path):
    # Режим пилота (решение гриля №5): строки dry-run батча, факты кладутся
    # рядом в JSONL, Sheets не трогается.
    manifest = media_on_disk(tmp_path)
    rows = [raw_row(manifest=manifest),
            raw_row(raw_id="tiktok_nomedia", media_status="failed")]
    src = tmp_path / "dry-run-tiktok-x.jsonl"
    src.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in rows), encoding="utf-8")
    sheets = make_sheets()
    summary = run(sheets, config={}, engine=ok_engine(),
                  media_root=tmp_path, jsonl_path=src)
    assert summary["done"] == 1
    out = tmp_path / "dry-run-tiktok-x-visual.jsonl"
    written = [json.loads(l) for l in out.read_text().splitlines()]
    assert written[0]["visual_status"] == "done"
    assert json.loads(written[0]["visual_facts"]) == VALID_FACTS
    assert written[1]["visual_status"] == ""          # вне очереди — как было
    assert sheets.tables["raw_tiktok"] == []          # Sheets не тронут
    assert summary["jsonl_out"] == str(out)
    assert sheets.tables["run_log"][0]["agent"] == "vision"


def test_jsonl_pilot_mode_reads_captions_with_unicode_line_separators(tmp_path):
    # Dry-run батч пишет write_jsonl_atomic, подписи авторов несут U+2028;
    # splitlines() резал запись и `cf vision --jsonl` падал на json.loads (14.09.2026).
    manifest = media_on_disk(tmp_path)
    rows = [raw_row(manifest=manifest, caption="первая\u2028вторая\u2029третья")]
    src = tmp_path / "dry-run-tiktok-y.jsonl"
    src.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in rows), encoding="utf-8")
    summary = run(make_sheets(), config={}, engine=ok_engine(),
                  media_root=tmp_path, jsonl_path=src)
    assert summary["done"] == 1


def test_frames_enabled_adds_frame_paths(tmp_path):
    # Этап 2 включается конфигом тем же интерфейсом: движку доезжают кадры.
    d = tmp_path / "tiktok" / "tiktok_v1"
    (d / "frames").mkdir(parents=True)
    (d / "cover.jpg").write_bytes(b"J")
    (d / "frames" / "001.jpg").write_bytes(b"F")
    (d / "frames" / "002.jpg").write_bytes(b"F")
    manifest = json.dumps({"dir": "tiktok/tiktok_v1", "cover": "cover.jpg",
                           "frames_dir": "frames", "frame_count": 2})
    row = raw_row(manifest=manifest)
    paths, reason = media_paths_for(row, tmp_path, frames_enabled=True)
    assert [Path(p).name for p in paths] == ["cover.jpg", "001.jpg", "002.jpg"]
    paths_v1, _ = media_paths_for(row, tmp_path, frames_enabled=False)
    assert [Path(p).name for p in paths_v1] == ["cover.jpg"]


def test_long_facts_truncated_to_cell_cap(tmp_path):
    manifest = media_on_disk(tmp_path)
    huge = dict(VALID_FACTS)
    huge["visual_evidence"] = [{"fact": "х" * 500, "media_ref": "cover"}
                               for _ in range(200)]
    sheets = make_sheets([raw_row(manifest=manifest)])
    run(sheets, config={},
        engine=lambda c, ctx, p, note="": json.dumps(huge, ensure_ascii=False),
        media_root=tmp_path)
    facts = sheets.tables["raw_tiktok"][0]["visual_facts"]
    assert len(facts) <= 45000                        # потолок ячейки, как raw_json
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == "done"


# --- Находки code-review 14.08 ---


def test_frames_only_row_deferred_not_buried(tmp_path):
    # Ревью 14.08: partial «кадры есть, обложки нет» при frames_enabled=false
    # нельзя хоронить failed'ом — failed из очереди не возвращается, и этап 2
    # (кадры) до строки уже не добрался бы. Спека: v1 обязан этапу 2 не мешать.
    d = tmp_path / "tiktok" / "tiktok_frames_only"
    (d / "frames").mkdir(parents=True)
    (d / "frames" / "001.jpg").write_bytes(b"F")
    manifest = json.dumps({"dir": "tiktok/tiktok_frames_only", "cover": "",
                           "frames_dir": "frames", "frame_count": 1})
    sheets = make_sheets([raw_row(raw_id="tiktok_frames_only",
                                  media_status="partial", manifest=manifest)])
    calls = []
    summary = run(sheets, config={}, engine=ok_engine(calls),
                  media_root=tmp_path)
    assert calls == []                                   # платного вызова нет
    row = sheets.tables["raw_tiktok"][0]
    assert row["visual_status"] == ""                    # строка ЦЕЛА в очереди
    assert summary["deferred"] == 1
    # этап 2: включение кадров конфигом (с обязательным капом — дефолта в
    # коде нет, тикет 04 этапа 2) разбирает ту же строку
    summary2 = run(sheets, config={"vision": {"frames_enabled": True,
                                              "frames_cap": 15}},
                   engine=ok_engine(), media_root=tmp_path)
    assert summary2["done"] == 1
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == "done"


def test_infra_failure_before_queue_leaves_runlog_trace(tmp_path):
    # Ревью 14.08 (правило №6): неизвестный движок в конфиге — след в Run Log,
    # а не голый exit 1 (прообраз — инфраструктурный сбой cmd_collect).
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    summary = run(sheets, config={"vision": {"engine": "нет-такого"}},
                  media_root=tmp_path)
    assert summary["status"] == "failed"
    log = sheets.tables["run_log"][0]
    assert log["status"] == "failed"
    assert "инфраструктурный сбой" in log["input_summary"]
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == ""  # очередь цела


# --- Этап 2 (тикет 05): нишевый фильтр и ручной ре-разбор ---


def test_niche_filter_limits_queue_and_lifts_default_limit(tmp_path):
    # Решение №8 гриля: нишевый прогон идёт ВСЕЙ очередью ниши — batch_limit
    # по умолчанию снимается, платим ровно за то, что будет прочитано.
    manifests = [media_on_disk(tmp_path, f"tiktok_v{i}") for i in range(4)]
    rows = [raw_row(f"tiktok_v{i}", manifest=manifests[i], niche="мужские-образы")
            for i in range(3)] + [raw_row("tiktok_v3", manifest=manifests[3],
                                          niche="уход")]
    sheets = make_sheets(rows)
    summary = run(sheets, config={"vision": {"batch_limit": 1}},
                  engine=ok_engine(), media_root=tmp_path,
                  niche="мужские-образы")
    assert summary["done"] == 3                       # вся очередь ниши, не 1
    statuses = {r["raw_id"]: r["visual_status"]
                for r in sheets.tables["raw_tiktok"]}
    assert statuses["tiktok_v3"] == ""                # чужая ниша не тронута
    assert "niche=мужские-образы" in sheets.tables["run_log"][0]["input_summary"]


def test_niche_with_tab_narrows_to_tab(tmp_path):
    m1 = media_on_disk(tmp_path, "tiktok_v1")
    m2 = media_on_disk(tmp_path, "instagram_C1", platform="instagram")
    sheets = make_sheets([raw_row(manifest=m1, niche="уход")],
                         [raw_row(raw_id="instagram_C1", manifest=m2,
                                  niche="уход")])
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path,
                  niche="уход", tabs=("raw_instagram",))
    assert summary["done"] == 1
    assert sheets.tables["raw_instagram"][0]["visual_status"] == "done"
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == ""


def test_niche_explicit_limit_respected(tmp_path):
    manifests = [media_on_disk(tmp_path, f"tiktok_v{i}") for i in range(3)]
    sheets = make_sheets([raw_row(f"tiktok_v{i}", manifest=manifests[i],
                                  niche="мужские-образы") for i in range(3)])
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path,
                  niche="мужские-образы", limit=2)
    assert summary["done"] == 2


def test_redo_without_addressing_refused(tmp_path):
    # «Ре-разбор всего» невозможен: --redo требует --raw-id либо --niche.
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest, visual_status="done")])
    calls = []
    summary = run(sheets, config={}, engine=ok_engine(calls),
                  media_root=tmp_path, redo=True)
    assert calls == []
    assert summary["status"] == "failed"
    assert "адресац" in summary["engine_error"]
    assert sheets.tables["run_log"][0]["status"] == "failed"


def test_redo_raw_ids_reparses_done_and_failed(tmp_path):
    manifests = [media_on_disk(tmp_path, f"tiktok_v{i}") for i in range(3)]
    sheets = make_sheets([
        raw_row("tiktok_v0", manifest=manifests[0], visual_status="done",
                visual_facts='{"старые": "факты"}'),
        raw_row("tiktok_v1", manifest=manifests[1], visual_status="failed"),
        raw_row("tiktok_v2", manifest=manifests[2])])
    calls = []
    summary = run(sheets, config={}, engine=ok_engine(calls),
                  media_root=tmp_path, redo=True,
                  raw_ids=("tiktok_v0", "tiktok_v1"))
    assert summary["done"] == 2
    assert len(calls) == 2
    rows = {r["raw_id"]: r for r in sheets.tables["raw_tiktok"]}
    assert json.loads(rows["tiktok_v0"]["visual_facts"]) == VALID_FACTS  # апсерт
    assert rows["tiktok_v1"]["visual_status"] == "done"
    assert rows["tiktok_v2"]["visual_status"] == ""   # вне адресации не тронут
    assert "redo=on" in sheets.tables["run_log"][0]["input_summary"]


def test_redo_niche_reparses_marked_rows_of_niche(tmp_path):
    manifests = [media_on_disk(tmp_path, f"tiktok_v{i}") for i in range(4)]
    sheets = make_sheets([
        raw_row("tiktok_v0", manifest=manifests[0], niche="уход",
                visual_status="done"),
        raw_row("tiktok_v1", manifest=manifests[1], niche="уход",
                visual_status="failed"),
        raw_row("tiktok_v2", manifest=manifests[2], niche="другая",
                visual_status="done"),
        raw_row("tiktok_v3", manifest=manifests[3], niche="уход")])
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path,
                  redo=True, niche="уход")
    assert summary["done"] == 2                       # done + failed ниши
    rows = {r["raw_id"]: r for r in sheets.tables["raw_tiktok"]}
    assert rows["tiktok_v2"]["visual_status"] == "done"   # чужая ниша цела
    assert rows["tiktok_v3"]["visual_status"] == ""   # неразмеченная — не redo


# ── ревью 14.09.2026: оплаченные разборы не теряются молча ──────────────────

NONE_FACTS = {**VALID_FACTS, "observed_media": "none", "visual_evidence": []}


def none_engine(calls=None):
    def engine(cfg, context, paths, media_note=""):
        if calls is not None:
            calls.append(paths)
        return json.dumps(NONE_FACTS, ensure_ascii=False)
    return engine


def test_engine_saying_it_read_nothing_is_not_written_as_done(tmp_path):
    # Движку подали файлы, а он ответил observed_media=none: это отказ движка
    # (недоверенный воркспейс, запрет Read), а не свойство строки. Раньше строка
    # получала done с пустыми фактами и уходила из очереди навсегда.
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    summary = run(sheets, config={}, engine=none_engine(), media_root=tmp_path)
    row = sheets.tables["raw_tiktok"][0]
    assert row["visual_status"] == "" and row["visual_facts"] == ""   # осталась в очереди
    assert summary["done"] == 0


def test_consecutive_read_nothing_answers_stop_the_run(tmp_path):
    manifests = [media_on_disk(tmp_path, f"tiktok_v{i}") for i in range(6)]
    sheets = make_sheets([raw_row(f"tiktok_v{i}", manifest=m)
                          for i, m in enumerate(manifests)])
    calls = []
    summary = run(sheets, config={}, engine=none_engine(calls), media_root=tmp_path)
    assert len(calls) == vision.READ_NOTHING_STOP     # дальше не платим
    assert summary["status"] == "failed"
    assert sheets.tables["run_log"][0]["status"] == "failed"


def test_missing_media_root_is_infrastructure_failure_not_failed_rows(tmp_path):
    # Опечатка в --media-root, запуск не из корня или перенесённый каталог медиа
    # помечали всю очередь failed, а в --redo стирали готовые факты.
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest, visual_status="done",
                                  visual_facts='{"старые": "факты"}')])
    calls = []
    summary = run(sheets, config={}, engine=ok_engine(calls),
                  media_root=tmp_path / "нет-такого", redo=True, raw_ids=("tiktok_v1",))
    row = sheets.tables["raw_tiktok"][0]
    assert calls == []
    assert summary["status"] == "failed"
    assert row["visual_status"] == "done" and row["visual_facts"] == '{"старые": "факты"}'


def test_failed_redo_keeps_previous_paid_facts(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest, visual_status="done",
                                  visual_facts='{"старые": "факты"}')])
    summary = run(sheets, config={},
                  engine=lambda cfg, c, p, note="": "извините, не могу",
                  media_root=tmp_path, redo=True, raw_ids=("tiktok_v1",))
    row = sheets.tables["raw_tiktok"][0]
    assert row["visual_status"] == "done"
    assert row["visual_facts"] == '{"старые": "факты"}'
    assert summary["failed"] == 1


def test_row_that_vanished_from_sheet_is_not_counted_done(tmp_path):
    manifest = media_on_disk(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    sheets.update_row_fields = lambda *a, **k: False  # строку удалил archive
    summary = run(sheets, config={}, engine=ok_engine(), media_root=tmp_path)
    assert summary["done"] == 0 and summary["failed"] == 1


def test_api_engine_truncated_or_refused_answer_is_engine_error(tmp_path, monkeypatch):
    # Ревью 14.09.2026: max_tokens=2048 обрезал кадровый ответ (факты пилота 3,5–8,7 тыс.
    # знаков), обрезанный JSON молча превращался в failed строки. Обрезку и отказ
    # модели ловим явно по stop_reason — это инфраструктура, а не строка.
    import httpx
    key = tmp_path / "key.txt"
    key.write_text("sk-test", encoding="utf-8")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"JPEG")
    sent = {}

    class Resp:
        def __init__(self, stop):
            self._stop = stop

        def raise_for_status(self):
            return None

        def json(self):
            return {"stop_reason": self._stop, "content": [{"type": "text", "text": "{\"obs"}]}

    for stop in ("max_tokens", "refusal"):
        def fake_post(url, headers=None, json=None, timeout=None, _stop=stop):
            sent.update(json)
            return Resp(_stop)
        monkeypatch.setattr(httpx, "post", fake_post)
        cfg = vision_cfg({"vision": {"engine": "anthropic-api", "api_key_file": str(key)}})
        with pytest.raises(VisionEngineError, match=stop):
            vision.api_engine(cfg, {"caption": "пост"}, [str(cover)])
    assert sent["max_tokens"] >= 16000
