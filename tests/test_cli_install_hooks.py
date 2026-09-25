# Аудит 2026-07-24 (H18): install-hooks писал Windows-путь .venv/Scripts/python
# и не ставил exec-бит — на Linux git молча пропускал неисполнимый хук.
import os

from cf.cli import cmd_install_hooks


def test_hook_linux_path_and_exec_bit(tmp_path, monkeypatch):
    (tmp_path / ".git/hooks").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    assert cmd_install_hooks(None, None) == 0

    hook = tmp_path / ".git/hooks/pre-commit"
    text = hook.read_text(encoding="utf-8")
    assert ".venv/bin/python -m cf check-commit" in text
    assert "Scripts" not in text
    assert os.access(hook, os.X_OK)
