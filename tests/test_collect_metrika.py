# UTM-контур, тикеты 02–03 — cf collect metrika: нормализация ответа Reporting
# API, upsert дневных/месячных строк в CF UTM Traffic, e-commerce покупки ->
# кандидаты в CF Orders (merge замораживает решённые и ручные строки), честный
# insufficient_data без токена/счётчика, ретраи клиента на 429. Живого API в
# тестах нет — всё за швом фейк-клиента (Testing Decisions спеки).
import httpx
import pytest

import cf.collect.metrika as metrika
from cf.retry import RetryError

from tests.fakes import FakeSheets

NOW = "2026-07-30T08:20:00.000Z"

CONFIG = {"accounts": [
    {"slug": "tiktok-1", "platform": "tiktok", "handle": "@x", "active": True},
    # выключенный слот — всё равно наш аккаунт: строки трафика к нему матчатся
    {"slug": "instagram-1", "platform": "instagram", "handle": "@y",
     "active": False},
]}


def dims(*names):
    return [{"name": n} for n in names]


def daily_item(day, campaign, content, visits, users):
    return {"dimensions": dims(day, campaign, content),
            "metrics": [visits, users]}


def monthly_item(campaign, users, visits):
    return {"dimensions": dims(campaign), "metrics": [users, visits]}


def order_item(purchase, day, source, campaign, content, revenue):
    return {"dimensions": dims(purchase, day, source, campaign, content),
            "metrics": [revenue]}


class FakeMetrika:
    """Скриптованный MetrikaClient: очередь ответов report() + журнал вызовов."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def report(self, date1, date2, dimensions, metrics,
               filters=metrika.UTM_MEDIUM_FILTER):
        self.calls.append({"date1": str(date1), "date2": str(date2),
                           "dimensions": tuple(dimensions),
                           "metrics": tuple(metrics), "filters": filters})
        out = self.responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


# ── Нормализация ответа Метрики ──────────────────────────────────────────────

def test_normalize_daily_rows_ids_accounts_and_reel():
    items = [daily_item("2026-07-29", "tiktok-1", "b42", 12.0, 10.0),
             daily_item("2026-07-29", "tiktok-1", None, 3.0, 2.0),
             daily_item("2026-07-30", "чужая-метка", "z", 1.0, 1.0),
             {"dimensions": dims(None, None, None), "metrics": [5, 5]}]
    rows = metrika.normalize_daily_rows(items, {"tiktok-1"}, NOW)
    assert [r["utm_id"] for r in rows] == [
        "d-2026-07-29-tiktok-1-b42",
        "d-2026-07-29-tiktok-1--",           # без метки ролика content = «-»
        "d-2026-07-30-чужая-метка-z",
    ]
    tagged = rows[0]
    assert tagged == {"utm_id": "d-2026-07-29-tiktok-1-b42",
                      "row_kind": "daily", "date": "2026-07-29",
                      "month": "2026-07", "account": "tiktok-1",
                      "utm_campaign": "tiktok-1", "utm_content": "b42",
                      "reel_id": "b42", "visits": 12, "users": 10,
                      "collected_at": NOW}
    assert rows[1]["reel_id"] == "" and rows[1]["utm_content"] == ""
    assert rows[2]["account"] == ""          # незнакомый campaign — не теряем


def test_normalize_monthly_rows_users_is_first_metric():
    # MONTHLY_METRICS = (users, visits): уники месяца — биллинговая цифра,
    # перепутать порядок = платить за визиты.
    rows = metrika.normalize_monthly_rows(
        [monthly_item("tiktok-1", 100.0, 140.0),
         {"dimensions": dims(None), "metrics": [9, 9]}],
        "2026-07", {"tiktok-1"}, NOW)
    assert rows == [{"utm_id": "m-2026-07-tiktok-1", "row_kind": "monthly",
                     "date": "", "month": "2026-07", "account": "tiktok-1",
                     "utm_campaign": "tiktok-1", "utm_content": "",
                     "reel_id": "", "visits": 140, "users": 100,
                     "collected_at": NOW}]


# ── Нормализация e-commerce покупок (тикет 03) ───────────────────────────────

def test_normalize_order_rows_shape_accounts_and_reel():
    items = [order_item("40129", "2026-07-29", "tiktok", "tiktok-1", "b42",
                        "1990.5"),                    # выручка строкой — num()
             order_item("40130", "2026-07-30", "instagram", "чужая-метка", "",
                        500),
             {"dimensions": dims(None, "2026-07-30", "t", "c", "x"),
              "metrics": [5]}]                        # без purchaseID — мусор
    rows = metrika.normalize_order_rows(items, {"tiktok-1"}, NOW)
    assert [r["order_id"] for r in rows] == ["mk-40129", "mk-40130"]
    assert rows[0] == {"order_id": "mk-40129", "source": "metrika_ecommerce",
                       "order_date": "2026-07-29", "revenue": 1990.5,
                       "utm_source": "tiktok", "utm_campaign": "tiktok-1",
                       "utm_content": "b42", "account": "tiktok-1",
                       "reel_id": "b42", "status": "candidate",
                       "status_changed_at": "", "decided_by": "", "notes": "",
                       "collected_at": NOW}
    # незнакомый campaign не роняет сбор: строка сохраняется с пустым account
    assert rows[1]["account"] == "" and rows[1]["revenue"] == 500
    assert rows[1]["reel_id"] == ""          # без метки ролика reel_id пуст


def test_order_merge_freezes_decided_and_manual_updates_candidates():
    # КРИТИЧНО (деньги): после решения человека сборщик не трогает строку совсем
    # — ни статус, ни выручку; ручные строки заморожены всегда. Кандидат
    # обновляется свежим снапшотом, но поля человека (заметки) не затираются.
    incoming = metrika.normalize_order_rows(
        [order_item("40129", "2026-07-29", "tiktok", "tiktok-1", "b42", 2000)],
        {"tiktok-1"}, "2026-07-31T08:20:00.000Z")[0]
    decided = dict(incoming, revenue=1000, status="confirmed",
                   status_changed_at=NOW, decided_by="human", collected_at=NOW)
    assert metrika.order_merge(incoming, decided) == decided
    manual = dict(incoming, source="manual", status="candidate")
    assert metrika.order_merge(incoming, manual) == manual
    candidate = dict(incoming, revenue=1000, notes="смотрю", collected_at=NOW)
    merged = metrika.order_merge(incoming, candidate)
    assert merged["revenue"] == 2000                      # свежий снапшот
    assert merged["collected_at"] == "2026-07-31T08:20:00.000Z"
    assert merged["status"] == "candidate"
    assert merged["notes"] == "смотрю"                    # поле человека цело
    assert metrika.order_merge(incoming, None) == incoming


# ── collect: запись в CF UTM Traffic ─────────────────────────────────────────

def test_collect_creates_tab_and_writes_both_kinds():
    client = FakeMetrika([
        [daily_item("2026-07-29", "tiktok-1", "b42", 12, 10),
         daily_item("2026-07-30", "смм-эксперимент", "", 2, 2)],
        [monthly_item("tiktok-1", 100, 140),
         monthly_item("instagram-1", 7, 8)],
        [],                                        # e-commerce — покупок нет
    ])
    sheets = FakeSheets(tables={"run_log": []})
    summary = metrika.collect(sheets, client=client, config=CONFIG, now_iso=NOW)
    assert summary["status"] == "success"
    assert summary["daily"] == 2 and summary["monthly"] == 2
    by_id = {r["utm_id"]: r for r in sheets.tables["utm_traffic"]}
    assert by_id["m-2026-07-tiktok-1"]["users"] == 100
    assert by_id["m-2026-07-instagram-1"]["account"] == "instagram-1"
    assert by_id["d-2026-07-29-tiktok-1-b42"]["reel_id"] == "b42"
    # неизвестный campaign: строка сохранена с пустым account и посчитана
    assert by_id["d-2026-07-30-смм-эксперимент--"]["account"] == ""
    assert summary["unknown_campaigns"] == ["смм-эксперимент"]
    run = sheets.tables["run_log"][0]
    assert run["agent"] == "collect-metrika" and run["status"] == "success"
    assert "unknown_campaigns=1" in run["input_summary"]


def test_collect_query_windows_and_cf_organic_filter():
    client = FakeMetrika([[], [], []])
    sheets = FakeSheets(tables={"run_log": []})
    summary = metrika.collect(sheets, client=client, config=CONFIG, now_iso=NOW)
    daily, monthly, ecom = client.calls
    assert daily["date1"] == "2026-07-23" and daily["date2"] == "2026-07-30"
    assert daily["dimensions"] == metrika.DAILY_DIMENSIONS
    assert monthly["date1"] == "2026-07-01" and monthly["date2"] == "2026-07-30"
    assert monthly["metrics"] == (metrika.METRIC_USERS, metrika.METRIC_VISITS)
    assert all(c["filters"] == metrika.UTM_MEDIUM_FILTER
               for c in (daily, monthly))
    # e-commerce: атрибуция «последний значимый», фильтр по нашей метке в той же
    # атрибуции, дозабор — тем же окном, что дневные строки
    assert ecom["dimensions"] == metrika.ECOM_DIMENSIONS
    assert ecom["metrics"] == (metrika.METRIC_REVENUE,)
    assert ecom["filters"] == metrika.ECOM_MEDIUM_FILTER
    assert ecom["date1"] == "2026-07-23" and ecom["date2"] == "2026-07-30"
    # ноль переходов — честный замер, не авария: вкладка есть, статус success
    assert summary["status"] == "success" and summary["rows"] == []


def test_second_run_same_day_overwrites_snapshot_without_duplicates():
    sheets = FakeSheets(tables={"run_log": []})
    for users in (50, 80):
        client = FakeMetrika([
            [daily_item("2026-07-30", "tiktok-1", "b42", 5, users)],
            [monthly_item("tiktok-1", users, users + 40)],
            [],
        ])
        metrika.collect(sheets, client=client, config=CONFIG, now_iso=NOW)
    rows = sheets.tables["utm_traffic"]
    monthly = [r for r in rows if r["row_kind"] == "monthly"]
    assert len(monthly) == 1                 # 2 прогона в день -> 1 строка
    assert monthly[0]["users"] == 80         # свежий снапшот Метрики победил
    assert monthly[0]["visits"] == 120
    daily = [r for r in rows if r["row_kind"] == "daily"]
    assert len(daily) == 1 and daily[0]["users"] == 80


def test_first_days_of_month_backfill_previous_month():
    client = FakeMetrika([
        [],                                        # дневной отчёт
        [monthly_item("tiktok-1", 10, 12)],        # август месяц-к-дате
        [monthly_item("tiktok-1", 200, 260)],      # дозапись целого июля
        [],                                        # e-commerce
    ])
    sheets = FakeSheets(tables={"run_log": []})
    metrika.collect(sheets, client=client, config=CONFIG,
                    now_iso="2026-08-02T08:20:00.000Z")
    assert len(client.calls) == 4
    prev = client.calls[2]
    assert prev["date1"] == "2026-07-01" and prev["date2"] == "2026-07-31"
    by_id = {r["utm_id"]: r for r in sheets.tables["utm_traffic"]}
    assert by_id["m-2026-08-tiktok-1"]["users"] == 10
    assert by_id["m-2026-07-tiktok-1"]["users"] == 200


def test_dry_run_touches_no_tabs_but_leaves_run_log_trace():
    client = FakeMetrika([[daily_item("2026-07-29", "tiktok-1", "", 1, 1)],
                          [monthly_item("tiktok-1", 1, 1)],
                          [order_item("40129", "2026-07-29", "tiktok",
                                      "tiktok-1", "b42", 1990)]])
    sheets = FakeSheets(tables={"run_log": []})
    summary = metrika.collect(sheets, client=client, config=CONFIG,
                              dry_run=True, now_iso=NOW)
    # заказ-кандидат виден в dry-run JSONL (summary["rows"]), вкладки не тронуты
    assert summary["status"] == "success" and len(summary["rows"]) == 3
    assert "utm_traffic" not in sheets.tables
    assert "orders" not in sheets.tables
    assert sheets.tables["run_log"][0]["trigger_type"] == "dry-run"


# ── collect: кандидаты в CF Orders (тикет 03) ────────────────────────────────

def test_collect_upserts_order_candidates_and_counts_them():
    client = FakeMetrika([
        [], [],
        [order_item("40129", "2026-07-29", "tiktok", "tiktok-1", "b42", 1990),
         order_item("40130", "2026-07-30", "instagram", "смм-эксперимент", "",
                    500)],
    ])
    sheets = FakeSheets(tables={"run_log": []})
    summary = metrika.collect(sheets, client=client, config=CONFIG, now_iso=NOW)
    assert summary["status"] == "success" and summary["orders"] == 2
    by_id = {r["order_id"]: r for r in sheets.tables["orders"]}
    assert by_id["mk-40129"]["status"] == "candidate"
    assert by_id["mk-40129"]["account"] == "tiktok-1"
    assert by_id["mk-40129"]["reel_id"] == "b42"
    # незнакомый campaign сохранён с пустым account и посчитан предупреждением
    assert by_id["mk-40130"]["account"] == ""
    assert summary["unknown_campaigns"] == ["смм-эксперимент"]
    assert "orders=2" in sheets.tables["run_log"][0]["input_summary"]


def test_second_collect_refreshes_candidates_without_duplicates():
    sheets = FakeSheets(tables={"run_log": []})
    for revenue in (1000, 1500):
        client = FakeMetrika([[], [], [order_item(
            "40129", "2026-07-29", "tiktok", "tiktok-1", "b42", revenue)]])
        metrika.collect(sheets, client=client, config=CONFIG, now_iso=NOW)
    orders = sheets.tables["orders"]
    assert len(orders) == 1                        # 2 прогона -> 1 строка
    assert orders[0]["revenue"] == 1500            # кандидат — свежий снапшот
    assert orders[0]["status"] == "candidate"


def test_collect_never_touches_decided_or_manual_rows():
    # ЗАМОРОЗКА (деньги): решённый заказ не возвращается в candidate, его
    # revenue не уплывает за свежим снапшотом; ручная строка переживает сбор
    # нетронутой.
    decided = {"order_id": "mk-40129", "source": "metrika_ecommerce",
               "order_date": "2026-07-29", "revenue": 1000,
               "utm_source": "tiktok", "utm_campaign": "tiktok-1",
               "utm_content": "b42", "account": "tiktok-1", "reel_id": "b42",
               "status": "confirmed", "status_changed_at": NOW,
               "decided_by": "human", "notes": "оплата пришла",
               "collected_at": NOW}
    manual = {"order_id": "manual-abc", "source": "manual",
              "order_date": "2026-07-28", "revenue": 3500, "utm_source": "",
              "utm_campaign": "", "utm_content": "", "account": "tiktok-1",
              "reel_id": "", "status": "confirmed", "status_changed_at": NOW,
              "decided_by": "human", "notes": "перевод на карту",
              "collected_at": NOW}
    sheets = FakeSheets(tables={"run_log": [],
                                "orders": [dict(decided), dict(manual)]},
                        headers={"orders": list(metrika.ORDER_HEADERS)})
    client = FakeMetrika([[], [], [order_item(
        "40129", "2026-07-29", "tiktok", "tiktok-1", "b42", 9999)]])
    metrika.collect(sheets, client=client, config=CONFIG,
                    now_iso="2026-07-31T08:20:00.000Z")
    assert len(sheets.tables["orders"]) == 2       # дублей нет
    by_id = {r["order_id"]: r for r in sheets.tables["orders"]}
    assert by_id["mk-40129"] == decided            # заморожен целиком
    assert by_id["manual-abc"] == manual           # ручная строка не тронута


# ── Честные отказы ───────────────────────────────────────────────────────────

def test_missing_counter_and_token_is_insufficient_data(tmp_path):
    config = {"metrika": {"token_file": str(tmp_path / "нет-токена.txt")}}
    sheets = FakeSheets(tables={"run_log": []})
    printed = []
    summary = metrika.collect(sheets, config=config, log=printed.append)
    assert summary["status"] == "insufficient_data"
    assert len(summary["missing"]) == 2
    assert "counter_id" in summary["error"] and "токен" in summary["error"]
    run = sheets.tables["run_log"][0]
    assert run["status"] == "insufficient_data"
    assert "counter_id" in run["input_summary"]
    assert "utm_traffic" not in sheets.tables
    assert "orders" not in sheets.tables
    assert printed and "не хватает" in printed[0]


def test_missing_prereqs_empty_when_configured(tmp_path):
    tok = tmp_path / "metrika-token.txt"
    tok.write_text("secret\n", encoding="utf-8")
    cfg = {"metrika": {"token_file": str(tok), "counter_id": "99"}}
    assert metrika.missing_prereqs(cfg) == []


def test_api_error_is_failed_run_and_nothing_written():
    client = FakeMetrika([RetryError("metrika report failed after 3 attempts")])
    sheets = FakeSheets(tables={"run_log": []})
    summary = metrika.collect(sheets, client=client, config=CONFIG, now_iso=NOW)
    assert summary["status"] == "failed"
    assert "Metrika API" in summary["error"]
    assert sheets.tables["run_log"][0]["status"] == "failed"
    assert "utm_traffic" not in sheets.tables
    assert "orders" not in sheets.tables


def test_run_log_agent_has_dashboard_label():
    # Подпись в лентах дашборда — человеческая, слаг не утекает продюсеру.
    from cf.dashboard.labels import agent_label
    assert agent_label("collect-metrika") != "collect-metrika"


# ── MetrikaClient: ретраи и пагинация ────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self)


class FakeHttp:
    """Скриптованный httpx-клиент: очередь ответов на get + журнал вызовов."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


def page(data, total_rows=None):
    payload = {"data": data}
    if total_rows is not None:
        payload["total_rows"] = total_rows
    return FakeResponse(200, payload)


def make_client(http, **kw):
    kw.setdefault("retry_delay", 0)
    return metrika.MetrikaClient("tok", "42", client=http, **kw)


def test_client_sends_counter_and_paginates_until_total():
    http = FakeHttp([page([daily_item("2026-07-29", "a", "", 1, 1),
                           daily_item("2026-07-29", "b", "", 1, 1)],
                          total_rows=3),
                     page([daily_item("2026-07-29", "c", "", 1, 1)],
                          total_rows=3)])
    rows = make_client(http).report("2026-07-23", "2026-07-30",
                                    metrika.DAILY_DIMENSIONS,
                                    metrika.DAILY_METRICS)
    assert len(rows) == 3
    (url1, p1), (url2, p2) = http.calls
    assert url1 == url2 == metrika.REPORT_PATH
    assert p1["ids"] == "42" and p1["offset"] == 1
    assert p2["offset"] == 3                    # offset Метрики 1-based
    assert p1["filters"] == metrika.UTM_MEDIUM_FILTER
    assert p1["dimensions"] == "ym:s:date,ym:s:UTMCampaign,ym:s:UTMContent"
    assert p1["metrics"] == "ym:s:visits,ym:s:users"


def test_client_retries_429_with_retry_after(monkeypatch):
    naps = []
    monkeypatch.setattr("cf.retry._sleep", naps.append)
    http = FakeHttp([FakeResponse(429, headers={"Retry-After": "2"}),
                     page([monthly_item("a", 1, 1)], total_rows=1)])
    rows = make_client(http).report("2026-07-01", "2026-07-30",
                                    metrika.MONTHLY_DIMENSIONS,
                                    metrika.MONTHLY_METRICS)
    assert len(rows) == 1
    assert len(http.calls) == 2                 # 429 переждали и повторили
    assert naps == [2.0]                        # пауза из Retry-After


def test_client_4xx_other_than_429_is_permanent(monkeypatch):
    naps = []
    monkeypatch.setattr("cf.retry._sleep", naps.append)
    http = FakeHttp([FakeResponse(403)])
    with pytest.raises(httpx.HTTPStatusError):
        make_client(http).report("2026-07-01", "2026-07-30",
                                 metrika.MONTHLY_DIMENSIONS,
                                 metrika.MONTHLY_METRICS)
    assert len(http.calls) == 1 and naps == []  # битый токен ретраем не лечится


def test_client_from_config_reads_token_file(tmp_path):
    tok = tmp_path / "metrika-token.txt"
    tok.write_text("secret-tok\n", encoding="utf-8")
    cfg = {"metrika": {"token_file": str(tok), "counter_id": 12345,
                       "http_timeout": 7}}
    client = metrika.client_from_config(cfg)
    assert client.counter_id == "12345"
    assert client._client.headers["Authorization"] == "OAuth secret-tok"
