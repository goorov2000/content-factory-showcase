# Этап 2 визуального контура (кадры), тикеты 02 и 04: выборщик кадров —
# новый шов vision-звена (стратегия за конфигом vision.frames_sampler),
# подача кадров движку под капом vision.frames_cap со строкой метаданных.
# Тесты шва — чистая логика выборки: движок, Sheets и сеть не зовутся.
import json
from pathlib import Path

import pytest

from cf import vision
from cf.vision import (VisionEngineError, media_plan_for, resolve_sampler,
                       run, uniform_sampler, vision_cfg)

from tests.fakes import FakeSheets
from tests.test_vision import (HEADERS, VALID_FACTS, make_sheets, ok_engine,
                               raw_row)


def frames(n):
    """[(исходный номер, путь)] — вход шва выборщика."""
    return [(i, f"frames/{i:03d}.jpg") for i in range(1, n + 1)]


# --- Тикет 02: шов выборщика кадров ---


def test_uniform_all_frames_when_under_cap():
    assert uniform_sampler(frames(5), 15) == frames(5)
    assert uniform_sampler(frames(15), 15) == frames(15)


def test_uniform_deterministic_first_last_cap_and_numbers():
    for n in (16, 19, 40, 60, 120):
        for cap in (2, 3, 15, 30):
            if n <= cap:
                continue
            picked = uniform_sampler(frames(n), cap)
            assert picked == uniform_sampler(frames(n), cap)   # детерминизм
            assert len(picked) == cap                          # кап не превышен
            assert picked[0] == (1, "frames/001.jpg")          # первый кадр
            assert picked[-1][0] == n                          # последний кадр
            numbers = [num for num, _ in picked]
            assert numbers == sorted(set(numbers))             # исходные номера,
            assert set(numbers) <= set(range(1, n + 1))        # без переиндексации


def test_uniform_exact_pick_is_stable():
    # Пиновка конкретной выборки: молчаливое изменение стратегии — это смена
    # evidence всех прогонов, оно обязано ломать тест.
    assert [num for num, _ in uniform_sampler(frames(10), 5)] == [1, 3, 5, 8, 10]


def test_sampler_resolved_from_config_default_uniform():
    assert resolve_sampler(vision_cfg({})) is uniform_sampler
    assert resolve_sampler(
        vision_cfg({"vision": {"frames_sampler": "uniform"}})) is uniform_sampler


def test_unknown_sampler_is_loud_with_available_list():
    with pytest.raises(VisionEngineError) as exc:
        resolve_sampler(vision_cfg({"vision": {"frames_sampler": "нет-такой"}}))
    assert "uniform" in str(exc.value)


# --- Тикет 04: подача кадров движку под капом + метаданные подачи ---


def media_with_frames(tmp_path, raw_id="tiktok_v1", n_frames=6, cover=True,
                      duration=None, cut_mode=None):
    d = tmp_path / "tiktok" / raw_id
    (d / "frames").mkdir(parents=True)
    if cover:
        (d / "cover.jpg").write_bytes(b"J")
    for i in range(1, n_frames + 1):
        (d / "frames" / f"{i:03d}.jpg").write_bytes(b"F")
    manifest = {"dir": f"tiktok/{raw_id}", "cover": "cover.jpg" if cover else "",
                "frames_dir": "frames", "frame_count": n_frames,
                "raw_deleted": True}
    if duration is not None:
        manifest["duration_sec"] = duration
    if cut_mode is not None:
        manifest["cut_mode"] = cut_mode
    return json.dumps(manifest)


def test_media_plan_cover_plus_capped_frames_with_note(tmp_path):
    manifest = media_with_frames(tmp_path, n_frames=6, duration=6.0,
                                 cut_mode="per-second")
    row = raw_row(manifest=manifest)
    paths, note, reason = media_plan_for(
        row, tmp_path, frames_enabled=True, frames_cap=4,
        sampler=uniform_sampler)
    assert reason == ""
    names = [Path(p).name for p in paths]
    assert names[0] == "cover.jpg"                       # обложка вне капа
    assert len(names) == 5                               # обложка + 4 кадра
    assert names[1] == "001.jpg" and names[-1] == "006.jpg"
    # пути кадров несут исходные номера — frame:N указывает на реальный файл
    assert all(n in {"001.jpg", "002.jpg", "003.jpg", "004.jpg", "005.jpg",
                     "006.jpg"} for n in names[1:])
    assert "4 из 6" in note                              # сколько подано из скольких
    assert "6" in note and "секунда" in note             # длительность и нумерация


def test_media_plan_stretch_note_names_grid_not_seconds(tmp_path):
    manifest = media_with_frames(tmp_path, n_frames=6, duration=300.0,
                                 cut_mode="stretch")
    _, note, _ = media_plan_for(raw_row(manifest=manifest), tmp_path,
                                frames_enabled=True, frames_cap=15,
                                sampler=uniform_sampler)
    assert "6 из 6" in note                              # кадров ≤ капа → все
    assert "300" in note
    assert "не секунда" in note                          # правило нумерации растяжки


def test_media_plan_old_manifest_without_duration_is_honest(tmp_path):
    manifest = media_with_frames(tmp_path, n_frames=3)   # манифест v1 — без полей
    _, note, _ = media_plan_for(raw_row(manifest=manifest), tmp_path,
                                frames_enabled=True, frames_cap=15,
                                sampler=uniform_sampler)
    assert "длительность неизвестна" in note
    assert "секунда" in note        # прежние нарезки посекундные по построению


def test_media_plan_without_frames_matches_v1_no_note(tmp_path):
    d = tmp_path / "tiktok" / "tiktok_v1"
    d.mkdir(parents=True)
    (d / "cover.jpg").write_bytes(b"J")
    manifest = json.dumps({"dir": "tiktok/tiktok_v1", "cover": "cover.jpg",
                           "frames_dir": "", "frame_count": 0})
    paths, note, reason = media_plan_for(raw_row(manifest=manifest), tmp_path,
                                         frames_enabled=False)
    assert [Path(p).name for p in paths] == ["cover.jpg"]
    assert note == ""                                    # v1-режим байт-в-байт
    assert reason == ""


def test_run_frames_engine_gets_subsample_and_note(tmp_path):
    manifest = media_with_frames(tmp_path, n_frames=6, duration=6.0,
                                 cut_mode="per-second")
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []

    def engine(cfg, context, paths, media_note=""):
        calls.append((paths, media_note))
        return json.dumps({**VALID_FACTS, "observed_media": "frames",
                           "frames_analyzed": 4}, ensure_ascii=False)

    summary = run(sheets, config={"vision": {"frames_enabled": True,
                                             "frames_cap": 4}},
                  engine=engine, media_root=tmp_path)
    assert summary["done"] == 1
    paths, note = calls[0]
    assert [Path(p).name for p in paths] == ["cover.jpg", "001.jpg", "003.jpg",
                                             "004.jpg", "006.jpg"]
    assert "4 из 6" in note


def test_run_frames_enabled_without_cap_stops_loudly_before_paid_calls(tmp_path):
    # Дефолта капа в коде НЕТ — его выставит вердикт пилота (тикет 09).
    manifest = media_with_frames(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []
    summary = run(sheets, config={"vision": {"frames_enabled": True}},
                  engine=ok_engine(calls), media_root=tmp_path)
    assert calls == []                                   # ни одного платного вызова
    assert summary["status"] == "failed"
    assert "frames_cap" in summary["engine_error"]
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == ""   # не failed
    assert sheets.tables["run_log"][0]["status"] == "failed"       # правило №6


def test_run_frames_cap_override_enables_frames_and_validates(tmp_path):
    # --frames-cap поверх конфига: пилотный прогон подаёт кадры без правки
    # cf.config.json; значение < 2 — отказ.
    manifest = media_with_frames(tmp_path, n_frames=6)
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []

    def engine(cfg, context, paths, media_note=""):
        calls.append(paths)
        return json.dumps(VALID_FACTS, ensure_ascii=False)

    summary = run(sheets, config={}, engine=engine, media_root=tmp_path,
                  frames_cap=3)
    assert summary["done"] == 1
    assert [Path(p).name for p in calls[0]] == ["cover.jpg", "001.jpg",
                                                "003.jpg", "006.jpg"]
    bad = run(make_sheets([raw_row(manifest=manifest)]), config={},
              engine=engine, media_root=tmp_path, frames_cap=1)
    assert bad["status"] == "failed"
    assert "frames_cap" in bad["engine_error"]


def test_frames_cap_flag_wins_over_config_value(tmp_path):
    # Кап задан И в конфиге, И флагом — флаг побеждает (ось капов пилота идёт
    # тремя прогонами по одному конфигу).
    manifest = media_with_frames(tmp_path, n_frames=6)
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []

    def engine(cfg, context, paths, media_note=""):
        calls.append(paths)
        return json.dumps(VALID_FACTS, ensure_ascii=False)

    run(sheets, config={"vision": {"frames_enabled": True, "frames_cap": 2}},
        engine=engine, media_root=tmp_path, frames_cap=4)
    assert len(calls[0]) == 5                         # обложка + 4 кадра, не 2


def test_run_unknown_sampler_stops_before_paid_calls(tmp_path):
    manifest = media_with_frames(tmp_path)
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []
    summary = run(sheets, config={"vision": {"frames_enabled": True,
                                             "frames_cap": 15,
                                             "frames_sampler": "нет-такой"}},
                  engine=ok_engine(calls), media_root=tmp_path)
    assert calls == []
    assert summary["status"] == "failed"
    assert "frames_sampler" in summary["engine_error"]


def test_run_cover_mode_engine_call_unchanged(tmp_path):
    # v1-режим не регрессирует: без кадров движок получает пустую строку
    # метаданных и только обложку.
    manifest = media_with_frames(tmp_path, n_frames=2)
    sheets = make_sheets([raw_row(manifest=manifest)])
    calls = []

    def engine(cfg, context, paths, media_note=""):
        calls.append((paths, media_note))
        return json.dumps(VALID_FACTS, ensure_ascii=False)

    run(sheets, config={}, engine=engine, media_root=tmp_path)
    paths, note = calls[0]
    assert [Path(p).name for p in paths] == ["cover.jpg"]
    assert note == ""


def test_frames_only_sentinel_row_parsed_when_frames_on(tmp_path):
    # Сентинел этапа 1 «кадры есть, обложки нет» исчерпывает себя конструкцией:
    # visual_status пуст — строка в обычной очереди при включённых кадрах.
    manifest = media_with_frames(tmp_path, raw_id="tiktok_frames_only",
                                 cover=False, n_frames=3)
    sheets = make_sheets([raw_row(raw_id="tiktok_frames_only",
                                  media_status="partial", manifest=manifest)])

    def engine(cfg, context, paths, media_note=""):
        assert all(Path(p).name != "cover.jpg" for p in paths)
        return json.dumps(VALID_FACTS, ensure_ascii=False)

    summary = run(sheets, config={"vision": {"frames_enabled": True,
                                             "frames_cap": 15}},
                  engine=engine, media_root=tmp_path)
    assert summary["done"] == 1
    assert summary["deferred"] == 0
    assert sheets.tables["raw_tiktok"][0]["visual_status"] == "done"


def test_headless_prompt_carries_note_between_header_and_paths(tmp_path, monkeypatch):
    # Строка метаданных подачи стоит в блоке «Медиа» реального движка.
    prompt_path = tmp_path / "vision-cover.md"
    prompt_path.write_text("промпт", encoding="utf-8")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["prompt"] = argv[2]
        class P:
            returncode = 0
            stdout = json.dumps({"result": "{}"})
            stderr = ""
        return P()

    monkeypatch.setattr(vision.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(vision.subprocess, "run", fake_run)
    cfg = vision_cfg({"vision": {"prompt_path": str(prompt_path)}})
    vision.headless_engine(cfg, {"caption": "x"}, ["cover.jpg", "001.jpg"],
                           media_note="Кадры: подано 1 из 1")
    assert "## Медиа" in seen["prompt"]
    assert "Кадры: подано 1 из 1\n- cover.jpg\n- 001.jpg" in seen["prompt"]
    # v1: пустая строка метаданных не меняет формат блока байт-в-байт
    vision.headless_engine(cfg, {"caption": "x"}, ["cover.jpg"])
    assert seen["prompt"].endswith(
        "## Медиа (прочитай файлы инструментом Read)\n- cover.jpg")
