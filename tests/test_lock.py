import threading

import pytest

from cf.lock import LockTimeout, ProcessLock, file_lock, repo_mutation_lock_path


# ── file_lock: базовая семантика ─────────────────────────────────────────────

def test_file_lock_second_acquire_from_same_thread_times_out(tmp_path):
    # Не-реентрантный: тот же поток, тот же путь → второй захват падает по таймауту.
    lockfile = tmp_path / "x.lock"
    with file_lock(lockfile, timeout=0.5):
        with pytest.raises(LockTimeout):
            with file_lock(lockfile, timeout=0.3):
                pass


def test_file_lock_released_then_reacquired_succeeds(tmp_path):
    lockfile = tmp_path / "x.lock"
    with file_lock(lockfile, timeout=0.5):
        pass
    # после выхода лок свободен — повторный захват не падает
    with file_lock(lockfile, timeout=0.5):
        pass


def test_lock_timeout_message_is_clear(tmp_path):
    lockfile = tmp_path / "x.lock"
    with file_lock(lockfile, timeout=0.3):
        with pytest.raises(LockTimeout) as ei:
            with file_lock(lockfile, timeout=0.2):
                pass
    msg = str(ei.value)
    assert "лок" in msg and (str(lockfile) in msg or "x.lock" in msg)


def test_file_lock_creates_parent_dir(tmp_path):
    lockfile = tmp_path / "sub" / "deep" / "x.lock"
    with file_lock(lockfile, timeout=0.5):
        assert lockfile.parent.is_dir()


def test_repo_mutation_lock_path_is_under_agent_runtime_locks(tmp_path):
    p = repo_mutation_lock_path(tmp_path)
    assert p.name == "repo-mutation.lock"
    assert p.parent.name == "locks"
    assert p.parent.parent.name == "agent-runtime"


# ── ProcessLock: кросс-дескрипторная (эмуляция кросс-процессной) блокировка ───

def test_process_lock_second_descriptor_refused(tmp_path):
    # ОС-лок держится на fd; второй дескриптор того же файла (эмуляция второго
    # процесса) не может взять лок, пока держит первый. Фиделити: тот же процесс,
    # другой fd — msvcrt/flock блокируют между разными открытыми описаниями файла.
    path = tmp_path / "stage-x.lock"
    a = ProcessLock(path)
    assert a.acquire() is True
    b = ProcessLock(path)
    assert b.acquire() is False          # занято дескриптором A
    a.release()
    assert b.acquire() is True           # A отпустил — теперь свободно
    b.release()
