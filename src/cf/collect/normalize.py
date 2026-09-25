"""Нормализация raw-рядов — порт 1:1 n8n Code-node узлов:

- tiktok_row/tiktok_rows — Normalize-TikTok-Raw-Rows.js (cf01-tiktok; snowball-копия
  отличалась только атрибуцией snowball:<seed> — здесь это параметр source_query,
  который передаёт пайплайн вместо $('Build Snowball Input').itemMatching);
- instagram_row/instagram_rows/instagram_finalize — Normalize-Instagram-Raw-Rows.js.

Ряды должны байт-в-байт совпадать с тем, что писал n8n (upsert по raw_id),
поэтому JSON сериализуем как JSON.stringify (без пробелов, non-ASCII как есть),
а обрезка raw_json считает UTF-16 code units, как String.slice.
"""
import json
import re
from datetime import datetime, timezone

from cf.collect.util import (
    age_hours,
    engagement_rate,
    first,
    is_apify_error,
    num,
    stable_hash,
    to_iso,
)


class CollectError(RuntimeError):
    """Громкий провал стадии сбора — аналог throw new Error() в Code-node."""


def _g(obj, *keys):
    """Опциональная цепочка raw.a?.b — None при не-dict на любом шаге."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


# Потолок текстовой ячейки Sheets в UTF-16 юнитах (лимит ячейки 50K с запасом).
# Публичный контракт: тем же потолком и той же обрезкой живёт visual_facts
# (cf.vision) — сериализации, уходящие в ячейку, режутся одним правилом.
CELL_TEXT_CAP = 45000


def js_slice(text, limit):
    """String.prototype.slice(0, limit): JS считает UTF-16 code units,
    астральный символ занимает 2; попавший на границу — отбрасывается."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


_js_slice = js_slice   # прежнее приватное имя — у порта 1:1 остаются вызовы


def _raw_json(raw):
    # JSON.stringify: без пробелов, ключи в порядке вставки, non-ASCII как есть.
    return _js_slice(json.dumps(raw, ensure_ascii=False,
                                separators=(",", ":")), CELL_TEXT_CAP)


def _now_from(collected_at):
    """Момент 'сейчас' для age_hours: в JS Date.now() совпадал с collectedAt
    с точностью до миллисекунд; здесь берём collected_at — ряд детерминирован."""
    if not isinstance(collected_at, str):
        return None
    try:
        dt = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------- TikTok

def _pick_subtitle(raw):
    """pickSubtitle: rus → ASR → eng → первый линк; нет линков → {}."""
    links = []
    for arr in (_g(raw, "videoMeta", "subtitleLinks"), raw.get("subtitleLinks"), raw.get("subtitles")):
        if isinstance(arr, list):
            links.extend(arr)
    if not links:
        return {}

    def lang(link):
        return str(_g(link, "language") or "")

    for match in (
        lambda l: re.match(r"rus", lang(l), re.I),
        lambda l: str(_g(l, "source") or "").upper() == "ASR",
        lambda l: re.match(r"eng", lang(l), re.I),
    ):
        for link in links:
            if match(link):
                return link
    # не-dict линк в JS дальше читался как «все поля undefined» — эквивалент {}
    return links[0] if isinstance(links[0], dict) else {}


def _tiktok_source_query(raw):
    # Грамматика атрибуции: hashtag:#… / query:… / unknown.
    name = _g(raw, "searchHashtag", "name")
    if name:
        return "hashtag:#" + re.sub(r"^#", "", str(name))
    if raw.get("searchQuery"):
        return "query:" + str(raw["searchQuery"])
    return "unknown"


def tiktok_row(raw, collected_at, source_query=None):
    """Один ряд CF Raw TikTok. source_query=None — вывод из raw (hashtag/query/
    unknown); строка — явная атрибуция (снежок передаёт 'snowball:<seed>')."""
    video_id = first(raw.get("id"), raw.get("videoId"), raw.get("aweme_id"), _g(raw, "video", "id"))
    url = first(raw.get("webVideoUrl"), raw.get("url"), raw.get("shareUrl"), raw.get("videoUrl"))
    author = first(_g(raw, "authorMeta", "name"), _g(raw, "authorMeta", "nickName"),
                   _g(raw, "author", "uniqueId"), raw.get("author"), raw.get("username"))
    caption = first(raw.get("text"), raw.get("desc"), raw.get("caption"))
    created_at = to_iso(first(raw.get("createTimeISO"), raw.get("createTime"),
                              raw.get("createTimeUTC"), raw.get("createdAt")))
    views = num(first(raw.get("playCount"), raw.get("views"), _g(raw, "stats", "playCount")))
    likes_raw = num(first(raw.get("diggCount"), raw.get("likes"), _g(raw, "stats", "diggCount")))
    likes = None if likes_raw < 0 else likes_raw  # отрицательное = скрыто: пусто, не ноль
    comments = num(first(raw.get("commentCount"), raw.get("comments"), _g(raw, "stats", "commentCount")))
    shares = num(first(raw.get("shareCount"), raw.get("shares"), _g(raw, "stats", "shareCount")))
    saves = num(first(raw.get("collectCount"), raw.get("saves"), _g(raw, "stats", "collectCount")))
    transcript_raw = first(raw.get("transcript"), raw.get("subtitleText"), raw.get("subtitlesText"),
                           raw.get("autoGeneratedTranscript"), _g(raw, "videoMeta", "subtitlesText"))
    # только реальная непустая строка; объект/массив/пусто → нет транскрипта
    transcript = transcript_raw if isinstance(transcript_raw, str) and transcript_raw.strip() else ""
    subtitle = _pick_subtitle(raw)
    subtitle_url = "" if transcript else first(
        _g(subtitle, "downloadLink"), _g(subtitle, "url"), _g(subtitle, "tiktokLink"))
    media = raw.get("mediaUrls")
    media_first = media[0] if isinstance(media, (list, str)) and media else None
    return {
        "raw_id": "tiktok_" + str(video_id if video_id else stable_hash(str(url) + str(caption))),
        "collected_at": collected_at,
        "platform": "tiktok",
        "source_actor": "clockworks/tiktok-scraper",
        "source_query": _tiktok_source_query(raw) if source_query is None else source_query,
        "is_ad": bool(raw.get("isAd") or raw.get("isSponsored")),
        "text_language": str(raw.get("textLanguage") or ""),
        "video_id": video_id,
        "url": url,
        "author": author,
        "caption": caption,
        "created_at": created_at,
        "duration_sec": num(first(_g(raw, "videoMeta", "duration"), raw.get("duration"), raw.get("durationSec"))),
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "engagement_rate": engagement_rate(likes_raw, comments, shares, saves, views),
        "age_hours": age_hours(created_at, now=_now_from(collected_at)),
        "thumbnail_url": first(_g(raw, "covers", "default"), _g(raw, "videoMeta", "coverUrl"),
                               raw.get("thumbnail"), raw.get("coverUrl")),
        "video_url": first(media_first, raw.get("videoUrl"), _g(raw, "videoMeta", "downloadAddr")),
        "transcript_text": transcript,
        "subtitle_url": subtitle_url,
        "subtitle_language": first(_g(subtitle, "language"), ""),
        "subtitle_source": first(_g(subtitle, "sourceUnabbreviated"), _g(subtitle, "source"), ""),
        "raw_json": _raw_json(raw),
        "processing_status": ("raw_saved_actor_transcript" if transcript
                              else "subtitle_pending_download" if subtitle_url
                              else "raw_saved_no_actor_transcript"),
    }


def _tiktok_loss(raw, position, batch_index, batch_sources):
    """P5.14 — loss-маркер упавшего батча. Идентичность батча: параметры вызова
    (аналог itemMatching → Build в JS) перекрывают поля error-item (маркер
    apify.run_batches, C1.2); позиция item — последний fallback, как index в JS."""
    idx = raw.get("batch_index")
    if idx is None:
        idx = position
    if batch_index is not None:
        idx = batch_index
    sources = []
    if isinstance(raw.get("batch_sources"), list):
        sources = raw["batch_sources"]
    if isinstance(batch_sources, list):
        sources = batch_sources
    return {
        "__apify_error": str(raw.get("error") or raw.get("errorMessage")
                             or raw.get("__apify_error") or "apify batch failed"),
        "source_query": "unknown",
        "batch_index": idx,
        "batch_sources": sources,
    }


def tiktok_rows(items, collected_at, batch_index=None, batch_sources=None, source_query=None):
    """Батч items → (rows, losses). Error-item НЕ становится мусорным рядом —
    форвардится loss-маркером для Ingestion Gate; пустые item'ы дропаются молча."""
    rows, losses = [], []
    for position, raw in enumerate(items):
        if is_apify_error(raw):
            losses.append(_tiktok_loss(raw, position, batch_index, batch_sources))
        elif isinstance(raw, dict) and raw:
            rows.append(tiktok_row(raw, collected_at, source_query=source_query))
    return rows, losses


# ------------------------------------------------- TikTok: адаптер apidojo
# Тикет 04: apidojo~tiktok-scraper — кандидат за конфиг-флагом (включение —
# runbook 19.08 после живого парити). Маппинг полей — по README актора, раздел
# «Example Output Object» (билд latest 0.0.1055, сверка по API 10.08):
# id/title/views/likes/comments/shares/bookmarks/hashtags/channel/
# uploadedAt(Formatted)/video{duration,url,cover,thumbnail}/subtitleInformation/
# postPage. Это документация вендора, не голое допущение, но живых ответов до
# 19.08 нет — подтверждение формата входит в runbook смоука (тикет 07).


def _apidojo_lang(link):
    """Код языка до региона: 'rus-RU' -> 'rus', 'en' -> 'en'. У apidojo язык
    лежит в lang (полный) и language_code (двухбуквенный) — сверяем оба."""
    raw = str(_g(link, "lang") or _g(link, "language_code") or "")
    return re.split(r"[-_]", raw)[0].lower()


def _apidojo_pick_subtitle(raw):
    """Приоритет — как у _pick_subtitle clockworks: rus → авто-генерация
    (аналог ASR) → eng → первый линк; нет subtitleInformation → {}."""
    info = raw.get("subtitleInformation")
    links = [l for l in info if isinstance(l, dict)] if isinstance(info, list) else []
    if not links:
        return {}
    for match in (
        lambda l: _apidojo_lang(l) in ("ru", "rus"),
        lambda l: bool(l.get("is_auto_generated")),
        lambda l: _apidojo_lang(l) in ("en", "eng"),
    ):
        for link in links:
            if match(link):
                return link
    return links[0]


def _apidojo_source_query(raw, batch_sources):
    """Атрибуция item'а к источнику батча. Первым — inputSource: живой парити
    10.08 показал, что актор кладёт туда ИСКОМЫЙ keyword («#тег») для каждого
    item, хотя README поле не документирует; без него 36% строк уходили в
    unknown (keyword-поиск возвращает ролики, где искомого тега нет в списке
    hashtags). Фолбэк — документированное поле hashtags (сравнение без
    регистра и «#», как канонизация M28). Совпадения нет — единственный
    источник батча берётся как есть (батч спрашивал только его); источников
    несколько и ни один не совпал — честный unknown, а не первый попавшийся."""
    markers = [str(m) for m in (batch_sources or [])]
    src = str(raw.get("inputSource") or "").lstrip("#").lower()
    tags = raw.get("hashtags")
    tags_lc = {str(t).lstrip("#").lower() for t in tags} if isinstance(tags, list) else set()
    for marker in markers:
        if marker.startswith("hashtag:#") \
                and marker[len("hashtag:#"):].lower() == src:
            return marker
        if marker.startswith("query:") and marker[len("query:"):].lower() == src:
            return marker
    for marker in markers:
        if marker.startswith("hashtag:#") \
                and marker[len("hashtag:#"):].lower() in tags_lc:
            return marker
    if len(markers) == 1:
        return markers[0]
    return "unknown"


def apidojo_tiktok_row(raw, collected_at, batch_sources=None):
    """Item apidojo → канонический ряд CF Raw TikTok: те же колонки в том же
    порядке, что у tiktok_row, — гейт, coalesce и анализ миграции не видят.
    Готового текста транскрипта в документированном выходе нет вообще,
    субтитры — только ссылками, поэтому transcript_text всегда пуст: линк
    уходит в существующий контур скачивания (subtitle_pending_download),
    без линка ряд честно получает raw_saved_no_actor_transcript."""
    video_id = first(raw.get("id"))
    url = first(raw.get("postPage"), raw.get("url"))
    channel = raw.get("channel") if isinstance(raw.get("channel"), dict) else {}
    caption = first(raw.get("title"), raw.get("text"))
    created_at = to_iso(first(raw.get("uploadedAtFormatted"), raw.get("uploadedAt")))
    views = num(raw.get("views"))
    likes_raw = num(raw.get("likes"))
    comments = num(raw.get("comments"))
    shares = num(raw.get("shares"))
    saves = num(raw.get("bookmarks"))
    subtitle = _apidojo_pick_subtitle(raw)
    subtitle_url = first(_g(subtitle, "url"))
    return {
        "raw_id": "tiktok_" + str(video_id if video_id else stable_hash(str(url) + str(caption))),
        "collected_at": collected_at,
        "platform": "tiktok",
        "source_actor": "apidojo/tiktok-scraper",
        "source_query": _apidojo_source_query(raw, batch_sources),
        "is_ad": bool(raw.get("isAd") or raw.get("isSponsored")),
        # языка текста в выходе apidojo нет; пустая строка — fail-open в гейте
        "text_language": str(raw.get("textLanguage") or ""),
        "video_id": video_id,
        "url": url,
        "author": first(_g(channel, "username"), _g(channel, "name")),
        "caption": caption,
        "created_at": created_at,
        "duration_sec": num(first(_g(raw, "video", "duration"), raw.get("duration"))),
        "views": views,
        "likes": None if likes_raw < 0 else likes_raw,  # отрицательное = скрыто
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "engagement_rate": engagement_rate(likes_raw, comments, shares, saves, views),
        "age_hours": age_hours(created_at, now=_now_from(collected_at)),
        "thumbnail_url": first(_g(raw, "video", "cover"), _g(raw, "video", "thumbnail")),
        "video_url": first(_g(raw, "video", "url")),
        "transcript_text": "",
        "subtitle_url": subtitle_url,
        "subtitle_language": first(_g(subtitle, "lang"), _g(subtitle, "language_code"), ""),
        # ASR-аналога у apidojo нет — честный маркер авто-генерации
        "subtitle_source": ("auto_generated" if _g(subtitle, "is_auto_generated")
                            else first(_g(subtitle, "source_tag"), "")),
        "raw_json": _raw_json(raw),
        "processing_status": ("subtitle_pending_download" if subtitle_url
                              else "raw_saved_no_actor_transcript"),
    }


def apidojo_tiktok_rows(items, collected_at, batch_index=None, batch_sources=None):
    """Батч items apidojo → (rows, losses). Обязательные поля адаптера — url
    (postPage) и стабильный идентификатор (id; при его отсутствии — сам url
    через hash, как у clockworks). Item без url — НЕузнанный формат выхода:
    батч помечается провальным одним loss-маркером существующей механики
    (__apify_error → гейт → причина в Run Log), молчаливых кривых строк не
    пишем (тикет 04). Пустые item'ы дропаются молча, как в tiktok_rows."""
    rows, losses = [], []
    unrecognized = 0
    for position, raw in enumerate(items):
        if is_apify_error(raw):
            losses.append(_tiktok_loss(raw, position, batch_index, batch_sources))
        elif not isinstance(raw, dict):
            if raw not in (None, ""):
                unrecognized += 1  # не-dict непустой item — не наш формат
        elif raw and not first(raw.get("postPage"), raw.get("url")):
            unrecognized += 1
        elif raw:
            rows.append(apidojo_tiktok_row(raw, collected_at,
                                           batch_sources=batch_sources))
    if unrecognized:
        losses.append({
            "__apify_error": (
                f"apidojo: {unrecognized} item(ов) без обязательных url/id — "
                "формат выхода не распознан, сверка формата — парити 19.08"),
            "source_query": "unknown",
            "batch_index": batch_index,
            "batch_sources": list(batch_sources or []),
        })
    return rows, losses


# ------------------------------------------------------------------ Instagram

_IG_TEXT_KEYS = ("text", "caption", "content", "line", "transcript")
_IG_NEST_KEYS = ("segments", "captions", "subtitles", "utterances")


def ig_transcript_text(value):
    """transcriptText: рекурсивная коэрция транскрипта любой формы к строке."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(p for p in (ig_transcript_text(v) for v in value) if p)
    if isinstance(value, dict):
        parts = []
        for key in _IG_TEXT_KEYS:
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        for key in _IG_NEST_KEYS:
            if value.get(key):
                parts.append(ig_transcript_text(value[key]))
        return " ".join(p for p in parts if p)
    return str(value or "").strip()


def is_usable(raw):
    """Годный reel = не error-item и есть реальный id/url (reel-scraper при
    onError=continueRegularOutput на 408/5xx отдаёт один {error:...} без id/url)."""
    if not isinstance(raw, dict):
        return False
    if raw.get("error") or raw.get("errorDescription") or raw.get("errorMessage"):
        return False
    return bool(first(raw.get("id"), raw.get("shortCode"), raw.get("shortcode"), raw.get("code"),
                      raw.get("url"), raw.get("displayUrl"), raw.get("videoUrl")))


def _is_ig_batch_error(raw):
    """P5.14 — маркер упавшего reel-батча; пустой {} — не потеря."""
    return bool(isinstance(raw, dict)
                and (raw.get("error") or raw.get("errorDescription")
                     or raw.get("errorMessage") or raw.get("__apify_error")))


def instagram_row(raw, collected_at, tag_by_code):
    """Один ряд CF Raw Instagram; source_query — по shortcode через tag_by_code."""
    video_id = first(raw.get("id"), raw.get("shortCode"), raw.get("shortcode"), raw.get("code"))
    code_match = re.search(r"instagram\.com/(?:p|reel|reels)/([^/?#]+)", str(raw.get("url") or ""), re.I)
    short_code = first(raw.get("shortCode"), raw.get("shortcode"), raw.get("code"),
                       code_match.group(1) if code_match else None)
    url = first(raw.get("url"), raw.get("displayUrl"), raw.get("videoUrl"),
                "https://www.instagram.com/reel/%s/" % video_id if video_id else "")
    caption = first(raw.get("caption"), raw.get("text"), raw.get("description"))
    created_at = to_iso(first(raw.get("timestamp"), raw.get("takenAtTimestamp"), raw.get("createdAt")))
    views = num(first(raw.get("videoViewCount"), raw.get("videoPlayCount"), raw.get("views"), raw.get("plays")))
    likes_raw = num(first(raw.get("likesCount"), raw.get("likes")))
    comments = num(first(raw.get("commentsCount"), raw.get("comments")))
    shares = num(first(raw.get("sharesCount"), raw.get("shares")))
    saves = num(first(raw.get("savesCount"), raw.get("saves")))
    transcript = first(
        ig_transcript_text(raw.get("transcript")),
        ig_transcript_text(raw.get("videoTranscript")),
        ig_transcript_text(raw.get("transcriptText")),
        ig_transcript_text(raw.get("subtitleText")),
        ig_transcript_text(raw.get("audioTranscript")),
    )
    tag = tag_by_code.get(short_code) if isinstance(tag_by_code, dict) else None
    return {
        "raw_id": "instagram_" + str(video_id if video_id else stable_hash(str(url) + str(caption))),
        "collected_at": collected_at,
        "platform": "instagram",
        "source_actor": "apify/instagram-reel-scraper",
        "source_query": "hashtag:#" + str(tag) if tag else "unknown",
        "video_id": video_id,
        "url": url,
        "author": first(raw.get("ownerUsername"), _g(raw, "owner", "username"),
                        raw.get("username"), raw.get("ownerFullName")),
        "caption": caption,
        "created_at": created_at,
        "duration_sec": num(first(raw.get("videoDuration"), raw.get("duration"), raw.get("durationSec"))),
        "views": views,
        "likes": None if likes_raw < 0 else likes_raw,  # -1 = скрытые лайки: пусто, не ноль
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "engagement_rate": engagement_rate(likes_raw, comments, shares, saves, views),
        "age_hours": age_hours(created_at, now=_now_from(collected_at)),
        "thumbnail_url": first(raw.get("displayUrl"), raw.get("thumbnailUrl"),
                               raw.get("thumbnail"), raw.get("imageUrl")),
        "video_url": first(raw.get("videoUrl"), raw.get("videoDownloadUrl")),
        "transcript_text": transcript,
        "raw_json": _raw_json(raw),
        "processing_status": "raw_saved_actor_transcript" if transcript else "raw_saved_no_actor_transcript",
    }


def instagram_rows(items, collected_at, tag_by_code, batch_index=None, batch_sources=None):
    """Один reel-батч → (rows, losses, reels_lost). Error/пустые item'ы дропаются
    без мусорного instagram_0; для error-item пишется потеря {reels, batch_index}
    (reels — параметр batch_sources, аналог Prepare.batch_sources/reel_urls в JS)."""
    rows = [instagram_row(raw, collected_at, tag_by_code) for raw in items if is_usable(raw)]
    losses = []
    for raw in items:
        if not _is_ig_batch_error(raw):
            continue
        idx = raw.get("batch_index")
        if batch_index is not None:
            idx = batch_index
        reels = []
        if isinstance(raw.get("batch_sources"), list):
            reels = raw["batch_sources"]  # маркер apify.run_batches (C1.2)
        if isinstance(batch_sources, list):
            reels = batch_sources
        losses.append({"reels": reels, "batch_index": idx})
    return rows, losses, sum(len(l["reels"]) for l in losses)


def instagram_finalize(all_rows, all_losses, items_total):
    """Сводка по всем reel-батчам (аналог console.log + throw в конце JS-узла).
    Частичный успех — потери только в счётчиках; полный ноль по всем батчам
    (item'ы пришли, 0 годных) — CollectError, мусор в Sheets не пишем.
    items_total — сумма len(items) всех вызовов instagram_rows: пустые {}
    не попадают ни в rows, ни в losses, но в JS считались в items.length."""
    # уникальные упавшие батчи по batch_index; маркеры без индекса — каждый как один
    failed_idx = {l["batch_index"] for l in all_losses if l["batch_index"] is not None}
    failed_unknown = sum(1 for l in all_losses if l["batch_index"] is None)
    if items_total > 0 and not all_rows:
        raise CollectError(
            "Instagram raw: " + str(items_total) + " item(s), 0 годных reels по всем "
            "reel-батчам — Apify transcript fetch упал (408/5xx); "
            "мусорный instagram_0 не пишем, ран падает")
    return {
        "input": items_total,
        "kept": len(all_rows),
        "batches_failed": len(failed_idx) + failed_unknown,
        "reels_lost": sum(len(l["reels"]) for l in all_losses),
    }
