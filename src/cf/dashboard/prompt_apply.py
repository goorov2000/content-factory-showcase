"""Включение промпта темы по черновику агента — ворота №2 конвейера.

Правило №3 (CLAUDE.md) запрещает менять промпты иначе как через proposal → ревью
оператора → применение + коммит. До 2026-07-26 «применение» состояло из четырёх
ручных шагов: прочитать черновик, скопировать текст в `prompts/briefs/{тема}/reel.md`,
выполнить `cf log-prompt-version`, закоммитить. Именно поэтому они не делались:
десять утверждённых рецептов в трёх темах не давали ни одного сценария.

Этот модуль превращает четыре шага в один — кнопку на воротах, и одновременно
делает применение СТРОЖЕ, чем оно было руками. Осознанный размен (решение
оператора 2026-07-26): глубина ревью падает с «переписал руками» до «прочитал и
нажал», зато проверок стало семь, и они машинные:

1. имя файла и расположение — только `proposals/YYYY-MM-DD-brief-{тема}-reel.md`;
2. статус черновика — ровно `proposed` (применённый повторно не применяется);
3. тема — в таксономии и вне `exclude_niches`;
4. sha256 показанного оператору текста — расхождение отдаёт 409 «перечитайте»;
5. в секции `## Proposed changes` ровно один markdown-блок;
6. текст не ссылается на ИЗМЕНЯЕМЫЙ черновик рецепта вместо иммутабельного
   снапшота — единственная проверка СОДЕРЖИМОГО, прямая защита evidence-цепочки
   (правило №1). Живой пример дефекта: `prompts/briefs/мужские-образы/reel.md:4`
   ссылается на `formulas/мужские-образы/short-styling-idea-reel.json`, поэтому
   содержимое активной версии может поехать без записи о версии;
7. файл промпта темы ещё не существует — правка существующего идёт через
   /cf-propose-update, а не через это звено.

Тот же код обслуживает кнопку дашборда и CLI-двойник `cf apply-brief-prompt`
(конвенция репозитория: за `mark_published` тоже стоят и маршрут, и команда) —
чтобы «включить промпт» шло одним путём и не разъехалось.
"""
import hashlib
import logging
import re
from datetime import date
from pathlib import Path

from cf.dashboard.queues import (_CANDIDATE_NAME_RE, brief_prompt_id,
                                 brief_prompt_path, fanout_exclude_niches,
                                 is_valid_niche_name)
from cf.io import read_json
from cf.runlog import log_run

logger = logging.getLogger(__name__)

# Имя черновика: proposals/YYYY-MM-DD-brief-{тема}-reel.md.
_DRAFT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-brief-(?P<niche>.+)-reel\.md$",
                            re.UNICODE)
_STATUS_RE = re.compile(r"^status:\s*(?P<status>\w+)\s*$", re.MULTILINE)


class PromptApplyError(Exception):
    """Отказ применения с человеческой причиной и HTTP-кодом для маршрута."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _draft_path(root, filename):
    """Путь черновика с жёсткой проверкой имени и расположения (отказ №1)."""
    if Path(filename).name != filename:
        raise PromptApplyError("небезопасное имя черновика")
    match = _DRAFT_NAME_RE.match(filename)
    if not match:
        raise PromptApplyError(
            "не похоже на черновик промпта темы: ожидается "
            "proposals/ГГГГ-ММ-ДД-brief-{тема}-reel.md")
    niche = match.group("niche")
    if not is_valid_niche_name(niche):
        raise PromptApplyError("небезопасное имя темы в черновике")
    proposals = (Path(root) / "proposals").resolve()
    resolved = (proposals / filename).resolve()
    if not (resolved.is_relative_to(proposals) and resolved.is_file()):
        raise PromptApplyError("черновик не найден", status=404)
    return niche, resolved


def _proposed_block(text):
    """Тело секции `## Proposed changes` — ровно один markdown-блок (отказ №5).

    Ноль блоков — применять нечего; два и больше — неизвестно, какой из них промпт,
    и «взять первый» здесь означало бы записать в боевой файл не то, что человек
    прочитал.

    Границу секции ищем ПОСТРОЧНО, с учётом ограждений блока. Наивное
    `split("\\n## ")` резало бы текст на первом же заголовке ВНУТРИ промпта — а
    промпт темы состоит из таких заголовков («## Вход», «## Задача», «## Запреты»),
    и реальный черновик «бренды-магазины» обрезался бы на 7-й строке из 163.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == "## Proposed changes"), None)
    if start is None:
        raise PromptApplyError("в черновике нет секции «## Proposed changes»")

    blocks, current, fence = [], None, None
    for line in lines[start + 1:]:
        if fence is None:
            if line.startswith("```"):
                fence, current = line[:3], []
                continue
            # заголовок второго уровня ВНЕ ограждения закрывает секцию
            if line.startswith("## "):
                break
        else:
            if line.startswith(fence) and not line[len(fence):].strip():
                blocks.append("\n".join(current))
                fence, current = None, None
                continue
            current.append(line)
    if fence is not None:
        raise PromptApplyError(
            "блок промпта в «## Proposed changes» не закрыт (нет закрывающих ```)")
    if not blocks:
        raise PromptApplyError(
            "в секции «## Proposed changes» нет блока с текстом промпта")
    if len(blocks) > 1:
        raise PromptApplyError(
            f"в секции «## Proposed changes» {len(blocks)} блоков — "
            f"непонятно, какой из них промпт темы")
    body = blocks[0].strip("\n")
    if not body.strip():
        raise PromptApplyError("блок промпта пуст")
    return body + "\n"


def _check_snapshot_links(prompt_text, niche):
    """Отказ №6: промпт обязан ссылаться на иммутабельные снапшоты рецептов.

    `formulas/{тема}/…` — рабочий черновик: он меняется следующим прогоном фан-аута,
    и активная версия промпта начала бы означать другой текст правил без записи о
    версии. Разрешена ровно одна форма упоминания черновика — запрет его читать
    (так написан промпт «бренды-магазины»), поэтому смотрим только на ссылки-пути
    вне строк с отрицанием.
    """
    bad = []
    for line in prompt_text.splitlines():
        if f"formulas/{niche}/" not in line:
            continue
        # строка-запрет («formulas/{тема}/ не читаешь») — легальна и полезна
        if re.search(r"\bне\s+(читаешь|читать|берёшь|брать)\b", line):
            continue
        bad.append(line.strip())
    if bad:
        raise PromptApplyError(
            "промпт ссылается на изменяемый черновик рецепта вместо снапшота "
            f"formulas/_approved/{niche}/: " + " | ".join(bad[:2]))


def read_draft(root, filename):
    """Черновик для показа оператору: тело промпта + sha256 для гарда подмены.

    sha256 считается от ТЕКСТА ПРОМПТА (а не от файла целиком): правка Evidence или
    Risks в черновике не должна аннулировать уже прочитанный оператором промпт,
    а вот подмена самого промпта — обязана.
    """
    niche, path = _draft_path(root, filename)
    text = path.read_text(encoding="utf-8")
    status = (_STATUS_RE.search(text) or {}) and _STATUS_RE.search(text)
    status = status.group("status") if status else ""
    prompt_text = _proposed_block(text)
    return {
        "niche": niche,
        "filename": filename,
        "path": path,
        "status": status,
        "prompt_text": prompt_text,
        "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    }


def _check_niche_allowed(root, niche):
    """Отказ №3: тема в таксономии и вне списка нецелевых."""
    if niche in fanout_exclude_niches(root):
        raise PromptApplyError(
            f"тема «{niche}» вне производства (exclude_niches) — промпт ей не нужен")
    tax_path = Path(root) / "prompts" / "agents" / "niche-taxonomy.json"
    try:
        tax = read_json(tax_path)
    except (FileNotFoundError, ValueError):
        return  # таксономии нет — не выдумываем отказ на пустом месте
    niches = tax.get("niches") or []
    if niches and niche not in niches:
        raise PromptApplyError(
            f"темы «{niche}» нет в prompts/agents/niche-taxonomy.json")


def _set_draft_status(text, status, applied=None):
    """Frontmatter черновика: status → applied/rejected + дата применения."""
    out = _STATUS_RE.sub(f"status: {status}", text, count=1)
    if applied and "\napplied:" not in out:
        out = out.replace(f"status: {status}",
                          f"status: {status}\napplied: {applied}", 1)
    return out


def _write_decision_file(root, niche, action, reason=""):
    """След решения в .claude/memory/decisions — как у решений по рецептам."""
    today = date.today().isoformat()
    title = ("включён промпт темы" if action == "applied"
             else "отклонён черновик промпта темы")
    lines = [f"# {today}: {title} {niche}", "",
             f"Тема: {niche}.",
             f"Версия промпта: {brief_prompt_id(niche)} v1."
             if action == "applied" else "Промпт не включён.",
             ""]
    if reason:
        lines += [f"Причина: {reason}.", ""]
    lines += ["Решение продюсера с дашборда (кнопка на воротах «Включение "
              "промпта темы»).", ""]
    slug = re.sub(r"[^\w-]+", "-", str(niche)).strip("-") or "niche"
    path = (Path(root) / ".claude" / "memory" / "decisions"
            / f"{today}-{action}-prompt-{slug}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def preflight(root, filename, versions=None):
    """Полный чек-лист черновика промпта темы — без единой записи.

    Семь отказов apply_prompt плюс проверки авто-режима из autogate (покрытие
    рецептов, свежесть снапшотов). Отдаёт тот же формат, что чек-лист рецепта:
    список check-словарей и признак green. Это то, что видит и оператор в карточке
    ворот, и раннер, решающий, включать ли промпт без человека.

    Показывать оператору 273 строки промпта, чтобы он нажал кнопку, — не ревью, а
    ритуал: 26.07 черновик «мужской-стиль» протух за час, и поймала это не пара
    глаз, а сверка со снапшотами. Поэтому карточка ворот показывает ЭТОТ список,
    а текст промпта — под «показать».
    """
    from cf.dashboard.autogate import make_check, prompt_draft_checks
    try:
        draft = read_draft(root, filename)
    except (PromptApplyError, OSError) as exc:
        return {"niche": "", "draft": None, "green": False,
                "checks": [make_check("draft", False, "черновик читается", str(exc))]}

    niche = draft["niche"]
    checks = [make_check("draft", True, "черновик читается", filename)]
    for key, label, probe in (
            ("status", "статус черновика",
             lambda: _require_proposed(draft)),
            ("niche_allowed", "тема в производстве",
             lambda: _check_niche_allowed(root, niche)),
            ("snapshot_links", "ссылки на снапшоты, а не на черновики",
             lambda: _check_snapshot_links(draft["prompt_text"], niche)),
            ("target_free", "промпта темы ещё нет",
             lambda: _require_no_target(root, niche))):
        try:
            probe()
        except PromptApplyError as exc:
            checks.append(make_check(key, False, label, str(exc)))
        else:
            checks.append(make_check(key, True, label, ""))
    checks += prompt_draft_checks(root, niche, draft["prompt_text"], versions)
    return {"niche": niche, "draft": draft, "checks": checks,
            "green": all(c["ok"] for c in checks),
            "reasons": [f"{c['label']}: {c['detail']}" if c["detail"] else c["label"]
                        for c in checks if not c["ok"]]}


def _require_proposed(draft):
    if draft["status"] != "proposed":                       # отказ №2
        raise PromptApplyError(
            f"черновик уже в статусе «{draft['status'] or 'без статуса'}» — "
            f"применяется только proposed")


def _require_no_target(root, niche):
    target = brief_prompt_path(root, niche)
    if target.exists():                                     # отказ №7
        raise PromptApplyError(
            f"{target.as_posix()} уже существует — правка действующего промпта "
            f"идёт через /cf-propose-update, а не через это звено")


def apply_prompt(root, filename, sha256=None, git=None, sheets=None,
                 require_green=False, versions=None):
    """Включить промпт темы по черновику. Возвращает тему; отказ — PromptApplyError.

    Порядок необратимости: сперва ВСЕ проверки, потом запись файла, затем активация
    версии в Sheets, затем статус черновика и коммит. Если Sheets недоступен,
    файл промпта уже записан — это состояние PROMPT_FILE_ONLY, оно видно на воротах
    и чинится повтором, в отличие от «версия активна, а файла нет».

    ``require_green`` — авто-режим (правило №3 в редакции 2026-07-26): включаем
    только полностью зелёный чек-лист preflight. Ручная кнопка оператора остаётся
    без этого условия НАМЕРЕННО: человек имеет право включить промпт, зная о
    предупреждении, — иначе у него не останется способа расшить тему руками.
    """
    root = Path(root)
    if require_green:
        report = preflight(root, filename, versions)
        if not report["green"]:
            raise PromptApplyError(
                "чек-лист не зелёный: " + "; ".join(report["reasons"][:3]))
    draft = read_draft(root, filename)
    niche = draft["niche"]

    _require_proposed(draft)                                # отказ №2
    _check_niche_allowed(root, niche)                       # отказ №3
    if sha256 is not None and sha256 != draft["sha256"]:    # отказ №4
        raise PromptApplyError(
            "черновик изменился с момента, когда вы его открыли — перечитайте текст",
            status=409)
    _check_snapshot_links(draft["prompt_text"], niche)      # отказ №6
    _require_no_target(root, niche)                         # отказ №7
    target = brief_prompt_path(root, niche)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(draft["prompt_text"], encoding="utf-8")

    prompt_id = brief_prompt_id(niche)
    changelog = (f"v1: промпт темы {niche} по её утверждённым рецептам "
                 f"(применён черновик {filename})")
    if sheets is not None:
        # Импорт внутри функции: cf.cli тянет тяжёлое ядро, а модуль должен
        # оставаться импортируемым в тестах шаблонов без него.
        from cf.cli import log_prompt_version
        try:
            log_prompt_version(sheets, prompt_id=prompt_id, version="v1",
                               path=target.relative_to(root).as_posix(),
                               changelog=changelog)
        except Exception as exc:  # noqa: BLE001 — откат файла важнее типа ошибки
            # Файл без строки версии — тупик: отказ №7 «уже существует» не даёт
            # повторить ни кнопке, ни авто-воротам (ревью 14.09.2026). Откатываем
            # файл, чтобы «чинится повтором» было правдой.
            target.unlink(missing_ok=True)
            raise PromptApplyError(
                f"версия промпта не записана в CF Prompt Versions ({exc}) — файл "
                f"откатан, нажмите «Включить» ещё раз (повтор безопасен)",
                status=503) from exc

    draft["path"].write_text(
        _set_draft_status(draft["path"].read_text(encoding="utf-8"), "approved",
                          applied=date.today().isoformat()),
        encoding="utf-8")
    _write_decision_file(root, niche, "applied")

    if git is None:
        from cf.dashboard.decisions import _default_git
        git = _default_git(root)
    git(f"prompt({niche}): включён промпт темы reel v1",
        ["prompts/briefs/", "proposals/", ".claude/memory/decisions/"])

    _log(sheets, "prompt-apply", "success",
         f"{prompt_id} v1 включён по черновику {filename}")
    return niche


def candidate_path(root, niche, version):
    """Файл кандидата: prompts/briefs/{тема}/reel-v{N}.md.

    Отдельный файл обязателен: log_prompt_version отказывает кандидату с тем же
    github_path, что у активной версии, — A/B двух одинаковых текстов дал бы вердикт
    по шуму."""
    return brief_prompt_path(root, niche).with_name(f"reel-v{int(version)}.md")


def read_candidate(root, filename):
    """Черновик ПРАВКИ действующего промпта: тело, тема, номер версии, sha."""
    if Path(filename).name != filename:
        raise PromptApplyError("небезопасное имя черновика")
    match = _CANDIDATE_NAME_RE.match(filename)
    if not match:
        raise PromptApplyError(
            "не похоже на правку промпта темы: ожидается "
            "proposals/ГГГГ-ММ-ДД-brief-{тема}-reel-v{N}.md")
    niche = match.group("niche")
    if not is_valid_niche_name(niche):
        raise PromptApplyError("небезопасное имя темы в черновике")
    proposals = (Path(root) / "proposals").resolve()
    path = (proposals / filename).resolve()
    if not (path.is_relative_to(proposals) and path.is_file()):
        raise PromptApplyError("черновик не найден", status=404)
    text = path.read_text(encoding="utf-8")
    status = _STATUS_RE.search(text)
    prompt_text = _proposed_block(text)
    return {"niche": niche, "version": int(match.group("version")),
            "filename": filename, "path": path,
            "status": status.group("status") if status else "",
            "prompt_text": prompt_text,
            "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()}


def candidate_preflight(root, filename, versions=None):
    """Чек-лист правки действующего промпта — без единой записи."""
    from cf.dashboard.autogate import make_check, prompt_candidate_checks
    try:
        draft = read_candidate(root, filename)
    except (PromptApplyError, OSError) as exc:
        return {"niche": "", "draft": None, "green": False, "reasons": [str(exc)],
                "checks": [make_check("draft", False, "черновик читается", str(exc))]}
    niche = draft["niche"]
    checks = [make_check("draft", True, "черновик читается", filename)]
    for key, label, probe in (
            ("status", "статус черновика", lambda: _require_proposed(draft)),
            ("niche_allowed", "тема в производстве",
             lambda: _check_niche_allowed(root, niche)),
            ("snapshot_links", "ссылки на снапшоты, а не на черновики",
             lambda: _check_snapshot_links(draft["prompt_text"], niche)),
            ("target_free", "файл этой версии ещё не написан",
             lambda: _require_free(candidate_path(root, niche, draft["version"])))):
        try:
            probe()
        except PromptApplyError as exc:
            checks.append(make_check(key, False, label, str(exc)))
        else:
            checks.append(make_check(key, True, label, ""))
    checks += prompt_candidate_checks(root, niche, draft["prompt_text"],
                                      draft["version"], versions)
    return {"niche": niche, "draft": draft, "checks": checks,
            "green": all(c["ok"] for c in checks),
            "reasons": [f"{c['label']}: {c['detail']}" if c["detail"] else c["label"]
                        for c in checks if not c["ok"]]}


def _require_free(target):
    if target.exists():
        raise PromptApplyError(
            f"{target.as_posix()} уже существует — эта версия кандидата уже написана")


def apply_candidate(root, filename, sheets=None, git=None, versions=None,
                    require_green=True):
    """Поставить правку промпта темы КАНДИДАТОМ в A/B. Возврат: (тема, путь файла).

    Активную версию не трогаем принципиально: по ней уже произведены сценарии, и
    подмена текста порвала бы связку «версия → текст → результат», на которой стоит
    eval. Кандидат встаёт рядом, генератор чередует версии между брифами одной
    формулы (cf.abtest.interleave_plan), и через неделю eval говорит, какая лучше.
    Это и есть «промпт не утверждается — он выигрывает A/B».
    """
    root = Path(root)
    if require_green:
        report = candidate_preflight(root, filename, versions)
        if not report["green"]:
            raise PromptApplyError(
                "чек-лист не зелёный: " + "; ".join(report["reasons"][:3]))
    draft = read_candidate(root, filename)
    niche, version = draft["niche"], draft["version"]
    _require_proposed(draft)
    _check_niche_allowed(root, niche)
    _check_snapshot_links(draft["prompt_text"], niche)
    target = candidate_path(root, niche, version)
    _require_free(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(draft["prompt_text"], encoding="utf-8")

    prompt_id = brief_prompt_id(niche)
    if sheets is not None:
        from cf.cli import log_prompt_version
        log_prompt_version(
            sheets, prompt_id=prompt_id, version=f"v{version}",
            path=target.relative_to(root).as_posix(),
            changelog=(f"v{version}: кандидат A/B к действующему промпту темы "
                       f"{niche} (черновик {filename})"),
            candidate=True)

    draft["path"].write_text(
        _set_draft_status(draft["path"].read_text(encoding="utf-8"), "approved",
                          applied=date.today().isoformat()),
        encoding="utf-8")

    if git is None:
        from cf.dashboard.decisions import _default_git
        git = _default_git(root)
    git(f"prompt({niche}): кандидат A/B reel v{version}",
        ["prompts/briefs/", "proposals/"])
    _log(sheets, "prompt-apply", "success",
         f"{prompt_id} v{version} поставлен кандидатом A/B по черновику {filename}")
    return niche, target


def reject_draft(root, filename, reason="", git=None, sheets=None):
    """Отклонить черновик промпта темы.

    Без этой кнопки агент писал бы новый черновик по той же теме каждый прогон:
    очередь воркера черновиков смотрит именно на `status: proposed`.
    """
    root = Path(root)
    niche, path = _draft_path(root, filename)
    text = path.read_text(encoding="utf-8")
    status = _STATUS_RE.search(text)
    status = status.group("status") if status else ""
    if status != "proposed":
        raise PromptApplyError(
            f"черновик уже в статусе «{status or 'без статуса'}»")
    path.write_text(_set_draft_status(text, "rejected"), encoding="utf-8")
    _write_decision_file(root, niche, "rejected", reason)

    if git is None:
        from cf.dashboard.decisions import _default_git
        git = _default_git(root)
    msg = f"prompt({niche}): отклонён черновик промпта темы"
    if reason:
        msg += f" — {reason}"
    git(msg, ["proposals/", ".claude/memory/decisions/"])

    _log(sheets, "prompt-apply", "skipped",
         f"черновик {filename} отклонён" + (f": {reason}" if reason else ""))
    return niche


def _log(sheets, agent, status, summary):
    """Best-effort строка в Run Log — правило №6; сбой Sheets решение не отменяет."""
    if sheets is None:
        return
    try:
        log_run(sheets, agent=agent, status=status, input_summary=summary,
                trigger_type="dashboard")
    except Exception:
        logger.warning("%s: не удалось записать run_log", agent, exc_info=True)
