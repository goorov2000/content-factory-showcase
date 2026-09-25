"""cf collect tiktok — конвейер сбора (эталон: воркфлоу n8n CF 01 TikTok).

Источники из реестра sources/tiktok.json -> батчи <=4 -> clockworks-актор
(асинхронный start+poll, батчи параллельно) -> нормализация -> гейт ->
скачивание субтитров (только у прошедших гейт — как в n8n, где Download стоит
после гейта) -> медиа-контур (обложка+кадры, тикет 03 визуального контура) ->
upsert по raw_id с coalesce P1.16 -> сводка в CF Run Log.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from cf.collect.apify import run_batches
from cf.collect import progress as progress_marker
from cf.collect.coalesce import coalesce_row
from cf.collect.gate import CollectGateError, tiktok_gate
from cf.collect.media import attach_media, summary_note as media_note
from cf.collect.normalize import apidojo_tiktok_rows, tiktok_rows
from cf.collect.sources import (
    RESULTS_PER_PAGE,
    active_sources,
    age_candidates,
    exploration_cfg,
    exploration_facts,
    exploration_pick,
    harvest_candidates,
    load_registry,
    make_batches,
    mark_explored,
    save_promote_proposal,
    save_registry,
    source_marker,
)
from cf.collect.subtitles import attach_transcript, download_subtitle
from cf.collect.util import (_iso_ms, _iso_seconds, _parse_date_string,
                             dedupe_by)
from cf.runlog import log_run

ACTOR_PATH_DEFAULT = "clockworks~tiktok-scraper"
HASHTAG_ACTOR_PATH_DEFAULT = "clockworks~tiktok-hashtag-scraper"
AGENT = "collect-tiktok"

# Фильтр популярности search-ветки НЕ шлём. Поле leastDiggs есть в схеме
# билда latest 0.0.583, но живой ран 10.08 его молча игнорирует: событий
# popularity-filter-applied 0, начислений 0, в выдаче likes от 2 — та же
# судьба, что у «UNDER MAINTENANCE»-полей тикета 01, только пока бесплатная.
# Слать no-op поле, которое однажды молча проснётся вместе с тарификацией
# $0.001/результат, — против US5 спеки; включение обратно — осознанным
# решением с замером (константа + поле в build_search_payload).


def build_hashtag_payload(batch):
    # Гибрид TikTok (тикет 02, 2026-08-10): хэштеги собирает
    # clockworks~tiktok-hashtag-scraper — $0.002/видео против $0.003, 99.8%
    # успешных прогонов против 92.5%, без actor-start и платных надбавок
    # конструктивно. Payload РОВНО по схеме билда latest (сверка 10.08,
    # REQUIRED [hashtags]): proxyCountryCode/searchQueries в схеме нет — не
    # шлём полей, которых нет в схеме. Субтитры — бесплатный вариант enum.
    return {
        "hashtags": batch.get("hashtags", []),
        "resultsPerPage": RESULTS_PER_PAGE,
        "shouldDownloadCovers": True,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadVideos": False,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
    }


def build_search_payload(batch):
    # База — HTTP-нода «Apify TikTok Raw Fetch» (bodyParameters workflow.json),
    # минус мёртвые платные поля (сверка со схемой билда latest, 10.08):
    # videoSearchSorting/videoSearchDateFilter у вендора «UNDER MAINTENANCE»
    # ($2.90/мес в никуда), shouldDownloadSubtitles из схемы удалён — субтитры
    # запрашивает enum downloadSubtitlesOptions, DOWNLOAD_SUBTITLES бесплатен
    # (готовые субтитры TikTok, платный speech-to-text не включается).
    # Поля hashtags больше нет: хэштеги ушли hashtag-scraper'у (тикет 02),
    # а пустой список — мусор в платном запросе.
    return {
        "searchQueries": batch.get("search_queries", []),
        "searchSection": "/video",
        "resultsPerPage": RESULTS_PER_PAGE,
        "excludePinnedPosts": False,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": True,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
        "shouldDownloadSlideshowImages": False,
        "scrapeRelatedVideos": False,
        "proxyCountryCode": "None",
    }


def is_apidojo_actor(actor_path):
    """Семейство актора по префиксу значения конфиг-ключа: 'apidojo~…' (и любой
    форк apidojo) означает схему входа apidojo и адаптер выдачи
    apidojo_tiktok_rows. Кодовые дефолты остаются clockworks — ветка включается
    ТОЛЬКО значением apify.actors.* в cf.config.json, и только после живого
    парити-замера 19.08 (тикет 07); откат — той же строкой конфига."""
    return str(actor_path or "").startswith("apidojo")


def build_apidojo_payload(batch):
    # Тикет 04: apidojo~tiktok-scraper — $0.0003/пост флэт, без единого платного
    # события (99.0% успеха на 12.4M прогонов, сверка по API 10.08). Схема билда
    # latest 0.0.1055: keywords/startUrls/maxItems/sortType/dateRange/location/
    # customMapFunction/includeSearchKeywords, REQUIRED []. Хэштеги подаём
    # keywords-путём «#тег», а не tag-URL в startUrls: один шаблон на оба kind,
    # кириллические теги не требуют URL-энкодинга, и только на search-путь
    # действуют серверные фильтры актора. sortType/dateRange НЕ шлём: enum'ы в
    # схеме есть, но недефолтное значение меняло бы семантику сбора без
    # evidence, а слать дефолт явно — мусор в платном запросе (философия
    # тикета 01). includeSearchKeywords не шлём: имя поля ВЫХОДА в README не
    # документировано, атрибуция и так восстанавливается из batch_sources.
    # maxItems — RESULTS_PER_PAGE на источник, как resultsPerPage у clockworks.
    terms = ["#" + str(h).lstrip("#") for h in batch.get("hashtags", [])]
    terms += [str(q) for q in batch.get("search_queries", [])]
    return {"keywords": terms, "maxItems": RESULTS_PER_PAGE * len(terms)}


def attach_transcripts(rows, downloader=None, http_client=None, max_workers=4,
                       token=None):
    """Скачивает субтитры у рядов subtitle_pending_download (параллельно) и
    прогоняет через attach_transcript. Возвращает (новые ряды, ok, failed).

    token нужен дефолтному скачивальщику для KV-store-ссылок hashtag-scraper
    (без подписи — 403, смоук 10.08); кастомный downloader токен не получает —
    его сигнатура (url, client) остаётся контрактом."""
    downloader = downloader or (
        lambda url, client=None: download_subtitle(url, client=client,
                                                   token=token))
    pending = [i for i, r in enumerate(rows)
               if r.get("processing_status") == "subtitle_pending_download"
               and r.get("subtitle_url")]
    if not pending:
        return list(rows), 0, 0

    def fetch(i):
        ok, body = downloader(rows[i]["subtitle_url"], client=http_client)
        return i, attach_transcript(rows[i], body, ok)

    out = list(rows)
    ok_count = failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, row in ex.map(fetch, pending):
            out[i] = row
            if row["processing_status"] == "raw_saved_actor_transcript":
                ok_count += 1
            else:
                failed += 1
    return out, ok_count, failed


def _exploration_batches(picked, start_index):
    """Exploration-зонды батчами своего kind (тикет 02): смешанный батч не мог
    бы уйти ни одному актору целиком — search-зонд у hashtag-scraper'а молча
    пропал бы из платного рана. Возвращает [(batch, записи_реестра)]: пары
    держат атрибуцию «какой батч чьих кандидатов», по ним считаются только
    УСПЕШНЫЕ exploration-раны (сбой сигнала кандидату не даёт)."""
    groups = [(kind, [r for r in picked if r.get("kind") == kind])
              for kind in ("hashtag", "search")]
    groups = [(kind, part) for kind, part in groups if part]
    total = start_index + len(groups)
    return [
        ({
            "batch_index": start_index + offset,
            "batch_total": total,
            "batch_size": len(part),
            "batch_sources": [source_marker(r) for r in part],
            "hashtags": [r["query"] for r in part] if kind == "hashtag" else [],
            "search_queries": [r["query"] for r in part] if kind == "search" else [],
            "source_query": ",".join(str(r["query"]) for r in part),
            "exploration": True,
        }, part)
        for offset, (kind, part) in enumerate(groups)
    ]


def collect(sheets, client, config=None, dry_run=False, now_iso=None, log=None,
            registry_root=None, limit_batches=None, explore=True,
            downloader=None, http_client=None, max_workers=2,
            media_root=None, media_downloader=None, frame_cutter=None,
            duration_prober=None):
    trigger = "dry-run" if dry_run else "cli"
    collected = now_iso or _iso_ms(datetime.now(timezone.utc))
    gate_now = _parse_date_string(collected)
    # started_at для Run Log: тот же момент, что collected_at, но в формате
    # now_iso() — сбор идёт минутами, и его длительность кормит ETA полоски
    # шага «Сбор» (progress.median_duration по агенту collect-tiktok).
    run_started = _iso_seconds(collected)
    # Гибрид акторов (тикет 02): hashtag-батчи — clockworks~tiktok-hashtag-scraper
    # ($0.002/видео, 99.8% успеха, без actor-start), search-батчи — прежний актор
    # (hashtag-scraper поиска не умеет, терять 5 search-источников нельзя —
    # решение владельца). Откат любой ветки — строка конфига apify.actors.
    actors_cfg = (config or {}).get("apify", {}).get("actors", {})
    actor_search = actors_cfg.get("tiktok", ACTOR_PATH_DEFAULT)
    actor_hashtag = actors_cfg.get("tiktok_hashtag", HASHTAG_ACTOR_PATH_DEFAULT)

    def actor_for(batch):
        # Батчи однородны по kind (make_batches и _exploration_batches режут
        # до сюда) — маршрут читает содержимое, а не отдельный флаг.
        return actor_search if batch.get("search_queries") else actor_hashtag

    def run_one(batch):
        # Семейство актора (тикет 04): apidojo-значение ключа меняет и схему
        # payload, и ветку нормализации ниже; clockworks-значения идут прежним
        # путём. Дефолты в коде — clockworks, включение только конфигом.
        actor = actor_for(batch)
        if is_apidojo_actor(actor):
            return client.run_actor(actor, build_apidojo_payload(batch))
        if batch.get("search_queries"):
            return client.run_actor(actor, build_search_payload(batch))
        return client.run_actor(actor, build_hashtag_payload(batch))

    # Пороги exploration — одним словарём на весь прогон: pick, harvest и
    # aging обязаны судить кандидата по одним и тем же числам (Д4).
    expl = exploration_cfg(config)

    registry = load_registry("tiktok", root=registry_root)
    batches = make_batches(active_sources(registry))
    # C4.1 — exploration-квота: <=batch кандидатов рядом с проверенными,
    # батчами своего kind (search-зонд не должен уехать hashtag-актору)
    explored = exploration_pick(registry, cfg=expl) if explore else []
    explore_pairs = _exploration_batches(explored, len(batches)) if explored else []
    batches.extend(b for b, _ in explore_pairs)
    if limit_batches:
        batches = batches[:limit_batches]
        explore_pairs = [(b, part) for b, part in explore_pairs
                         if b["batch_index"] < len(batches)]
    if not batches:
        log_run(sheets, AGENT, "insufficient_data",
                input_summary="реестр sources/tiktok.json без active-источников",
                trigger_type=trigger,
                started_at=run_started)
        return {"status": "insufficient_data", "batches": 0, "rows": []}

    # §7 timeline: подготовка закончена — реестр прочитан, батчи собраны.
    progress_marker.emit(log, "prepare", total=len(batches))
    progress_marker.emit(log, "fetch", done=0, total=len(batches))
    results, losses = run_batches(
        batches, run_one,
        max_workers=max_workers,
        on_done=lambda done, total: progress_marker.emit(log, "fetch", done=done,
                                                         total=total))
    rows = []
    for batch, items in results:
        # Ветка нормализации — по актору батча (тикет 04): формат выдачи
        # apidojo другой, его ряды строит адаптер; каноническая форма на
        # выходе одна, дальше гейт/субтитры/coalesce общие.
        to_rows = (apidojo_tiktok_rows if is_apidojo_actor(actor_for(batch))
                   else tiktok_rows)
        r, l = to_rows(items, collected, batch_index=batch["batch_index"],
                       batch_sources=batch["batch_sources"])
        rows.extend(r)
        losses.extend(l)

    progress_marker.emit(log, "gate")
    try:
        gated = tiktok_gate(rows + losses, now=gate_now)
    except CollectGateError as exc:
        # (разбор 2026-07-27) Полный провал раньше давал в Run Log один текст
        # «0 реальных рядов при N упавших батчах» — ни какие источники потеряны,
        # ни почему. Теперь гейт несёт то и другое (контракт CollectGateError),
        # причина едет и в сводке (её видит Telegram), и в errors.
        note = str(exc)
        if exc.lost_reasons:
            note += " | причины: " + "; ".join(exc.lost_reasons)
        log_run(sheets, AGENT, "failed", input_summary=note,
                errors=[str(exc)] + exc.lost_sources + exc.lost_reasons,
                trigger_type=trigger, started_at=run_started)
        return {"status": "failed", "error": note, "batches": len(batches),
                "rows": [], "lost_sources": exc.lost_sources,
                "lost_reasons": exc.lost_reasons}

    kept, gs = gated["rows"], gated["summary"]
    kept, tr_ok, tr_failed = attach_transcripts(
        kept, downloader=downloader, http_client=http_client,
        token=getattr(client, "token", None))

    # H12: одно видео из двух батчей (пересечение хэштега и поиска) — один ряд
    kept = dedupe_by(kept, "raw_id", merge=coalesce_row)

    # Медиа-контур (тикет 03 визуального контура): обложка + кадры каждой
    # прошедшей гейт строки, без фильтров (решение гриля №2). После дедупа —
    # одно скачивание на raw_id. Работает и в dry-run (механизм пилота);
    # отказ медиа не валит сбор и статус рана не деградирует.
    kept, media_stats = attach_media(
        kept, "tiktok", root=media_root, downloader=media_downloader,
        cutter=frame_cutter, prober=duration_prober, http_client=http_client,
        token=getattr(client, "token", None))

    progress_marker.emit(log, "write")
    upsert = {"updated": 0, "appended": 0}
    if kept and not dry_run:
        upsert = sheets.upsert_rows("raw_tiktok", "raw_id", kept, merge=coalesce_row)
    # итог единицы работы числом — лента показывает ролики, а не платформы
    progress_marker.emit(log, "write", rows=len(kept), new=upsert["appended"])

    # Счётчики exploration — только если exploration-ран реально состоялся
    # (упавший батч сигнала кандидату не даёт) и запись боевая. Батчей теперь
    # до двух (по kind, тикет 02) — каждый судит только СВОИХ кандидатов:
    # сбой search-зонда не крадёт прогон у hashtag-зондов и наоборот.
    # Тикет 06: те же факты уезжают в сводку (out["exploration"]) маркерами —
    # блок про источники в Telegram собирает из них cf.messages; наружу идёт
    # только записанное (dry-run и упавшие зонды фактов не дают).
    registry_dirty = False
    explored_recs, retired_recs, promoted_recs = [], [], []
    succeeded_idx = {b["batch_index"] for b, _ in results}
    counted = [r for b, part in explore_pairs
               if b["batch_index"] in succeeded_idx for r in part]
    if counted and not dry_run:
        # M28: TikTok канонизирует хэштеги в lowercase — атрибуция без регистра,
        # иначе кандидат «МужскойСтиль» никогда не получал rows_passed_gate.
        markers = {source_marker(r).lower(): r["query"] for r in counted}
        rows_by_query = {}
        for row in kept:
            q = markers.get(str(row.get("source_query", "")).lower())
            if q:
                rows_by_query[q] = rows_by_query.get(q, 0) + 1
        mark_explored(registry, [r["query"] for r in counted], collected,
                      rows_by_query)
        explored_recs = counted
        registry_dirty = True
    # C4.2 — harvest кандидатов из капшенов прошедших гейт строк (в пределах
    # потолка очереди: приток без потолка обгонял зондирование, Д4)
    if kept and not dry_run \
            and harvest_candidates(registry, kept, today=collected[:10], cfg=expl):
        registry_dirty = True
    # C4.3 — взросление/отсев кандидатов по итогам окна raw
    aged_note = ""
    if not dry_run:
        retired, promotable = age_candidates(
            registry, sheets.read_rows("raw_tiktok"), collected, cfg=expl)
        if retired:
            registry_dirty = True
            aged_note = f" retired={len(retired)}"
            retired_recs = retired
        if promotable:
            path = save_promote_proposal("tiktok", promotable, collected,
                                         root=registry_root)
            aged_note += f" promote_proposal={path.name}"
            promoted_recs = promotable
    if registry_dirty:
        save_registry("tiktok", registry, root=registry_root)

    summary_line = (f"batches={len(batches)} rows={gs['input']} kept={gs['kept']} "
                    f"drops={sum(gs['drops'].values())} "
                    f"batches_failed={gs['batches_failed']} "
                    f"sources_lost={gs['sources_lost']} "
                    f"subtitles ok={tr_ok} failed={tr_failed} "
                    + media_note(media_stats) + " "
                    f"upsert updated={upsert['updated']} appended={upsert['appended']}"
                    + aged_note)
    # (разбор 2026-07-27) Частичный сбор — уже не «Успех». Ночью 2 батча из 6 не
    # стартовали (потолок трат Apify): rows 460→282, kept 200→92, appended 75→4,
    # семь потерянных источников — и статус «success», а Telegram молчит, потому
    # что cli.py шлёт алерт только на failed/insufficient_data. Правило №2
    # CLAUDE.md: данных не хватило — так и говорим. Exit-код при этом остаётся 0
    # (верх конвейера не гейтится), звено дашборда судит по коду и не встаёт в
    # error, а причина деградации едет в самой сводке — не только в errors.
    degraded = bool(gs["batches_failed"])
    if degraded:
        summary_line += " деградация: " + "; ".join(gs["lost_reasons"])
    if log:
        log(summary_line)
    status = "insufficient_data" if degraded or not kept else "success"
    log_run(sheets, AGENT, status, input_summary=summary_line,
            errors=gs["lost_sources"] + gs["lost_reasons"], trigger_type=trigger,
            started_at=run_started)
    out = {"status": status, "batches": len(batches), "rows": kept,
           "drops": gs["drops"], "batches_failed": gs["batches_failed"],
           # ИМЕНА потерянных источников, а не только их число (ревью 2026-07-27):
           # cli.collect_stdout_lines печатает «источников не опрошено: N» по
           # ключу lost_sources, и без него частичная потеря на экране отчёта
           # этапа оставалась безымянной — ровно как в ночь 27.07.
           "sources_lost": gs["sources_lost"], "lost_sources": gs["lost_sources"],
           "lost_reasons": gs["lost_reasons"], "upsert": upsert,
           "subtitles_ok": tr_ok, "subtitles_failed": tr_failed,
           "media": media_stats,
           "exploration": exploration_facts(explored_recs, retired_recs,
                                            promoted_recs)}
    if degraded:
        # cli.py шлёт в Telegram summary["error"] и без него подставляет «строк
        # не привезено» — при частичной потере это враньё, кладём причину.
        out["error"] = "; ".join(gs["lost_reasons"])
    return out
