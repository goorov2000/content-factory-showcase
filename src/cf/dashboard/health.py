import shutil
import threading
import time
from datetime import datetime

_ORDER = {"ok": 0, "warn": 1, "fail": 2}


class HealthMonitor:
    def __init__(self, checks, interval=60.0):
        self.checks = checks  # name -> callable -> (status, detail); status: ok|warn|fail
        self.interval = interval
        self.results = {name: ("warn", "ещё не проверялось") for name in checks}
        self.checked_at = None
        self.thread = None  # фоновый поток появляется только после start()
        # Событие остановки: loop ждёт на нём вместо time.sleep, поэтому stop()
        # будит цикл мгновенно, а не через полный interval.
        self._stop = threading.Event()

    def run_once(self):
        for name, check in self.checks.items():
            try:
                self.results[name] = check()
            except Exception as exc:
                self.results[name] = ("fail", str(exc))
        self.checked_at = datetime.now().strftime("%H:%M")

    def worst(self):
        return max((s for s, _ in self.results.values()),
                   key=_ORDER.__getitem__, default="warn")

    def start(self):
        # Идемпотентно: повторный start() при живом потоке не плодит второй.
        if self.thread is not None and self.thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                self.run_once()
                # wait() просыпается сразу при stop() (в отличие от time.sleep),
                # поэтому остановка отзывчива и не ждёт полный interval.
                self._stop.wait(self.interval)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self, timeout=5.0):
        # Взводим событие -> loop.wait() просыпается -> цикл завершается.
        # join с таймаутом, чтобы shutdown/тест не завис, если поток застрял.
        self._stop.set()
        thread = self.thread
        if thread is not None:
            thread.join(timeout)


def make_health_sheets(sheets):
    """Отдельный экземпляр Sheets для фонового health-цикла.

    check_sheets крутится в daemon-потоке параллельно обработчикам запросов,
    а gspread-клиент держит requests.Session, которая НЕ потокобезопасна.
    Общий клиент -> гонка между health-циклом и хендлерами. Поэтому health
    получает свой инстанс со своим клиентом (своей Session), конфиг переиспользуем.
    """
    from cf.sheets import Sheets
    return Sheets(config=sheets.config)


def default_checks(sheets, config):
    # health читает Sheets из фонового потока — даём ему отдельный клиент,
    # чтобы не делить requests.Session с обработчиками запросов (см. выше).
    health_sheets = make_health_sheets(sheets)

    def check_sheets():
        # P3.4: лёгкий пинг связности — строка заголовков одной вкладки (row_values(1)),
        # а не выкачивание всего run_log каждые 60 с. Round-trip к Sheets сохраняется,
        # поэтому разрыв связи по-прежнему всплывает как fail.
        t0 = time.monotonic()
        health_sheets.header("run_log")
        return ("ok", f"подключено · {time.monotonic() - t0:.1f} с")

    def check_claude():
        if shutil.which("claude"):
            return ("ok", "CLI найден")
        return ("fail", "claude CLI не найден в PATH")

    # Дрейф схемы боевых листов меняется руками оператора (раз в месяцы), поэтому
    # перепроверяем не чаще 10 мин — иначе каждые 60 с ещё 2 HTTP на пустом месте.
    schema_state = {"at": 0.0, "value": None}
    schema_ttl = 600.0

    def check_schema():
        from cf.sheets import schema_drift
        now = time.monotonic()
        if schema_state["value"] is None or now - schema_state["at"] > schema_ttl:
            schema_state["value"] = schema_drift(health_sheets)
            schema_state["at"] = now
        drift = schema_state["value"]
        if not drift:
            return ("ok", "колонки на месте")
        tabs = (config or {}).get("tabs", {})
        parts = [f"{tabs.get(tab, tab)}: нет {', '.join(cols)}"
                 for tab, cols in drift.items()]
        return ("warn", " · ".join(parts) + " — часть данных не сохраняется, "
                                            "недельный лимит считается по дате создания")

    # n8n из списка систем убран (решение владельца 2026-07-28): сбор на нём
    # списан 2026-07-24, воркфлоу деактивированы, и строка «n8n · отвечает»
    # отчитывалась о системе, которая в работе завода не участвует. Инстанс жив
    # ради publish-звена этапа 6 — вернётся в список вместе с ним.
    return {"Google Sheets": check_sheets,
            "Claude Code": check_claude, "Схема таблиц": check_schema}
