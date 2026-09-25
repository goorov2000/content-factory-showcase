"""Межпроцессные + внутрипроцессные локи для мутаций общего состояния репозитория.

Зачем: index.json, git-коммиты и захват звена дашборда трогаются одновременно из
разных источников — фан-аут (несколько потоков), кнопка дашборда, CLI, вторая
slash-команда. ОС-локи файлов (msvcrt.locking / fcntl.flock) действуют НА ПРОЦЕСС:
N потоков одного процесса делят дескриптор и через один только файловый лок между
собой не сериализуются. Поэтому лок совмещает два слоя:

  * threading.Lock — сериализует потоки ВНУТРИ процесса (реестр по нормализованному
    пути лок-файла: одно имя → один и тот же threading.Lock);
  * ОС-лок на лок-файле — сериализует РАЗНЫЕ процессы (и разные дескрипторы одного
    процесса) между собой.

ОС-лок автоматически снимается, если процесс умирает, — проблемы «протухшего» лока
нет. Платформа выбирается в рантайме: msvcrt на Windows, fcntl на POSIX (тесты
проходят и на рабочей Windows-машине, и в CI).
"""
import contextlib
import os
import threading
import time
from pathlib import Path

try:  # выбор ОС-примитива в рантайме
    import msvcrt  # Windows
    _WINDOWS = True
except ImportError:  # POSIX
    msvcrt = None
    _WINDOWS = False
    import fcntl


LOCKS_SUBDIR = ("agent-runtime", "locks")
REPO_MUTATION_LOCK = "repo-mutation.lock"


class LockTimeout(TimeoutError):
    """Лок не удалось захватить за отведённый таймаут."""


# Реестр внутрипроцессных локов: нормализованный путь лок-файла → threading.Lock.
# Одно имя лока → один и тот же объект threading.Lock во всём процессе.
_registry_lock = threading.Lock()
_thread_locks = {}


def _key_for(path):
    """Стабильный ключ реестра: без требования, чтобы файл существовал; с учётом
    регистронезависимости путей Windows (normcase)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _thread_lock_for(key):
    with _registry_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


def _os_lock_nb(fd):
    """Неблокирующая попытка взять эксклюзивный ОС-лок. True — взяли, False — занято."""
    try:
        if _WINDOWS:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd):
    try:
        if _WINDOWS:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:  # best-effort: дескриптор всё равно закроется следом
        pass


def repo_mutation_lock_path(repo_root):
    """Единый лок-файл мутаций репозитория: <repo_root>/agent-runtime/locks/repo-mutation.lock.

    Один и тот же путь используют set_formula_status (index.json), оба git-коммиттера
    и захват звена — чтобы запись индекса, git-коммит и правки не наезжали друг на друга.
    """
    return Path(repo_root).joinpath(*LOCKS_SUBDIR, REPO_MUTATION_LOCK)


def stage_lock_path(locks_dir, stage):
    """Лок-файл захвата звена: <locks_dir>/stage-<stage>.lock."""
    return Path(locks_dir) / f"stage-{stage}.lock"


@contextlib.contextmanager
def file_lock(path, timeout=10.0, poll=0.1):
    """Контекст-менеджер: эксклюзивный лок «path» (внутри- и межпроцессный).

    Захват: сначала внутрипроцессный threading.Lock (с таймаутом), затем ОС-лок на
    лок-файле с ретраями до timeout. Освобождение — в обратном порядке, в т.ч. при
    исключении. Не реентрантный: повторный захват того же пути из того же потока
    падает LockTimeout (внутрипроцессный слой не пускает).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock_for(_key_for(path))
    if not tlock.acquire(timeout=timeout):
        raise LockTimeout(
            f"лок «{path}» занят (внутрипроцессный), таймаут {timeout}s")
    fd = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + timeout
        while not _os_lock_nb(fd):
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"лок «{path}» занят (межпроцессный), таймаут {timeout}s")
            time.sleep(poll)
        try:
            yield
        finally:
            _os_unlock(fd)
    finally:
        if fd is not None:
            os.close(fd)
        tlock.release()


class ProcessLock:
    """Межпроцессный ОС-лок с раздельными acquire()/release().

    В отличие от file_lock (лок на время with-блока), acquire и release разнесены —
    для удержания через границы потоков/колбэков, где scope не подходит: захват звена
    берётся в _claim, а отпускается в _finish (уже в фоновом потоке). Только ОС-слой:
    сериализация потоков одного экземпляра обеспечивается вызывающим (StageRunner._lock
    + проверка state), кросс-экземплярную/кросс-процессную даёт сам ОС-лок.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        """Неблокирующе. True — взяли (или уже держим), False — занято другим fd/процессом."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        if _os_lock_nb(fd):
            self._fd = fd
            return True
        os.close(fd)
        return False

    def release(self):
        if self._fd is None:
            return
        _os_unlock(self._fd)
        os.close(self._fd)
        self._fd = None
