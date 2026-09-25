"""cf collect snowball — сбор по seed-роликам (эталон: воркфлоу CF 01b).

Seeds из вкладки CF Seeds (active=TRUE, tiktok.com), дедуп по нормализованному
URL (P1.20 — один ролик с разными query-параметрами не порождает два платных
рана), один батч на seed, атрибуция source_query='snowball:<seed>'. Дальше
общий tiktok-тракт: гейт -> субтитры -> медиа-контур (обложка+кадры,
тикет 03 визуального контура) -> upsert с coalesce (пишет в тот же лист
CF Raw TikTok — coalesce обязателен, иначе повторный сбор затёр бы
атрибуцию первого сбора).
"""
from datetime import datetime, timezone

from cf.collect.apify import run_batches
from cf.collect.coalesce import coalesce_row
from cf.collect.gate import CollectGateError, tiktok_gate
from cf.collect.media import attach_media, summary_note as media_note
from cf.collect.normalize import tiktok_rows
from cf.collect.subtitles import download_subtitle
from cf.collect.tiktok import ACTOR_PATH_DEFAULT, attach_transcripts
from cf.collect.util import (_iso_ms, _iso_seconds, _parse_date_string,
                             dedupe_by, normalize_url)
from cf.runlog import log_run

AGENT = "collect-snowball"


def build_snowball_batches(seed_rows, collected_at):
    """Порт Build-Snowball-Input.js: фильтр active=TRUE + tiktok.com, дедуп по
    normalize_url (первый выигрывает, порядок стабильный), 1 item на seed."""
    seeds = [row for row in seed_rows
             if str(row.get("active")).upper() == "TRUE"
             and "tiktok.com" in str(row.get("seed_url") or "").lower()]
    seen, deduped = set(), []
    for row in seeds:
        key = normalize_url(row.get("seed_url"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return [
        {
            "platform": "tiktok",
            "actor": "clockworks/tiktok-scraper",
            "actor_path": ACTOR_PATH_DEFAULT,
            "seed_url": str(row.get("seed_url")).strip(),
            "seed_niche": str(row.get("niche") or ""),
            "batch_index": index,
            "batch_total": len(deduped),
            "batch_size": 1,
            "batch_sources": ["snowball:" + str(row.get("seed_url")).strip()],
            "collected_at": collected_at,
        }
        for index, row in enumerate(deduped)
    ]


def build_payload(batch):
    # База — HTTP-нода «Apify Snowball Fetch» (bodyParameters workflow.json),
    # но shouldDownloadSubtitles вендор удалил из схемы актора (сверка со
    # схемой билда latest, 10.08) — субтитры запрашивает enum
    # downloadSubtitlesOptions, DOWNLOAD_SUBTITLES бесплатен (готовые субтитры
    # TikTok, платный speech-to-text не включается).
    return {
        "postURLs": [batch["seed_url"]],
        "scrapeRelatedVideos": True,
        "resultsPerPage": 15,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": True,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
        "shouldDownloadSlideshowImages": False,
        "proxyCountryCode": "None",
    }


def collect(sheets, client, config=None, dry_run=False, now_iso=None, log=None,
            limit_batches=None, downloader=None, http_client=None, max_workers=2,
            media_root=None, media_downloader=None, frame_cutter=None,
            duration_prober=None):
    trigger = "dry-run" if dry_run else "cli"
    collected = now_iso or _iso_ms(datetime.now(timezone.utc))
    gate_now = _parse_date_string(collected)
    run_started = _iso_seconds(collected)   # started_at Run Log в формате now_iso()
    actor = ((config or {}).get("apify", {}).get("actors", {})
             .get("tiktok", ACTOR_PATH_DEFAULT))

    batches = build_snowball_batches(sheets.read_rows("seeds"), collected)
    if limit_batches:
        batches = batches[:limit_batches]
    if not batches:
        log_run(sheets, AGENT, "insufficient_data",
                input_summary="CF Seeds без активных tiktok-строк",
                trigger_type=trigger,
                started_at=run_started)
        return {"status": "insufficient_data", "batches": 0, "rows": []}

    results, losses = run_batches(
        batches, lambda b: client.run_actor(actor, build_payload(b)),
        max_workers=max_workers)
    rows = []
    for batch, items in results:
        # атрибуция целиком батча: у снежка source_query = 'snowball:<seed>'
        r, l = tiktok_rows(items, collected, batch_index=batch["batch_index"],
                           batch_sources=batch["batch_sources"],
                           source_query=batch["batch_sources"][0])
        rows.extend(r)
        losses.extend(l)

    try:
        gated = tiktok_gate(rows + losses, now=gate_now)
    except CollectGateError as exc:
        # (разбор 2026-07-27) Симметрично tiktok.collect: у снежка тот же гейт и
        # та же слепота — текст исключения без имён seed'ов и без причины отказа.
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

    # Дедуп ДО медиа-контура (related-выдачи соседних seed'ов пересекаются):
    # одно скачивание на raw_id, и в сводку/upsert идёт один и тот же набор.
    kept = dedupe_by(kept, "raw_id", merge=coalesce_row)

    # Медиа-контур (тикет 03 визуального контура) — тот же тракт, что у
    # tiktok.collect: снежок пишет в тот же лист и тот же каталог медиа
    # (raw_id — общее пространство имён), идемпотентность у них общая.
    kept, media_stats = attach_media(
        kept, "tiktok", root=media_root, downloader=media_downloader,
        cutter=frame_cutter, prober=duration_prober, http_client=http_client,
        token=getattr(client, "token", None))

    upsert = {"updated": 0, "appended": 0}
    if kept and not dry_run:
        upsert = sheets.upsert_rows("raw_tiktok", "raw_id", kept,
                                    merge=coalesce_row)

    # sources_lost в сводке (разбор 2026-07-27): у снежка источник = seed-ролик,
    # и до правки его сводка молчала даже о числе потерянных — tiktok эту цифру
    # печатал. Расхождение двух веток одного тракта прячет ровно ту потерю,
    # ради которой счётчик и заводили.
    summary_line = (f"seeds={len(batches)} rows={gs['input']} kept={gs['kept']} "
                    f"drops={sum(gs['drops'].values())} "
                    f"batches_failed={gs['batches_failed']} "
                    f"sources_lost={gs['sources_lost']} "
                    f"subtitles ok={tr_ok} failed={tr_failed} "
                    + media_note(media_stats) + " "
                    f"upsert updated={upsert['updated']} appended={upsert['appended']}")
    # (разбор 2026-07-27) Тот же контракт, что у tiktok: батч потерян — статус
    # insufficient_data (правило №2 CLAUDE.md), причина в сводке, exit-код 0.
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
           # Как у tiktok (ревью 2026-07-27): cli печатает «источников не
           # опрошено» по ключу lost_sources, а у снежка источник — это
           # seed-ролик, и без имён неясно, какой seed остался неопрошен.
           "sources_lost": gs["sources_lost"],
           "lost_sources": gs["lost_sources"],
           "lost_reasons": gs["lost_reasons"], "upsert": upsert,
           "media": media_stats}
    if degraded:
        # cli.py подставит «строк не привезено», если error пуст — а seed'ы
        # потеряны частично, и оператору нужна причина, а не заглушка.
        out["error"] = "; ".join(gs["lost_reasons"])
    return out
