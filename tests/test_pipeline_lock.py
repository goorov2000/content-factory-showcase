# Аудит 2026-07-24 (H8/H10/M7/M37): межпроцессные stage-локи для CLI-путей.
import pytest

from cf import pipeline_lock as pl
from cf.lock import LockTimeout, ProcessLock, stage_lock_path


def test_try_stage_locks_all_acquired_then_conflict(tmp_path):
    locks, busy = pl.try_stage_locks(("raw", "factory"), locks_dir=tmp_path)
    assert busy is None and len(locks) == 2
    # пока держим — повторный захват не проходит (flock второго fd отклоняется)
    locks2, busy2 = pl.try_stage_locks(("raw",), locks_dir=tmp_path)
    assert busy2 == "raw" and locks2 == []
    pl.release_stage_locks(locks)
    locks3, busy3 = pl.try_stage_locks(("raw",), locks_dir=tmp_path)
    assert busy3 is None
    pl.release_stage_locks(locks3)


def test_conflict_releases_partially_taken_locks(tmp_path):
    held = ProcessLock(stage_lock_path(tmp_path, "factory"))
    assert held.acquire()
    try:
        locks, busy = pl.try_stage_locks(("raw", "factory"), locks_dir=tmp_path)
        assert busy == "factory" and locks == []
        raw = ProcessLock(stage_lock_path(tmp_path, "raw"))
        assert raw.acquire()          # raw не остался захваченным после отката
        raw.release()
    finally:
        held.release()


def test_repo_root_points_to_repository():
    assert (pl.REPO_ROOT / "src" / "cf" / "pipeline_lock.py").is_file()


def test_registry_lock_conflict_raises_locktimeout(tmp_path):
    with pl.registry_lock("tiktok", locks_dir=tmp_path):
        with pytest.raises(LockTimeout):
            with pl.registry_lock("tiktok", locks_dir=tmp_path, timeout=0.2):
                pass


def test_registry_locks_are_per_platform(tmp_path):
    with pl.registry_lock("tiktok", locks_dir=tmp_path):
        with pl.registry_lock("instagram", locks_dir=tmp_path, timeout=0.2):
            pass  # разные платформы не сериализуют друг друга


def test_locks_held_by_parent_reads_env():
    assert pl.locks_held_by_parent({pl.LOCKS_HELD_ENV: "1"}) is True
    assert pl.locks_held_by_parent({}) is False
