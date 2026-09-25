# Аудит 2026-07-24 (H4/M5): среда тестов герметична — проверки conftest-фикстур.
import socket

import pytest

from cf.notify import telegram_configured


def test_default_telegram_secrets_are_isolated():
    # Без явного config дефолтные пути секретов подменены conftest'ом:
    # даже на боевом VPS с живыми ~/.cf/secrets телеграм «не сконфигурирован».
    assert telegram_configured() is False


def test_runner_default_locks_dir_is_tmp():
    from cf.dashboard.runner import StageRunner
    from tests.fakes import FakeSheets

    runner = StageRunner(FakeSheets(tables={"run_log": []}), {},
                         http_post=lambda u: None,
                         run_command=lambda argv: (0, ""))
    assert "agent-runtime" not in str(runner.locks_dir)


def test_outbound_socket_blocked():
    with pytest.raises(RuntimeError, match="исходящая сеть запрещена"):
        socket.create_connection(("example.com", 443), timeout=1)


@pytest.mark.network
def test_network_marker_allows_local_connect():
    # Осознанное исключение: соединение до собственного слушателя на loopback.
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        cli = socket.create_connection(srv.getsockname(), timeout=2)
        cli.close()
    finally:
        srv.close()
