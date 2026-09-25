"""Парсер TikTok-субтитров — порт 1:1 n8n/*/code/Attach-TikTok-Transcript.js
(cf01-tiktok ≡ cf01b-snowball, эталон заморожен до этапа 6 миграции).

P1.17: мусор не должен пройти как транскрипт. Порядок веток критичен:
collect_text ВСЕГДА первым (реальный текст побеждает даже при error-ключе
рядом), распарсившийся JSON никогда не падает в VTT/plain-ветку — тело
ошибки даёт '' без дампа сырого JSON. Явной проверки status_code/error нет
и в JS: любой JSON без caption-ключей → ''.
"""
import json
import re
from urllib.parse import urlparse

import httpx

from cf.retry import with_retry

_CAPTION_KEYS = ("text", "caption", "content", "line", "utf8")
_NESTED_KEYS = ("segments", "captions", "subtitles", "utterances", "events", "body", "segs")
# ??-цепочка обёрток из JS: subtitle_raw (n8n HTTP-узел) / data / body / text
_WRAPPER_KEYS = ("subtitle_raw", "data", "body", "text")


def collect_text(value, acc=None):
    if acc is None:
        acc = []
    if value is None:
        return acc
    if isinstance(value, str):
        if value.strip():
            acc.append(value.strip())
        return acc
    if isinstance(value, list):
        for entry in value:
            collect_text(entry, acc)
        return acc
    if isinstance(value, dict):
        for key in _CAPTION_KEYS:
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                acc.append(v.strip())
        for key in _NESTED_KEYS:
            # JS-истинность: ''/0/None не рекурсятся; []/{} в JS truthy, но
            # рекурсия в них — no-op, так что питоновская истинность эквивалентна
            if value.get(key):
                collect_text(value[key], acc)
    return acc


def _looks_like_html(text):
    t = str(text or "").strip()
    if not t:
        return False
    if re.match(r"<!doctype", t, re.I) or re.match(r"<html", t, re.I):
        return True
    if re.search(r"</?(html|head|body|title|script|style|div|table)\b", t, re.I):
        return True
    tag_len = len("".join(re.findall(r"<[^>]+>", t)))
    return tag_len > len(t) * 0.5  # почти сплошная разметка → страница ошибки


def _squash(parts):
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _no_js_constants(value):
    # JSON.parse строже json.loads: NaN/Infinity — не JSON, уходят в plain-ветку
    raise ValueError(f"not strict JSON: {value}")


def parse_subtitle(raw):
    value = raw
    if isinstance(raw, dict):
        for key in _WRAPPER_KEYS:
            if raw.get(key) is not None:  # ??-семантика: None падает дальше, '' — нет
                value = raw[key]
                break
    if not value:
        return ""
    # Объект: сначала тянем реальный текст субтитров — он ВСЕГДА побеждает;
    # иначе '' (в т.ч. тело ошибки).
    if isinstance(value, (dict, list)):
        return _squash(collect_text(value))
    text = str(value).strip()
    if not text:
        return ""
    # Строка-JSON: collect_text ПЕРВЫМ — реальный текст побеждает даже при error-ключе рядом.
    try:
        parsed = json.loads(text, parse_constant=_no_js_constants)
    except ValueError:
        pass
    else:
        # '' если текста субтитров нет (тело ошибки/пусто) — без дампа сырого JSON
        return _squash(collect_text(parsed))
    # Не JSON: HTML-страница ошибки → '' ; иначе разбираем как VTT/plain.
    if _looks_like_html(text):
        return ""
    cleaned = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    lines = (line.strip() for line in re.split(r"\r?\n", cleaned))
    # [0-9]+ — как JS /^\d+$/ (ASCII), не юникодные цифры
    kept = [line for line in lines
            if line and "-->" not in line
            and not re.fullmatch(r"[0-9]+", line)
            and not re.match(r"WEBVTT", line, re.I)
            and not re.match(r"NOTE", line, re.I)]
    return _squash(kept)


def attach_transcript(row, body, download_ok=True):
    """Аналог Code-узла Attach TikTok Transcript: row — ряд до скачивания (base)."""
    transcript = parse_subtitle(body) if download_ok else ""
    out = dict(row)
    out["transcript_text"] = transcript or row.get("transcript_text") or ""
    out["processing_status"] = ("raw_saved_actor_transcript" if transcript
                                else "raw_saved_subtitle_download_failed")
    return out


def download_subtitle(url, client=None, token=None):
    """GET как n8n-узел Download TikTok Subtitle: maxTries=2/1s, onError=continue →
    (False, '') вместо исключения. В отличие от n8n (neverError) не-2xx — тоже
    (False, ''): текстовое тело 403 парсер не отличал от субтитров (14.09.2026).

    token подписывает ТОЛЬКО api.apify.com: hashtag-scraper хранит .vtt в
    приватном KV-store рана (без подписи — 403, смоук 10.08), а CDN-ссылки
    TikTok чужому хосту секрет видеть не должны (правило №5)."""
    own = client is None
    if own:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
    headers = None
    if token and urlparse(url).hostname == "api.apify.com":
        headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = with_retry(lambda: client.get(url, headers=headers),
                          attempts=2, base_delay=1.0,
                          label="download subtitle")
        # Статус проверяем, как download_media (ревью 14.09.2026): парсер отсекает
        # только JSON и HTML, а короткое текстовое тело 403 («Forbidden») проходило
        # насквозь и записывалось как расшифровка.
        status = getattr(resp, "status_code", 200)
        if not 200 <= int(status) < 300:
            return False, ""
        return True, resp.text
    except Exception:
        return False, ""
    finally:
        if own:
            client.close()
