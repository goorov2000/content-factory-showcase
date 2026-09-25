"""Решения продюсера по формулам и нишам с дашборда.

Кнопки /lab пишут в файлы репозитория и коммитят через инъектируемый git.
Безопасность: пути жёстко проверяются на выход за пределы разрешённых папок.
"""
import logging
import re
import subprocess
from datetime import date
from pathlib import Path

from cf.cli import set_formula_status
from cf.dashboard.autogate import append_ledger
from cf.io import read_json, write_json_atomic
from cf.lock import LockTimeout, file_lock, repo_mutation_lock_path
from cf.runlog import log_run, now_iso

logger = logging.getLogger(__name__)

FORMULA_DECISIONS = {"approved", "paused", "rejected", "proposed"}
NICHE_DECISIONS = {"accepted", "rejected"}


class FormulaAlreadyApproved(ValueError):
    """Повтор approve по уже одобренной паре (рецепт, версия) — но-оп с причиной.

    Наследование от ValueError не косметика: маршрут /formulas/decision уже
    переводит ValueError в 422 с текстом исключения, поэтому честная причина
    доезжает до оператора без правки роутера.
    """


_TRAILER = "\n\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>"


def _default_git(root):
    """Возвращает callable(message, paths) -> problem|None. Best-effort, git-сбой не роняет.

    add -> diff --cached --quiet (пусто → выходим) -> commit с трейлером.
    Скоуп жёстко на переданные paths (pathspec) — чужой staged в коммит не попадёт.

    Вся последовательность git — под локом мутаций репозитория: тем же, что держат
    set_formula_status (запись index.json) и коммиттер фан-аута. Так кнопка дашборда
    не дерётся с фан-аутом за .git/index.lock и не коммитит поверх недописанного
    индекса. Сбой git по-прежнему логируется предупреждением, но дополнительно
    возвращается строкой-проблемой (кнопочный маршрут /lab её пока не показывает —
    отдельного канала problems у него нет; см. отчёт задачи).
    """
    root = str(root)

    def _git(message, paths):
        paths = list(paths)
        try:
            with file_lock(repo_mutation_lock_path(root)):
                subprocess.run(["git", "-C", root, "add", *paths],
                               capture_output=True, text=True)
                staged = subprocess.run(
                    ["git", "-C", root, "diff", "--cached", "--quiet", "--", *paths])
                if staged.returncode == 0:
                    return None  # нечего коммитить
                res = subprocess.run(
                    ["git", "-C", root, "commit", "-m", message + _TRAILER, "--", *paths],
                    capture_output=True, text=True)
                if res.returncode != 0:
                    logger.warning("git commit решения: код %s: %s",
                                   res.returncode, res.stderr)
                    return "git-коммит решения не удался"
        except LockTimeout:
            logger.warning("git-коммит решения: лок репозитория занят", exc_info=True)
            return "git-коммит решения: репозиторий занят"
        except Exception:
            logger.warning("git-коммит решения не удался", exc_info=True)
            return "git-коммит решения не удался"
        return None

    return _git


def _log(sheets, agent, name, decision):
    """Best-effort запись в run_log — сбой Sheets решение не отменяет."""
    if sheets is None:
        return
    try:
        log_run(sheets, agent=agent, status="success",
                input_summary=f"{name}: {decision}", trigger_type="dashboard")
    except Exception:
        logger.warning("%s: не удалось записать run_log", agent, exc_info=True)


def _write_decision_file(root, formula):
    """Файл решения в .claude/memory/decisions — след одобрения формулы."""
    name = formula.get("name", "")
    version = formula.get("version", "")
    niche = formula.get("niche", "")
    evidence = formula.get("evidence", {}) or {}
    urls = evidence.get("source_urls")
    n_urls = len(urls) if isinstance(urls, list) else 0

    ev = f"Evidence: {n_urls} URL"
    extras = []
    if evidence.get("avg_views") is not None:
        extras.append(f"avg_views {evidence['avg_views']}")
    if evidence.get("avg_er") is not None:
        extras.append(f"avg_er {evidence['avg_er']}")
    if extras:
        ev += ", " + ", ".join(extras)

    today = date.today().isoformat()
    lines = [
        f"# {today}: утверждена формула {name} v{version}",
        "",
        f"Ниша: {niche}.",
        ev + ".",
        "",
        "Утверждено с дашборда (кнопка), решение продюсера.",
        "",
    ]
    # имя пишут агенты — в путь только слаг, traversal в name не выведет md наружу
    slug = re.sub(r"[^\w-]+", "-", str(name)).strip("-") or "formula"
    path = root / ".claude" / "memory" / "decisions" / f"{today}-approve-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _approved_entry(root, path):
    """Запись индекса approved по той же тройке (name, niche, version) — или None.

    Кнопка «Одобрить» не одноразовая: двойной клик 26.07 дал два коммита подряд
    (278b6a1 19:01:26 и 4250977 19:01:28), второй менял ровно approved_at —
    момент решения оператора переезжал на момент лишнего клика, а в Run Log
    ложилась вторая строка dashboard-formula. Пустого diff тут не бывает никогда:
    запись индекса штампуется свежим now_iso(), так что отсечь дубль может только
    эта сверка. Сверяем ВЕРСИЮ тоже — approve следующей версии того же рецепта
    (v2 после v1) это не дубль, он обязан проходить.
    """
    try:
        formula = read_json(path)
        index = read_json(Path(root) / "formulas" / "_approved" / "index.json")
    except (FileNotFoundError, ValueError):
        return None      # нет файла/индекса или битый JSON — разберётся обычный путь
    if not isinstance(index, dict):
        return None
    for entry in index.get("approved") or []:
        if all(str(entry.get(key)) == str(formula.get(key))
               for key in ("name", "niche", "version")):
            return entry
    return None


def decide_formula(root, rel_path, decision, reason="", git=None, sheets=None,
                   actor="human"):
    """Решение по формуле: обновляет JSON + индекс approved, коммитит.

    Невалидное решение → ValueError. Небезопасный путь / нет файла → False.
    Повторный approve уже одобренной пары (рецепт, версия) → FormulaAlreadyApproved
    и ни одной записи: но-оп, а не второе «решение оператора».

    ``actor`` — кто решил: ``human`` (кнопка/CLI оператора) или ``auto`` (машинный
    чек-лист). Пишется в журнал решений formulas/_decisions.jsonl, потому что
    статус в файле рецепта отказ НЕ переживает: следующий прогон фан-аута
    перезаписывает тот же файл черновиком v+1 со статусом proposed, и «нет»
    оператора исчезает. Журнал — то, чем autogate.human_rejections удерживает
    авто-режим от возврата отклонённого рецепта.
    """
    if decision not in FORMULA_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(FORMULA_DECISIONS)}, got {decision!r}")

    root = Path(root)
    formulas_root = (root / "formulas").resolve()
    approved_root = (formulas_root / "_approved")
    resolved = (root / rel_path).resolve()

    # безопасность пути: строго внутри formulas/, .json, не под _approved
    if not (resolved.is_relative_to(formulas_root)
            and resolved.suffix == ".json"
            and not resolved.is_relative_to(approved_root)):
        return False

    # Идемпотентность по (рецепт, версия) — тем же приёмом, что mark_published
    # (cf/publish.py:73): ранний возврат ДО первой мутации. Повтор не пишет ничего —
    # ни снапшота (formulas/_approved/** иммутабельны), ни индекса, ни коммита,
    # ни строки Run Log, — и говорит оператору, что решение уже принято.
    if decision == "approved":
        entry = _approved_entry(root, resolved)
        if entry is not None:
            raise FormulaAlreadyApproved(
                f"рецепт {entry.get('name')} v{entry.get('version')} уже одобрен "
                f"({entry.get('approved_at')}) — ничего не делаю")

    try:
        formula = set_formula_status(
            resolved, decision, reason=reason,
            index_path=root / "formulas" / "_approved" / "index.json")
    except (FileNotFoundError, ValueError, KeyError, LockTimeout):
        # FileNotFoundError/ValueError — нет файла / путь вне репозитория;
        # KeyError — неполная формула (нет name/niche/version): при апруве
        # set_formula_status упадёт ДО мутации файла, откатывать нечего.
        # LockTimeout — лок мутаций репозитория занят (фан-аут пишет индекс):
        # мягкая деградация вместо необработанного 500, продюсер повторит.
        return False

    if git is None:
        git = _default_git(root)
    name = formula.get("name", "")
    niche = formula.get("niche", "")
    version = formula.get("version", "")

    # Журнал решений пишется ДО git: коммит best-effort и может не удаться, а
    # память о решении терять нельзя. Файл в formulas/ — версионируется вместе с
    # рецептами тем же коммитом ниже.
    append_ledger(root, {"at": now_iso(), "niche": niche, "name": name,
                         "version": version, "decision": decision,
                         "actor": actor, "reason": reason})

    if decision == "approved":
        _write_decision_file(root, formula)
        git(f"formula({niche}): approve {name} v{version}",
            ["formulas/", ".claude/memory/decisions/"])
    else:
        msg = f"formula({niche}): {decision} {name}"
        if reason:
            msg += f" — {reason}"
        git(msg, ["formulas/"])

    _log(sheets, "dashboard-formula", name, decision)
    return True


def decide_niche(root, filename, decision, git=None, sheets=None):
    """Решение по предложению ниши: статус в JSON, при accept — правка таксономии.

    Невалидное решение → ValueError. Небезопасное имя / нет файла → False.
    """
    if decision not in NICHE_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(NICHE_DECISIONS)}, got {decision!r}")

    # только голое имя файла — никаких путей
    if Path(filename).name != filename:
        return False

    root = Path(root)
    proposals_dir = (root / "agent-runtime" / "niche-proposals").resolve()
    resolved = (proposals_dir / filename).resolve()
    if not (resolved.is_relative_to(proposals_dir) and resolved.is_file()):
        return False

    try:
        proposal = read_json(resolved)
    except (FileNotFoundError, ValueError):
        return False

    # Таксономию читаем ДО мутации proposal (аудит M25): битый JSON единственного
    # источника истины по нишам всплывает JSONDecodeError и НЕ трактуется как
    # пустой файл — иначе accept затирал бы весь список одной новой нишей и
    # тут же коммитил потерю. Отсутствие файла — легально (первая ниша).
    tax = tax_path = None
    if decision == "accepted":
        tax_path = root / "prompts" / "agents" / "niche-taxonomy.json"
        try:
            tax = read_json(tax_path)
        except FileNotFoundError:
            tax = {"niches": []}

    proposal["status"] = decision
    write_json_atomic(resolved, proposal)  # файл в gitignored agent-runtime — не коммитим

    name = proposal.get("name", "")
    if git is None:
        git = _default_git(root)

    if decision == "accepted":
        niches = tax.get("niches", []) or []
        if name and name not in niches:  # дедуп
            niches.append(name)
        tax["niches"] = niches
        tax["updated_at"] = date.today().isoformat()
        write_json_atomic(tax_path, tax)
        git(f"niches: accept {name}", ["prompts/agents/niche-taxonomy.json"])
    # rejected: agent-runtime игнорируется git'ом — коммитить нечего

    _log(sheets, "dashboard-niche", name, decision)
    return True

