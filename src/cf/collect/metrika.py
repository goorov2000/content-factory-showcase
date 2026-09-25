"""cf collect metrika — переходы и покупки из Яндекс.Метрики (UTM-контур, 02–03).

Три класса запросов Reporting API с фильтром по нашей метке cf-organic:
(1) дневные визиты/уники в разрезе date × utm_campaign × utm_content;
(2) уникальные посетители месяца по utm_campaign — биллинговая цифра схемы
оплаты, она НИКОГДА не считается суммой дневных уников (один человек за месяц
приходит в разные дни). Обе пишутся в CF UTM Traffic upsert'ом по utm_id;
merge — перезапись: строки этой вкладки — снапшоты Метрики, не решения человека.
(3) e-commerce покупки -> КАНДИДАТЫ в реестр CF Orders (status=candidate).
У заказов merge противоположный: строки со статусом решения человека и все
ручные строки ЗАМОРОЖЕНЫ — сборщик их не трогает совсем, включая revenue
(после решения цифра, с которой считаются 10%, уплыть не может). Свежий
снапшот обновляет только кандидатов, и то не затирая поля человека.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from cf.collect.util import _iso_ms, _iso_seconds, _parse_date_string, num
from cf.config import accounts_from_config
from cf.retry import with_retry
from cf.runlog import log_run

DEFAULT_BASE_URL = "https://api-metrika.yandex.net"
REPORT_PATH = "/stat/v1/data"
DEFAULT_TOKEN_FILE = "~/.cf/secrets/metrika-token.txt"

# Словарь запросов Reporting API — в одном месте: на приёмке с живым токеном
# имена dimensions/metrics правятся здесь, а не поиском по коду.
DIM_DATE = "ym:s:date"
DIM_CAMPAIGN = "ym:s:UTMCampaign"
DIM_CONTENT = "ym:s:UTMContent"
METRIC_VISITS = "ym:s:visits"
METRIC_USERS = "ym:s:users"
# Метка завода в utm_medium — единственный экземпляр токена: его же ставит
# генератор ссылок для bio (cf.payout.bio_links), а фильтры ниже собираются
# из него — конвенция ссылок и фильтр сбора разъехаться не могут.
UTM_MEDIUM = "cf-organic"
UTM_MEDIUM_FILTER = f"ym:s:UTMMedium=='{UTM_MEDIUM}'"

DAILY_DIMENSIONS = (DIM_DATE, DIM_CAMPAIGN, DIM_CONTENT)
DAILY_METRICS = (METRIC_VISITS, METRIC_USERS)
MONTHLY_DIMENSIONS = (DIM_CAMPAIGN,)
MONTHLY_METRICS = (METRIC_USERS, METRIC_VISITS)

# E-commerce покупки (тикет 03): UTM ласт-клика в атрибуции «последний
# значимый» (lastsign*) — точная модель подтверждается на приёмке с живым
# токеном; если счётчик отдаёт другую (last*/first*), правится ЗДЕСЬ, в одном
# месте. Фильтр — по той же атрибуции, что и dimensions: покупка без нашей
# метки заводу не нужна, но существование строки заказа фильтром НЕ
# обусловлено — отбор делает сам API, а не проверка полей ответа.
DIM_PURCHASE = "ym:s:purchaseID"
DIM_ECOM_SOURCE = "ym:s:lastsignUTMSource"
DIM_ECOM_CAMPAIGN = "ym:s:lastsignUTMCampaign"
DIM_ECOM_CONTENT = "ym:s:lastsignUTMContent"
METRIC_REVENUE = "ym:s:ecommerceRevenue"
ECOM_MEDIUM_FILTER = f"ym:s:lastsignUTMMedium=='{UTM_MEDIUM}'"

ECOM_DIMENSIONS = (DIM_PURCHASE, DIM_DATE, DIM_ECOM_SOURCE,
                   DIM_ECOM_CAMPAIGN, DIM_ECOM_CONTENT)
ECOM_METRICS = (METRIC_REVENUE,)

# Дневные строки перезабираются окном назад: данные Метрики дозревают, свежий
# снапшот перезаписывает вчерашний. Месяц прошлый дозаписывается в первые дни
# нового — хвост месяца доезжает в отчёты с опозданием.
DAILY_LOOKBACK_DAYS = 7
MONTH_BACKFILL_DAYS = 3
PAGE_LIMIT = 10000

HEADERS = ["utm_id", "row_kind", "date", "month", "account", "utm_campaign",
           "utm_content", "reel_id", "visits", "users", "collected_at"]

# Схема реестра CF Orders (источник истины по деньгам, тикет 03). Вкладка
# никогда не ротируется; статусы candidate|confirmed|returned|rejected меняет
# ТОЛЬКО человек в пульте (src/cf/dashboard/actions.py), сборщик пишет лишь
# candidate. source: metrika_ecommerce | manual (позже kit_api/site_webhook).
ORDER_HEADERS = ["order_id", "source", "order_date", "revenue", "utm_source",
                 "utm_campaign", "utm_content", "account", "reel_id", "status",
                 "status_changed_at", "decided_by", "notes", "collected_at"]

# Что сборщику МОЖНО обновить у кандидата свежим снапшотом. Полей решения
# человека (status, status_changed_at, decided_by, notes) здесь нет намеренно:
# их сборщик не трогает ни у кого, даже у кандидатов.
ORDER_SNAPSHOT_FIELDS = ("source", "order_date", "revenue", "utm_source",
                         "utm_campaign", "utm_content", "account", "reel_id",
                         "collected_at")


class MetrikaClient:
    """GET /stat/v1/data с OAuth-заголовком, ретраями и пагинацией.

    client= — шов для тестов (как у ApifyClient): живого API в сюите нет.
    """

    def __init__(self, token, counter_id, client=None, base_url=DEFAULT_BASE_URL,
                 http_timeout=30.0, retry_delay=1.0):
        self.counter_id = str(counter_id)
        self._client = client or httpx.Client(
            base_url=base_url, timeout=http_timeout,
            headers={"Authorization": f"OAuth {token}"})
        self.retry_delay = retry_delay

    def report(self, date1, date2, dimensions, metrics, filters=UTM_MEDIUM_FILTER):
        """Все строки отчёта (постранично): [{dimensions: [...], metrics: [...]}].

        offset у Метрики 1-based; конец — пустая страница или добор total_rows.
        429/5xx/сеть ретраятся with_retry (Retry-After уважается), 4xx кроме
        429 — постоянная ошибка, всплывает сразу.
        """
        rows, offset = [], 1
        while True:
            params = {"ids": self.counter_id, "date1": str(date1),
                      "date2": str(date2), "dimensions": ",".join(dimensions),
                      "metrics": ",".join(metrics), "filters": filters,
                      "limit": PAGE_LIMIT, "offset": offset}

            def op():
                resp = self._client.get(REPORT_PATH, params=params)
                resp.raise_for_status()
                return resp
            payload = with_retry(op, base_delay=self.retry_delay,
                                 label=f"metrika report {date1}..{date2}").json()
            page = payload.get("data") or []
            rows.extend(page)
            offset += len(page)
            total = payload.get("total_rows")
            if not page or (isinstance(total, int) and offset > total):
                return rows


def client_from_config(config, **overrides):
    cfg = (config or {}).get("metrika", {})
    token_path = Path(cfg.get("token_file", DEFAULT_TOKEN_FILE)).expanduser()
    kw = {"http_timeout": cfg.get("http_timeout", 30.0)}
    kw.update(overrides)
    return MetrikaClient(token_path.read_text(encoding="utf-8").strip(),
                         cfg.get("counter_id", ""), **kw)


def missing_prereqs(config):
    """Чего не хватает для похода в API — перечень честного insufficient_data."""
    cfg = (config or {}).get("metrika") or {}
    missing = []
    if not str(cfg.get("counter_id") or "").strip():
        missing.append("metrika.counter_id в cf.config.json")
    token_path = Path(cfg.get("token_file") or DEFAULT_TOKEN_FILE).expanduser()
    if not token_path.exists():
        missing.append(f"файл OAuth-токена {token_path}")
    return missing


def _dim(item, i):
    dims = item.get("dimensions") or []
    if i >= len(dims):
        return ""
    name = (dims[i] or {}).get("name")
    return str(name).strip() if name is not None else ""


def _metric(item, i):
    values = item.get("metrics") or []
    return num(values[i]) if i < len(values) else 0


def _row(kind, utm_id, campaign, known_slugs, collected_at, **extra):
    row = {"utm_id": utm_id, "row_kind": kind, "date": "", "month": "",
           "account": campaign if campaign in known_slugs else "",
           "utm_campaign": campaign, "utm_content": "", "reel_id": "",
           "visits": 0, "users": 0, "collected_at": collected_at}
    row.update(extra)
    return row


def normalize_daily_rows(items, known_slugs, collected_at):
    """Ответ дневного отчёта -> строки CF UTM Traffic (row_kind=daily).

    utm_content = метка ролика (reel_id), когда ссылка стояла под роликом;
    без метки content в ключе — «-». Строки без даты или campaign — мусор вне
    нашей UTM-конвенции, пропускаются.
    """
    rows = []
    for item in items:
        day, campaign, content = _dim(item, 0), _dim(item, 1), _dim(item, 2)
        if not day or not campaign:
            continue
        rows.append(_row(
            "daily", f"d-{day}-{campaign}-{content or '-'}", campaign,
            known_slugs, collected_at, date=day, month=day[:7],
            utm_content=content, reel_id=content,
            visits=_metric(item, 0), users=_metric(item, 1)))
    return rows


def normalize_monthly_rows(items, month, known_slugs, collected_at):
    """Ответ месячного отчёта -> строки month × campaign (row_kind=monthly).

    users — первая метрика запроса (MONTHLY_METRICS): именно уники месяца
    оплачиваются, перепутать порядок = платить за визиты.
    """
    rows = []
    for item in items:
        campaign = _dim(item, 0)
        if not campaign:
            continue
        rows.append(_row(
            "monthly", f"m-{month}-{campaign}", campaign, known_slugs,
            collected_at, month=month,
            users=_metric(item, 0), visits=_metric(item, 1)))
    return rows


def normalize_order_rows(items, known_slugs, collected_at):
    """Ответ e-commerce отчёта -> кандидаты CF Orders (status=candidate).

    order_id = "mk-" + purchaseID; строка без purchaseID — мусор без ключа,
    пропускается. Пустые UTM-поля строку НЕ отсеивают (отбор по метке делает
    фильтр запроса): незнакомый campaign даёт пустой account, отсутствие метки
    ролика — пустой reel_id. revenue через num(): Метрика отдаёт и строки.
    """
    rows = []
    for item in items:
        purchase = _dim(item, 0)
        if not purchase:
            continue
        campaign, content = _dim(item, 3), _dim(item, 4)
        rows.append({
            "order_id": f"mk-{purchase}", "source": "metrika_ecommerce",
            "order_date": _dim(item, 1), "revenue": _metric(item, 0),
            "utm_source": _dim(item, 2), "utm_campaign": campaign,
            "utm_content": content,
            "account": campaign if campaign in known_slugs else "",
            "reel_id": content, "status": "candidate",
            "status_changed_at": "", "decided_by": "", "notes": "",
            "collected_at": collected_at})
    return rows


def order_merge(incoming, existing):
    """Merge-хук upsert'а заказов — КРИТИЧНАЯ семантика (деньги).

    Решённые человеком строки (status != candidate) и все ручные
    (source=manual) заморожены целиком: сборщик не меняет в них ничего,
    включая revenue, и не возвращает их в candidate. У кандидата обновляются
    только снапшотные поля (ORDER_SNAPSHOT_FIELDS) — поля решения человека
    не затираются даже пустыми значениями свежего кандидата. Незнакомое
    состояние (пустой/чужой status) читается как «не кандидат», то есть тоже
    заморожено: при сомнении деньги не трогаем.
    """
    if existing is None:
        return dict(incoming)
    if (str(existing.get("source") or "").strip().lower() == "manual"
            or str(existing.get("status") or "").strip().lower() != "candidate"):
        return dict(existing)
    return {**existing, **{k: incoming[k] for k in ORDER_SNAPSHOT_FIELDS}}


def collect(sheets, client=None, config=None, dry_run=False, now_iso=None,
            log=None):
    """Полный сбор: Reporting API -> CF UTM Traffic (upsert) + run_log.

    client=None — боевой путь: клиент собирается из конфига, а отсутствие
    counter_id/файла токена даёт честный insufficient_data с перечнем
    недостающего (не падение). Инжектированный client — шов тестов, он несёт
    counter сам, поэтому проверка конфига не выполняется.
    """
    trigger = "dry-run" if dry_run else "cli"
    collected = now_iso or _iso_ms(datetime.now(timezone.utc))
    run_started = _iso_seconds(collected)
    today = (_parse_date_string(str(collected))
             or datetime.now(timezone.utc)).date()

    if client is None:
        missing = missing_prereqs(config)
        if missing:
            summary = "не хватает: " + "; ".join(missing)
            if log:
                log(summary)
            log_run(sheets, "collect-metrika", "insufficient_data",
                    input_summary=summary, trigger_type=trigger,
                    started_at=run_started)
            return {"status": "insufficient_data", "error": summary,
                    "missing": missing, "rows": []}
        client = client_from_config(config)

    known_slugs = {a["slug"] for a in accounts_from_config(config)}
    month_first = today.replace(day=1)
    try:
        daily_raw = client.report(today - timedelta(days=DAILY_LOOKBACK_DAYS),
                                  today, DAILY_DIMENSIONS, DAILY_METRICS)
        months = [(f"{today:%Y-%m}",
                   client.report(month_first, today, MONTHLY_DIMENSIONS,
                                 MONTHLY_METRICS))]
        if today.day <= MONTH_BACKFILL_DAYS:
            prev_last = month_first - timedelta(days=1)
            months.append((f"{prev_last:%Y-%m}",
                           client.report(prev_last.replace(day=1), prev_last,
                                         MONTHLY_DIMENSIONS, MONTHLY_METRICS)))
        # E-commerce покупки — кандидаты в заказы; окно то же, что у дневных
        # строк: данные дозревают, дозабор перекрывает лаг доставки событий.
        orders_raw = client.report(today - timedelta(days=DAILY_LOOKBACK_DAYS),
                                   today, ECOM_DIMENSIONS, ECOM_METRICS,
                                   filters=ECOM_MEDIUM_FILTER)
    except Exception as exc:
        summary = f"Metrika API: {exc}"
        if log:
            log(summary)
        log_run(sheets, "collect-metrika", "failed", input_summary=summary,
                errors=[str(exc)], trigger_type=trigger, started_at=run_started)
        return {"status": "failed", "error": summary, "rows": []}

    rows = normalize_daily_rows(daily_raw, known_slugs, collected)
    for month, items in months:
        rows += normalize_monthly_rows(items, month, known_slugs, collected)
    orders = normalize_order_rows(orders_raw, known_slugs, collected)
    # Незнакомый campaign — строка сохраняется с пустым account (данные важнее
    # реестра), но попадает в счётчик предупреждений: либо дыра в accounts,
    # либо кто-то размечает ссылки мимо конвенции. Заказы считаются тем же
    # счётчиком (кампания без campaign — не предупреждение, а пустая метка).
    unknown = sorted({r["utm_campaign"] for r in rows + orders
                      if r["utm_campaign"] and not r["account"]})

    if not dry_run:
        sheets.ensure_tab("utm_traffic", HEADERS)
        sheets.upsert_rows("utm_traffic", "utm_id", rows)
        # Заказы — НЕ снапшот-вкладка: order_merge замораживает решённые и
        # ручные строки, свежий снапшот обновляет только кандидатов.
        sheets.ensure_tab("orders", ORDER_HEADERS)
        sheets.upsert_rows("orders", "order_id", orders, merge=order_merge)

    daily_n = sum(1 for r in rows if r["row_kind"] == "daily")
    summary = (f"daily={daily_n} monthly={len(rows) - daily_n} "
               f"orders={len(orders)} unknown_campaigns={len(unknown)}")
    if unknown:
        summary += " (" + ", ".join(unknown) + ")"
    if not rows:
        summary += " — переходов по cf-organic не найдено"
    if log:
        log(summary)
    log_run(sheets, "collect-metrika", "success", input_summary=summary,
            trigger_type=trigger, started_at=run_started)
    # rows отдаёт всё привезённое (в dry-run это содержимое JSONL): строки
    # трафика и кандидатов различаются по схеме (utm_id против order_id).
    return {"status": "success", "rows": rows + orders, "daily": daily_n,
            "monthly": len(rows) - daily_n, "orders": len(orders),
            "unknown_campaigns": unknown}
