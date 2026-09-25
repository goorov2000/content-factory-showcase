import argparse
import subprocess
import time
from pathlib import Path

import pytest

from cf.cli import cmd_check_commit
from cf.guard import SECRET_PATTERNS, check_staged

PEM = "-----BEGIN " + "PRIVATE" + " KEY-----"
REPO_ROOT = Path(__file__).resolve().parents[1]

# Триггеры собраны конкатенацией: сам тестовый файл не должен содержать
# сплошных «секретоподобных» литералов и обязан проходить собственный guard.
# Формат: id -> (ожидаемая метка в сообщении, токен).
TOKEN_SAMPLES = {
    "apify": ("apify", "apify_api_" + "wXyZ" * 7),          # ~38 символов, как реальный токен
    "openai": ("openai", "sk-" + "Ab1" * 8),                # 24 символа после префикса
    "openai_proj": ("openai", "sk-" + "proj-" + "Ab1" * 8), # project-ключ: дефис внутри префикса
    "ghp": ("ghp", "ghp_" + "Ab1" * 12),                    # classic PAT, 36 символов
    "github_pat": ("github_pat", "github_pat_" + "Ab1_" * 8),  # fine-grained PAT
    "aws": ("aws", "AKIA" + "IOSFODNN7EXAMPLE"),            # ровно 16 [0-9A-Z]
    "jwt": ("jwt", "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"  # ключи n8n API — это JWT
            + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "sig"),
    # Telegram bot-токен (аудит 2026-07-24: проект держит telegram-token.txt и
    # шлёт уведомления — guard обязан его знать). Образец — ПРИМЕР ИЗ ДОКУМЕНТАЦИИ
    # Telegram, у него секрет в 34 знака: прежний шаблон требовал ровно 35 и
    # этот токен пропускал, а прежний тест собирал образец ровно на 35 знаков и
    # потому дыру не видел.
    # Конкатенация — как в guard.py: файл не должен сам содержать маркер,
    # иначе pre-commit guard блокирует коммит этого теста.
    "telegram": ("telegram", "110201543:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
}

# Формы, которые guard обязан ловить помимо основного образца: длины секрета и
# bot id контрактом Telegram не зафиксированы, а id со временем растут.
TELEGRAM_SHAPES = (
    ("секрет 34 знака", "110201543:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
    ("секрет 35 знаков", "8538262218:" + "A" * 35),
    ("bot id из 12 цифр", "123456789012:" + "B" * 35),
    ("старый bot id из 7 цифр", "1234567:" + "C" * 34),
)


def read_map(files):
    def read_text(path):
        if path not in files:
            raise OSError("deleted")
        return files[path]
    return read_text


def test_blocks_runtime_and_secret_paths():
    violations = check_staged(
        ["agent-runtime/evals/x.json", "secrets/key.json", "src/cf/cli.py"],
        read_map({"src/cf/cli.py": "print('ok')"}),
    )
    assert len(violations) == 2
    assert any("agent-runtime" in v for v in violations)


def test_blocks_pem_content_anywhere():
    violations = check_staged(["oops.txt"], read_map({"oops.txt": f"header\n{PEM}\n"}))
    assert len(violations) == 1
    assert "(pem)" in violations[0]


def test_unreadable_staged_file_blocked_fail_closed():
    violations = check_staged(["gone.txt"], read_map({}))
    assert len(violations) == 1


def test_clean_files_pass():
    assert check_staged(["a.py"], read_map({"a.py": "x = 1"})) == []


@pytest.mark.parametrize("label,token", TOKEN_SAMPLES.values(), ids=TOKEN_SAMPLES.keys())
def test_blocks_api_token_patterns(label, token):
    violations = check_staged(
        ["config.py"], read_map({"config.py": f"TOKEN = '{token}'\n"})
    )
    assert len(violations) == 1
    assert "config.py" in violations[0]
    assert f"({label})" in violations[0]  # сообщение называет паттерн, не сам секрет


def test_blocks_legacy_json_private_key_marker():
    field = '"private' + '_key"'
    violations = check_staged(
        ["sa.json"], read_map({"sa.json": '{%s: "-----"}' % field})
    )
    assert len(violations) == 1
    assert "(sa-json)" in violations[0]


def test_jwt_pattern_linear_on_eyj_runs():
    # Регрессия производительности: раньше префикс eyJ входил в неограниченный
    # класс, и длинный base64url-ран без точек пересканировался с каждого eyJ
    # (квадратично). Lookbehind запрещает старт матча внутри рана. Таймер
    # обязателен: без него тест — тавтология (старый паттерн тоже вернул бы
    # None, только за сотни секунд).
    pattern = dict(SECRET_PATTERNS)["jwt"]
    start = time.perf_counter()
    assert pattern.search("eyJ" * 400_000) is None
    assert time.perf_counter() - start < 1.0  # линейно ~0.01s; квадратично — сотни секунд


@pytest.mark.parametrize("snippet", [
    "skill = 'fishing'",                    # слово со 'sk' без токена
    "background: sky-blue;",                # 'sky-' — не 'sk-'
    "token = 'eyJhb'",                      # короткий JWT-огрызок
    "пример: apify_api_REDACTED",           # 8 символов после префикса — < 16
    "пример: apify_api_xxx",                # 3 символа — < 16
    "headers['X-N8N-API-KEY'] = key",       # имя заголовка — не секрет
    "digest = 'aGVsbG8gd29ybGQ='",          # короткая base64-строка
    "task-specific-configuration-value-here",            # дефисная проза
    "mode: asterisk-delimitedtokenstreamparser",         # 'sk-' внутри слова + длинный хвост
    "начало окна 07:30, конец 08:20",                    # время — не telegram-токен
    "ratio = '12345678:abcdef'",                         # хвост короче 30 символов
])
def test_legit_code_not_blocked(snippet):
    assert check_staged(["a.py"], read_map({"a.py": snippet})) == []


@pytest.mark.parametrize("doc", [
    "docs/AUDIT-2026-07.md",
    "docs/plans/2026-07-audit-fixes.md",
])
def test_committed_docs_stay_committable(doc):
    # Эти файлы уже в репозитории и упоминают apify_api_ в прозе —
    # усиленный guard не должен блокировать их повторный коммит.
    path = REPO_ROOT / doc
    assert path.exists(), f"канарейка-док переехал: {doc}"
    text = path.read_text(encoding="utf-8")
    assert check_staged([doc], read_map({doc: text})) == []


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git("init", cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=tmp_path)
    git("config", "user.name", "t", cwd=tmp_path)
    git("config", "commit.gpgsign", "false", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_check_commit_scans_staged_blob_not_working_copy(repo, capsys):
    f = repo / "config.py"
    f.write_text(f"key = '{PEM}'\n", encoding="utf-8")
    git("add", "config.py", cwd=repo)
    f.write_text("key = 'clean'\n", encoding="utf-8")  # секрет остался в индексе
    assert cmd_check_commit(None, argparse.Namespace()) == 1
    assert "config.py" in capsys.readouterr().out


def test_check_commit_scans_blob_when_worktree_file_deleted(repo, capsys):
    f = repo / "oops.txt"
    f.write_text(PEM, encoding="utf-8")
    git("add", "oops.txt", cwd=repo)
    f.unlink()
    assert cmd_check_commit(None, argparse.Namespace()) == 1


def test_check_commit_skips_staged_deletion(repo, capsys):
    f = repo / "old.txt"
    f.write_text("data", encoding="utf-8")
    git("add", "old.txt", cwd=repo)
    git("commit", "-m", "add", cwd=repo)
    git("rm", "-q", "old.txt", cwd=repo)
    assert cmd_check_commit(None, argparse.Namespace()) == 0


def test_check_commit_scans_cyrillic_filename(repo, capsys):
    # Пути от git приходят в UTF-8; декодирование локальной кодировкой (cp1251)
    # ломало путь, git show падал, а fail-open guard молча пропускал секрет.
    f = repo / "решение.md"
    f.write_text(PEM, encoding="utf-8")
    git("add", "решение.md", cwd=repo)
    assert cmd_check_commit(None, argparse.Namespace()) == 1
    assert "маркер секрета" in capsys.readouterr().out


@pytest.mark.parametrize("name,token", TELEGRAM_SHAPES,
                         ids=[n for n, _ in TELEGRAM_SHAPES])
def test_telegram_token_shapes_are_all_caught(name, token):
    # Правка 2026-08-10: шаблон требовал РОВНО 35 знаков секрета и 8-10 цифр id.
    # Мимо проходил даже пример из официальной документации Telegram, а боевой
    # токен завода спасала случайность — у него оказалось ровно 35.
    violations = check_staged(
        ["config.py"], read_map({"config.py": f"TOKEN = '{token}'\n"}))
    assert len(violations) == 1
    assert "(telegram)" in violations[0]
