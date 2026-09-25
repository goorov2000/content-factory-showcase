"""cf collect instagram — конвейер сбора (эталон: воркфлоу CF 01 IG; транскрипты
вынесены за гейт тикетом 03 плана 2026-08-10-apify-costs).

discovery (search-scraper по запросам) -> мерж seed+discovered тегов
(Normalize-Instagram-Hashtags.js: нишевый фильтр по границе слова,
регистронезависимый дедуп, <=24 discovered / <=28 итог) -> hashtag-scraper
батчами <=4 -> IG-гейт (метаданные, ДО дорогого reel-scraper) -> уникальные
reel-URL <=30 с tag_by_code (Prepare-...-Input.js) -> reel-scraper батчами
<=6 (без транскрипт-аддона) -> нормализация -> транскрипты apple_yang одним
bulk-вызовом только для строк без готового текста (attach_bulk_transcripts) ->
медиа-контур (обложка+кадры, тикет 04 визуального контура) ->
upsert по raw_id с coalesce P1.16 (тикет 01 визуального контура: JS-эталон IG
мержа не имел, и пересбор затирал дорогие поля — атрибуцию, first-seen,
транскрипт; TikTok/snowball исходно шли с coalesce) -> run_log.
Сбой discovery — деградация до seed-тегов (в n8n onError: continue); сбой
транскрипт-этапа — той же конструкции: строки уезжают без текста, причина в
сводке и Run Log.
"""
import re
from datetime import datetime, timezone

from cf.collect.apify import run_batches
from cf.collect import progress as progress_marker
from cf.collect.coalesce import coalesce_row
from cf.collect.media import attach_media, summary_note as media_note
# losses_detail — общий разбор loss-маркеров (имена источников + причины по
# контракту «батч N: текст Apify»). Импортируем, а не копируем: две реализации
# одного формата разъезжаются, а разбор 2026-07-27 начался ровно с того, что
# reel-стадия описывала свои потери по-своему и до Run Log они не доезжали.
from cf.collect.gate import CollectGateError, instagram_gate, losses_detail
# ig_transcript_text — та же коэрция текста, что у актор-транскриптов
# normalize: транскрипт apple_yang обязан лечь в каноническое поле БАЙТ-В-БАЙТ
# так же, как ложился транскрипт-аддон reel-scraper, иначе гейт и анализ увидят
# два разных формата одного поля (публичное имя — контракт между модулями,
# ревью 2026-08-10).
from cf.collect.normalize import (CollectError, ig_transcript_text,
                                  instagram_finalize, instagram_rows)
from cf.collect.sources import (
    BATCH_SIZE,
    REEL_BATCH_SIZE,
    RESULTS_LIMIT,
    SEARCH_LIMIT_PER_QUERY,
    active_sources,
    age_candidates,
    candidate_room,
    chunk,
    exploration_cfg,
    exploration_facts,
    exploration_pick,
    harvest_candidates,
    load_registry,
    mark_explored,
    save_promote_proposal,
    save_registry,
    source_marker,
)
from cf.collect.util import (_iso_ms, _iso_seconds, _parse_date_string,
                             dedupe_by, first)
from cf.runlog import log_run

AGENT = "collect-instagram"
ACTORS_DEFAULT = {
    "instagram_discovery": "apify~instagram-search-scraper",
    "instagram_hashtag": "apify~instagram-hashtag-scraper",
    "instagram_reel": "apify~instagram-reel-scraper",
    # Расшифровка ПОСЛЕ гейта (тикет 03 плана 2026-08-10-apify-costs):
    # $0.001/результат + speech2text $0.0035/начатую минуту против $0.041/мин
    # у транскрипт-аддона reel-scraper — в 12 раз дешевле, и платим только за
    # прошедшее гейт. Откат = вернуть includeTranscript в build_reel_payload.
    "instagram_transcripts": "apple_yang~instagram-transcripts-scraper",
}

_NICHE = re.compile(r"муж|\bmen|style|fashion|outfit|streetwear|одеж|образ|гардероб|юмор",
                    re.I)
_REEL_CODE = re.compile(r"instagram\.com/(?:p|reel|reels)/([^/?#]+)", re.I)
_EXPLORE_TAG = re.compile(r"explore/tags/([^/?#]+)", re.I)


def matches_niche(tag):
    # '\bmen' по границе слова: 'men' не ловит 'women' (P1.19)
    return bool(_NICHE.search(str(tag)))


def dedupe_hashtags(tags):
    # IG-теги регистронезависимы; первая встреченная форма побеждает (seed первым)
    seen, out = set(), []
    for tag in tags:
        key = str(tag).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _flatten(items):
    out = []
    for j in items:
        if isinstance(j, list):
            out.extend(j)
        elif isinstance(j, dict) and isinstance(j.get("items"), list):
            out.extend(j["items"])
        elif isinstance(j, dict) and isinstance(j.get("data"), list):
            out.extend(j["data"])
        else:
            out.append(j if j is not None else {})
    return out


def merge_hashtags(seed, discovery_items):
    """Порт склейки Normalize-Instagram-Hashtags.js: discovered из ответа
    discovery-актора (hashtag/name/title), фильтр ниши, <=24; итог <=28."""
    discovered = []
    for row in _flatten(discovery_items):
        row = row if isinstance(row, dict) else {}
        tag = str(first(row.get("hashtag"), row.get("name"), row.get("title"))
                  ).lstrip("#").strip()
        if tag and matches_niche(tag):
            discovered.append(tag)
    discovered = discovered[:24]
    return [t for t in dedupe_hashtags(list(seed) + discovered) if t][:28]


def hashtag_batches(hashtags):
    full = ",".join(hashtags)
    groups = chunk(list(hashtags), BATCH_SIZE)
    return [{"platform": "instagram", "instagram_hashtags": group,
             "source_query": full, "batch_index": i, "batch_total": len(groups),
             "batch_size": len(group),
             "batch_sources": ["hashtag:#" + h for h in group],
             "results_limit": RESULTS_LIMIT}
            for i, group in enumerate(groups)]


def _reel_url(raw):
    value = str(first(raw.get("url"), raw.get("reelUrl"), raw.get("videoUrl"),
                      raw.get("inputUrl")) or "")
    m = _REEL_CODE.search(value)
    if m:
        return "https://www.instagram.com/reel/" + m.group(1) + "/"
    code = first(raw.get("shortCode"), raw.get("shortcode"), raw.get("code"))
    return "https://www.instagram.com/reel/%s/" % code if code else ""


def _short_code_of(raw):
    value = str(first(raw.get("url"), raw.get("reelUrl"), raw.get("videoUrl"),
                      raw.get("inputUrl")) or "")
    m = _REEL_CODE.search(value)
    if m:
        return m.group(1)
    return str(first(raw.get("shortCode"), raw.get("shortcode"), raw.get("code")))


def _source_tag(raw):
    from urllib.parse import unquote
    m = _EXPLORE_TAG.search(str(raw.get("inputUrl") or ""))
    if m:
        return unquote(m.group(1))
    return str(raw.get("queryTag") or raw.get("hashtag") or "").lstrip("#")


def _url_code(url):
    """shortcode из нормализованного reel-URL (ключ tag_by_code)."""
    m = _REEL_CODE.search(str(url or ""))
    return m.group(1) if m else ""


def _round_robin(groups, limit):
    """Раздача limit слотов по кругу: каждой очереди по одному за круг.

    (разбор 2026-07-27) Резерв кандидатов раздавался срезом cand[:k] — первый
    же урожайный тег забирал все шесть слотов. 26.07 так и вышло: тег 164494443
    взял 5 слотов, а avocadostyle/mensfashion/mydubai не получили ни одного и
    поехали к автоматическому ретайру «yield: rows_passed_gate=0». Круг даёт
    каждому кандидату хотя бы один слот, пока резерв не кончился, — иначе
    зондирование меряет не тег, а его место в списке.
    """
    queues = [list(v) for v in groups.values() if v]
    out = []
    while len(out) < limit and any(queues):
        for q in queues:
            if not q:
                continue
            out.append(q.pop(0))
            if len(out) >= limit:
                break
    return out


def prepare_reel_batches(reels, source_query="", candidate_tags=(),
                         reserved_candidate_slots=6):
    """Порт Prepare-Instagram-Reel-Transcript-Input.js: tag_by_code
    (первый тег на shortcode), уникальные URL <=30, батчи <=6.

    H11 (аудит 2026-07-24): кандидатские теги домешиваются в КОНЕЦ списка
    хэштегов, их reels срезались капом 30 — rows_passed_gate вечно 0 и
    age_candidates ретирил всех. Кандидатам гарантируется до
    reserved_candidate_slots слотов в капе; делится резерв ПО КАНДИДАТАМ
    (см. _round_robin), а не по порядку URL."""
    rows = [r if isinstance(r, dict) else {} for r in reels]
    tag_by_code = {}
    for raw in rows:
        code, tag = _short_code_of(raw), _source_tag(raw)
        if code and tag and code not in tag_by_code:
            tag_by_code[code] = tag
    urls, seen, url_tag = [], set(), {}
    for raw in rows:
        url = _reel_url(raw)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
            url_tag[url] = str(tag_by_code.get(_short_code_of(raw), "")).lower()
    cand_tags = {str(t).lstrip("#").lower() for t in candidate_tags if str(t).strip()}
    if cand_tags and len(urls) > 30:
        cand = [u for u in urls if url_tag.get(u, "") in cand_tags]
        # Очереди по тегам в порядке первого появления URL — раздача
        # детерминирована (тот же вход даёт тот же набор слотов).
        by_tag = {}
        for u in cand:
            by_tag.setdefault(url_tag.get(u, ""), []).append(u)
        reserved = _round_robin(by_tag, min(reserved_candidate_slots, len(cand)))
        k = len(reserved)
        rest = [u for u in urls if u not in set(cand)]
        pick = set(rest[:30 - k]) | set(reserved)
        for u in urls:                      # добор до 30 в исходном порядке
            if len(pick) >= 30:
                break
            pick.add(u)
        urls = [u for u in urls if u in pick][:30]
    else:
        urls = urls[:30]
    if not urls:
        return [], tag_by_code
    groups = chunk(urls, REEL_BATCH_SIZE)
    return [{"platform": "instagram", "source_query": source_query,
             "tag_by_code": tag_by_code, "reel_urls": group,
             "batch_index": i, "batch_total": len(groups),
             "batch_size": len(group),
             "batch_sources": ["reel:" + u for u in group],
             "results_limit": len(group)}
            for i, group in enumerate(groups)], tag_by_code


def candidate_probes(candidate_tags, tag_by_code, reel_batches,
                     delivered_batches=()):
    """Что прогон РЕАЛЬНО узнал о каждом кандидате (разбор 2026-07-27).

    available — сколько рилсов тега дожило до входа reel-стадии (после гейта),
    delivered — сколько из них уехало в успешный reel-батч. Зонд честен, когда
    available=0 (тег пуст — вердикт полный, сбор ничего не потерял) либо
    delivered>0 (материал доехал и его видно в финальных строках). Случай
    «рилсы были, а слотов не досталось» — голодание раздачи, а не бесплодие
    тега; засчитывать его нельзя, потому что sources.age_candidates ретайрит
    НЕОБРАТИМО: 26.07 так ушёл avocadostyle, не получив ни одного слота.
    Эталон — tiktok.collect: там прогон засчитывается только состоявшемуся
    exploration-рану, упавший батч сигнала кандидату не даёт.
    """
    codes = tag_by_code if isinstance(tag_by_code, dict) else {}

    def key(tag):
        return str(tag or "").lstrip("#").lower()

    stat = {key(t): {"available": 0, "delivered": 0}
            for t in candidate_tags if str(t).strip()}
    for tag in codes.values():
        if key(tag) in stat:
            stat[key(tag)]["available"] += 1
    delivered = set(delivered_batches or ())
    for batch in reel_batches or []:
        if batch.get("batch_index") not in delivered:
            continue
        for url in batch.get("reel_urls") or []:
            tag = key(codes.get(_url_code(url)))
            if tag in stat:
                stat[tag]["delivered"] += 1
    return stat


def build_discovery_payload(search_queries):
    return {"search": ",".join(search_queries), "searchType": "hashtag",
            "searchLimit": SEARCH_LIMIT_PER_QUERY}


def build_hashtag_payload(batch):
    return {"hashtags": batch["instagram_hashtags"], "resultsType": "reels",
            "resultsLimit": batch["results_limit"], "keywordSearch": False}


def build_reel_payload(batch):
    # контракт reel-scraper: reel-URL в поле username (странность актора, 1:1).
    # includeTranscript снят (тикет 03 плана 2026-08-10-apify-costs): аддон
    # стоил $0.041/начатую минуту за КАЖДЫЙ собранный ролик — 40% счёта Apify.
    # Расшифровка — отдельный этап ПОСЛЕ гейта, см. attach_bulk_transcripts.
    return {"username": batch["reel_urls"], "resultsLimit": 1,
            "includeDownloadedVideo": False,
            "includeSharesCount": False, "skipPinnedPosts": True,
            "skipTrialReels": True}


def build_transcript_payload(urls):
    # Схема билда apple_yang~instagram-transcripts-scraper: единственное поле
    # bulkUrls (сверено по схеме билда latest, разведка 10.08).
    return {"bulkUrls": list(urls)}


# Маппинг ВЫХОДА apple_yang — ДОПУЩЕНИЕ (README актора без примера ответа):
# текст ищем по ключам transcript -> text -> segments[].text (склейку и
# вложенные формы даёт ig_transcript_text), строку узнаём по url/inputUrl/
# shortCode (_short_code_of). Подтверждение на живом ответе — runbook 19.08
# (тикет 07); до тех пор неузнанный формат обязан давать ГРОМКУЮ деградацию
# с примером ключей, а не молчаливые пустые транскрипты.
_BULK_TEXT_KEYS = ("transcript", "text", "segments")


def _bulk_transcript_text(item):
    for key in _BULK_TEXT_KEYS:
        text = ig_transcript_text(item.get(key))
        if text:
            return text
    return ""


def attach_bulk_transcripts(rows, runner, existing=None):
    """Транскрипт-этап ПОСЛЕ гейта (тикет 03 плана 2026-08-10-apify-costs).

    rows — канонические строки, пережившие гейт и reel-стадию; runner(payload)
    -> RunResult транскрипт-актора. В bulkUrls идут ТОЛЬКО строки без готового
    текста: уже расшифрованное в прогоне не трогаем, а расшифрованное прежними
    сборами переносим из existing ({raw_id: text} вкладки) — без переноса
    каждый дневной прогон заново оплачивал бы расшифровку тех же рилсов (сам
    текст в листе теперь бережёт и coalesce на upsert, но платный вызов
    отсекается только здесь). Пустой pending — платного вызова нет вообще.

    Сбой этапа (исключение, не-ok ран, неузнанный формат) сбор не роняет —
    прайор-арт деградации discovery: строки уезжают без транскрипта
    (processing_status остаётся raw_saved_no_actor_transcript), причина — в
    stats["error"], откуда collect кладёт её в сводку и errors Run Log.

    Возвращает (строки, stats): requested/reused/ok/failed/error.
    """
    existing = existing or {}
    out = list(rows)
    stats = {"requested": 0, "reused": 0, "ok": 0, "failed": False, "error": ""}
    pending = []
    for i, row in enumerate(out):
        if str(row.get("transcript_text") or "").strip():
            continue
        prev = str(existing.get(row.get("raw_id")) or "").strip()
        if prev:
            # Контракт normalize 1:1: непустой текст => raw_saved_actor_transcript.
            patched = dict(row)
            patched["transcript_text"] = prev
            patched["processing_status"] = "raw_saved_actor_transcript"
            out[i] = patched
            stats["reused"] += 1
            continue
        if row.get("url"):
            pending.append(i)
    stats["requested"] = len(pending)
    if not pending:
        return out, stats

    urls = [out[i]["url"] for i in pending]
    try:
        result = runner(build_transcript_payload(urls))
        ok = bool(getattr(result, "ok", False))
        if not ok:
            stats["failed"] = True
            stats["error"] = str(getattr(result, "error", "")
                                 or getattr(result, "status", "")
                                 or "transcripts failed")
            return out, stats
        items = [it for it in _flatten(result.items) if it]
    except Exception as exc:  # noqa: BLE001 — деградация, но не молча (как discovery)
        stats["failed"] = True
        stats["error"] = str(exc) or exc.__class__.__name__
        return out, stats

    by_code = {}
    for i in pending:
        code = _url_code(out[i]["url"])
        if code and code not in by_code:
            by_code[code] = i
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = by_code.get(_short_code_of(item))
        if idx is None:
            continue
        if str(out[idx].get("transcript_text") or "").strip():
            continue  # дубль URL в выдаче: первый результат победил, ok не двоится
        text = _bulk_transcript_text(item)
        if not text:
            continue
        row = dict(out[idx])
        row["transcript_text"] = text
        row["processing_status"] = "raw_saved_actor_transcript"
        out[idx] = row
        stats["ok"] += 1
    if not stats["ok"]:
        stats["failed"] = True
        if items:
            # Пример ключей первого item — без него «0 транскриптов» не отличить
            # от «маппинг-допущение протухло», а чинятся они разными руками.
            sample = next((sorted(it) for it in items if isinstance(it, dict)),
                          [type(items[0]).__name__])
            stats["error"] = ("формат ответа транскрипт-актора не распознан: "
                              f"0 транскриптов на {len(urls)} URL, "
                              f"ключи первого item: {sample}")
        else:
            stats["error"] = f"транскрипт-актор вернул 0 items на {len(urls)} URL"
    return out, stats


def _fail(sheets, trigger, message, started_at=None, errors=None):
    # errors отдельным параметром (разбор 2026-07-27): у полного провала есть
    # что сказать сверх текста исключения — потерянные теги и причины отказа.
    log_run(sheets, AGENT, "failed", input_summary=message,
            errors=errors if errors is not None else [message],
            trigger_type=trigger, started_at=started_at)
    return {"status": "failed", "error": message, "rows": []}


def collect(sheets, client, config=None, dry_run=False, now_iso=None, log=None,
            registry_root=None, limit_batches=None, explore=True,
            max_workers=2, media_root=None, media_downloader=None,
            frame_cutter=None, duration_prober=None, http_client=None):
    trigger = "dry-run" if dry_run else "cli"
    collected = now_iso or _iso_ms(datetime.now(timezone.utc))
    gate_now = _parse_date_string(collected)
    run_started = _iso_seconds(collected)   # started_at Run Log в формате now_iso()
    actors = {**ACTORS_DEFAULT, **((config or {}).get("apify", {}).get("actors", {}))}

    # Пороги exploration — одним словарём на весь прогон: pick, harvest,
    # discovery-приток и aging судят кандидата по одним и тем же числам (Д4).
    expl = exploration_cfg(config)

    registry = load_registry("instagram", root=registry_root)
    srcs = active_sources(registry)
    seed = srcs["hashtags"]
    # C4.1 — exploration-квота: <=batch кандидат-тегов домешиваются ПОСЛЕ лимита 28,
    # чтобы активные теги их не вытесняли (search-кандидатов нет — их роль у discovery)
    candidates = (exploration_pick(registry, kinds=("hashtag",), cfg=expl)
                  if explore else [])

    # Стадия 1 — discovery; сбой не роняет сбор (деградация до seed-тегов)
    discovery_items, discovery_failed, discovery_error = [], False, ""
    if srcs["search_queries"]:
        try:
            result = client.run_actor(actors["instagram_discovery"],
                                      build_discovery_payload(srcs["search_queries"]))
            ok = bool(getattr(result, "ok", False))
            discovery_items = result.items if ok else []
            discovery_failed = not ok
            if not ok:
                discovery_error = str(getattr(result, "error", "")
                                      or getattr(result, "status", "")
                                      or "discovery failed")
        except Exception as exc:  # noqa: BLE001 — деградация до seed, но не молча
            # (разбор 2026-07-27) Голый except терял текст: «discovery_failed=True»
            # одинаково выглядит при 402 (кончился лимит трат Apify, ночь 27.07),
            # 401 (протух токен) и таймауте — а лечится это тремя разными руками.
            discovery_failed = True
            discovery_error = str(exc) or exc.__class__.__name__

    hashtags = merge_hashtags(seed, discovery_items)
    if candidates:
        hashtags = [t for t in dedupe_hashtags(
            hashtags + [r["query"] for r in candidates]) if t]
    hb = hashtag_batches(hashtags)
    if limit_batches:
        hb = hb[:limit_batches]
    if not hb:
        log_run(sheets, AGENT, "insufficient_data",
                input_summary="нет hashtag-источников (реестр пуст, discovery без находок)",
                trigger_type=trigger,
                started_at=run_started)
        return {"status": "insufficient_data", "rows": []}

    # §7 timeline: хэштеги найдены — подготовка закончена, дальше две выкачки
    progress_marker.emit(log, "prepare", total=len(hb))
    progress_marker.emit(log, "fetch", done=0, total=len(hb))
    # Стадия 2 — hashtag-scraper; входы гейта восстанавливаем в порядке батчей
    results, losses = run_batches(
        hb, lambda b: client.run_actor(actors["instagram_hashtag"],
                                       build_hashtag_payload(b)),
        max_workers=max_workers,
        on_done=lambda done, total: progress_marker.emit(log, "fetch", done=done,
                                                        total=total))
    by_index = {batch["batch_index"]: items for batch, items in results}
    loss_by_index = {l["batch_index"]: l for l in losses}
    gate_inputs, gate_batches = [], []
    for batch in hb:
        i = batch["batch_index"]
        gate_inputs.append(by_index.get(i, loss_by_index.get(i)))
        gate_batches.append({"batch_index": i, "batch_sources": batch["batch_sources"]})
    progress_marker.emit(log, "gate")
    try:
        gated = instagram_gate(gate_inputs, batches=gate_batches, now=gate_now)
    except CollectGateError as exc:
        # (разбор 2026-07-27) Ночью тут легли ВСЕ 5 hashtag-батчей, а в Run Log
        # уехал один текст исключения: ни тегов, ни причины отказа Apify —
        # разбираться пришлось в чужом API. Имена и причины несёт само
        # исключение (контракт CollectGateError), кладём их в сводку и errors.
        note = str(exc)
        if exc.lost_reasons:
            note += " | причины: " + "; ".join(exc.lost_reasons)
        out = _fail(sheets, trigger, note, run_started,
                    errors=[str(exc)] + exc.lost_sources + exc.lost_reasons)
        out["lost_sources"] = exc.lost_sources
        out["lost_reasons"] = exc.lost_reasons
        return out
    gs = gated["summary"]

    reel_batches, tag_by_code = prepare_reel_batches(
        gated["rows"], source_query=",".join(hashtags),
        candidate_tags=[r["query"] for r in candidates])
    if not reel_batches:
        note = (f"гейт не пропустил ни одного reel "
                f"(input={gs['input']}, drops={gs['drops']})")
        out = {"status": "insufficient_data", "rows": [], "gate": gs,
               "lost_sources": gs["lost_sources"],
               "lost_reasons": gs["lost_reasons"]}
        if gs["batches_failed"]:
            # (разбор 2026-07-27) «Гейт всё выбросил» и «половина батчей не
            # стартовала» — разные аварии с разным лечением, а статус у них
            # один; без причины в сводке оператор лечит не то.
            note += " деградация: " + "; ".join(gs["lost_reasons"])
            out["error"] = "; ".join(gs["lost_reasons"])
        log_run(sheets, AGENT, "insufficient_data", input_summary=note,
                errors=gs["lost_sources"] + gs["lost_reasons"],
                trigger_type=trigger, started_at=run_started)
        return out

    progress_marker.emit(log, "reels", done=0, total=len(reel_batches))
    # Стадия 3 — reel-scraper (двухточечный учёт потерь: гейт + reel-стадия)
    reel_results, reel_run_losses = run_batches(
        reel_batches, lambda b: client.run_actor(actors["instagram_reel"],
                                                 build_reel_payload(b)),
        max_workers=max_workers,
        on_done=lambda done, total: progress_marker.emit(log, "reels", done=done,
                                                        total=total))
    all_rows, all_losses, items_total = [], [], 0
    for batch, items in reel_results:
        rows, batch_losses, _lost = instagram_rows(
            items, collected, tag_by_code, batch_index=batch["batch_index"],
            batch_sources=batch["batch_sources"])
        all_rows.extend(rows)
        all_losses.extend(batch_losses)
        items_total += len(items)
    # (разбор 2026-07-27) Причины падений reel-стадии — тем же разбором, что у
    # hashtag-стадии: до правки они не попадали в Run Log НИКОГДА, и потеря
    # дорогой стадии выглядела как «reels_lost=N» без единого слова почему.
    reel_lost, reel_reasons = losses_detail(reel_run_losses)
    for marker in reel_run_losses:
        # упавший run-sync в JS давал один error-item на входе Normalize;
        # __apify_error здесь больше не выбрасывается — маркер потери обязан
        # нести причину, иначе она умирает на этой строке.
        all_losses.append({"reels": marker.get("batch_sources") or [],
                           "batch_index": marker.get("batch_index"),
                           "__apify_error": marker.get("__apify_error")})
        items_total += 1
    try:
        fin = instagram_finalize(all_rows, all_losses, items_total)
    except CollectError as exc:
        return _fail(sheets, trigger, str(exc), run_started,
                     errors=[str(exc)] + reel_lost + reel_reasons)

    # Деградация прогона — одно понятие на статус, aging и алерт оператору:
    # потеря батча на ЛЮБОЙ из двух выкачек означает неполную выдачу.
    degraded = bool(gs["batches_failed"] or fin["batches_failed"])

    # H12/M33: related-выдачи и пересечения тегов приносят один reel дважды;
    # мерж — правилом coalesce (тикет 01): «последний победил» затирал непустой
    # транскрипт первого вхождения пустым дублем.
    all_rows = dedupe_by(all_rows, "raw_id", merge=coalesce_row)

    # Стадия 4 — транскрипты apple_yang одним bulk-вызовом (тикет 03 плана
    # 2026-08-10-apify-costs). Готовые тексты прежних сборов сначала переносим
    # из вкладки: гейт держит ролик до 30 дней, и без переноса каждый дневной
    # прогон заново оплачивал бы расшифровку одних и тех же рилсов. Вкладку
    # читаем только когда есть кому переносить — иначе это пустой запрос.
    existing = {}
    if any(not str(r.get("transcript_text") or "").strip() for r in all_rows):
        for prev in sheets.read_rows("raw_instagram"):
            text = str(prev.get("transcript_text") or "").strip()
            if text and prev.get("raw_id"):
                existing[prev["raw_id"]] = text
    all_rows, transcripts = attach_bulk_transcripts(
        all_rows,
        lambda payload: client.run_actor(actors["instagram_transcripts"], payload),
        existing=existing)

    # Медиа-контур (тикет 04 визуального контура) — модуль тикета 03 без
    # переделки на четырёхстадийном IG-тракте: обложка + кадры каждой
    # прошедшей гейт строки, отказ не валит сбор и статус не деградирует.
    all_rows, media_stats = attach_media(
        all_rows, "instagram", root=media_root, downloader=media_downloader,
        cutter=frame_cutter, prober=duration_prober, http_client=http_client,
        token=getattr(client, "token", None))

    progress_marker.emit(log, "write")
    upsert = {"updated": 0, "appended": 0}
    if all_rows and not dry_run:
        upsert = sheets.upsert_rows("raw_instagram", "raw_id", all_rows,
                                    merge=coalesce_row)
    # итог единицы работы числом — лента показывает ролики, а не платформы
    progress_marker.emit(log, "write", rows=len(all_rows), new=upsert["appended"])

    # Счётчики exploration: прогон засчитан кандидату, чей hashtag-батч доехал
    # И чьи рилсы прошли reel-стадию (candidate_probes); rows_passed_gate — по
    # финальным строкам с его атрибуцией (сигнал = что дошло).
    # (разбор 2026-07-27) Раньше runs_count начислялся по успеху ОДНОЙ лишь
    # hashtag-стадии, а судили кандидата по строкам ПОСЛЕ reel-стадии: зонда
    # не было, а вердикт «бесплоден» был — и необратимый.
    # Тикет 06: те же факты уезжают в сводку (out["exploration"]) маркерами —
    # блок про источники в Telegram собирает из них cf.messages; наружу идёт
    # только записанное (dry-run, голодание и упавшие зонды фактов не дают).
    registry_dirty = False
    explored_recs, retired_recs, promoted_recs = [], [], []
    probe_note = ""
    if candidates and not dry_run:
        succeeded = {batch["batch_index"] for batch, _ in results}
        delivered = {batch["batch_index"] for batch, _ in reel_results}
        probes = candidate_probes([r["query"] for r in candidates], tag_by_code,
                                  reel_batches, delivered)
        ran, starved = [], []
        for rec in candidates:
            q = rec["query"]
            if not any(q in b["instagram_hashtags"]
                       and b["batch_index"] in succeeded for b in hb):
                continue
            st = probes.get(str(q).lstrip("#").lower(),
                            {"available": 0, "delivered": 0})
            if st["available"] and not st["delivered"]:
                starved.append(q)
                continue
            ran.append(q)
        if ran:
            rows_by_query = {}
            for row in all_rows:
                sq = str(row.get("source_query", "")).lower()
                for q in ran:
                    # M28: атрибуция без регистра — IG канонизирует теги в lowercase
                    if sq == ("hashtag:#" + q).lower():
                        rows_by_query[q] = rows_by_query.get(q, 0) + 1
            mark_explored(registry, ran, collected, rows_by_query)
            explored_recs = [rec for rec in candidates if rec["query"] in ran]
            registry_dirty = True
        if starved:
            # Видно в сводке: голодание раздачи слотов и потеря reel-батча —
            # дефекты конвейера, а не свойство тега, и они должны быть заметны
            # без чтения реестра (26.07 их не увидел никто).
            probe_note = (" зонд не засчитан (рилсы не доехали): "
                          + ", ".join(starved))
    if not dry_run:
        # C4.2 — harvest из капшенов финальных строк
        if all_rows and harvest_candidates(registry, all_rows,
                                           today=collected[:10], cfg=expl):
            registry_dirty = True
        # Apify-discovery подпитывает кандидатов (origin=discovery): нишевые
        # теги этого прогона, которых нет ни в seed, ни в реестре.
        # Д4: это ВТОРОЙ канал притока (до 24 тегов за прогон при батче в 4
        # зонда), и он живёт под тем же потолком очереди, что и harvest —
        # иначе потолок обходится по соседней трубе.
        known = {str(r.get("query", "")).lower() for r in registry}
        seed_lower = {str(t).lower() for t in seed}
        room = candidate_room(registry, expl)
        for tag in hashtags:
            if room <= 0:
                break
            if tag.lower() in known or tag.lower() in seed_lower:
                continue
            registry.append({"query": tag.lower(), "kind": "hashtag",
                             "niche": next((r.get("niche") for r in registry
                                            if r.get("status") == "active"
                                            and r.get("niche")), "other"),
                             "status": "candidate", "origin": "discovery",
                             "added_at": collected[:10]})
            known.add(tag.lower())
            room -= 1
            registry_dirty = True
    # C4.3 — взросление/отсев кандидатов по итогам окна raw
    aged_note = ""
    if degraded and not dry_run:
        # (разбор 2026-07-27) Прогон с потерянными батчами видел неполную
        # выдачу, а ретайр необратим (в active — только proposal + apply-sources).
        # Судить кандидатов по такому прогону — та же ошибка, что судить их по
        # зонду, которого не было: правило №2 CLAUDE.md, ждём целого прогона.
        aged_note = " aging пропущен: потери батчей"
    elif not dry_run:
        retired, promotable = age_candidates(
            registry, sheets.read_rows("raw_instagram"), collected, cfg=expl)
        if retired:
            registry_dirty = True
            aged_note = f" retired={len(retired)}"
            retired_recs = retired
        if promotable:
            path = save_promote_proposal("instagram", promotable, collected,
                                         root=registry_root)
            aged_note += f" promote_proposal={path.name}"
            promoted_recs = promotable
    if registry_dirty:
        save_registry("instagram", registry, root=registry_root)

    discovery_note = f"discovery_failed={discovery_failed}"
    if discovery_error:
        discovery_note += f": {discovery_error}"
    # Сбой транскрипт-этапа — та же конструкция, что у discovery: причина обязана
    # доехать до сводки (её видит Telegram) и до errors, а не только до счётчиков.
    transcript_note = (f"transcripts ok={transcripts['ok']}/{transcripts['requested']} "
                       f"reused={transcripts['reused']}")
    if transcripts["error"]:
        transcript_note += f" (сбой этапа: {transcripts['error']})"
    summary_line = (f"hashtags={len(hashtags)} ({discovery_note}) "
                    f"gate kept={gs['kept']}/{gs['input']} "
                    f"hashtag_batches_failed={gs['batches_failed']} "
                    f"sources_lost={gs['sources_lost']} "
                    f"reel kept={fin['kept']}/{fin['input']} "
                    f"reel_batches_failed={fin['batches_failed']} "
                    f"reels_lost={fin['reels_lost']} "
                    + transcript_note + " "
                    + media_note(media_stats) + " "
                    f"upsert updated={upsert['updated']} appended={upsert['appended']}"
                    + probe_note + aged_note)
    # (разбор 2026-07-27) Частичный сбор — уже не «Успех». Ночью не стартовали
    # все 5 hashtag-батчей IG, и в другие ночи так же тихо теряются reel-батчи:
    # статус зависел только от «привезли ли хоть строку», Telegram молчал
    # (cli.py шлёт алерт на failed/insufficient_data). Правило №2 CLAUDE.md:
    # данных не хватило — так и говорим. Exit-код остаётся 0, верх конвейера
    # не гейтится, а причина деградации едет в самой сводке.
    reasons = list(gs["lost_reasons"]) + reel_reasons
    if degraded:
        summary_line += " деградация: " + "; ".join(reasons)
    if log:
        log(summary_line)
    status = "insufficient_data" if degraded or not all_rows else "success"
    errors = (gs["lost_sources"] + gs["lost_reasons"] + reel_lost + reel_reasons
              + ([f"discovery: {discovery_error}"] if discovery_error else [])
              + ([f"transcripts: {transcripts['error']}"]
                 if transcripts["error"] else []))
    log_run(sheets, AGENT, status, input_summary=summary_line,
            errors=errors, trigger_type=trigger,
            started_at=run_started)
    out = {"status": status, "rows": all_rows, "gate": gs, "reel": fin,
           "discovery_failed": discovery_failed,
           "discovery_error": discovery_error, "upsert": upsert,
           "transcripts": transcripts, "media": media_stats,
           "exploration": exploration_facts(explored_recs, retired_recs,
                                            promoted_recs),
           "lost_sources": gs["lost_sources"] + reel_lost,
           "lost_reasons": reasons}
    if degraded:
        # cli.py подставит «строк не привезено», если error пуст — при частичной
        # потере это враньё: строки-то есть, нет доброй половины источников.
        out["error"] = "; ".join(reasons)
    return out
