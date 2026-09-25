"""Telegram-уведомления оператору (спека §5, этап 3 миграции).

Замена пустого dashboard.notify_url: сводки идут напрямую в Bot API.
Токен и chat_id — файлы в ~/.cf/secrets/ (пути в cf.config.json, блок
telegram). Деградация мягкая: секретов нет / сеть упала — warning в лог,
конвейер не блокируется; уведомление сугубо информационное.
"""
import logging
import os
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Стоп-кран отправки. Прогон инструмента, запущенный руками или агентом ради
# проверки, шлёт владельцу тот же дайджест, что и плановый, — и 04.09 это
# вылилось в поток одинаковых сообщений в чат, пока в репозитории шла работа
# над самим прогоном. Файл-флаг (а не переменная окружения) выбран потому, что
# перекрывает и чужие процессы, запущенные не из этой оболочки.
#
# У крана ТАКОГО радиуса (он глушит весь телеграм завода, включая
# OnFailure-алерты всех юнитов) обязан быть срок: аварийный флаг ставят на
# время работ, а снимать его некому — 04.09 он же молча уронил три теста этого
# модуля. Поэтому флаг живёт DEFAULT_MUTE_HOURS от времени своей правки, если
# в нём не написан явный срок строкой «until: <дата или момент>». Вышел срок —
# транспорт снова звучит и на каждом вызове напоминает убрать файл.
NOTIFY_OFF_FILE = Path(__file__).resolve().parents[2] / "agent-runtime" / "notify-off"
NOTIFY_OFF_ENV = "CF_NOTIFY_OFF"
DEFAULT_MUTE_HOURS = 24.0
# Строки-приставки срока: файл читает и правит человек, поэтому обе формы.
_UNTIL_PREFIXES = ("until:", "до:")


def _local_now():
    return datetime.now().astimezone()


def _as_local(moment):
    """Наивный момент считаем местным: флаг пишет человек, а не машина."""
    return moment if moment.tzinfo else moment.astimezone()


def _parse_moment(text):
    """Строка -> момент | None. Голая дата — это ВЕСЬ день, а не его полночь.

    «until: 2026-09-05» человек пишет про сутки целиком; истолковать это как
    00:00 значит снять кран на день раньше, чем он рассчитывал.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return _as_local(datetime.combine(date.fromisoformat(text),
                                          dtime(23, 59, 59)))
    except ValueError:
        pass
    try:
        return _as_local(datetime.fromisoformat(text))
    except ValueError:
        return None


def _until_from_file(path, now):
    """Срок стоп-крана: явный «until:» из файла, иначе время правки + TTL."""
    try:
        body = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — битый/недоступный файл флага не бросает
        body = ""
    for line in body.splitlines():
        low = line.strip().lower()
        for prefix in _UNTIL_PREFIXES:
            if low.startswith(prefix):
                moment = _parse_moment(line.strip()[len(prefix):])
                if moment is not None:
                    return moment
    # Опечатка в дате не имеет права давать бессрочную тишину — падаем на TTL.
    try:
        stamp = _as_local(datetime.fromtimestamp(path.stat().st_mtime))
    except OSError:
        stamp = now
    return stamp + timedelta(hours=DEFAULT_MUTE_HOURS)


def mute_state(now=None, path=None, env=None):
    """Состояние стоп-крана отправки. Исключений не бросает (контракт модуля).

    -> {"muted": bool — глушить ли отправку прямо сейчас,
        "source": "переменная окружения" | "файл" | "",
        "path": Path | None — чем именно заглушено,
        "until": datetime | None — до какого момента (у env срока нет: она
                 живёт ровно столько, сколько процесс),
        "expired": bool — флаг лежит, но срок вышел: транспорт снова звучит}
    """
    now = now or _local_now()
    env = os.environ if env is None else env
    if env.get(NOTIFY_OFF_ENV):
        return {"muted": True, "source": "переменная окружения", "path": None,
                "until": None, "expired": False}
    path = Path(path or NOTIFY_OFF_FILE)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        return {"muted": False, "source": "", "path": None, "until": None,
                "expired": False}
    until = _until_from_file(path, now)
    muted = now <= until
    return {"muted": muted, "source": "файл", "path": path, "until": until,
            "expired": not muted}


def mute_note(now=None, path=None, env=None):
    """Почему транспорт молчит — словами для человека. "" — не заглушено.

    Нужна инструментам оператора (`cf notify-test`, дайджест планового
    прогона): без неё они печатают «отправить не удалось: причина в журнале —
    токен, chat_id или сеть», и тот, кто пришёл проверить бота, идёт чинить
    исправное. Состояние крана известно целиком — и источник, и срок, —
    поэтому молчание обязано называть себя само.
    """
    state = mute_state(now=now, path=path, env=env)
    if not state["muted"]:
        return ""
    until = state["until"]
    when = (f", до {until.isoformat(timespec='minutes')}" if until
            else ", до конца процесса")
    return (f"уведомления заглушены стоп-краном ({state['source']} "
            f"{state['path'] or NOTIFY_OFF_ENV}{when}) — это не поломка бота: "
            f"снимите кран или дождитесь срока")


def notifications_muted(now=None, path=None, env=None):
    """True — отправка заглушена флагом (файл agent-runtime/notify-off или
    переменная CF_NOTIFY_OFF). Молчание тут — норма, а не сбой.

    Тонкая обёртка над mute_state: подробности (чем и до какого момента) —
    там, здесь только ответ «молчать ли».
    """
    return mute_state(now=now, path=path, env=env)["muted"]


DEFAULT_TOKEN_FILE = "~/.cf/secrets/telegram-token.txt"
DEFAULT_CHAT_ID_FILE = "~/.cf/secrets/telegram-chat-id.txt"

# Жёсткий предел Bot API на sendMessage. Длиннее — HTTP 400 и сообщение
# пропадает целиком. Ловушка была реальной: алерт «сбор упал» рос линейно от
# числа упавших батчей (2615 знаков на 85 источниках, 64% лимита), то есть
# первым молча отваливался бы отчёт о самой крупной аварии.
TELEGRAM_LIMIT = 4096
_CUT_NOTE = "\n\n…остальное — в пульте, раздел «Отчёты этапов»."


def _read_secret(path):
    # Ловим ВСЁ, а не только OSError: битый/недокодируемый файл секрета даёт
    # UnicodeDecodeError (это ValueError), и он пролетал наружу, нарушая
    # обещание модуля «не бросать». В cli.py уведомление зовут из ветки
    # except — исключение оттуда маскировало исходную ошибку сбора.
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 — контракт модуля: молчать и вернуть ""
        return ""


def _credentials(config):
    # `or {}` вместо `.get(..., {})`: в конфиге может лежать явный null.
    cfg = (config or {}).get("telegram") or {}
    token = _read_secret(cfg.get("token_file") or DEFAULT_TOKEN_FILE)
    chat_id = _read_secret(cfg.get("chat_id_file") or DEFAULT_CHAT_ID_FILE)
    return token, chat_id


def fit(text):
    """Обрезать до предела Bot API, сказав человеку, где лежит остальное."""
    text = str(text)
    if len(text) <= TELEGRAM_LIMIT:
        return text
    return text[:TELEGRAM_LIMIT - len(_CUT_NOTE)] + _CUT_NOTE


def telegram_configured(config=None):
    token, chat_id = _credentials(config)
    return bool(token and chat_id)


def notify_telegram(text, config=None, client=None):
    """Отправить сообщение оператору. True — ушло; False — не настроено/сбой
    (без исключений: см. докстринг модуля)."""
    state = mute_state()
    if state["muted"]:
        # Молчание обязано быть видно: журнал юнита — единственное место, где
        # человек поймёт, почему алерт не пришёл.
        until = state["until"]
        logger.warning("telegram-уведомления заглушены (%s %s%s) — сообщение "
                       "не отправлено: %s",
                       state["source"], state["path"] or NOTIFY_OFF_ENV,
                       f", до {until.isoformat(timespec='minutes')}"
                       if until else ", до конца процесса",
                       str(text)[:120])
        return False
    if state["expired"]:
        logger.warning("стоп-кран %s просрочен с %s — уведомления снова идут; "
                       "файл пора удалить", state["path"],
                       state["until"].isoformat(timespec="minutes"))
    token, chat_id = _credentials(config)
    if not token or not chat_id:
        logger.warning("telegram не настроен (нет токена/chat_id) — "
                       "уведомление пропущено: %s", str(text)[:120])
        return False
    own = client is None
    if own:
        client = httpx.Client(timeout=10.0)
    try:
        # disable_web_page_preview: тексты ошибок несут ссылки, и Telegram
        # разворачивал каждую в карточку-превью — стена на телефоне росла вдвое.
        resp = client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": fit(text),
                                 "disable_web_page_preview": True})
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort по контракту модуля
        # Без exc_info и без текста исключения: httpx.HTTPStatusError несёт полный
        # URL с bot-токеном — трейс в journald сливал бы секрет (аудит H9).
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("telegram-уведомление не отправлено (%s%s)",
                       type(exc).__name__,
                       f", HTTP {status}" if status is not None else "")
        return False
    finally:
        if own:
            client.close()
