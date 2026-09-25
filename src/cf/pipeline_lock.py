"""Межпроцессные локи конвейера (аудит 2026-07-24: H8/H10/M1/M7/M37).

Дашборд берёт ОС-локи звеньев только внутри StageRunner; systemd-таймеры
`cf collect ...` и `cf archive` шли мимо него — заявленный мьютекс raw↔factory
между процессами не действовал. Этот модуль даёт CLI те же stage-локи и локи
реестров источников. Каталог локов — от корня репозитория, не от CWD.

Процесс, уже держащий stage-локи (StageRunner запускает `cf collect` как
subprocess), помечает окружение CF_STAGE_LOCKS_HELD=1 — дочерний CLI локи
не берёт, иначе он бы конфликтовал с родителем.
"""
import os
from pathlib import Path

from cf.lock import ProcessLock, file_lock, stage_lock_path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCKS_DIR = REPO_ROOT / "agent-runtime" / "locks"

LOCKS_HELD_ENV = "CF_STAGE_LOCKS_HELD"


def locks_held_by_parent(environ=None):
    return (environ or os.environ).get(LOCKS_HELD_ENV) == "1"


def try_stage_locks(stages, locks_dir=None):
    """Неблокирующий захват набора stage-локов.

    Все взяты -> (список локов, None); конфликт -> ([], имя занятого звена),
    уже захваченные откатываются. Освобождение — release_stage_locks."""
    locks_dir = Path(locks_dir) if locks_dir is not None else DEFAULT_LOCKS_DIR
    taken = []
    for stage in stages:
        lock = ProcessLock(stage_lock_path(locks_dir, stage))
        if not lock.acquire():
            release_stage_locks(taken)
            return [], stage
        taken.append(lock)
    return taken, None


def release_stage_locks(locks):
    for lock in locks:
        lock.release()


def registry_lock(platform, locks_dir=None, timeout=10.0):
    """Лок реестра sources/<platform>.json (M37): collect держит его весь прогон,
    apply-sources берёт с коротким таймаутом и честно отказывает, если занято."""
    locks_dir = Path(locks_dir) if locks_dir is not None else DEFAULT_LOCKS_DIR
    return file_lock(locks_dir / f"sources-{platform}.lock", timeout=timeout)
