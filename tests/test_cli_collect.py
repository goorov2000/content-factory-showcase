# C3.1 + C2.6 — CLI cf collect: диспетчеризация стадий, exit-коды,
# dry-run пишет JSONL вместо Sheets.
import argparse
import json

import pytest

import cf.collect.apify as apify_mod
from cf.cli import build_parser, cmd_collect
from cf.collect.apify import RunResult

from tests.fakes import FakeSheets

NOW_ITEM = {
    "id": "v1", "text": "видео #стиль", "createTimeISO": "2099-01-01T00:00:00.000Z",
    "playCount": 1000, "webVideoUrl": "https://tt/v1", "transcript": "текст",
}


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run_actor(self, actor_path, payload):
        self.calls.append(actor_path)
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def patched_client(monkeypatch):
    holder = {}

    def fake_from_config(config, **kw):
        return holder["client"]

    monkeypatch.setattr(apify_mod, "client_from_config", fake_from_config)
    return holder


def ns(stage, dry_run=False):
    return argparse.Namespace(stage=stage, dry_run=dry_run)


def test_parser_has_collect():
    args = build_parser().parse_args(["collect", "tiktok", "--dry-run"])
    assert args.stage == "tiktok"
    assert args.dry_run is True
    assert args.func is cmd_collect


def test_dry_run_writes_jsonl_not_sheets(tmp_path, monkeypatch, patched_client):
    monkeypatch.chdir(tmp_path)
    # dry-run: limit_batches=1 -> ровно один вызов актора при полном реестре
    patched_client["client"] = StubClient([RunResult("SUCCEEDED", items=[NOW_ITEM])])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    code = cmd_collect(sheets, ns("tiktok", dry_run=True))
    assert code == 0
    assert len(patched_client["client"].calls) == 1
    assert sheets.tables["raw_tiktok"] == []
    files = list((tmp_path / "agent-runtime" / "collect").glob("dry-run-tiktok-*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in
            files[0].read_text(encoding="utf-8").splitlines() if line]
    assert rows[0]["raw_id"] == "tiktok_v1"
    assert sheets.tables["run_log"][0]["trigger_type"] == "dry-run"


def test_performance_stage_dispatch(patched_client):
    patched_client["client"] = StubClient([])
    sheets = FakeSheets(tables={"reels": [], "performance": [], "run_log": []})
    code = cmd_collect(sheets, ns("performance"))
    # 0 published-строк -> insufficient_data, но это НЕ авария: exit 0
    assert code == 0
    assert sheets.tables["run_log"][0]["agent"] == "collect-performance"
    assert sheets.tables["run_log"][0]["status"] == "insufficient_data"


def test_failed_status_exit_1(tmp_path, monkeypatch, patched_client):
    monkeypatch.chdir(tmp_path)
    patched_client["client"] = StubClient([RuntimeError("actor dead")])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
    code = cmd_collect(sheets, ns("tiktok", dry_run=True))
    assert code == 1
    assert sheets.tables["run_log"][0]["status"] == "failed"


# ── Аудит 2026-07-24 (H8/H10/M7): stage-локи CLI-сбора ────────────────────────

def test_collect_skipped_when_pipeline_busy(patched_client):
    from cf import pipeline_lock
    from cf.lock import ProcessLock, stage_lock_path

    patched_client["client"] = StubClient([])
    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "factory"))
    assert held.acquire()
    try:
        sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
        code = cmd_collect(sheets, ns("tiktok"))
        assert code == 0
        assert patched_client["client"].calls == []      # ни одного платного рана
        assert sheets.tables["run_log"][0]["agent"] == "collect-tiktok"
        assert sheets.tables["run_log"][0]["status"] == "skipped"
    finally:
        held.release()


def test_collect_performance_ignores_raw_lock_uses_stats(patched_client):
    from cf import pipeline_lock
    from cf.lock import ProcessLock, stage_lock_path

    patched_client["client"] = StubClient([])
    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "raw"))
    assert held.acquire()
    try:
        sheets = FakeSheets(tables={"reels": [], "performance": [], "run_log": []})
        code = cmd_collect(sheets, ns("performance"))
        assert code == 0
        statuses = [r["status"] for r in sheets.tables["run_log"]]
        assert "skipped" not in statuses                 # у performance звено stats
    finally:
        held.release()

    # а вот занятое звено stats performance пропускает
    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "stats"))
    assert held.acquire()
    try:
        sheets = FakeSheets(tables={"reels": [], "performance": [], "run_log": []})
        assert cmd_collect(sheets, ns("performance")) == 0
        assert sheets.tables["run_log"][0]["status"] == "skipped"
    finally:
        held.release()


def test_collect_releases_locks_after_run(tmp_path, monkeypatch, patched_client):
    from cf import pipeline_lock

    monkeypatch.chdir(tmp_path)
    for _ in range(2):
        patched_client["client"] = StubClient([RunResult("SUCCEEDED", items=[NOW_ITEM])])
        sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
        assert cmd_collect(sheets, ns("tiktok", dry_run=True)) == 0
        assert all(r["status"] != "skipped" for r in sheets.tables["run_log"])
    # и после двух прогонов локи свободны
    locks, busy = pipeline_lock.try_stage_locks(("raw", "factory", "stats"))
    assert busy is None
    pipeline_lock.release_stage_locks(locks)


def test_collect_skips_locking_when_parent_holds(monkeypatch, tmp_path, patched_client):
    # Дашборд запускает `cf collect` из захваченного звена: env-флаг выключает
    # повторный захват, иначе дочерний процесс конфликтовал бы с родителем.
    from cf import pipeline_lock
    from cf.lock import ProcessLock, stage_lock_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(pipeline_lock.LOCKS_HELD_ENV, "1")
    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "raw"))
    assert held.acquire()
    try:
        patched_client["client"] = StubClient([RunResult("SUCCEEDED", items=[NOW_ITEM])])
        sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})
        assert cmd_collect(sheets, ns("tiktok", dry_run=True)) == 0
        assert all(r["status"] != "skipped" for r in sheets.tables["run_log"])
    finally:
        held.release()


def test_infra_failure_logs_failed_and_notifies(monkeypatch, patched_client):
    # H3/M26 (аудит 2026-07-24): исключение вне гейта (битый реестр, Sheets,
    # токен) раньше умирало в main() без следа — теперь run_log failed + Telegram.
    patched_client["client"] = StubClient([])
    monkeypatch.setattr(
        "cf.collect.tiktok.load_registry",
        lambda *a, **k: (_ for _ in ()).throw(
            FileNotFoundError("sources/tiktok.json")))
    sent = []
    monkeypatch.setattr("cf.notify.notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})

    code = cmd_collect(sheets, ns("tiktok"))

    assert code == 1
    rows = sheets.tables["run_log"]
    assert rows and rows[-1]["agent"] == "collect-tiktok"
    assert rows[-1]["status"] == "failed"
    # Текст адресован человеку: имя сборщика по-русски, без слова failed.
    assert sent and "Сбор роликов из TikTok" in sent[0]


def test_infra_failure_with_sheets_down_still_notifies(monkeypatch, patched_client):
    patched_client["client"] = StubClient([])
    monkeypatch.setattr(
        "cf.collect.tiktok.load_registry",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("битый JSON реестра")))
    sent = []
    monkeypatch.setattr("cf.notify.notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)

    class DeadSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("Sheets недоступен")

    code = cmd_collect(DeadSheets(tables={"raw_tiktok": [], "run_log": []}),
                       ns("tiktok"))

    assert code == 1
    # алерт ушёл даже без Run Log
    assert sent and "Сбор роликов из TikTok" in sent[0]


# ── Разбор 2026-07-27: причина падения обязана доходить до экрана ────────────
# Ночью Apify упёрся в потолок трат и перестал стартовать раны; гейт написал, что
# именно потеряно, но в «Отчёты этапов» дашборда попали 25 символов «collect
# instagram: failed». Фолбэк раннера на stderr (runner.py:1321) не сработал —
# stdout был непуст. Текст был только в памяти процесса, Telegram и Run Log.

def test_collect_prints_reason_and_lost_batches_to_stdout(monkeypatch,
                                                          patched_client, capsys):
    import cf.collect.instagram as ig_mod

    summary = {
        "status": "failed", "rows": [],
        "error": "гейт: 0 годных рядов при 5 упавших батчах",
        "lost_reasons": ["батч 2: monthly usage hard limit exceeded",
                         "батч 3: monthly usage hard limit exceeded"],
        "lost_sources": ["#менстайл", "#образмужчины", "#стритвир"],
    }
    monkeypatch.setattr(ig_mod, "collect", lambda *a, **k: summary)
    monkeypatch.setattr("cf.notify.notify_telegram",
                        lambda text, cfg=None, client=None: True)
    patched_client["client"] = StubClient([])
    sheets = FakeSheets(tables={"raw_instagram": [], "run_log": []})

    code = cmd_collect(sheets, ns("instagram"))

    out = capsys.readouterr().out
    assert code == 1
    assert "collect instagram: failed" in out
    assert "0 годных рядов при 5 упавших батчах" in out
    assert "monthly usage hard limit exceeded" in out
    assert "источников не опрошено: 3" in out


def test_collect_line_survives_summary_without_loss_keys():
    # Сводки сборщиков доезжают до контракта не одновременно: печать читает ключи
    # через .get() и на старой сводке остаётся прежней однострочной.
    from cf.cli import collect_stdout_lines
    assert collect_stdout_lines("tiktok", {"status": "success"}) == [
        "collect tiktok: success"]


# ── Тикет 06 (план 2026-08-10-apify-costs): блок источников в Telegram-сводке ─


def test_collect_summary_with_exploration_reaches_telegram(monkeypatch,
                                                           patched_client):
    # Факты сборщика (проверено/предложено/ретирнуто) доезжают до владельца в
    # том же сообщении, что и исход сбора, — по существующему пути отправки.
    import cf.collect.tiktok as tt_mod

    summary = {"status": "insufficient_data", "rows": [],
               "exploration": {"explored": ["hashtag:#а", "hashtag:#б"],
                               "promoted": ["hashtag:#а"],
                               "retired": ["hashtag:#б"]}}
    monkeypatch.setattr(tt_mod, "collect", lambda *a, **k: summary)
    sent = []
    monkeypatch.setattr("cf.notify.notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    patched_client["client"] = StubClient([])
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})

    assert cmd_collect(sheets, ns("tiktok")) == 0
    assert sent and "сегодня ничего нового" in sent[0]
    assert "🌱 Завод заодно проверил 2 пробных источника: #а, #б." in sent[0]
    assert "готово предложение добавить его насовсем" in sent[0]
    assert "#б больше не проверяется" in sent[0]


def test_recovered_message_carries_exploration_of_the_healing_run(monkeypatch,
                                                                  patched_client):
    # «Снова работает» — единственное сообщение успешного прогона; факты про
    # источники обязаны ехать в нём, иначе успешный exploration нем.
    import cf.collect.tiktok as tt_mod

    summaries = [
        {"status": "failed", "rows": [], "error": "apify 403"},
        {"status": "success", "rows": [],
         "exploration": {"explored": ["hashtag:#новый"], "promoted": [],
                         "retired": []}},
    ]
    monkeypatch.setattr(tt_mod, "collect",
                        lambda *a, **k: summaries.pop(0))
    sent = []
    monkeypatch.setattr("cf.notify.notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    patched_client["client"] = StubClient([])

    assert cmd_collect(FakeSheets(tables={"raw_tiktok": [], "run_log": []}),
                       ns("tiktok")) == 1
    assert cmd_collect(FakeSheets(tables={"raw_tiktok": [], "run_log": []}),
                       ns("tiktok")) == 0
    assert len(sent) == 2
    assert sent[1].startswith("✅ Сбор роликов из TikTok снова работает.")
    assert "проверил 1 пробный источник: #новый." in sent[1]


# ── UTM-контур, тикет 02: стадия metrika ─────────────────────────────────────

def test_parser_accepts_metrika():
    args = build_parser().parse_args(["collect", "metrika"])
    assert args.stage == "metrika"
    assert args.func is cmd_collect


def test_metrika_without_secrets_is_insufficient_exit_0(tmp_path, capsys):
    # Apify-клиент для метрики не строится вовсе: client_from_config здесь
    # НЕ запатчен — упади cmd_collect в него, тест бы это увидел.
    config = {"metrika": {"token_file": str(tmp_path / "нет-токена.txt")}}
    sheets = FakeSheets(tables={"run_log": []}, config=config)
    code = cmd_collect(sheets, ns("metrika"))
    assert code == 0
    row = sheets.tables["run_log"][0]
    assert row["agent"] == "collect-metrika"
    assert row["status"] == "insufficient_data"
    out = capsys.readouterr().out
    assert "collect metrika: insufficient_data" in out
    assert "counter_id" in out                  # перечень недостающего на экране


def test_metrika_dry_run_writes_jsonl_not_sheets(tmp_path, monkeypatch):
    import cf.collect.metrika as metrika_mod

    monkeypatch.chdir(tmp_path)
    tok = tmp_path / "metrika-token.txt"
    tok.write_text("t", encoding="utf-8")

    class OneShot:
        # Диспетчер по числу dimensions: 3 — дневной, 1 — месячный,
        # 5 — e-commerce покупки (тикет 03).
        def report(self, date1, date2, dimensions, metrics, filters=None):
            if len(dimensions) == 3:
                return [{"dimensions": [{"name": "2026-07-29"},
                                        {"name": "tiktok-1"}, {"name": "b42"}],
                         "metrics": [3, 2]}]
            if len(dimensions) == 5:
                return [{"dimensions": [{"name": "40129"},
                                        {"name": "2026-07-29"},
                                        {"name": "tiktok"},
                                        {"name": "tiktok-1"}, {"name": "b42"}],
                         "metrics": [1990]}]
            return [{"dimensions": [{"name": "tiktok-1"}], "metrics": [10, 12]}]

    monkeypatch.setattr(metrika_mod, "client_from_config",
                        lambda cfg, **kw: OneShot())
    config = {"metrika": {"token_file": str(tok), "counter_id": "55"}}
    sheets = FakeSheets(tables={"run_log": []}, config=config)
    code = cmd_collect(sheets, ns("metrika", dry_run=True))
    assert code == 0
    assert "utm_traffic" not in sheets.tables   # Sheets не тронуты
    assert "orders" not in sheets.tables
    files = list((tmp_path / "agent-runtime" / "collect")
                 .glob("dry-run-metrika-*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in
            files[0].read_text(encoding="utf-8").splitlines() if line]
    # трафик и кандидат в заказы едут в один dry-run JSONL (схемы различимы
    # по ключу: utm_id у трафика, order_id у заказа)
    assert {r.get("row_kind") for r in rows if "utm_id" in r} == {"daily", "monthly"}
    (order,) = [r for r in rows if "order_id" in r]
    assert order["order_id"] == "mk-40129" and order["status"] == "candidate"
    assert sheets.tables["run_log"][0]["trigger_type"] == "dry-run"


def test_collect_metrika_ignores_raw_lock_uses_stats(tmp_path):
    from cf import pipeline_lock
    from cf.lock import ProcessLock, stage_lock_path

    config = {"metrika": {"token_file": str(tmp_path / "нет-токена.txt")}}
    # занятое звено raw метрике не мешает — её звено stats, как у performance
    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "raw"))
    assert held.acquire()
    try:
        sheets = FakeSheets(tables={"run_log": []}, config=config)
        assert cmd_collect(sheets, ns("metrika")) == 0
        assert all(r["status"] != "skipped" for r in sheets.tables["run_log"])
    finally:
        held.release()

    held = ProcessLock(stage_lock_path(pipeline_lock.DEFAULT_LOCKS_DIR, "stats"))
    assert held.acquire()
    try:
        sheets = FakeSheets(tables={"run_log": []}, config=config)
        assert cmd_collect(sheets, ns("metrika")) == 0
        assert sheets.tables["run_log"][0]["status"] == "skipped"
    finally:
        held.release()


def test_apify_token_failure_logs_failed_and_notifies(monkeypatch):
    # Ревью аудита: чтение apify-токена (client_from_config) — внутри try,
    # иначе ротация токена роняла сбор мимо Run Log и Telegram.
    def boom(config, **kw):
        raise FileNotFoundError("~/.cf/secrets/apify-token.txt")

    monkeypatch.setattr(apify_mod, "client_from_config", boom)
    sent = []
    monkeypatch.setattr("cf.notify.notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    sheets = FakeSheets(tables={"raw_tiktok": [], "run_log": []})

    assert cmd_collect(sheets, ns("tiktok")) == 1
    assert sheets.tables["run_log"][-1]["status"] == "failed"
    assert sent and "Сбор роликов из TikTok" in sent[0]


def test_stdout_lines_show_media_counters():
    # Ревью 14.08 (тикет 03): счётчики медиа — в гарантированном stdout-блоке
    # статуса, который «Отчёты этапов» показывают всегда.
    from cf.cli import collect_stdout_lines
    lines = collect_stdout_lines("tiktok", {
        "status": "success",
        "media": {"saved": 3, "partial": 1, "failed": 2, "frames": 40,
                  "reused": 5, "expired": 1}})
    media_line = next(l for l in lines if "медиа" in l)
    assert "сохранено=3" in media_line
    assert "кадров=40" in media_line
    assert "протухших=1" in media_line


def test_stdout_lines_without_media_key_unchanged():
    # Сводки performance/metrika media не несут — блок не печатается, печать
    # не падает (контракт .get() из docstring collect_stdout_lines).
    from cf.cli import collect_stdout_lines
    lines = collect_stdout_lines("performance", {"status": "success"})
    assert not any("медиа" in l for l in lines)
