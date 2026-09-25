"""Apify-клиент: асинхронный start + poll вместо run-sync (спека §2).

Снимает потолок 300 с на весь вход и даёт параллельные батчи. Сбой батча —
loss-маркер того же формата, что в n8n (P5.14): гейт считает потери, остальные
батчи доезжают. Терминальные статусы и таймаут поллинга — новые режимы отказа,
run-sync их не имел.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from cf.retry import with_retry

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}
DEFAULT_BASE_URL = "https://api.apify.com"

# Обрыв рана на стороне Apify — повтор осмыслен: причина не в наших данных, а в
# том, что ран не доехал (вытеснен, упёрся в собственный таймаут, брошен нами по
# poll_timeout). FAILED сюда СОЗНАТЕЛЬНО не входит: актор, упавший на разборе
# входа, упадёт и во второй раз — это платный повтор ради того же результата.
RETRIABLE_STATUSES = {"ABORTED", "TIMED-OUT", "TIMED_OUT", "POLL-TIMEOUT"}
DEFAULT_BATCH_ATTEMPTS = 2      # одна попытка + один повтор
BATCH_RETRY_DELAY = 5.0         # пауза перед повтором, с


@dataclass
class RunResult:
    status: str
    items: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self):
        return self.status == "SUCCEEDED"


class ApifyClient:
    def __init__(self, token, client=None, base_url=DEFAULT_BASE_URL,
                 http_timeout=30.0, poll_interval=5.0, poll_timeout=900.0,
                 retry_delay=1.0, sleep=time.sleep, monotonic=time.monotonic):
        self._client = client or httpx.Client(
            base_url=base_url, timeout=http_timeout,
            headers={"Authorization": f"Bearer {token}"})
        # Токен публичен для контура субтитров: hashtag-scraper кладёт .vtt в
        # приватный KV-store рана, скачивание без подписи — 403 (смоук 10.08).
        self.token = token
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.retry_delay = retry_delay
        self._sleep = sleep
        self._monotonic = monotonic

    def _request(self, label, fn):
        def op():
            resp = fn()
            resp.raise_for_status()
            return resp
        # httpx.HTTPStatusError несёт response — 4xx (кроме 429) не ретраится
        # силами retry._permanent_api_error, сетевые/5xx/429 ретраятся.
        return with_retry(op, base_delay=self.retry_delay, label=label)

    def _start_request(self, label, fn):
        # M29: POST старта рана НЕидемпотентен — ретраим только когда ран
        # доказуемо не создан: соединение не установилось (ConnectError/
        # ConnectTimeout) либо пришёл ответ (HTTPStatusError; 4xx/429 решает
        # _permanent_api_error). Потерянный ПОСЛЕ отправки ответ (ReadTimeout,
        # обрыв) не ретраим: повтор запускал бы второй платный ран-сироту.
        def op():
            resp = fn()
            resp.raise_for_status()
            return resp
        return with_retry(op, base_delay=self.retry_delay, label=label,
                          retriable=(httpx.ConnectError, httpx.ConnectTimeout,
                                     httpx.HTTPStatusError))

    def _abort_run(self, run_id, reason=""):
        """Best-effort POST abort (M30): брошенный ран иначе продолжает жечь
        кредиты до собственного таймаута актора, а его данные никто не заберёт."""
        if not run_id:
            return
        try:
            self._client.post(f"/v2/actor-runs/{run_id}/abort")
        except Exception:  # noqa: BLE001 — abort не важнее основного результата
            pass

    def run_actor(self, actor_path, payload):
        resp = self._start_request(
            f"apify start {actor_path}",
            lambda: self._client.post(f"/v2/acts/{actor_path}/runs", json=payload))
        data = resp.json().get("data", {})
        run_id = data.get("id")
        dataset_id = data.get("defaultDatasetId", "")

        started = self._monotonic()
        status = data.get("status", "")
        while status not in TERMINAL_STATUSES:
            if self._monotonic() - started >= self.poll_timeout:
                self._abort_run(run_id, "poll timeout")
                return RunResult(status="POLL-TIMEOUT",
                                 error=f"apify poll timeout after {self.poll_timeout}s "
                                       f"({actor_path}, run {run_id})")
            self._sleep(self.poll_interval)
            try:
                resp = self._request(
                    f"apify poll {run_id}",
                    lambda: self._client.get(f"/v2/actor-runs/{run_id}"))
            except Exception:
                # поллинг умер (RetryError и пр.) — ран не бросаем живым
                self._abort_run(run_id, "poll failed")
                raise
            data = resp.json().get("data", {})
            status = data.get("status", "")
            dataset_id = data.get("defaultDatasetId") or dataset_id

        if status != "SUCCEEDED":
            return RunResult(status=status, error=f"apify run {status} ({actor_path})")
        if not dataset_id:
            return RunResult(status=status)
        resp = self._request(
            f"apify items {dataset_id}",
            lambda: self._client.get(f"/v2/datasets/{dataset_id}/items",
                                     params={"clean": "true", "format": "json"}))
        items = resp.json()
        return RunResult(status=status, items=items if isinstance(items, list) else [])


def _loss_marker(batch, message):
    # Формат маркера — как у n8n-нормализаторов: гейт считает batches_failed
    # по batch_index и sources_lost по batch_sources.
    return {
        "__apify_error": str(message) or "apify batch failed",
        "source_query": "unknown",
        "batch_index": batch.get("batch_index"),
        "batch_sources": list(batch.get("batch_sources") or []),
    }


def run_batches(batches, runner, max_workers=2, on_done=None,
                attempts=DEFAULT_BATCH_ATTEMPTS, retry_delay=BATCH_RETRY_DELAY,
                sleep=time.sleep):
    """Гоняет runner(batch) по батчам параллельно. Возвращает
    ([(batch, items), ...] в исходном порядке, [loss_marker, ...]).

    on_done(done, total) — колбэк после КАЖДОГО завершённого батча (успех или
    потеря): единственная настоящая гранулярность внутри сбора, из неё дашборд
    рисует живой прогресс (спека §7 timeline). Считаем по факту готовности
    future, а не по порядку батчей, поэтому счётчик не «залипает» из-за медленного
    первого батча. Сбой самого колбэка сбор не роняет — прогресс не важнее данных.

    attempts — сколько РАЗ пробуем батч, у которого ран оборвался на стороне
    Apify (разбор 2026-07-27). Порт с n8n потерял половину контракта эталонного
    узла: «continue» (loss-маркер) перенесли, а retryOnFail=true/maxTries=2 —
    нет, и обрыв актора стоил суток простоя источникам батча. Ретраим ТОЛЬКО
    RETRIABLE_STATUSES: там ран уже создан и оплачен, повтор ничего не удваивает.
    Исключение (отказ СТАРТА: 401/402/429/сеть) не ретраим здесь никогда — при
    упёртом лимите Apify, как в ночь 27.07, повтор лишь удвоил бы бесполезные
    запросы, а на потерянном ответе создал бы второй платный ран-сироту. Ретраем
    сетевых сбоев старта заведует _start_request/with_retry (M29), и только он.
    """
    attempts = max(1, int(attempts))

    def safe(batch):
        message, tries = None, 0
        for attempt in range(attempts):
            try:
                tries += 1
                out = runner(batch)
            except Exception as exc:
                return None, str(exc)      # отказ старта — см. докстринг
            if not isinstance(out, RunResult):
                return out, None
            if out.ok:
                return out.items, None
            message = out.error or out.status
            if out.status not in RETRIABLE_STATUSES or attempt + 1 >= attempts:
                break
            sleep(retry_delay)
        # Число ФАКТИЧЕСКИХ попыток уходит в текст потери: иначе по Run Log не
        # отличить «упало сразу» от «упало и после повтора» — диагнозы разные.
        if tries > 1 and message:
            message = f"{message} [попыток: {tries}]"
        return None, message

    total = len(batches)
    results, losses = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(safe, b) for b in batches]
        if on_done is not None:
            done_count = 0
            for _ in as_completed(futures):
                done_count += 1
                try:
                    on_done(done_count, total)
                except Exception:      # noqa: BLE001 — прогресс не ломает сбор
                    pass
        # порядок результатов — исходный порядок батчей (гейт считает по batch_index)
        for batch, fut in zip(batches, futures):
            items, err = fut.result()
            if err is not None:
                losses.append(_loss_marker(batch, err))
            else:
                results.append((batch, items))
    return results, losses


def client_from_config(config, **overrides):
    cfg = (config or {}).get("apify", {})
    token_path = Path(cfg.get("token_file", "~/.cf/secrets/apify-token.txt")).expanduser()
    kw = {
        "base_url": cfg.get("base_url", DEFAULT_BASE_URL),
        "poll_interval": cfg.get("poll_interval", 5.0),
        "poll_timeout": cfg.get("poll_timeout", 900.0),
        "http_timeout": cfg.get("http_timeout", 30.0),
    }
    kw.update(overrides)
    return ApifyClient(token_path.read_text(encoding="utf-8").strip(), **kw)
