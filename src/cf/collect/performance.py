"""cf collect performance — порт 1:1 cf04-performance (CF 04).

Build-Apify-Metric-Requests.js: по published-строкам CF Published Reels строит
запросы на дозамер метрик (TikTok/IG — разные акторы; у reel-scraper reel-URL
передаётся в поле username — странность актора). Normalize-Performance-Rows.js:
матчит ответ к запросу по нормализованному URL и пишет строку CF Performance.
result_label остаётся 'unknown' — оценка дело eval-агента, не сбора.

Хелперы cf04 отличаются от Normalize-раскладки (num с запятой-десятичной,
text со stringify объектов, свой normalizeUrl) — они здесь, не в util.
"""
import json
import re
from datetime import datetime, timezone

from cf.collect.util import (_iso_ms, _iso_seconds, _parse_date_string, first,
                             js_round, stable_hash)
from cf.runlog import log_run

# display-имя и path акторов дозамера (эталон: apify_url в Build-JS)
# Маркер сбойного замера (H16): строка с views=0 из упавшего Apify-рана несёт
# его в eval_notes — eval-датасет обязан отличать такой «замер» от честного нуля.
METRICS_EMPTY = "metrics_empty_or_unavailable"
FAILED_MEASUREMENT_MARKER = "collection_status=" + METRICS_EMPTY

ACTORS = {
    "tiktok": ("clockworks/tiktok-scraper", "clockworks~tiktok-scraper"),
    "instagram": ("apify/instagram-reel-scraper", "apify~instagram-reel-scraper"),
}

_IG_URL = re.compile(r"instagram\.com/(?:p|reel|reels)/([^/?#]+)", re.I)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _num(value):
    # cf04-вариант: запятая как десятичный разделитель (ru-локаль Sheets)
    try:
        n = float(str(value if value is not None else "").replace(",", "."))
    except ValueError:
        return 0
    if n != n or n in (float("inf"), float("-inf")):
        return 0
    return int(n) if n.is_integer() else n


def perf_normalize_url(value):
    url = _text(value)
    if not url:
        return ""
    m = _IG_URL.search(url)
    if m:
        return "https://www.instagram.com/reel/" + m.group(1) + "/"
    return url.split("?")[0]


def _platform_for(row, url):
    platform = _text(row.get("platform")).lower()
    if platform in ("tiktok", "instagram"):
        return platform
    if re.search(r"tiktok\.com", url, re.I):
        return "tiktok"
    if re.search(r"instagram\.com", url, re.I):
        return "instagram"
    return ""


def _to_iso(value):
    # cf04 toIso: только new Date(value) — числовые epoch-строки НЕ парсятся
    if not value or not isinstance(value, str):
        return ""
    dt = _parse_date_string(value.strip())
    return _iso_ms(dt) if dt else ""


def build_metric_requests(rows):
    requests = []
    for row in rows:
        status = _text(row.get("status") or "published").lower()
        post_url = perf_normalize_url(first(row.get("post_url"), row.get("published_url"),
                                            row.get("url")))
        platform = _platform_for(row, post_url)
        brief_id = _text(row.get("brief_id"))
        prompt_version = _text(row.get("prompt_version"))
        if status != "published" or not post_url or not platform \
                or not brief_id or not prompt_version:
            continue
        published_id = _text(row.get("published_id")) or \
            "pub_" + platform + "_" + stable_hash(brief_id + post_url)
        display, actor_path = ACTORS[platform]
        if platform == "instagram":
            payload = {"username": [post_url], "resultsLimit": 1,
                       "includeTranscript": False, "includeDownloadedVideo": False,
                       "includeSharesCount": True, "skipPinnedPosts": True,
                       "skipTrialReels": True}
        else:
            # shouldDownloadSubtitles вендор удалил из схемы (миграция на
            # downloadSubtitlesOptions, сверка 2026-08-10); performance субтитры
            # не нужны — поле не шлём вовсе (дефолт NEVER_DOWNLOAD_SUBTITLES).
            payload = {"postURLs": [post_url], "resultsPerPage": 1,
                       "shouldDownloadVideos": False, "shouldDownloadCovers": False,
                       "shouldDownloadSlideshowImages": False,
                       "scrapeRelatedVideos": False, "proxyCountryCode": "None"}
        requests.append({
            "published_id": published_id,
            "brief_id": brief_id,
            "prompt_version": prompt_version,
            "platform": platform,
            "post_url": post_url,
            "published_at": _to_iso(row.get("published_at")),
            "creator": _text(row.get("creator")),
            "content_owner": _text(row.get("content_owner")),
            "source_actor": display,
            "actor_path": actor_path,
            "payload": payload,
        })
    return requests


def _raw_url(raw):
    return perf_normalize_url(first(raw.get("url"), raw.get("webVideoUrl"),
                                    raw.get("shareUrl"), raw.get("inputUrl"),
                                    raw.get("reelUrl"), raw.get("videoUrl")))


def _hours_since(published_at, measured_at):
    published = _parse_date_string(str(published_at).strip()) if published_at else None
    measured = _parse_date_string(str(measured_at).strip())
    if published is None or measured is None:
        return 0
    return max(0, js_round((measured - published).total_seconds() / 3600 * 10) / 10)


def normalize_performance_row(request, response_items, measured_at, run_failed=False):
    items = [i for i in response_items if isinstance(i, dict)] or []
    want = perf_normalize_url(request.get("post_url"))
    raw = next((c for c in items if _raw_url(c) == want), None) or \
        (items[0] if items else {})

    views = _num(first(raw.get("playCount"), raw.get("videoViewCount"),
                       raw.get("viewCount"), raw.get("views"), raw.get("viewsCount"),
                       raw.get("videoPlayCount"), (raw.get("stats") or {}).get("playCount")))
    likes = _num(first(raw.get("diggCount"), raw.get("likesCount"), raw.get("likeCount"),
                       raw.get("likes"), (raw.get("stats") or {}).get("diggCount")))
    comments = _num(first(raw.get("commentCount"), raw.get("commentsCount"),
                          raw.get("comments"), (raw.get("stats") or {}).get("commentCount")))
    shares = _num(first(raw.get("shareCount"), raw.get("sharesCount"), raw.get("shares"),
                        (raw.get("stats") or {}).get("shareCount")))
    saves = _num(first(raw.get("collectCount"), raw.get("savesCount"), raw.get("saves"),
                       (raw.get("stats") or {}).get("collectCount")))
    er = js_round((likes + comments + shares + saves) / views * 10000) / 10000 if views else 0
    hours = _hours_since(request.get("published_at"), measured_at)
    views_per_hour = js_round(views / hours * 100) / 100 if hours else 0
    published_id = _text(request.get("published_id"))
    # Маркер METRICS_EMPTY — упавший ран ИЛИ пустой ответ (замера не было).
    # Честный ноль (актор вернул item с 0 просмотров) — валидный замер
    # metrics_zero: eval не выбрасывает его из 7-дневного окна (ревью аудита —
    # старый код клеил маркер на любой views=0, и провальный рил терял
    # канонический 7-дневный замер).
    if run_failed or not items:
        status = METRICS_EMPTY
    elif views:
        status = "metrics_collected"
    else:
        status = "metrics_zero"
    stamp = re.sub(r"[-:T]", "", str(measured_at)[:16])
    return {
        "performance_id": published_id + "_" + stamp + "_" +
                          stable_hash(_raw_url(raw) or request.get("post_url")),
        "published_id": published_id,
        "brief_id": _text(request.get("brief_id")),
        "prompt_version": _text(request.get("prompt_version")),
        "platform": _text(request.get("platform")),
        "measured_at": measured_at,
        "hours_since_publish": hours,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "engagement_rate": er,
        "views_per_hour": views_per_hour,
        "result_label": "unknown",
        "result_reason": "not_evaluated_in_n8n_agent_required",
        "eval_notes": "; ".join([
            "n8n_collected_metrics_only",
            "collection_status=" + status,
            "post_url=" + _text(request.get("post_url")),
            "source_actor=" + _text(request.get("source_actor")),
        ]),
    }


def collect(sheets, client, dry_run=False, now_iso=None, log=None, limit=None):
    """Полный дозамер: CF Published Reels -> Apify -> CF Performance + run_log.

    Сбой отдельного рана — строка metrics_empty_or_unavailable (как error-item
    HTTP-ноды с onError:continue), потеря видна в summary.runs_failed.

    Маркеров прогресса тут нет намеренно: звено «Статистика» — живой бэклог, а не
    шаг с полоской, поэтому раннер читает его stdout не потоково и маркеры
    отфильтровал бы обратно. Понадобится полоска — инструментация вернётся вместе
    с producing-шагом."""
    trigger = "dry-run" if dry_run else "cli"
    measured = now_iso or _iso_ms(datetime.now(timezone.utc))
    # started_at Run Log = момент старта дозамера, в формате now_iso(). Прогон
    # растёт линейно с числом опубликованных рилсов — именно эту динамику
    # нулевая длительность и скрывала.
    run_started = _iso_seconds(measured)
    reels = sheets.read_rows("reels")
    requests = build_metric_requests(reels)
    if not requests:
        log_run(sheets, "collect-performance", "insufficient_data",
                input_summary="нет published-строк с полными полями "
                              f"(reels={len(reels)})", trigger_type=trigger,
                              started_at=run_started)
        return {"status": "insufficient_data", "requests": 0, "rows": [],
                "runs_failed": 0}

    if limit:
        # --dry-run = «1 батч» (AGENTS.md): каждый запрос — платный ран Apify,
        # поэтому лимит режет сами запросы (ревью 14.09.2026).
        requests = requests[:limit]
    rows, runs_failed, errors = [], 0, []
    for req in requests:
        failed = False
        try:
            result = client.run_actor(req["actor_path"], req["payload"])
            items = result.items if getattr(result, "ok", False) else []
            if not getattr(result, "ok", False):
                failed = True
                errors.append(getattr(result, "error", "") or "apify run failed")
        except Exception as exc:
            items, failed = [], True
            errors.append(str(exc))
        runs_failed += failed
        rows.append(normalize_performance_row(req, items, measured, run_failed=failed))
    rows = [r for r in rows
            if r["published_id"] and r["brief_id"] and r["prompt_version"]]

    if runs_failed == len(requests):
        # M27 (аудит 2026-07-24): полный отказ Apify (битый токен, сервис лежит) —
        # это failed с алертом оператору, а не success; N строк с нулевыми
        # метриками не пишем — они питали бы eval как «настоящие» замеры.
        summary = f"все {runs_failed} Apify-ранов дозамера упали"
        if log:
            log(summary)
        log_run(sheets, "collect-performance", "failed", input_summary=summary,
                errors=errors, trigger_type=trigger,
                started_at=run_started)
        return {"status": "failed", "error": summary, "requests": len(requests),
                "rows": [], "runs_failed": runs_failed}

    if not dry_run:
        sheets.append_rows("performance", rows)
    summary = (f"requests={len(requests)} rows={len(rows)} "
               f"runs_failed={runs_failed}")
    if log:
        log(summary)
    log_run(sheets, "collect-performance", "success", input_summary=summary,
            errors=errors, trigger_type=trigger,
            started_at=run_started)
    return {"status": "success", "requests": len(requests), "rows": rows,
            "runs_failed": runs_failed}
