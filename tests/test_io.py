import os

import pytest

import cf.io as io
from cf.io import read_json, read_jsonl, write_json_atomic, write_jsonl_atomic


def test_write_and_read_roundtrip(tmp_path):
    path = tmp_path / "sub" / "data.json"
    write_json_atomic(path, {"x": 1, "текст": "да"})
    assert read_json(path) == {"x": 1, "текст": "да"}


def test_no_tmp_leftovers(tmp_path):
    write_json_atomic(tmp_path / "d.json", [1, 2])
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_jsonl_roundtrip_preserves_types_and_unicode(tmp_path):
    rows = [{"raw_id": "t1", "views": 1000, "niche": "мужские-образы"},
            {"raw_id": "t2", "views": 0, "raw_json": '{"a": 1}'}]
    path = tmp_path / "sub" / "b.jsonl"
    write_jsonl_atomic(path, rows)
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 2                 # по строке на запись
    assert "мужские-образы" in text              # ensure_ascii=False
    assert read_jsonl(path) == rows              # типы (int) сохранены


def test_jsonl_roundtrip_survives_unicode_line_separators(tmp_path):
    """json.dumps(ensure_ascii=False) оставляет U+2028/U+2029/U+0085 как есть, а
    str.splitlines() режет по ним строку. Так 14.09.2026 не читался ни один бэкап
    raw-вкладок: одна подпись автора с U+2028 из пяти тысяч роликов роняла весь
    `cf restore` с «Unterminated string»."""
    rows = [{"raw_id": "t1", "caption": "строка\u2028вторая\u2029третья\x85"},
            {"raw_id": "t2", "raw_json": '{"text": "a\u2028b"}'}]
    path = tmp_path / "raw.jsonl"
    write_jsonl_atomic(path, rows)
    assert path.read_text(encoding="utf-8").count("\n") == 2
    assert read_jsonl(path) == rows


def test_read_jsonl_skips_blank_lines(tmp_path):
    path = tmp_path / "b.jsonl"
    path.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
    assert read_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_write_jsonl_no_tmp_leftovers(tmp_path):
    write_jsonl_atomic(tmp_path / "d.jsonl", [{"a": 1}])
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_atomic_write_retries_on_permission_error(tmp_path, monkeypatch):
    # os.replace на Windows кидает PermissionError, если файл открыт другим
    # процессом; ретрай должен переждать и довести запись до конца.
    monkeypatch.setattr(io.time, "sleep", lambda *_: None)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "locked")
        return real_replace(src, dst)

    monkeypatch.setattr(io.os, "replace", flaky)
    path = tmp_path / "d.json"
    write_json_atomic(path, {"x": 1})
    assert calls["n"] == 3
    assert read_json(path) == {"x": 1}
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_atomic_write_reraises_and_no_leak_on_persistent_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(io.time, "sleep", lambda *_: None)

    def always(src, dst):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(io.os, "replace", always)
    with pytest.raises(PermissionError):
        write_json_atomic(tmp_path / "d.json", {"x": 1})
    # временный файл не должен утечь после финального провала
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


@pytest.mark.skipif(not os.path.exists("/proc/self/fd"),
                    reason="тест читает /proc/self/fd — только Linux")
def test_atomic_write_syncs_parent_directory_after_rename(tmp_path, monkeypatch):
    # fsync файла делает durable содержимое, но не запись каталога (переименование
    # tmp -> path): при сбое ОС в этом окне на диске остался бы старый файл, а
    # `cf archive` уже удалил строки из Sheets (ревью 14.09.2026).
    synced = []
    real_fsync = os.fsync

    def spy(fd):
        synced.append(os.path.realpath(f"/proc/self/fd/{fd}"))
        return real_fsync(fd)

    monkeypatch.setattr(io.os, "fsync", spy)
    target = tmp_path / "sub" / "archive.jsonl"
    write_jsonl_atomic(target, [{"a": 1}])
    assert str(target.parent.resolve()) in synced
