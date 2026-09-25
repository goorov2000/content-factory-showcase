import pytest

from cf.retry import RetryError, with_retry


def test_returns_result_on_first_success():
    assert with_retry(lambda: 42) == 42


def test_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    assert with_retry(flaky, base_delay=0) == "ok"
    assert calls["n"] == 3


def test_raises_retry_error_with_label_after_attempts():
    def always_fails():
        raise ConnectionError("boom")

    with pytest.raises(RetryError) as exc_info:
        with_retry(always_fails, base_delay=0, label="sheets read")
    assert "sheets read" in str(exc_info.value)
    assert "3 attempts" in str(exc_info.value)


class FakeAPIError(Exception):
    """Как gspread APIError: несёт response со status_code."""

    def __init__(self, status):
        super().__init__(f"http {status}")
        self.response = type("Resp", (), {"status_code": status})()


def count_calls(exc):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise exc

    return fn, calls


def test_programming_error_raised_immediately_without_retry():
    fn, calls = count_calls(KeyError("tabs"))
    with pytest.raises(KeyError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 1


def test_missing_credentials_file_not_retried():
    fn, calls = count_calls(FileNotFoundError("secrets/service-account.json"))
    with pytest.raises(FileNotFoundError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 1


def test_permanent_api_error_4xx_not_retried():
    fn, calls = count_calls(FakeAPIError(403))
    with pytest.raises(FakeAPIError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 1


def test_rate_limit_429_is_retried():
    fn, calls = count_calls(FakeAPIError(429))
    with pytest.raises(RetryError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 3


def test_server_error_5xx_is_retried():
    fn, calls = count_calls(FakeAPIError(503))
    with pytest.raises(RetryError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 3


# ── M34 (аудит 2026-07-24): 429-квота поминутная — пауза до следующего окна ──

class _Resp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _HttpError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.response = _Resp(status_code, headers)


def _sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr("cf.retry._sleep", lambda s: slept.append(s))
    return slept


def test_429_without_header_sleeps_full_quota_window(monkeypatch):
    slept = _sleeps(monkeypatch)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HttpError(429)
        return "ok"

    assert with_retry(flaky) == "ok"
    assert slept == [65.0]              # не 1с — квота живёт до конца минуты


def test_429_respects_retry_after_capped(monkeypatch):
    slept = _sleeps(monkeypatch)

    def always():
        raise _HttpError(429, {"Retry-After": "7"})

    with pytest.raises(RetryError):
        with_retry(always)
    assert slept == [7.0, 7.0]

    slept.clear()

    def huge():
        raise _HttpError(429, {"Retry-After": "600"})

    with pytest.raises(RetryError):
        with_retry(huge)
    assert slept == [65.0, 65.0]        # кап — окно минутной квоты


def test_non_429_keeps_exponential_backoff(monkeypatch):
    slept = _sleeps(monkeypatch)

    def always():
        raise ConnectionError("boom")

    with pytest.raises(RetryError):
        with_retry(always, base_delay=1.0)
    assert slept == [1.0, 2.0]


# ── ревью 14.09.2026: транзиентные ошибки, которые with_retry считал постоянными ──

def test_request_timeout_408_is_retried():
    # gspread-овский BackOffHTTPClient сам ретраит 408; CF берёт обычный клиент,
    # и with_retry — единственный повтор: отброшенный им 408 означал failed-прогон.
    fn, calls = count_calls(FakeAPIError(408))
    with pytest.raises(RetryError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 3


class _TruncatedBodyError(OSError, ValueError):
    """Форма requests.exceptions.JSONDecodeError: он наследник И OSError (через
    RequestException), И ValueError (через json.JSONDecodeError). Так всплывает
    обрезанное тело ответа 200 на response.json() внутри gspread."""


def test_truncated_response_body_is_retried_but_plain_value_error_is_not():
    fn, calls = count_calls(_TruncatedBodyError("Expecting value: line 1 column 1"))
    with pytest.raises(RetryError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 3

    fn, calls = count_calls(ValueError("ошибка данных"))
    with pytest.raises(ValueError):
        with_retry(fn, base_delay=0)
    assert calls["n"] == 1
