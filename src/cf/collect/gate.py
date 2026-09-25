"""Ingestion-гейты — порт 1:1 n8n Ingestion-Gate.js (cf01-tiktok ≡ cf01b-snowball)
и Ingestion-Gate-IG.js (cf01-instagram).

Дропают мусор ДО дорогих стадий (субтитры / reel-scraper) и считают потери
батчей (P5.14): loss-маркеры исключаются из данных, batches_failed — по
уникальным batch_index, sources_lost — из batch_sources маркеров, lost_reasons —
тексты отказов Apify (разбор 2026-07-27: сводка их теряла). Полный
провал (0 годных рядов при >=1 упавшем батче) — громкий CollectGateError,
чтобы «зелёный ран с нулём строк» не проходил тихо. Фейл-открытые: пустые
поля не дропают ряд (у TikTok кроме рекламы).
"""
import re
from datetime import datetime, timezone

from cf.collect.util import _parse_date_string, is_apify_error, num

# Дефолты порогов — константы как в JS-CFG; cfg-параметр гейтов переопределяет.
TIKTOK_CFG = {
    "max_age_days": 30,
    "min_views_stale": 500,   # применяется к постам старше stale_after_days
    "stale_after_days": 14,
    "max_caption_hashtags": 9,
    "languages_allow": ["ru", "en", "un", ""],
    "block_authors": ["wbinsidik", "wb.naxodkaaa", "wb.kiko2", "baza.store.kz",
                      "kingsman.premium", "dom_sumok42", "alex_fitrend"],
    "block_author_patterns": [re.compile(r"^wb[._]", re.I),
                              re.compile(r"^вб[._]", re.I)],
}

INSTAGRAM_CFG = {
    "max_age_days": 30,
    "min_views_stale": 500,
    "stale_after_days": 14,
    "max_caption_hashtags": 9,
    "block_authors": ["meijorofficial", "dom_sumok42", "renaciuki"],
    "block_author_patterns": [re.compile(r"^wb[._]", re.I),
                              re.compile(r"shop$", re.I),
                              re.compile(r"store$", re.I)],
}


class CollectGateError(Exception):
    """Полный провал сбора: 0 годных рядов при >=1 упавшем батче.

    Несёт lost_sources/lost_reasons (разбор 2026-07-27): при полном провале
    _result не вызывается вообще, поэтому наружу уходил один текст исключения —
    ни имён потерянных источников, ни причины отказа Apify. Ночью 27.07 сбор
    встал из-за потолка трат аккаунта, и ровно эти данные пришлось доставать
    руками из чужого API.
    """

    def __init__(self, message, lost_sources=None, lost_reasons=None):
        super().__init__(message)
        self.lost_sources = list(lost_sources or [])
        self.lost_reasons = list(lost_reasons or [])


def _age_days(value, now):
    """JS `new Date(x)` + разница в сутках. Сюда приходят ISO-строки (created_at
    из to_iso, timestamp скрейпера); пусто/мусор/не-строка -> None (возрастные
    фильтры фейл-открыто пропускают, как NaN-guard в JS)."""
    if not value or not isinstance(value, str):
        return None
    dt = _parse_date_string(value.strip())
    if dt is None:
        return None
    return (now - dt).total_seconds() / 86400


def _count_failed_batches(losses):
    # Уникальные упавшие батчи по batch_index (retry одного батча не двоится);
    # маркеры без batch_index — каждый как один.
    idx = set()
    unknown = 0
    for r in losses:
        if r.get("batch_index") is not None:
            idx.add(r["batch_index"])
        else:
            unknown += 1
    return len(idx) + unknown


def _loss_reason(row):
    """Причина падения батча одной строкой: «батч N: <текст Apify>».

    (разбор 2026-07-27) Текст причины у маркера ЕСТЬ с самого начала
    (apify._loss_marker кладёт в __apify_error «apify run FAILED (...)»,
    «apify poll timeout after 900s», текст HTTP-ошибки старта) — его уничтожала
    сводка гейта, оставляя одни имена источников. Из-за этого «кончились деньги
    на Apify» было не отличить от «протух токен» и «актор упал».
    """
    reason = row.get("__apify_error") or row.get("error") or "apify batch failed"
    return f"батч {row.get('batch_index')}: {reason}"


def losses_detail(losses):
    """(имена потерянных источников, уникальные причины) — общее для сводки и
    для CollectGateError: в обоих местах нужны те же два перечня."""
    sources, reasons = [], []
    for r in losses:
        if isinstance(r.get("batch_sources"), list):
            sources.extend(r["batch_sources"])
        reason = _loss_reason(r)
        # Уникальность с сохранением порядка: retry одного батча даёт два
        # маркера с тем же текстом, дублировать его в Run Log незачем.
        if reason not in reasons:
            reasons.append(reason)
    return sources, reasons


def _result(kept, data_count, drops, losses):
    # Аналог return kept + console.log-сводки JS-гейтов (единая структура).
    lost, reasons = losses_detail(losses)
    return {
        "rows": kept,
        "summary": {
            "input": data_count, "kept": len(kept), "drops": drops,
            "batches_failed": _count_failed_batches(losses),
            "sources_lost": len(lost), "lost_sources": lost,
            # (разбор 2026-07-27) причины — рядом с именами: сборщики пишут их
            # в errors Run Log, иначе разбор ночного отказа опять уйдёт в API.
            "lost_reasons": reasons,
        },
    }


def tiktok_gate(items, cfg=None, now=None):
    """Гейт TikTok/Snowball (Ingestion-Gate.js: код в cf01 и cf01b идентичен).

    items — нормализованные ряды вперемешку с loss-маркерами Normalize
    (__apify_error + batch_sources/batch_index). Возвращает {rows, summary}.
    """
    cfg = {**TIKTOK_CFG, **(cfg or {})}
    now = now or datetime.now(timezone.utc)
    drops = {}

    def drop(reason):
        drops[reason] = drops.get(reason, 0) + 1

    # Разводим упавшие батчи (loss-маркеры) и реальные ряды ДО фильтрации.
    # В JS-гейте isApifyError мягче (без проверки id), но на входе гейта ряды
    # Normalize никогда не несут error — строгий util.is_apify_error эквивалентен.
    losses, data = [], []
    for row in items:
        row = row if isinstance(row, dict) else {}
        (losses if is_apify_error(row) else data).append(row)

    # Полный провал: реальных рядов ноль, но были упавшие батчи -> валим громко.
    # (разбор 2026-07-27) Вместе с текстом отдаём имена и причины: raise идёт ДО
    # _result, и без этого самый громкий отказ был как раз самым бессодержательным.
    if not data and losses:
        sources, reasons = losses_detail(losses)
        raise CollectGateError(
            "Ingestion Gate: 0 реальных рядов при %d упавших батчах — весь сбор "
            "провалился, тихий зелёный ран не пропускаем"
            % _count_failed_batches(losses),
            lost_sources=sources, lost_reasons=reasons)

    kept = []
    for row in data:
        if row.get("is_ad"):
            drop("ad")
            continue
        author = str(row.get("author") or "").lower()
        if author in cfg["block_authors"] or \
                any(p.search(author) for p in cfg["block_author_patterns"]):
            drop("blocked_author")
            continue
        lang = str(row.get("text_language") or "").lower()
        if cfg["languages_allow"] and lang not in cfg["languages_allow"]:
            drop("language")
            continue
        # каждый #тег отдельно (#a#b#c -> 3, не 1) — P1.18
        tags = len(re.findall(r"#[^\s#]+", str(row.get("caption") or "")))
        if tags > cfg["max_caption_hashtags"]:
            drop("hashtag_stuffing")
            continue
        age = _age_days(row.get("created_at"), now)
        if age is not None and age > cfg["max_age_days"]:
            drop("too_old")
            continue
        views = num(row.get("views"))
        if age is not None and age > cfg["stale_after_days"] \
                and views < cfg["min_views_stale"]:
            drop("stale_low_views")
            continue
        kept.append(row)
    return _result(kept, len(data), drops, losses)


# Строгий детект как util.is_apify_error, но id-ключи IG-актора: per-hashtag
# error внутри УСПЕШНОГО прогона (реальный item с id) — данные, не потеря.
_IG_ID_KEYS = ("id", "shortCode", "shortcode", "code", "url", "displayUrl", "videoUrl")


def _is_apify_error_ig(row):
    if not isinstance(row, dict):
        return False
    if row.get("__apify_error"):
        return True
    err = row.get("error")
    if err is None or err is False or err == "":
        return False
    return not any(row.get(k) for k in _IG_ID_KEYS)


def instagram_gate(items, cfg=None, batches=None, now=None):
    """IG-гейт (Ingestion-Gate-IG.js): выход hashtag-scraper ДО reel-scraper.

    batches — аналог $('Normalize Instagram Hashtags').itemMatching(i): список
    по индексам входа с batch_sources/batch_index батчей; None/короче входа —
    как недоступный Normalize в JS (catch: без пер-source гранулярности).
    Возвращает {rows, summary}.
    """
    cfg = {**INSTAGRAM_CFG, **(cfg or {})}
    now = now or datetime.now(timezone.utc)
    drops = {}

    def drop(reason):
        drops[reason] = drops.get(reason, 0) + 1

    # Пер-item скан ДО разворачивания массивов: успешный батч — массив рилсов
    # ({items:[]}/{data:[]}/голый список), упавший — одиночный {error}.
    losses, rows = [], []
    for i, j in enumerate(items):
        j = j if j is not None else {}
        if _is_apify_error_ig(j):
            batch_sources = []
            batch_index = j.get("batch_index")
            b = batches[i] if batches and i < len(batches) and isinstance(batches[i], dict) else {}
            if isinstance(b.get("batch_sources"), list):
                batch_sources = b["batch_sources"]
            if b.get("batch_index") is not None:
                batch_index = b["batch_index"]
            losses.append({
                "error": str(j.get("error") or j.get("__apify_error") or "apify batch failed"),
                "batch_sources": batch_sources, "batch_index": batch_index,
            })
            continue
        if isinstance(j, list):
            rows.extend(j)
            continue
        if isinstance(j, dict) and isinstance(j.get("items"), list):
            rows.extend(j["items"])
            continue
        if isinstance(j, dict) and isinstance(j.get("data"), list):
            rows.extend(j["data"])
            continue
        rows.append(j)

    # Полный провал: ни одного годного item при >=1 упавшем батче -> валим громко.
    # (разбор 2026-07-27) Ночью тут легли ВСЕ 5 hashtag-батчей IG, а причина
    # («Apify не стартует раны») до Run Log не доехала — отдаём её с исключением.
    if not rows and losses:
        sources, reasons = losses_detail(losses)
        raise CollectGateError(
            "IG Ingestion Gate: 0 годных item при %d упавших hashtag-батчах — весь "
            "сбор провалился, тихий зелёный ран не пропускаем"
            % _count_failed_batches(losses),
            lost_sources=sources, lost_reasons=reasons)

    kept = []
    for raw in rows:
        r = raw if isinstance(raw, dict) else {}
        author = str(r.get("ownerUsername") or r.get("username") or "").lower()
        if author in cfg["block_authors"] or \
                any(p.search(author) for p in cfg["block_author_patterns"]):
            drop("blocked_author")
            continue
        # у IG-эталона старый /#\S+/ (слитная цепочка = 1 тег) — фикс P1.18 туда не вносился
        tags = len(re.findall(r"#\S+", str(r.get("caption") or "")))
        if tags > cfg["max_caption_hashtags"]:
            drop("hashtag_stuffing")
            continue
        age = _age_days(r.get("timestamp"), now)
        if age is not None and age > cfg["max_age_days"]:
            drop("too_old")
            continue
        views = num(r.get("videoViewCount") or r.get("videoPlayCount"))  # отсутствуют/NaN -> 0
        # Числовое сравнение без falsy-guard: 0-view stale-пост тоже дропаем (P1.19).
        if age is not None and age > cfg["stale_after_days"] \
                and views < cfg["min_views_stale"]:
            drop("stale_low_views")
            continue
        kept.append(raw)
    return _result(kept, len(rows), drops, losses)
