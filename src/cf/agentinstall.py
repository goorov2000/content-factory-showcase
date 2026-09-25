"""Установка НОВОГО агента по черновику из proposals/ — правило №3 в редакции 26.07.

Каталоги ``prompts/agents/**`` и ``.claude/commands/**`` закрыты deny-правилом
харнесса: агент кладёт черновик, устанавливает человек. До этого модуля «человек
устанавливает» означало вручную вырезать два блока из markdown и разложить по
файлам — ровно тот ручной ритуал, из-за которого промпты тем не включались неделями.

Здесь тот же размен, что в prompt_apply: одна команда вместо копипаста, но проверок
больше, чем было у рук.

1. черновик лежит в ``proposals/`` и имеет ``status: proposed``;
2. каждый блок объявлен заголовком ``## Файл N: <путь>``;
3. путь — только ``prompts/agents/<имя>.md`` или ``.claude/commands/<имя>.md``
   (иначе черновик мог бы записать что угодно куда угодно);
4. файла ещё нет: это установка НОВОГО агента, а правка действующего промпта идёт
   через /cf-propose-update и запись версии;
5. блоков ровно столько, сколько заголовков, и ни один не пуст.

Проверки идут ДО первой записи: половина установленного агента (промпт без
слэш-команды) хуже, чем неустановленный.
"""
import re
from pathlib import Path

_STATUS_RE = re.compile(r"^status:\s*(?P<status>\w+)\s*$", re.MULTILINE)
_FILE_HEADING_RE = re.compile(r"^##\s+Файл\s+\d+:\s*(?P<path>\S+)\s*$")
# Куда разрешено писать. Оба каталога закрыты от Edit-инструментов агента —
# именно поэтому установка идёт командой человека, а не записью файла.
ALLOWED_DIRS = ("prompts/agents", ".claude/commands")
_NAME_RE = re.compile(r"^[\w-]+\.md$", re.UNICODE)
# Codex-обёртка команды (генерат codex-sync) — пишется вместе с командой, иначе
# новый агент существует только для Claude Code и совместимость дрейфует.
CODEX_SKILLS_DIR = ".agents/skills"


class AgentInstallError(Exception):
    """Отказ установки с человеческой причиной."""


def _check_codex_wrappers(root, files):
    """Обёртка каждой команды черновика СОБИРАЕТСЯ (имя годится как имя скилла,
    description есть, имя не занято рукописным скиллом) — проверка целиком до
    первой записи, чтобы отказ не оставлял полуустановленного агента."""
    from cf.codexwrap import CodexWrapError, check_collision, wrapper_files
    for rel, body in files:
        rel = rel.replace("\\", "/")
        if rel.rpartition("/")[0] != ".claude/commands":
            continue
        name = rel.rpartition("/")[2].removesuffix(".md")
        try:
            check_collision(root, name)
            wrapper_files(name, body)
        except CodexWrapError as exc:
            raise AgentInstallError(str(exc)) from exc


def _sync_codex_wrappers(root, files):
    """Перегенерировать обёртки команд, записанных из черновика."""
    from cf.codexwrap import write_wrapper
    written = []
    for rel, body in files:
        rel = rel.replace("\\", "/")
        if rel.rpartition("/")[0] != ".claude/commands":
            continue
        name = rel.rpartition("/")[2].removesuffix(".md")
        written += write_wrapper(root, name, body)
    return written


def _blocks_after(lines, start):
    """Первый огороженный блок после строки start — или None."""
    fence = None
    body = []
    for line in lines[start + 1:]:
        if fence is None:
            if line.startswith("```"):
                fence = line[:3]
                continue
            if line.startswith("## "):
                return None            # следующий заголовок раньше блока
        else:
            if line.startswith(fence) and not line[len(fence):].strip():
                return "\n".join(body)
            body.append(line)
    if fence is not None:
        raise AgentInstallError("блок файла не закрыт (нет закрывающих ```)")
    return None


def require_proposed(text):
    """Статус черновика — ровно proposed (иначе применён/отклонён). Отказ — исключение.

    Отдельно от parse_draft НАМЕРЕННО: разбор не должен знать про жизненный цикл.
    Иначе уже установленный черновик становится нечитаемым, и сверить его с боевым
    файлом (не разъехались ли они) нечем."""
    status = _STATUS_RE.search(text)
    if not status or status.group("status") != "proposed":
        raise AgentInstallError(
            f"черновик в статусе «{status.group('status') if status else 'без статуса'}» "
            f"— устанавливается только proposed")


def parse_draft(text):
    """[(относительный путь, содержимое)] из черновика. Отказ — AgentInstallError."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        match = _FILE_HEADING_RE.match(line)
        if not match:
            continue
        rel = match.group("path").strip("`")
        body = _blocks_after(lines, i)
        if body is None:
            raise AgentInstallError(f"под заголовком «{rel}» нет блока с текстом")
        if not body.strip():
            raise AgentInstallError(f"блок файла «{rel}» пуст")
        out.append((rel, body.rstrip("\n") + "\n"))
    if not out:
        raise AgentInstallError(
            "в черновике нет ни одного заголовка «## Файл N: <путь>»")
    return out


def _check_target(root, rel):
    """Путь допустим, лежит внутри разрешённого каталога и ещё не занят."""
    rel = rel.replace("\\", "/")
    parent, _, name = rel.rpartition("/")
    if parent not in ALLOWED_DIRS:
        raise AgentInstallError(
            f"путь «{rel}» вне разрешённых каталогов: "
            + ", ".join(f"{d}/" for d in ALLOWED_DIRS))
    if not _NAME_RE.fullmatch(name):
        raise AgentInstallError(f"недопустимое имя файла «{name}»")
    target = (Path(root) / rel).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise AgentInstallError(f"путь «{rel}» выводит за пределы репозитория")
    if target.exists():
        raise AgentInstallError(
            f"{rel} уже существует — правка действующего промпта идёт через "
            f"/cf-propose-update, а не через установку нового агента")
    return target


# Правка ДЕЙСТВУЮЩЕГО промпта или команды агента. Отдельный путь от install:
# установка нового агента отказывается писать поверх работающего файла (отказ №4),
# и это правильно — но пути «поправить существующее» не было вовсе, а deny-правила
# харнесса не пускают туда Edit-инструменты. Получалось, что поправить инструкцию
# агента не мог никто, кроме человека вручную, — ровно та ручная петля, которую
# убрали у промптов тем 26.07.
MIN_EDIT_SHARE = 0.6   # новый текст короче 60% прежнего — подозрение на обрыв


def _check_edit(root, rel, body):
    """Правка допустима: путь разрешён, файл существует, текст цел и не обрублен."""
    rel = rel.replace("\\", "/")
    parent, _, name = rel.rpartition("/")
    if parent not in ALLOWED_DIRS:
        raise AgentInstallError(
            f"путь «{rel}» вне разрешённых каталогов: "
            + ", ".join(f"{d}/" for d in ALLOWED_DIRS))
    if not _NAME_RE.fullmatch(name):
        raise AgentInstallError(f"недопустимое имя файла «{name}»")
    target = (Path(root) / rel).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise AgentInstallError(f"путь «{rel}» выводит за пределы репозитория")
    if not target.is_file():
        raise AgentInstallError(
            f"{rel} не существует — это установка нового агента, она идёт через "
            f"cf install-agent")
    current = target.read_text(encoding="utf-8")
    if current.strip() == body.strip():
        raise AgentInstallError(f"{rel}: текст не отличается от действующего")
    # Тот же класс, что обрубленный промпт темы 27.07: текст применился зелёным и
    # потерял половину смысла. Здесь секций нет, поэтому смотрим объём и хвост.
    if len(body.strip()) < MIN_EDIT_SHARE * len(current.strip()):
        raise AgentInstallError(
            f"{rel}: новый текст короче {int(MIN_EDIT_SHARE * 100)}% прежнего "
            f"({len(body.strip())} против {len(current.strip())} символов) — "
            f"похоже на обрыв блока, а не на правку")
    tail = [l for l in body.splitlines() if l.strip()]
    tail = tail[-1].strip() if tail else ""
    if tail.endswith((":", "—", ",")) or tail.startswith("#"):
        raise AgentInstallError(f"{rel}: текст оборван на «{tail[:60]}»")
    if parent == ".claude/commands" and not body.lstrip().startswith("---"):
        raise AgentInstallError(
            f"{rel}: у слэш-команды нет frontmatter — она не подхватится")
    return target


def apply_edit(root, filename, git=None, sheets=None):
    """Применить правку действующего промпта/команды агента. Возврат: список путей.

    Версия правки — git-коммит и строка в Run Log: в отличие от промптов тем, у
    промптов агентов нет строк prompt_versions (лист хранит только brief-*-reel),
    и заводить их ради одной цифры дороже, чем читать историю файла.
    """
    root = Path(root)
    if Path(filename).name != filename:
        raise AgentInstallError("небезопасное имя черновика")
    draft = (root / "proposals" / filename).resolve()
    proposals = (root / "proposals").resolve()
    if not (draft.is_relative_to(proposals) and draft.is_file()):
        raise AgentInstallError(f"черновик proposals/{filename} не найден")

    text = draft.read_text(encoding="utf-8")
    require_proposed(text)
    files = parse_draft(text)
    targets = [(rel, _check_edit(root, rel, body), body) for rel, body in files]
    # тот же контракт, что у install: проверки (включая сборку Codex-обёртки)
    # идут ДО первой записи, иначе отказ оставляет полуприменённую правку
    _check_codex_wrappers(root, files)

    written = []
    for rel, target, body in targets:
        target.write_text(body, encoding="utf-8")
        written.append(rel)
    wrappers = _sync_codex_wrappers(root, files)
    written += wrappers

    draft.write_text(_STATUS_RE.sub("status: approved", text, count=1),
                     encoding="utf-8")

    agent = re.search(r"^agent:\s*(\S+)\s*$", text, re.MULTILINE)
    agent = agent.group(1) if agent else Path(filename).stem
    if git is None:
        from cf.dashboard.decisions import _default_git
        git = _default_git(root)
    git(f"agent({agent}): правка по черновику {filename}",
        [*ALLOWED_DIRS, *wrappers, "proposals/"])

    if sheets is not None:
        from cf.runlog import log_run
        try:
            log_run(sheets, agent="apply-agent-edit", status="success",
                    input_summary=f"{agent}: " + ", ".join(written),
                    trigger_type="cli")
        except Exception:      # noqa: BLE001 — лог не отменяет правку
            pass
    return written


def install(root, filename, git=None, sheets=None):
    """Установить агента по черновику proposals/<filename>. Возврат: список путей."""
    root = Path(root)
    if Path(filename).name != filename:
        raise AgentInstallError("небезопасное имя черновика")
    draft = (root / "proposals" / filename).resolve()
    proposals = (root / "proposals").resolve()
    if not (draft.is_relative_to(proposals) and draft.is_file()):
        raise AgentInstallError(f"черновик proposals/{filename} не найден")

    text = draft.read_text(encoding="utf-8")
    require_proposed(text)
    files = parse_draft(text)
    targets = [(rel, _check_target(root, rel), body) for rel, body in files]
    # до первой записи: полагента хуже, чем ничего
    _check_codex_wrappers(root, files)

    written = []
    for rel, target, body in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(rel)
    wrappers = _sync_codex_wrappers(root, files)
    written += wrappers

    draft.write_text(_STATUS_RE.sub("status: approved", text, count=1),
                     encoding="utf-8")

    if git is None:
        from cf.dashboard.decisions import _default_git
        git = _default_git(root)
    agent = re.search(r"^agent:\s*(\S+)\s*$", text, re.MULTILINE)
    agent = agent.group(1) if agent else Path(filename).stem
    # обёртки — точечными путями: pathspec .agents/skills целиком заметал бы в
    # коммит чужие грязные файлы скиллпака
    git(f"agent({agent}): установлен по черновику {filename}",
        [*ALLOWED_DIRS, *wrappers, "proposals/"])

    if sheets is not None:
        from cf.runlog import log_run
        try:
            log_run(sheets, agent="install-agent", status="success",
                    input_summary=f"{agent}: " + ", ".join(written),
                    trigger_type="cli")
        except Exception:      # noqa: BLE001 — лог не отменяет установку
            pass
    return written
