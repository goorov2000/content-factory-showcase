# Герметичность тестовой среды (аудит 2026-07-24, находки H4/M5).
#
# Сюита гоняется в том числе на боевом VPS, где существуют настоящие
# ~/.cf/secrets/telegram-*.txt и живой дашборд держит agent-runtime/locks.
# Без изоляции тесты шлют реальные Telegram-сообщения боевым ботом и
# конфликтуют с прод-локами звеньев.
import shutil
import socket
from pathlib import Path

import pytest

import cf.dashboard.runner as runner_mod
import cf.notify
from cf import pipeline_lock


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: тесту разрешены исходящие сетевые соединения")


@pytest.fixture(autouse=True)
def _telegram_secrets_isolated(monkeypatch, tmp_path):
    # Без явного config _credentials() падает на дефолтные пути ~/.cf/secrets —
    # подменяем их несуществующими, чтобы telegram_configured() был False,
    # пока тест сам не передал config с секретами.
    monkeypatch.setattr(cf.notify, "DEFAULT_TOKEN_FILE",
                        str(tmp_path / "no-telegram-token.txt"))
    monkeypatch.setattr(cf.notify, "DEFAULT_CHAT_ID_FILE",
                        str(tmp_path / "no-telegram-chat.txt"))


@pytest.fixture(autouse=True)
def _locks_dir_isolated(monkeypatch, tmp_path):
    # DEFAULT_LOCKS_DIR указывает на agent-runtime/locks репозитория — в тестах
    # это боевые локи прод-дашборда. ProcessLock/file_lock сами делают mkdir.
    monkeypatch.setattr(runner_mod, "DEFAULT_LOCKS_DIR", tmp_path / "locks")
    monkeypatch.setattr(pipeline_lock, "DEFAULT_LOCKS_DIR", tmp_path / "locks")
    # изоляция от флага родительского процесса (вдруг тесты гоняются под раннером)
    monkeypatch.delenv(pipeline_lock.LOCKS_HELD_ENV, raising=False)


@pytest.fixture(autouse=True)
def _notify_mute_isolated(monkeypatch, tmp_path):
    # Стоп-кран agent-runtime/notify-off — БОЕВОЙ файл на машине. Пока он лежал
    # (поставлен 04.09 против спама в чат), notify_telegram молча отвечал False
    # и три теста test_notify.py падали, ничего не сказав про причину. Флаг
    # эксплуатации не должен решать судьбу сюиты: тест, которому кран нужен,
    # подставляет свой путь сам.
    monkeypatch.setattr(cf.notify, "NOTIFY_OFF_FILE", tmp_path / "notify-off")
    monkeypatch.delenv(cf.notify.NOTIFY_OFF_ENV, raising=False)


@pytest.fixture(autouse=True)
def _alerts_state_isolated(monkeypatch, tmp_path):
    # STATE_PATH указывает на agent-runtime/alerts-state.json репозитория — то
    # есть на боевое состояние приглушения. Тест, прогнавший cmd_collect, иначе
    # пометил бы живую проблему как «уже сказали» и съел бы утренний алерт.
    import cf.alerts
    monkeypatch.setattr(cf.alerts, "STATE_PATH", tmp_path / "alerts-state.json")


@pytest.fixture(autouse=True)
def _runner_root_isolated(monkeypatch, tmp_path):
    # StageRunner(root=None) брал Path(".") — корень РЕПОЗИТОРИЯ. Пока раннер читал
    # оттуда только formulas/ в страховке гварда, это сходило с рук; с появлением
    # очередей воркеров (queues.py) фан-аут в тестах увидел боевые темы завода и
    # начал планировать по ним черновики промптов. Дефолт — во временный каталог;
    # тесты, которым нужно дерево, передают root/analysis_dir явно.
    monkeypatch.setattr(runner_mod, "DEFAULT_ROOT", tmp_path / "repo")


@pytest.fixture(autouse=True)
def _runner_state_files_isolated(monkeypatch, tmp_path):
    # build_production_app передаёт в StageRunner относительные пути
    # agent-runtime/reports/{stage-reports,pipeline-progress}.json и
    # agent-runtime/analysis — при запуске из корня это файлы ЖИВОГО дашборда:
    # раннер читает прогресс и при шаге running переписывает его (ревью 14.09.2026).
    monkeypatch.setattr(runner_mod, "REPORTS_STATE_PATH",
                        tmp_path / "reports" / "stage-reports.json")
    monkeypatch.setattr(runner_mod, "PROGRESS_STATE_PATH",
                        tmp_path / "reports" / "pipeline-progress.json")
    monkeypatch.setattr(runner_mod, "ANALYSIS_DIR", tmp_path / "analysis")


@pytest.fixture(autouse=True)
def _retry_sleep_disabled(monkeypatch):
    # 429-пауза ретраев — до 65с (cf.retry._retry_after_seconds): сюита не должна
    # спать по-настоящему. Тесты таймингов переопределяют cf.retry._sleep сами.
    monkeypatch.setattr("cf.retry._sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_outbound_network(request, monkeypatch):
    # Страховка от любых незастабленных HTTP-клиентов (telegram, apify, n8n).
    # Тест, которому сеть нужна осознанно, помечается @pytest.mark.network.
    if request.node.get_closest_marker("network"):
        yield
        return

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "исходящая сеть запрещена в тестах "
            "(@pytest.mark.network — осознанное исключение)")

    # Витринная копия, прогон на Windows: ProactorEventLoop asyncio при старте делает
    # socket.socketpair(), который там эмулируется через connect к 127.0.0.1 — без
    # этого исключения падал бы каждый тест с TestClient. Внешняя сеть по-прежнему
    # запрещена; на loopback в тестах никто не слушает.
    real_connect = socket.socket.connect

    def _connect_loopback_only(sock, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else None
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(sock, address, *args, **kwargs)
        return _blocked()

    monkeypatch.setattr(socket.socket, "connect", _connect_loopback_only)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


# --- Витринная копия: боевых cf.config.json и sources/*.json в репозитории нет ---------
REPO_ROOT = Path(__file__).resolve().parents[1]


def repo_config_path():
    """Боевой cf.config.json, а при его отсутствии — cf.config.example.json.

    Тесты инвариантов конфига читают ИМЕННО файл репозитория, а не фикстуру
    (test_config_invariants.py). В витринной копии рабочий конфиг не публикуется, и его
    роль играет пример с той же структурой; продовая логика чтения конфига не тронута.
    """
    live = REPO_ROOT / "cf.config.json"
    return live if live.is_file() else REPO_ROOT / "cf.config.example.json"


@pytest.fixture(autouse=True)
def _example_sources_when_real_absent(monkeypatch, tmp_path_factory):
    # load_registry(platform) без явного root читает <repo>/sources/<platform>.json.
    # В витринной копии реальных реестров нет — подставляем корень с копиями
    # sources/*.example.json. Тесты, передающие root явно, не затрагиваются.
    import cf.collect.sources as sources_mod
    if (REPO_ROOT / "sources" / "tiktok.json").is_file():
        return
    root = tmp_path_factory.mktemp("example-sources")
    (root / "sources").mkdir()
    for example in (REPO_ROOT / "sources").glob("*.example.json"):
        shutil.copyfile(example, root / "sources" / example.name.replace(".example", ""))
    monkeypatch.setattr(sources_mod, "_ROOT", root)
