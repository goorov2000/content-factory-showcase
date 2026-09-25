# Этап 3 — Telegram-уведомления (спека §5): мягкая деградация, best-effort.
import httpx

import cf.notify as notify_mod
from cf.notify import notify_telegram, telegram_configured


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse()
        self.exc = exc
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.response


def secrets(tmp_path, token="123:abc", chat="42"):
    t = tmp_path / "token.txt"
    c = tmp_path / "chat.txt"
    t.write_text(token + "\n", encoding="utf-8")
    c.write_text(chat + "\n", encoding="utf-8")
    return {"telegram": {"token_file": str(t), "chat_id_file": str(c)}}


def test_sends_message(tmp_path):
    config = secrets(tmp_path)
    client = FakeClient()
    assert notify_telegram("сводка цикла", config, client=client) is True
    url, payload = client.calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert payload == {"chat_id": "42", "text": "сводка цикла",
                       "disable_web_page_preview": True}


def test_not_configured_returns_false_no_raise(tmp_path):
    config = {"telegram": {"token_file": str(tmp_path / "нет.txt"),
                           "chat_id_file": str(tmp_path / "тоже нет.txt")}}
    assert telegram_configured(config) is False
    assert notify_telegram("текст", config, client=FakeClient()) is False


def test_network_error_swallowed(tmp_path):
    config = secrets(tmp_path)
    client = FakeClient(exc=ConnectionError("сеть упала"))
    assert notify_telegram("текст", config, client=client) is False


def test_http_error_swallowed(tmp_path):
    config = secrets(tmp_path)
    client = FakeClient(response=FakeResponse(403))
    assert notify_telegram("текст", config, client=client) is False


def test_runner_notify_sends_telegram(tmp_path, monkeypatch):
    # раннер шлёт сводку цикла в Telegram, когда секреты на месте,
    # даже при пустом notify_url (замена P5.3-вебхука)
    from cf.dashboard.runner import StageRunner
    from tests.fakes import FakeSheets

    config = {**secrets(tmp_path), "dashboard": {"notify_url": ""}}
    sent = []
    monkeypatch.setattr(notify_mod, "notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    sheets = FakeSheets(tables={"run_log": [], "briefs": []})
    runner = StageRunner(sheets, config, http_post=lambda u: None,
                         run_command=lambda argv: (0, ""))
    runner._notify("factory", formulas=3, detail="ок")
    assert len(sent) == 1
    assert "3 рецепта" in sent[0]


def test_long_message_is_cut_to_api_limit(tmp_path):
    # Лимит Bot API — 4096; длиннее сообщение пропадает целиком (HTTP 400).
    # Алерт «сбор упал» рос от числа упавших батчей, то есть первым молча
    # отваливался бы отчёт о самой крупной аварии.
    config = secrets(tmp_path)
    client = FakeClient()
    assert notify_telegram("я" * 9000, config, client=client) is True
    text = client.calls[0][1]["text"]
    assert len(text) <= notify_mod.TELEGRAM_LIMIT
    assert text.endswith("«Отчёты этапов».")     # человеку сказали, где остальное


def test_broken_secret_file_does_not_raise(tmp_path):
    # Контракт модуля — «не бросать никогда». Недекодируемый файл давал
    # UnicodeDecodeError (это ValueError, не OSError), и в cli.py он летел
    # из ветки except, маскируя исходный сбой сбора.
    bad = tmp_path / "token.bin"
    bad.write_bytes(b"\xff\xfe\x00")
    config = {"telegram": {"token_file": str(bad), "chat_id_file": str(bad)}}
    assert telegram_configured(config) is False
    assert notify_telegram("текст", config, client=FakeClient()) is False


def test_null_telegram_block_does_not_raise():
    assert telegram_configured({"telegram": None}) is False
    assert notify_telegram("текст", {"telegram": None},
                           client=FakeClient()) is False


def test_http_error_log_has_no_token(tmp_path, caplog):
    # H9 (аудит 2026-07-24): HTTPStatusError несёт URL с bot-токеном — в журнал
    # уходит только тип ошибки и код статуса, не трейс и не текст исключения.
    config = secrets(tmp_path, token="123456789:SECRETBOTTOKEN")
    client = FakeClient(response=FakeResponse(429))
    import logging
    with caplog.at_level(logging.WARNING, logger="cf.notify"):
        assert notify_telegram("текст", config, client=client) is False
    assert "SECRETBOTTOKEN" not in caplog.text
    assert "429" in caplog.text


# ---------------------------------------------------------------------------
# Стоп-кран отправки (инцидент 04.09: поток одинаковых сообщений в чат)
# ---------------------------------------------------------------------------
#
# Аварийный флаг 04.09 глушил ВЕСЬ телеграм завода — включая OnFailure-алерты
# всех юнитов — бессрочно и без единого теста (он же и уронил три теста этого
# файла: сюита не изолировала боевой agent-runtime/notify-off). Здесь
# закрепляется договор: заглушка видна в логе на КАЖДОМ вызове и живёт
# ограниченный срок, после которого транспорт снова звучит.

import logging
from datetime import datetime, timedelta


def moment(text):
    return datetime.fromisoformat(text).astimezone()


def flag(tmp_path, body="", mtime=None):
    """Файл стоп-крана с заданным содержимым и временем правки."""
    path = tmp_path / "notify-off"
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        stamp = moment(mtime).timestamp()
        import os
        os.utime(path, (stamp, stamp))
    return path


def test_no_flag_means_transport_sounds(tmp_path):
    state = notify_mod.mute_state(path=tmp_path / "нет-такого", env={})
    assert state["muted"] is False and state["expired"] is False
    assert notify_mod.notifications_muted(path=tmp_path / "нет-такого",
                                          env={}) is False


def test_flag_without_a_date_lives_only_its_ttl(tmp_path):
    """Флаг без срока — это флаг «на время работ», а не навсегда: срок
    считается от времени правки файла и виден в состоянии."""
    path = flag(tmp_path, "стоп-кран на время работ", mtime="2026-09-04T14:00:00")
    inside = notify_mod.mute_state(now=moment("2026-09-04T20:00:00"), path=path,
                                   env={})
    assert inside["muted"] is True and inside["source"] == "файл"
    assert inside["until"] == moment("2026-09-04T14:00:00") + timedelta(
        hours=notify_mod.DEFAULT_MUTE_HOURS)


def test_expired_flag_lets_the_message_through(tmp_path):
    """Забытый флаг не имеет права глушить завод вечно: срок вышел —
    транспорт снова звучит, а в лог идёт напоминание убрать файл."""
    path = flag(tmp_path, "работы", mtime="2026-09-04T14:00:00")
    state = notify_mod.mute_state(now=moment("2026-09-06T09:00:00"), path=path,
                                  env={})
    assert state["muted"] is False and state["expired"] is True


def test_explicit_until_wins_over_the_default_ttl(tmp_path):
    """Оператор вправе назначить срок сам — строкой «until:» в файле."""
    path = flag(tmp_path, "работы над прогоном\nuntil: 2026-09-10T08:00:00\n",
                mtime="2026-09-04T14:00:00")
    assert notify_mod.mute_state(now=moment("2026-09-06T09:00:00"), path=path,
                                 env={})["muted"] is True
    assert notify_mod.mute_state(now=moment("2026-09-11T09:00:00"), path=path,
                                 env={})["muted"] is False


def test_until_as_a_bare_date_covers_the_whole_day(tmp_path):
    """«until: 2026-09-05» человек пишет про ВЕСЬ день, а не про его полночь."""
    path = flag(tmp_path, "until: 2026-09-05", mtime="2026-09-04T14:00:00")
    assert notify_mod.mute_state(now=moment("2026-09-05T23:00:00"), path=path,
                                 env={})["muted"] is True
    assert notify_mod.mute_state(now=moment("2026-09-06T00:30:00"), path=path,
                                 env={})["muted"] is False


def test_unparsable_until_falls_back_to_the_ttl(tmp_path):
    """Опечатка в дате не должна давать бессрочную тишину."""
    path = flag(tmp_path, "until: когда-нибудь", mtime="2026-09-04T14:00:00")
    assert notify_mod.mute_state(now=moment("2026-09-04T20:00:00"), path=path,
                                 env={})["muted"] is True
    assert notify_mod.mute_state(now=moment("2026-09-06T20:00:00"), path=path,
                                 env={})["muted"] is False


def test_env_var_mutes_only_this_process(tmp_path):
    """Переменная окружения живёт ровно столько, сколько процесс, — срок ей
    не нужен, но источник обязан быть виден."""
    state = notify_mod.mute_state(path=tmp_path / "нет", env={"CF_NOTIFY_OFF": "1"})
    assert state["muted"] is True and state["source"] == "переменная окружения"


def test_muted_send_is_refused_and_logged(tmp_path, monkeypatch, caplog):
    """Молчание должно быть ВИДНО: каждый заглушённый вызов пишет в лог, что
    именно его заглушило и до какого момента."""
    path = flag(tmp_path, "работы")
    monkeypatch.setattr(notify_mod, "NOTIFY_OFF_FILE", path)
    client = FakeClient()
    with caplog.at_level(logging.WARNING, logger="cf.notify"):
        assert notify_telegram("текст", secrets(tmp_path), client=client) is False
    assert client.calls == []
    assert "заглушены" in caplog.text and str(path) in caplog.text


def test_expired_flag_sends_and_warns(tmp_path, monkeypatch, caplog):
    path = flag(tmp_path, "работы", mtime="2026-01-01T10:00:00")
    monkeypatch.setattr(notify_mod, "NOTIFY_OFF_FILE", path)
    client = FakeClient()
    with caplog.at_level(logging.WARNING, logger="cf.notify"):
        assert notify_telegram("текст", secrets(tmp_path), client=client) is True
    assert client.calls
    assert "просрочен" in caplog.text


def test_mute_state_survives_an_unreadable_flag(tmp_path):
    """Контракт модуля — не бросать никогда, даже на битом файле флага."""
    path = tmp_path / "notify-off"
    path.write_bytes(b"\xff\xfe\x00")
    assert notify_mod.mute_state(path=path, env={})["muted"] is True


def test_directory_instead_of_a_flag_is_not_a_crash(tmp_path):
    (tmp_path / "notify-off").mkdir()
    assert notify_mod.notifications_muted(path=tmp_path / "notify-off",
                                          env={}) is True


def test_mute_note_names_the_source_and_the_deadline(tmp_path):
    """Тот, кто пришёл проверить бота, обязан узнать про стоп-кран, а не
    получить подсказку «токен, chat_id или сеть» и пойти чинить исправное."""
    path = flag(tmp_path, "работы", mtime="2026-09-04T14:00:00")
    note = notify_mod.mute_note(now=moment("2026-09-04T20:00:00"), path=path,
                                env={})
    assert "стоп-краном" in note and str(path) in note
    assert "2026-09-05T14:00" in note
    assert "не поломка бота" in note


def test_mute_note_is_empty_when_the_transport_sounds(tmp_path):
    assert notify_mod.mute_note(path=tmp_path / "нет-такого", env={}) == ""


def test_mute_note_says_when_the_env_var_is_to_blame(tmp_path):
    note = notify_mod.mute_note(path=tmp_path / "нет",
                                env={"CF_NOTIFY_OFF": "1"})
    assert "переменная окружения" in note and "до конца процесса" in note
