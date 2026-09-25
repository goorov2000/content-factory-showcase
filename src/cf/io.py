import json
import os
import tempfile
import time
from pathlib import Path

# os.replace на Windows кидает PermissionError (WinError 5/32), если цель открыта
# другим процессом (дашборд читает JSON для /lab, Яндекс.Диск синхронит файл).
# Пережидаем короткими паузами, после N попыток пробрасываем исходную ошибку.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.1  # сек между попытками


def _is_locked_error(exc):
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (5, 32)


def _replace_retrying(tmp, path):
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            if not _is_locked_error(exc) or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF)


def _fsync_dir(directory):
    """Сбросить на диск запись каталога после os.replace (ревью 14.09.2026).

    fsync файла делает durable содержимое, но не само переименование: при сбое ОС
    каталог мог откатиться к старому файлу, а `cf archive` к тому моменту уже удалил
    строки из Sheets. Стоит ~0,03 мс против ~6 мс самой записи. На Windows открыть
    каталог на чтение нельзя — там шаг пропускается.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path, writer):
    """Записать файл атомарно: temp в той же папке -> os.replace, чистка при любом сбое.

    writer получает открытый на запись файловый объект (UTF-8). Общий каркас для
    write_json_atomic / write_jsonl_atomic: половинчатый файл хуже отсутствующего.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            writer(f)
            # fsync (аудит, low): archive удаляет строки из Sheets сразу после
            # записи файла — без fsync данные могли жить только в page cache
            # и не пережить сбой ОС, оставшись единственной копией нигде.
            f.flush()
            os.fsync(f.fileno())
        _replace_retrying(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_json_atomic(path, data):
    return _atomic_write(path, lambda f: json.dump(data, f, ensure_ascii=False, indent=2))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl_atomic(path, rows):
    """JSONL-снимок: по одному JSON-объекту на строку, UTF-8, ensure_ascii=False.

    Пишется атомарно, как write_json_atomic: половинчатый бэкап хуже отсутствующего.
    """
    def writer(f):
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")

    return _atomic_write(path, writer)


def read_jsonl(path):
    """Прочитать JSONL в список dict; пустые строки пропускаем.

    Делим ровно по "\n", которым пишет write_jsonl_atomic, а не splitlines():
    тот режет ещё и по U+2028/U+2029/U+0085, которые json.dumps(ensure_ascii=False)
    оставляет внутри строк как есть. До 14.09.2026 из-за этого не читался ни один
    бэкап raw-вкладок — одна подпись с U+2028 роняла `cf restore` целиком.
    """
    rows = []
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
