# C1.4 — порт парсера субтитров: JS-эталон n8n/cf01-tiktok/code/Attach-TikTok-Transcript.js
# (копия в cf01b-snowball байт-в-байт идентична). Инварианты: n8n/__tests__/subtitle.test.js
# (все 21, P1.17 — мусор не должен пройти как транскрипт); имя JS-теста — в комментарии
# над каждым pytest. Плюс download_subtitle на моке httpx-клиента (в JS сеть — узел n8n).
from pathlib import Path

import pytest

import httpx

from cf.collect.subtitles import (
    attach_transcript,
    collect_text,
    download_subtitle,
    parse_subtitle,
)

ROOT = Path(__file__).resolve().parents[1]
TIKTOK_ATTACH = "n8n/cf01-tiktok/code/Attach-TikTok-Transcript.js"
SNOWBALL_ATTACH = "n8n/cf01b-snowball/code/Attach-TikTok-Transcript.js"


# --- parse_subtitle: тела ошибок ---

# JS: "parseSubtitle: 403 JSON error body -> пусто"
def test_parse_403_json_error_body_empty():
    body = '{"status_code":10204,"status_msg":"item doesn\'t exist"}'
    assert parse_subtitle(body) == ""


# JS: "parseSubtitle: JSON error с statusCode/error -> пусто"
def test_parse_json_error_status_code_error_empty():
    assert parse_subtitle('{"statusCode":403,"error":"Forbidden"}') == ""


# JS: "parseSubtitle: HTML-страница ошибки (doctype) -> пусто"
def test_parse_html_doctype_empty():
    assert parse_subtitle("<!doctype html><html><body>403 Forbidden</body></html>") == ""


# JS: "parseSubtitle: HTML без doctype, но со структурными тегами -> пусто"
def test_parse_html_without_doctype_empty():
    assert parse_subtitle(
        "<html><head><title>Error</title></head><body>Access Denied</body></html>"
    ) == ""


# --- parse_subtitle: JSON3 ---

# JS: "parseSubtitle: валидный JSON3 {events:[{segs:[{utf8}]}]} -> текст (fix segs)"
def test_parse_json3_segs_utf8():
    assert parse_subtitle('{"events":[{"segs":[{"utf8":"привет"}]}]}') == "привет"


# JS: "parseSubtitle: JSON3 с несколькими segs -> склейка"
def test_parse_json3_multiple_segs_joined():
    raw = '{"events":[{"segs":[{"utf8":"привет"},{"utf8":" мир"}]}]}'
    assert parse_subtitle(raw) == "привет мир"


# JS: "collectText рекурсит в segs"
def test_collect_text_recurses_into_segs():
    assert collect_text({"events": [{"segs": [{"utf8": "a"}, {"utf8": "b"}]}]}) == ["a", "b"]


# --- caption-wins (fix #3: текст ВСЕГДА побеждает error-ключ рядом) ---

# JS: "caption-wins: JSON3 c error:null рядом -> реальный текст (строка)"
def test_caption_wins_error_null_string():
    raw = '{"events":[{"segs":[{"utf8":"real caption"}]}],"error":null}'
    assert parse_subtitle(raw) == "real caption"


# JS: "caption-wins: JSON3 c error:null рядом -> реальный текст (объект)"
def test_caption_wins_error_null_object():
    raw = {"events": [{"segs": [{"utf8": "real caption"}]}], "error": None}
    assert parse_subtitle(raw) == "real caption"


# JS: "caption-wins: {text:\"real caption\", error:\"x\"} -> реальный текст"
def test_caption_wins_text_key_with_error():
    assert parse_subtitle('{"text":"real caption","error":"x"}') == "real caption"
    assert parse_subtitle({"text": "real caption", "error": "x"}) == "real caption"


# JS: "caption-wins: чистое тело ошибки без текста -> \"\" (не изменилось)"
def test_caption_wins_pure_error_body_empty():
    assert parse_subtitle('{"status_code":10204,"status_msg":"gone"}') == ""
    assert parse_subtitle({"status_code": 10204, "status_msg": "gone"}) == ""


# --- parse_subtitle: VTT / plain / сырой JSON ---

# JS: "parseSubtitle: нормальный VTT -> корректный текст"
def test_parse_vtt():
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nПривет мир\n\n"
           "00:00:03.000 --> 00:00:05.000\nкак дела")
    assert parse_subtitle(vtt) == "Привет мир как дела"


# JS: "parseSubtitle: простой plain-caption -> сам текст"
def test_parse_plain_caption():
    assert parse_subtitle("Just a plain caption line") == "Just a plain caption line"


# JS: "parseSubtitle: JSON без caption-ключей -> пусто (НЕ сырой дамп)"
def test_parse_json_without_caption_keys_no_raw_dump():
    body = '{"foo":"bar","nested":{"baz":1}}'
    out = parse_subtitle(body)
    assert out == ""
    assert "foo" not in out, "сырой JSON не должен утечь в транскрипт"


# --- attach_transcript e2e (в JS — прогон Code-узла через runNode) ---

def run_attach(base, payload):
    # аналог runAttach: base — ряд из 'Has TikTok Subtitle URL?', payload — $json HTTP-узла
    return attach_transcript(base, payload, download_ok=True)


# JS: "Attach e2e: JSON error body (в $json.body) -> статус failed, транскрипт не мусор"
def test_attach_json_error_body_failed_no_garbage():
    base = {"video_id": "v1", "transcript_text": ""}
    out = run_attach(base, {"body": '{"status_code":10204,"status_msg":"gone"}'})
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == ""
    assert "status_code" not in str(out["transcript_text"])


# JS: "Attach e2e: JSON error body напрямую в $json (объект) -> статус failed"
def test_attach_json_error_object_failed():
    base = {"video_id": "v1", "transcript_text": ""}
    out = run_attach(base, {"status_code": 10204, "status_msg": "gone"})
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == ""


# JS: "Attach e2e: HTML error body -> статус failed, без текста страницы"
def test_attach_html_error_body_failed():
    base = {"video_id": "v1", "transcript_text": ""}
    out = run_attach(base, {"body": "<html><body>Access Denied</body></html>"})
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == ""
    assert "Access" not in str(out["transcript_text"])


# JS: "Attach e2e: провал скачивания -> транскрипт откатывается на base.transcript_text"
def test_attach_failed_download_rolls_back_to_base_transcript():
    base = {"video_id": "v1", "transcript_text": "актёрский транскрипт"}
    out = run_attach(base, {"body": '{"status_code":10204}'})
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == "актёрский транскрипт"


# JS: "Attach e2e: успешный VTT -> транскрипт и статус success"
def test_attach_vtt_success():
    base = {"video_id": "v1", "transcript_text": ""}
    out = run_attach(base, {"body": "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nПривет"})
    assert out["processing_status"] == "raw_saved_actor_transcript"
    assert out["transcript_text"] == "Привет"


# JS: "Attach e2e: успешный JSON3 -> транскрипт и статус success"
def test_attach_json3_success():
    base = {"video_id": "v1", "transcript_text": ""}
    out = run_attach(base, {"body": '{"events":[{"segs":[{"utf8":"текст"}]}]}'})
    assert out["processing_status"] == "raw_saved_actor_transcript"
    assert out["transcript_text"] == "текст"


# JS: "обе Attach-копии байт-в-байт идентичны"
@pytest.mark.skipif(not (ROOT / TIKTOK_ATTACH).is_file(),
                    reason="n8n/ (JS-эталон) отсутствует в витринной копии")
def test_both_attach_copies_byte_identical():
    a = (ROOT / TIKTOK_ATTACH).read_bytes()
    b = (ROOT / SNOWBALL_ATTACH).read_bytes()
    assert a == b, "cf01-tiktok и cf01b-snowball Attach должны совпадать"


# --- download_subtitle: мок httpx-клиента; параметры n8n-узла Download TikTok Subtitle
# (neverError: любой HTTP-статус отдаёт тело; maxTries=2; onError=continue) ---

class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.headers_seen = []

    def get(self, url, headers=None):
        self.calls.append(url)
        self.headers_seen.append(headers)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, _Resp):
            return out
        return _Resp(out)


def test_download_ok_returns_body():
    client = _FakeClient(["WEBVTT\n\nтекст"])
    ok, body = download_subtitle("https://sub.example/x.vtt", client=client)
    assert ok is True
    assert body == "WEBVTT\n\nтекст"
    assert client.calls == ["https://sub.example/x.vtt"]


def test_download_retries_transport_error(monkeypatch):
    monkeypatch.setattr("cf.retry.time.sleep", lambda s: None)
    client = _FakeClient([httpx.ConnectError("boom"), "ok-body"])
    ok, body = download_subtitle("https://sub.example/x.vtt", client=client)
    assert ok is True
    assert body == "ok-body"
    assert len(client.calls) == 2


def test_download_fails_after_max_tries(monkeypatch):
    monkeypatch.setattr("cf.retry.time.sleep", lambda s: None)
    client = _FakeClient([httpx.ConnectError("a"), httpx.ConnectError("b")])
    ok, body = download_subtitle("https://sub.example/x.vtt", client=client)
    assert ok is False
    assert body == ""
    assert len(client.calls) == 2  # maxTries=2 как в n8n-узле, без исключения наружу


def test_download_error_body_flows_to_failed_status():
    # neverError: 403-тело приходит как body — мусор отсекает парсер, не транспорт
    client = _FakeClient(['{"status_code":10204,"status_msg":"gone"}'])
    ok, body = download_subtitle("https://sub.example/x.vtt", client=client)
    assert ok is True
    out = attach_transcript({"video_id": "v1", "transcript_text": ""}, body, ok)
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == ""


# --- подпись KV-store-ссылок токеном (смоук 10.08: hashtag-scraper хранит .vtt
# в приватном KV-store рана, без Authorization — 403) ---

def test_download_signs_apify_kv_store_url_with_token():
    client = _FakeClient(["WEBVTT\n\nтекст"])
    ok, _ = download_subtitle(
        "https://api.apify.com/v2/key-value-stores/S/records/x.vtt",
        client=client, token="tok-123")
    assert ok is True
    assert client.headers_seen == [{"Authorization": "Bearer tok-123"}]


def test_download_never_signs_foreign_host():
    # Правило №5: CDN TikTok (и любой не-apify хост) секрета видеть не должен
    client = _FakeClient(["WEBVTT\n\nтекст"])
    download_subtitle("https://v16-webapp.tiktok.com/x.vtt",
                      client=client, token="tok-123")
    assert client.headers_seen == [None]


def test_download_without_token_sends_no_header():
    client = _FakeClient(["WEBVTT\n\nтекст"])
    download_subtitle("https://api.apify.com/v2/key-value-stores/S/records/x.vtt",
                      client=client)
    assert client.headers_seen == [None]


def test_attach_download_not_ok_rolls_back():
    # download_ok=False: тело игнорируется, транскрипт откатывается на прежний
    out = attach_transcript({"transcript_text": "старый"}, "что угодно", False)
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == "старый"


def test_plain_text_error_page_is_not_saved_as_transcript():
    # Ревью 14.09.2026: парсер отсекает только JSON и HTML, а CDN на протухшую ссылку
    # отвечает 403 коротким текстом — «Forbidden» уезжал в transcript_text со статусом
    # raw_saved_actor_transcript, и формула могла выводиться по такому «сценарию».
    # В бэкапе 14.08 такая строка уже есть (tiktok_7657940825103469856, Varnish 403).
    client = _FakeClient([_Resp("Forbidden", status_code=403)])
    ok, body = download_subtitle("https://sub.example/x.vtt", client=client)
    assert ok is False and body == ""
    out = attach_transcript({"video_id": "v1", "transcript_text": ""}, body, ok)
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["transcript_text"] == ""
