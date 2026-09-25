"""Замыкание контура публикации: одна строка в CF Published Reels на опубликованный рил.

Единая логика для CLI (`cf mark-published`) и дашборда (POST /briefs/{id}/published) —
чтобы разбор reel_id из URL, подтяжка prompt_version из брифа и валидация жили в одном
месте и не расходились. Пишет строку в reels-вкладку и след в CF Run Log.
"""
import re
from urllib.parse import urlsplit

from cf.runlog import log_run, now_iso


class MarkPublishedError(ValueError):
    """Публикацию нельзя записать: неизвестный brief_id или URL не http(s).

    Наследник ValueError — вызывающий (CLI/route) ловит его и отдаёт внятную ошибку
    оператору, а не голый трейсбек; ничего в Sheets при этом не записано."""


# TikTok: .../video/<числовой id>. Instagram: /reel/<code>, /reels/<code>, /p/<code>.
_TIKTOK_RE = re.compile(r"/video/(\d+)")
_INSTAGRAM_RE = re.compile(r"/(?:reels?|p)/([^/?#]+)")


def parse_reel_id(url):
    """reel_id из URL рилса. TikTok -> числовой id из /video/<id>;
    Instagram -> код из /reel|/reels|/p/<code>; иначе -> последний непустой сегмент
    пути; если и его нет — сам URL. Никогда не падает (задача: не крашить контур)."""
    s = str(url).strip()
    m = _TIKTOK_RE.search(s)
    if m:
        return m.group(1)
    m = _INSTAGRAM_RE.search(s)
    if m:
        return m.group(1)
    segments = [seg for seg in urlsplit(s).path.split("/") if seg]
    return segments[-1] if segments else s


def _platform_from_url(url):
    """Площадка из хоста URL для колонки platform (пусто, если не распознали)."""
    host = urlsplit(str(url)).netloc.lower()
    if "tiktok" in host:
        return "tiktok"
    if "instagram" in host:
        return "instagram"
    return ""


def mark_published(sheets, brief_id, url, notes="", account="", now=None,
                   trigger_type="manual"):
    """Записать опубликованный рил в CF Published Reels + строку в Run Log.

    Валидация: URL обязан быть http(s); brief_id обязан существовать в briefs —
    иначе MarkPublishedError и НИЧЕГО не пишется (ни рил, ни лог). Строка несёт
    reel_id (разобран из URL), brief_id, platform, post_url, published_at (штамп с
    временем — сегодня через now_iso(); `now` переопределяет для тестов),
    prompt_version (подтянут из брифа), production_notes и account (слаг из реестра
    accounts cf.config.json; сверку со списком делают вызывающие — CLI и маршрут,
    по одному геттеру accounts_from_config; пустой account — легальная старая
    публикация без привязки). Возвращает записанную строку.
    """
    url = str(url).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise MarkPublishedError(f"URL рилса должен быть http(s): {url!r}")
    brief_id = str(brief_id)
    brief = next((b for b in sheets.read_rows("briefs")
                  if str(b.get("brief_id")) == brief_id), None)
    if brief is None:
        raise MarkPublishedError(
            f"бриф {brief_id!r} не найден в CF Creative Briefs — публикация не записана")

    reel_id = parse_reel_id(url)
    # M23/M32 (аудит 2026-07-24): двойной сабмит формы / ретрай CLI не плодит
    # дубли — ключ идемпотентности (brief_id, reel_id): один бриф легально
    # публикуется на двух площадках (разные reel_id), но не дважды на одной.
    for existing in sheets.read_rows("reels"):
        if (str(existing.get("brief_id")) == brief_id
                and str(existing.get("reel_id")) == str(reel_id)):
            log_run(sheets, agent="mark-published", status="success",
                    input_summary=f"brief_id={brief_id} reel_id={reel_id} — "
                                  f"уже записан, идемпотентный повтор",
                    trigger_type=trigger_type)
            return existing
    account = str(account or "").strip()
    row = {
        "reel_id": reel_id,
        "brief_id": brief_id,
        "platform": _platform_from_url(url),
        "post_url": url,
        "account": account,
        "published_at": now or now_iso(),
        "prompt_version": str(brief.get("prompt_version", "")).strip(),
        "production_notes": notes or "",
    }
    sheets.append_row("reels", row)
    log_run(sheets, agent="mark-published", status="success",
            input_summary=f"brief_id={brief_id} reel_id={reel_id} url={url}"
                          + (f" account={account}" if account else ""),
            trigger_type=trigger_type)
    return row
