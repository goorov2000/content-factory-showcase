import time


class RetryError(RuntimeError):
    pass


# Ошибки кода/данных и отсутствующий файл ключа: повтор не поможет — всплывают сразу.
NON_RETRIABLE = (KeyError, TypeError, AttributeError, IndexError, ValueError, FileNotFoundError)


def _permanent_api_error(exc):
    # gspread APIError несёт requests.Response; 4xx не лечится ретраем, кроме
    # 429 (квота) и 408 (таймаут запроса — его ретраит и сам gspread-овский
    # BackOffHTTPClient; CF берёт обычный клиент, так что повтор здесь единственный).
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status is not None and 400 <= status < 500 and status not in (408, 429)


def _truncated_response_body(exc):
    """requests.exceptions.JSONDecodeError — наследник И OSError, И ValueError:
    так всплывает обрезанное тело ответа 200 на response.json() внутри gspread.
    ValueError в NON_RETRIABLE, поэтому без этой проверки сетевой чих не ретраился.
    Чистый ValueError (ошибка данных, UnknownFieldsError) по-прежнему мгновенный."""
    return isinstance(exc, OSError) and isinstance(exc, ValueError)


# 429-квота Sheets — поминутная: экспоненциальный backoff 1с/2с гарантированно
# сгорает внутри той же минуты (аудит M34). Пауза до следующей минуты — 65с максимум.
QUOTA_SLEEP_CAP = 65.0


def _retry_after_seconds(exc):
    """Пауза для 429: Retry-After ответа (сек), капнутый QUOTA_SLEEP_CAP;
    без заголовка — весь кап (квота обновляется на границе минуты). Не-429 -> None."""
    resp = getattr(exc, "response", None)
    if getattr(resp, "status_code", None) != 429:
        return None
    try:
        value = float(getattr(resp, "headers", {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        value = QUOTA_SLEEP_CAP
    return min(max(value, 1.0), QUOTA_SLEEP_CAP)


def _sleep(seconds):
    """Единственная точка ожидания ретраев — шов для тестов (429 ждёт до 65с,
    сюита не должна спать по-настоящему)."""
    time.sleep(seconds)


def with_retry(fn, *, attempts=3, base_delay=1.0, retriable=(Exception,),
               non_retriable=NON_RETRIABLE, label=""):
    # non_retriable проверяется раньше retriable: чтобы ретраить ошибку из
    # NON_RETRIABLE, нужно явно передать non_retriable=().
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except non_retriable as exc:
            if not _truncated_response_body(exc):
                raise
            last = exc
        except retriable as exc:
            if _permanent_api_error(exc):
                raise
            last = exc
        if attempt < attempts:
            delay = _retry_after_seconds(last)
            _sleep(delay if delay is not None
                   else base_delay * 2 ** (attempt - 1))
    raise RetryError(
        f"{label or 'operation'} failed after {attempts} attempts: {last}"
    ) from last
