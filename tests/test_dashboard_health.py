import time

from fastapi.testclient import TestClient

from cf.dashboard import health as health_mod
from cf.dashboard.app import create_app
from cf.dashboard.health import HealthMonitor, default_checks, make_health_sheets
from tests.fakes import FakeSheets


class _ProbeSheets:
    """Спай для check_sheets: считает лёгкие header-пинги и полные read_rows."""

    def __init__(self, fail=False):
        self.header_calls = []
        self.read_rows_calls = []
        self.fail = fail

    def header(self, tab_key):
        self.header_calls.append(tab_key)
        if self.fail:
            raise ConnectionError("sheets down")
        return ["run_id", "agent", "status"]

    def read_rows(self, tab_key, include_heavy=False):
        self.read_rows_calls.append(tab_key)
        return []


def test_check_sheets_uses_light_header_not_full_read(monkeypatch):
    # P3.4: связность Sheets проверяется дешёвой строкой заголовков, а не выкачиванием
    # всего run_log каждые 60 с.
    probe = _ProbeSheets()
    monkeypatch.setattr(health_mod, "make_health_sheets", lambda s: probe)
    checks = default_checks(FakeSheets({}), {})
    status, detail = checks["Google Sheets"]()
    assert status == "ok" and "подключено" in detail
    assert probe.header_calls == ["run_log"]        # один лёгкий пинг
    assert probe.read_rows_calls == []              # run_log целиком не читался


def test_check_sheets_reports_broken_on_failure(monkeypatch):
    # Разрыв связи по-прежнему всплывает как fail (проверка сохраняет свою функцию).
    probe = _ProbeSheets(fail=True)
    monkeypatch.setattr(health_mod, "make_health_sheets", lambda s: probe)
    monitor = HealthMonitor(default_checks(FakeSheets({}), {}))
    monitor.run_once()
    status, detail = monitor.results["Google Sheets"]
    assert status == "fail" and "sheets down" in detail
    assert probe.read_rows_calls == []              # даже при сбое не выкачивали run_log


def test_run_once_collects_results():
    monitor = HealthMonitor({
        "Google Sheets": lambda: ("ok", "подключено"),
        "Схема таблиц": lambda: ("warn", "нет колонки"),
    })
    monitor.run_once()
    assert monitor.results["Google Sheets"] == ("ok", "подключено")
    assert monitor.results["Схема таблиц"] == ("warn", "нет колонки")
    assert monitor.worst() == "warn"


def test_check_exception_becomes_fail():
    def boom():
        raise ConnectionError("timeout")
    monitor = HealthMonitor({"Google Sheets": boom})
    monitor.run_once()
    status, detail = monitor.results["Google Sheets"]
    assert status == "fail" and "timeout" in detail
    assert monitor.worst() == "fail"


def test_systems_list_has_no_n8n_anymore():
    # Решение владельца 2026-07-28: n8n из списка систем убран. Сбор на нём
    # списан 24.07, воркфлоу деактивированы — строка «n8n · отвечает»
    # отчитывалась о системе, которая в работе завода не участвует.
    checks = default_checks(FakeSheets({}),
                            {"n8n": {"base_url": "https://n8n.example"}})
    assert "n8n" not in checks
    assert set(checks) == {"Google Sheets", "Claude Code", "Схема таблиц"}


def test_health_popover_is_not_clipped_by_the_sidebar():
    # Абсолютную панель резал overflow-y сайдбара (240px) — со стороны это
    # выглядело как «страница перекрывает всплывашку с состоянием систем».
    from cf.dashboard.app import BASE_DIR
    css = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    pop = css[css.index(".sys-pop {"):]
    pop = pop[:pop.index("}") + 1]
    assert "position: fixed" in pop                  # не режется границей сайдбара
    assert "width: 340px" in pop                     # деталь проверки помещается
    assert "z-index: 400" in pop                     # поверх страницы
    # Деталь переносится вторым ярусом, а не обрезается в строку с именем
    row = css[css.index(".sys-row .row-meta {"):]
    row = row[:row.index("}") + 1]
    assert "overflow-wrap: anywhere" in row and "white-space: normal" in row
    js = (BASE_DIR / "static" / "systems.js").read_text(encoding="utf-8")
    assert "getBoundingClientRect" in js             # координаты по месту кнопки
    # слушатели на document: партиал health.html htmx меняет целиком раз в 60 с
    assert "document.addEventListener" in js
    monitor = HealthMonitor({"Google Sheets": lambda: ("ok", "подключено")})
    monitor.run_once()
    client = TestClient(create_app(sheets=FakeSheets({}), health=monitor))
    assert "/static/systems.js" in client.get("/overview").text


def test_unchecked_defaults_to_warn():
    monitor = HealthMonitor({"x": lambda: ("ok", "")})
    assert monitor.results["x"][0] == "warn"
    assert monitor.worst() == "warn"


def test_thread_is_none_until_start():
    monitor = HealthMonitor({"x": lambda: ("ok", "")}, interval=30.0)
    assert monitor.thread is None
    try:
        monitor.start()
        assert monitor.thread is not None and monitor.thread.daemon
    finally:
        # Обязательный teardown: без stop() daemon-поток жил бы до конца pytest-сессии.
        monitor.stop()


def test_stop_terminates_thread_and_is_responsive():
    # interval=30с: если бы остановка ждала полный interval (time.sleep),
    # join(5с) не дождался бы и поток остался бы живым. Event.wait просыпается
    # мгновенно на stop() — поток гарантированно умирает за доли секунды.
    monitor = HealthMonitor({"x": lambda: ("ok", "")}, interval=30.0)
    monitor.start()
    assert monitor.thread.is_alive()
    t0 = time.monotonic()
    monitor.stop(timeout=5.0)
    elapsed = time.monotonic() - t0
    assert not monitor.thread.is_alive()  # поток остановлен
    assert elapsed < 5.0  # остановка отзывчива, не ждали полный interval


def test_start_is_idempotent_no_double_thread():
    monitor = HealthMonitor({"x": lambda: ("ok", "")}, interval=30.0)
    try:
        monitor.start()
        first = monitor.thread
        monitor.start()  # повторный start() не плодит второй поток
        assert monitor.thread is first and monitor.thread.is_alive()
    finally:
        monitor.stop()


def test_health_check_uses_dedicated_sheets_instance():
    # health-цикл крутится в фоновом потоке параллельно обработчикам запросов;
    # общий gspread-клиент на requests.Session (не потокобезопасна) привёл бы
    # к гонке. Поэтому health получает СВОЙ экземпляр Sheets со своим клиентом.
    from cf.sheets import Sheets

    request_client = object()
    request_sheets = Sheets(config={"tabs": {}}, client=request_client)
    health_sheets = make_health_sheets(request_sheets)
    assert health_sheets is not request_sheets  # отдельный инстанс
    # не делит клиент (а значит и requests.Session) с обработчиками запросов
    assert health_sheets._client is not request_client
    assert isinstance(health_sheets, Sheets)


def test_app_shutdown_stops_health_thread():
    # Lifespan-хук create_app должен гасить health-поток при остановке сервера.
    monitor = HealthMonitor({"x": lambda: ("ok", "")}, interval=30.0)
    monitor.start()
    assert monitor.thread.is_alive()
    app = create_app(sheets=FakeSheets({}), health=monitor)
    with TestClient(app):  # вход/выход контекста запускает startup/shutdown
        pass
    assert not monitor.thread.is_alive()  # shutdown вызвал monitor.stop()


def test_health_partial_renders_lamps():
    monitor = HealthMonitor({"Google Sheets": lambda: ("ok", "подключено")})
    monitor.run_once()
    app = create_app(sheets=FakeSheets({}), health=monitor)
    client = TestClient(app)
    resp = client.get("/partials/health")
    assert resp.status_code == 200
    assert "СОСТОЯНИЕ СИСТЕМ" in resp.text and "подключено" in resp.text
