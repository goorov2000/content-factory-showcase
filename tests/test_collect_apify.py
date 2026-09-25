# C1.2 — Apify-клиент start+poll (спека §2): новые режимы отказа против run-sync —
# терминальные статусы FAILED/ABORTED/TIMED-OUT и таймаут поллинга; изоляция сбоя
# батча (loss-маркер вместо падения всего сбора) сохраняется как в n8n.
import httpx
import pytest

from cf.collect.apify import ApifyClient, run_batches


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self)


class FakeHttp:
    """Скриптованный httpx-клиент: очередь ответов на post/get + журнал вызовов."""

    def __init__(self, post_responses=(), get_responses=()):
        self.post_responses = list(post_responses)
        self.get_responses = list(get_responses)
        self.calls = []

    def post(self, url, json=None):
        self.calls.append(("POST", url))
        return self.post_responses.pop(0)

    def get(self, url, params=None):
        self.calls.append(("GET", url))
        return self.get_responses.pop(0)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make_client(http, clock=None, **kw):
    clock = clock or FakeClock()
    kw.setdefault("retry_delay", 0)
    return ApifyClient("tok", client=http, sleep=clock.sleep,
                       monotonic=clock.monotonic, **kw)


def started(run_id="run1", dataset="ds1", status="RUNNING"):
    return FakeResponse(201, {"data": {"id": run_id, "status": status,
                                       "defaultDatasetId": dataset}})


def run_status(status, dataset="ds1"):
    return FakeResponse(200, {"data": {"status": status,
                                       "defaultDatasetId": dataset}})


def test_run_actor_happy_path():
    http = FakeHttp(
        post_responses=[started()],
        get_responses=[run_status("RUNNING"), run_status("SUCCEEDED"),
                       FakeResponse(200, [{"id": "v1"}, {"id": "v2"}])])
    result = make_client(http).run_actor("clockworks~tiktok-scraper", {"x": 1})
    assert result.ok
    assert result.status == "SUCCEEDED"
    assert [r["id"] for r in result.items] == ["v1", "v2"]
    assert http.calls[0] == ("POST", "/v2/acts/clockworks~tiktok-scraper/runs")


@pytest.mark.parametrize("status", ["FAILED", "ABORTED", "TIMED-OUT"])
def test_run_actor_terminal_failure(status):
    http = FakeHttp(post_responses=[started()],
                    get_responses=[run_status(status)])
    result = make_client(http).run_actor("a~b", {})
    assert not result.ok
    assert result.status == status
    assert status in result.error
    assert result.items == []
    # dataset items не запрашивались
    assert all("/datasets/" not in url for _, url in http.calls)


def test_run_actor_poll_timeout():
    clock = FakeClock()
    http = FakeHttp(post_responses=[started()],
                    get_responses=[run_status("RUNNING") for _ in range(100)])
    client = make_client(http, clock=clock, poll_interval=10, poll_timeout=25)
    result = client.run_actor("a~b", {})
    assert not result.ok
    assert "timeout" in result.error.lower()
    assert result.items == []


def test_run_actor_retries_429_on_start():
    http = FakeHttp(
        post_responses=[FakeResponse(429), started()],
        get_responses=[run_status("SUCCEEDED"), FakeResponse(200, [])])
    result = make_client(http).run_actor("a~b", {})
    assert result.ok
    assert [c for c in http.calls if c[0] == "POST"] == \
        [("POST", "/v2/acts/a~b/runs")] * 2


def test_run_actor_404_not_retried():
    http = FakeHttp(post_responses=[FakeResponse(404)])
    with pytest.raises(httpx.HTTPStatusError):
        make_client(http).run_actor("a~b", {})
    assert len(http.calls) == 1


# --- run_batches: изоляция сбоя батча ---

def _batch(i, sources):
    return {"batch_index": i, "batch_sources": sources}


def test_run_batches_isolates_failed_batch():
    batches = [_batch(0, ["hashtag:#a"]), _batch(1, ["query:b"]),
               _batch(2, ["hashtag:#c"])]

    def runner(batch):
        if batch["batch_index"] == 1:
            raise RuntimeError("actor exploded")
        return [{"id": f"v{batch['batch_index']}"}]

    results, losses = run_batches(batches, runner, max_workers=2)
    assert [items for _, items in results] == [[{"id": "v0"}], [{"id": "v2"}]]
    assert len(losses) == 1
    loss = losses[0]
    assert loss["__apify_error"] == "actor exploded"
    assert loss["batch_index"] == 1
    assert loss["batch_sources"] == ["query:b"]
    assert loss["source_query"] == "unknown"


def test_run_batches_failed_runresult_becomes_loss():
    # runner возвращает неуспешный RunResult (терминальный статус актора) —
    # это тоже потеря батча, не пустой успех
    from cf.collect.apify import RunResult

    def runner(batch):
        return RunResult(status="FAILED", items=[], error="run FAILED")

    results, losses = run_batches([_batch(0, ["hashtag:#x"])], runner)
    assert results == []
    assert losses[0]["__apify_error"] == "run FAILED"
    assert losses[0]["batch_index"] == 0


def test_run_batches_retries_aborted_run_once():
    """Оборванный ран повторяется — порт с n8n потерял retryOnFail (разбор 27.07).

    ABORTED/TIMED-OUT лечатся повтором: ран уже создан, причина не в наших данных.
    Сутки простоя источникам батча из-за одного обрыва — цена, которой не было в
    эталоне (n8n/cf01-tiktok: retryOnFail=true, maxTries=2).
    """
    from cf.collect.apify import RunResult

    calls, naps = [], []

    def runner(batch):
        calls.append(batch["batch_index"])
        if len(calls) == 1:
            return RunResult(status="ABORTED", error="apify run ABORTED (act)")
        return [{"id": "v0"}]

    results, losses = run_batches([_batch(0, ["hashtag:#x"])], runner,
                                  sleep=naps.append)
    assert [items for _, items in results] == [[{"id": "v0"}]]
    assert losses == []
    assert calls == [0, 0]          # ровно один повтор
    assert naps == [5.0]            # и пауза перед ним


def test_run_batches_does_not_retry_failed_or_start_refusal():
    """Повтор строго для обрывов — не для отказа старта и не для FAILED.

    Ночью 27.07 Apify упёрся в потолок трат и отказывал на СТАРТЕ (402). Повтор
    там удвоил бы бесполезные запросы, а на потерянном ответе создал бы второй
    платный ран-сироту (M29). FAILED повторять тоже незачем: актор, упавший на
    разборе входа, упадёт так же и во второй раз.
    """
    from cf.collect.apify import RunResult

    starts = []

    def refusing(batch):
        starts.append("start")
        raise RuntimeError("402 Payment Required")

    _r, losses = run_batches([_batch(0, ["hashtag:#x"])], refusing)
    assert starts == ["start"]                        # ни одного повтора
    assert losses[0]["__apify_error"] == "402 Payment Required"
    assert "попыток" not in losses[0]["__apify_error"]

    failed = []

    def failing(batch):
        failed.append("run")
        return RunResult(status="FAILED", error="run FAILED")

    _r2, losses2 = run_batches([_batch(0, ["hashtag:#x"])], failing)
    assert failed == ["run"]
    assert losses2[0]["__apify_error"] == "run FAILED"


def test_run_batches_reports_how_many_attempts_were_spent():
    """Текст потери отличает «упало сразу» от «упало и после повтора»."""
    from cf.collect.apify import RunResult

    def always_aborted(batch):
        return RunResult(status="ABORTED", error="apify run ABORTED (act)")

    _r, losses = run_batches([_batch(0, ["hashtag:#x"])], always_aborted,
                             sleep=lambda _s: None)
    assert losses[0]["__apify_error"] == "apify run ABORTED (act) [попыток: 2]"


def test_run_batches_order_stable():
    batches = [_batch(i, []) for i in range(5)]

    def runner(batch):
        return [{"id": batch["batch_index"]}]

    results, losses = run_batches(batches, runner, max_workers=4)
    assert [items[0]["id"] for _, items in results] == [0, 1, 2, 3, 4]
    assert losses == []


# ── M29/M30 (аудит 2026-07-24): неидемпотентный старт и abort брошенных ранов ─

class _ExplodingHttp(FakeHttp):
    """post кидает подготовленные исключения до того, как отдать ответы."""

    def __init__(self, post_errors=(), post_responses=(), get_responses=()):
        super().__init__(post_responses=post_responses, get_responses=get_responses)
        self.post_errors = list(post_errors)

    def post(self, url, json=None):
        self.calls.append(("POST", url))
        if self.post_errors:
            raise self.post_errors.pop(0)
        return self.post_responses.pop(0)


def test_start_read_timeout_not_retried_single_post():
    # Ответ потерян ПОСЛЕ отправки — ран мог создаться; повтор дал бы второй
    # платный ран-сироту. Одна попытка, исключение наружу (loss-маркер батча).
    http = _ExplodingHttp(post_errors=[httpx.ReadTimeout("lost response")])
    client = make_client(http)
    with pytest.raises(httpx.ReadTimeout):
        client.run_actor("acts~x", {})
    assert [c for c in http.calls if c[0] == "POST"] == [("POST", "/v2/acts/acts~x/runs")]


def test_start_connect_error_is_retried():
    # Соединение не установилось — запрос до сервера не дошёл, ран точно не создан.
    ok = FakeResponse(payload={"data": {"id": "r1", "status": "SUCCEEDED",
                                        "defaultDatasetId": ""}})
    http = _ExplodingHttp(post_errors=[httpx.ConnectError("refused")],
                          post_responses=[ok])
    client = make_client(http)
    result = client.run_actor("acts~x", {})
    assert result.status == "SUCCEEDED"
    assert len([c for c in http.calls if c[0] == "POST"]) == 2


def test_poll_timeout_aborts_run():
    start = FakeResponse(payload={"data": {"id": "r9", "status": "RUNNING",
                                           "defaultDatasetId": ""}})
    polling = FakeResponse(payload={"data": {"status": "RUNNING"}})
    abort_ok = FakeResponse(payload={})
    http = FakeHttp(post_responses=[start, abort_ok],
                    get_responses=[polling] * 50)
    client = make_client(http, poll_timeout=10, poll_interval=5)
    result = client.run_actor("acts~x", {})
    assert result.status == "POLL-TIMEOUT"
    assert ("POST", "/v2/actor-runs/r9/abort") in http.calls   # ран не брошен живым


def test_poll_failure_aborts_and_raises():
    start = FakeResponse(payload={"data": {"id": "r9", "status": "RUNNING",
                                           "defaultDatasetId": ""}})
    abort_ok = FakeResponse(payload={})

    class DyingHttp(FakeHttp):
        def get(self, url, params=None):
            self.calls.append(("GET", url))
            raise httpx.ConnectError("сеть умерла")

    http = DyingHttp(post_responses=[start, abort_ok])
    client = make_client(http, poll_timeout=100, poll_interval=1)
    from cf.retry import RetryError
    with pytest.raises(RetryError):
        client.run_actor("acts~x", {})
    assert ("POST", "/v2/actor-runs/r9/abort") in http.calls


# ── Повтор оборванного батча (разбор 2026-07-27) ────────────────────────────

from cf.collect.apify import (BATCH_RETRY_DELAY,  # noqa: E402 — раздел разбора
                              RETRIABLE_STATUSES, RunResult)


@pytest.mark.parametrize("status", sorted(RETRIABLE_STATUSES))
def test_run_batches_retries_every_status_of_a_broken_run(status):
    """Повтор ловит ВСЕ обрывы рана, а не только ABORTED из теста выше.

    Набор статусов — контракт с run_actor: POLL-TIMEOUT мы сочиняем сами (ран
    брошен нами по таймауту), и строка живёт в двух местах. Разъедутся — батч,
    брошенный по таймауту, перестанет повторяться молча: та же немота, что
    стоила суток в ночь 27.07.
    """
    calls, naps = [], []

    def runner(batch):
        calls.append(status)
        if len(calls) == 1:
            return RunResult(status=status, error=f"apify run {status} (act)")
        return [{"id": "v0"}]

    results, losses = run_batches([_batch(0, ["hashtag:#x"])], runner,
                                  sleep=naps.append)
    assert [items for _, items in results] == [[{"id": "v0"}]]
    assert losses == []
    assert len(calls) == 2                    # одна попытка + один повтор
    assert naps == [BATCH_RETRY_DELAY]        # и пауза между ними


def test_poll_timeout_status_is_the_one_run_batches_retries():
    """Статус брошенного по таймауту рана обязан быть в RETRIABLE_STATUSES."""
    clock = FakeClock()
    http = FakeHttp(post_responses=[started(run_id="r7"), FakeResponse(200, {})],
                    get_responses=[run_status("RUNNING") for _ in range(10)])
    result = make_client(http, clock=clock, poll_interval=10,
                         poll_timeout=25).run_actor("a~b", {})
    assert result.status in RETRIABLE_STATUSES


def test_run_batches_attempts_and_pause_are_parameters():
    """Глубину повтора и паузу задаёт вызывающий, а не магическое число внутри.

    Раны платные: сбор обязан уметь выключить повтор (attempts=1) без правки
    клиента — иначе следующий потолок трат чинится редактированием кода.
    """
    calls, naps = [], []

    def aborted(batch):
        calls.append("run")
        return RunResult(status="ABORTED", error="apify run ABORTED (act)")

    _r, losses = run_batches([_batch(0, [])], aborted, attempts=1,
                             sleep=naps.append)
    assert calls == ["run"] and naps == []
    assert "попыток" not in losses[0]["__apify_error"]   # повтора и не было

    calls.clear()
    _r2, losses2 = run_batches([_batch(0, [])], aborted, attempts=3,
                               retry_delay=0.5, sleep=naps.append)
    assert len(calls) == 3 and naps == [0.5, 0.5]
    assert "[попыток: 3]" in losses2[0]["__apify_error"]
