import argparse
import json

from cf.cli import cmd_brief_version, cmd_log_prompt_version

from tests.fakes import FakeSheets

HEADERS = ["prompt_id", "version", "github_path", "active",
           "activated_at", "deactivated_at", "changelog"]


def ns(**kw):
    return argparse.Namespace(**kw)


def test_new_version_appended_and_previous_deactivated():
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md",
         "active": "TRUE", "deactivated_at": ""},
        {"prompt_id": "brief-food", "version": "v3", "github_path": "prompts/briefs/food/y.md",
         "active": "TRUE", "deactivated_at": ""},
    ]}, headers={"prompt_versions": [
        "prompt_id", "version", "github_path", "active",
        "activated_at", "deactivated_at", "changelog"]})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/x.md",
                                           changelog="хук-вопрос обязателен"))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    old = next(r for r in rows if r["version"] == "v1")
    other = next(r for r in rows if r["prompt_id"] == "brief-food")
    new = next(r for r in rows if r["version"] == "v2")
    assert old["active"] == "FALSE" and old["deactivated_at"]
    assert other["active"] == "TRUE"          # чужой промпт не тронут
    assert new["active"] == "TRUE" and new["changelog"] == "хук-вопрос обязателен"


def test_active_stored_as_one_or_yes_is_deactivated():
    # P1.13.5: active хранится как '1'/'yes' (та же семантика, что _prompt_is_active).
    # При логировании новой версии старые активные должны деактивироваться, иначе
    # остаётся две активные версии одного промпта.
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md",
         "active": "1", "deactivated_at": ""},
        {"prompt_id": "brief-pets", "version": "v1b", "github_path": "prompts/briefs/pets/x.md",
         "active": "yes", "deactivated_at": ""},
    ]}, headers={"prompt_versions": [
        "prompt_id", "version", "github_path", "active",
        "activated_at", "deactivated_at", "changelog"]})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/x.md",
                                           changelog="новая версия"))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    old1 = next(r for r in rows if r["version"] == "v1")
    old2 = next(r for r in rows if r["version"] == "v1b")
    new = next(r for r in rows if r["version"] == "v2")
    assert old1["active"] == "FALSE" and old1["deactivated_at"]
    assert old2["active"] == "FALSE" and old2["deactivated_at"]
    assert new["active"] == "TRUE"


# --- P5.13: candidate для честного A/B ---------------------------------------

def test_candidate_does_not_deactivate_active():
    # P5.13: --candidate пишет версию со статусом CANDIDATE и НЕ гасит active.
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md",
         "active": "TRUE", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/x2.md",
                                           changelog="кандидат A/B", candidate=True))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    active = next(r for r in rows if r["version"] == "v1")
    cand = next(r for r in rows if r["version"] == "v2")
    assert active["active"] == "TRUE"            # active НЕ тронут
    assert cand["active"] == "CANDIDATE"


def test_second_candidate_replaces_previous_candidate():
    # Только один кандидат за раз: новый candidate гасит прежний, active не трогает.
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "x",
         "active": "TRUE", "deactivated_at": ""},
        {"prompt_id": "brief-pets", "version": "v2", "github_path": "x2",
         "active": "CANDIDATE", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v3",
                                           path="x3", changelog="новый кандидат",
                                           candidate=True))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    assert next(r for r in rows if r["version"] == "v1")["active"] == "TRUE"
    assert next(r for r in rows if r["version"] == "v2")["active"] == "FALSE"   # старый кандидат погашен
    assert next(r for r in rows if r["version"] == "v3")["active"] == "CANDIDATE"


def test_activate_clears_previous_candidate():
    # Активация новой версии гасит и прежний candidate (A/B устарел — сменилась база).
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "x",
         "active": "TRUE", "deactivated_at": ""},
        {"prompt_id": "brief-pets", "version": "v2", "github_path": "x2",
         "active": "CANDIDATE", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v3",
                                           path="x3", changelog="активируем",
                                           candidate=False))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    assert next(r for r in rows if r["version"] == "v1")["active"] == "FALSE"
    assert next(r for r in rows if r["version"] == "v2")["active"] == "FALSE"   # candidate тоже погашен
    assert next(r for r in rows if r["version"] == "v3")["active"] == "TRUE"


def test_cmd_brief_version_emits_interleave_plan(capsys):
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets-reel", "version": "v2", "github_path": "a.md", "active": "TRUE"},
        {"prompt_id": "brief-pets-reel", "version": "v3", "github_path": "b.md", "active": "CANDIDATE"},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_brief_version(sheets, ns(prompt_id="brief-pets-reel", count=2))
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)                 # stdout — чистый JSON
    assert {p["cohort"] for p in plan} == {"active", "candidate"}
    assert {p["prompt_version"] for p in plan} == {"v2", "v3"}


def test_cmd_brief_version_warns_when_count_below_2(capsys):
    # P5.13: count<2 не даёт пары в одном прогоне — предупреждение (в stderr, stdout чист).
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "p", "version": "a", "github_path": "x", "active": "TRUE"},
        {"prompt_id": "p", "version": "c", "github_path": "y", "active": "CANDIDATE"},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_brief_version(sheets, ns(prompt_id="p", count=1))
    assert rc == 0
    captured = capsys.readouterr()
    json.loads(captured.out)                                   # stdout остаётся валидным JSON
    assert "count" in captured.err.lower() and "2" in captured.err


def test_candidate_same_path_as_active_is_rejected():
    # Гард P5.13: candidate с github_path активной версии -> A/B двух идентичных файлов
    # (вердикт по шуму). Отказ exit 1, строка НЕ добавлена.
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1",
         "github_path": "prompts/briefs/pets/reel.md",
         "active": "TRUE", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/reel.md",
                                           changelog="кандидат", candidate=True))
    assert rc == 1
    rows = sheets.read_rows("prompt_versions")
    assert all(r["version"] != "v2" for r in rows)             # ничего не добавлено
    assert next(r for r in rows if r["version"] == "v1")["active"] == "TRUE"


def test_candidate_different_path_is_accepted():
    sheets = FakeSheets({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1",
         "github_path": "prompts/briefs/pets/reel.md",
         "active": "TRUE", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/reel-v2.md",
                                           changelog="кандидат", candidate=True))
    assert rc == 0
    rows = sheets.read_rows("prompt_versions")
    assert next(r for r in rows if r["version"] == "v2")["active"] == "CANDIDATE"


# ── M36 (аудит 2026-07-24): активация не оставляет промпт без активной версии ─

def test_new_version_appended_before_old_deactivated():
    ops = []

    class Recording(FakeSheets):
        def append_row(self, tab_key, row):
            ops.append("append")
            return super().append_row(tab_key, row)

        def update_rows_where(self, tab_key, match, fields, exclude=None):
            ops.append("deactivate")
            return super().update_rows_where(tab_key, match, fields, exclude=exclude)

    sheets = Recording({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md",
         "active": "TRUE", "activated_at": "2026-07-01T00:00:00+00:00", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    rc = cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                           path="prompts/briefs/pets/x.md",
                                           changelog="правка"))
    assert rc == 0
    assert ops[0] == "append"                     # сначала новая строка
    rows = sheets.read_rows("prompt_versions")
    assert next(r for r in rows if r["version"] == "v1")["active"] == "FALSE"
    assert next(r for r in rows if r["version"] == "v2")["active"] == "TRUE"


def test_crash_after_append_leaves_new_version_active():
    # Сбой деактивации после append -> ДВЕ активные строки (последняя выигрывает
    # в select_versions), а не ноль, как при старом порядке «погасить -> append».
    class Boom(FakeSheets):
        def update_rows_where(self, tab_key, match, fields, exclude=None):
            raise ConnectionError("квота Sheets")

    sheets = Boom({"prompt_versions": [
        {"prompt_id": "brief-pets", "version": "v1", "github_path": "prompts/briefs/pets/x.md",
         "active": "TRUE", "activated_at": "2026-07-01T00:00:00+00:00", "deactivated_at": ""},
    ]}, headers={"prompt_versions": HEADERS})
    import pytest
    with pytest.raises(ConnectionError):           # сбой не замалчивается
        cmd_log_prompt_version(sheets, ns(prompt_id="brief-pets", version="v2",
                                          path="prompts/briefs/pets/x.md",
                                          changelog="правка"))
    from cf.abtest import select_versions
    active, _ = select_versions(sheets.read_rows("prompt_versions"), "brief-pets")
    assert active is not None and active["version"] == "v2"   # промпт живой
