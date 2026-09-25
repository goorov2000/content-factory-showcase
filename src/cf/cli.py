import argparse
import contextlib
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cf import runlog
from cf.abtest import ACTIVE_TOKENS, interleave_plan, select_versions
from cf.analyze import analyze_rows
from cf.cluster import cluster_rows
from cf.dashboard.runner import FANOUT_DEFAULTS, PROFILE_MIN_ROWS
from cf.io import read_json, read_jsonl, write_json_atomic, write_jsonl_atomic
from cf.lock import file_lock, repo_mutation_lock_path
from cf.profile import profile_rows
from cf.runlog import now_iso
from cf.sheets import Sheets, schema_drift


def force_utf8_stdio():
    """Windows-консоль по умолчанию cp1251/cp866 — кириллица в выводе ломается."""
    for stream in (sys.stdout, sys.stderr):
        if getattr(stream, "encoding", "").lower() not in ("utf-8", "utf8") \
                and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def today():
    """Точка инъекции «сегодня» для тестов: бэкапы датируются ею, ретенция считает от неё."""
    return date.today()


def apply_filters(rows, niche=None, since=None):
    if niche:
        # strip как у очереди фан-аута/cluster-other: ' стритвир ' == 'стритвир'
        target = niche.strip().lower()
        rows = [r for r in rows
                if str(r.get("niche", "")).strip().lower() == target]
    if since:
        rows = [r for r in rows if str(r.get("posted_at", "")) >= since]
    return rows


def norm_status(value):
    """review_status как у дашборда (cf.dashboard.data.brief_status): strip+lower.

    Значения набиваются руками в Sheets ('Approved', 'Rejected ', 'Pending'),
    поэтому CLI и дашборд обязаны нормализовать одинаково, иначе кап/гвардия/
    авто-одобрение/история reject расходятся."""
    return str(value).strip().lower()


def dedupe_rows(rows, key="source_url"):
    """Одна строка на source_url: побеждает последняя (самый свежий сбор)."""
    out, index = [], {}
    for r in rows:
        k = str(r.get(key, "")).strip()
        if not k:
            out.append(r)
        elif k in index:
            out[index[k]] = r
        else:
            index[k] = len(out)
            out.append(r)
    return out


def cmd_read(sheets, args):
    rows = apply_filters(sheets.read_rows(args.tab), args.niche, args.since)
    if getattr(args, "dedup", True):
        rows = dedupe_rows(rows)
    # niche-empty ПОСЛЕ дедупа: устаревшая строка без ниши не должна проходить, если
    # свежая строка того же source_url уже классифицирована (её выбирает dedupe).
    if getattr(args, "niche_empty", False):
        rows = [r for r in rows if not str(r.get("niche", "")).strip()]
    if getattr(args, "fields", None):
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        rows = [{f: r.get(f, "") for f in fields} for r in rows]
    if args.out:
        write_json_atomic(args.out, rows)
        print(f"Wrote {len(rows)} rows to {args.out}")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _started_at_arg(value):
    """--started-at: ISO8601-момент старта прогона -> канонический вид now_iso().

    Мусор НЕ роняет команду. Соблазн сделать type=-валидацию с exit 2 велик
    (тихий фолбэк — ровно тот баг, ради которого флаг заводится), но в
    headless-фан-ауте `cf log-run` — последний шаг агента: argparse exit 2
    означал бы, что строки в Run Log нет ВООБЩЕ, StageRunner._agent_logged
    объявит прогон ниши холостым и переведёт нишу в error. Железное правило №6
    («каждый запуск логируется») старше точности метрики, поэтому битое
    значение = громкое предупреждение в stderr + строка с нулевой длительностью.

    Naive-время (агент дал время без зоны) считаем UTC — часы агента и хоста
    одни и те же. Нормализация обязательна: started_at и completed_at обязаны
    лежать в колонке в ОДНОМ формате, иначе progress.median_duration и
    StageRunner._agent_logged читают разнородные строки.
    """
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        print(f"warning: --started-at {value!r} не разобрано как ISO8601 — строка "
              f"пишется с нулевой длительностью (ожидался вид "
              f"2026-07-26T09:15:00+00:00)", file=sys.stderr)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def run_started(args):
    """started_at для Run Log: момент старта процесса CLI (проставляет main()).

    Тесты зовут cmd_* с самодельным argparse.Namespace без этого поля -> None,
    и log_run ставит started_at = completed_at, как было до правки."""
    return getattr(args, "run_started_at", None)


def cmd_log_run(sheets, args):
    started = getattr(args, "started_at", None)
    # Старт в будущем — почти всегда опечатка агента. Строку всё равно пишем
    # (правило №6 важнее точности метрики), но молча врать длительностью не даём.
    if started and started > runlog.now_iso():
        print(f"warning: --started-at {started} в будущем — длительность выйдет "
              f"отрицательной и медиана её отбросит", file=sys.stderr)
    row = runlog.log_run(sheets, agent=args.agent, status=args.status,
                         input_summary=args.input or "",
                         output_paths=args.outputs, errors=args.errors,
                         trigger_type=args.trigger, started_at=started)
    note = f" started_at={row['started_at']}" if started else ""
    print(f"logged run {row['run_id']} ({args.agent}: {args.status}){note}")
    return 0


def _proposal_status(text):
    """Статус proposal из YAML-фронтматтера (первый ---...--- блок), НЕ из тела.

    Подстрока 'status: approved' может встречаться в прозе тела — считать по всему
    файлу неверно. Разбираем фронтматтер так же, как proposals.validate_proposal_text
    (первый ----делимитированный блок)."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "status":
            return value.strip()
    return None


def cmd_status(sheets, args):
    print("=== Content Factory Status ===")
    all_runs = sheets.read_rows("run_log")
    # limit<=0: явно ноль запусков. Иначе runs[-0:] в Python == runs[0:] и печатал бы ВСЁ.
    runs = all_runs[-args.limit:][::-1] if args.limit > 0 else []
    print(f"Recent runs ({len(runs)}):")
    for r in runs:
        print(f"  {str(r.get('run_id', '')):14} {str(r.get('agent', '')):22} "
              f"{str(r.get('status', '')):18} {r.get('completed_at', '')}")

    # (разбор 2026-07-27) «Отложено заводом» (кап формулы) — НЕ очередь человека:
    # решать по такому сценарию нечего, завод вернётся к нему сам
    # (cf retry-deferred). Из-за общего счёта cf status показывал 8 против 5 на
    # дашборде, и оператор шёл искать три несуществующих решения. Предикат берём
    # ТОТ ЖЕ, что у дашборда (dashboard/data.py:180) — четвёртой копии признака
    # не заводим (CLAUDE.md: три копии «готова ли тема» уже стоили 10 рецептов).
    from cf.dashboard.data import brief_is_deferred
    briefs = sheets.read_rows("briefs")
    waiting = [b for b in briefs if norm_status(b.get("review_status")) == "pending"]
    pending = [str(b.get("brief_id")) for b in waiting if not brief_is_deferred(b)]
    deferred = len(waiting) - len(pending)
    suffix = f" ({', '.join(pending)})" if pending else ""
    # Отложенные не прячем совсем: молчащий счётчик — как раз то, из-за чего
    # заблокированный тюнинг источников однажды остался невидимым.
    tail = f"; отложено заводом: {deferred}" if deferred else ""
    print(f"Pending briefs: {len(pending)}{suffix}{tail}")

    # Словарь статусов .md-proposal нигде не зафиксирован схемой: при создании
    # proposals.validate_proposal_text требует ровно `status: proposed`, дальше
    # статус правится руками. Соседний schemas/source-proposal.schema.json — про
    # ДРУГОЙ артефакт (JSON-предложение источников) и легализует там `pending`;
    # именно это слово и утекло в proposals/2026-07-26-sources-instagram.md.
    # Поэтому всё, что не в словаре, считаем отдельно и печатаем ГРОМКО: молчащий
    # счётчик прятал заблокированный тюнинг источников от оператора целиком.
    counts = {"proposed": 0, "approved": 0, "rejected": 0}
    unknown = []                       # (файл, статус) — статус вне словаря или его нет
    proposals_dir = Path(args.proposals_dir)
    if proposals_dir.exists():
        for p in sorted(proposals_dir.glob("*.md")):
            status = _proposal_status(p.read_text(encoding="utf-8"))
            if status in counts:
                counts[status] += 1
            else:
                unknown.append((p.name, status))
    total = sum(counts.values()) + len(unknown)
    tail = (f", {len(unknown)} с нераспознанным статусом (файлов всего: {total})"
            if unknown else "")
    print(f"Proposals: {counts['proposed']} proposed, "
          f"{counts['approved']} approved, {counts['rejected']} rejected{tail}")
    for name, status in unknown:
        seen = f"status: {status}" if status else "нет status во frontmatter"
        print(f"ВНИМАНИЕ: proposal {name} не попал в сводку ({seen}) — "
              f"словарь статусов: proposed/approved/rejected")

    print("Active prompts:")
    for v in sheets.read_rows("prompt_versions"):
        if _prompt_is_active(v):
            print(f"  {v.get('prompt_id', '')} {v.get('version', '')} {v.get('github_path', '')}")

    # Дрейф схемы боевых листов — тот же реестр, что у health-чека дашборда.
    # Отсутствующая колонка меняет поведение молча, поэтому печатаем громко.
    try:
        drift = schema_drift(sheets)
    except Exception as exc:            # диагностика не должна ронять статус
        drift = {}
        print(f"Схема таблиц: проверить не удалось ({exc})")
    for tab, cols in drift.items():
        name = sheets.config.get("tabs", {}).get(tab, tab)
        print(f"ВНИМАНИЕ: в листе {name} нет колонок: {', '.join(cols)} "
              f"(docs/RUNBOOK.md → «Google Sheets (колонки в живых листах)»)")
    return 0


def cmd_profile(sheets, args):
    rows = apply_filters(sheets.read_rows(args.tab), args.niche, args.since)
    report = profile_rows(rows, min_rows=args.min_rows)
    report["meta"] = {"source_tab": args.tab, "niche": args.niche,
                      "since": args.since, "generated_at": now_iso()}
    # niche в имени файла — иначе profile --niche перезаписывает общий отчёт вкладки
    niche_slug = f"-{re.sub(r'[^\w-]+', '-', args.niche).strip('-')}" if args.niche else ""
    path = Path(args.out_dir) / f"{date.today().isoformat()}-{args.tab}{niche_slug}-profile.json"
    write_json_atomic(path, report)
    status = "success" if report["ready_for_analysis"] else "insufficient_data"
    runlog.log_run(sheets, agent="raw-batch-profiler", status=status,
                   input_summary=f"{args.tab} niche={args.niche or '-'} "
                                 f"since={args.since or '-'} rows={report['total_rows']}",
                   output_paths=[str(path)],
                   started_at=run_started(args))
    print(f"profile report: {path}")
    print(f"ready_for_analysis: {report['ready_for_analysis']}")
    for issue in report["issues_list"]:
        print(f"  - {issue}")
    return 0


def cmd_analyze_batch(sheets, args):
    rows = apply_filters(sheets.read_rows(args.tab), args.niche, args.since)
    report = analyze_rows(rows, min_rows=args.min_rows, min_views=args.min_views)
    report["meta"] = {"source_tab": args.tab, "niche": args.niche,
                      "since": args.since, "generated_at": now_iso()}
    # niche в имени файла — та же защита, что и у profile
    niche_slug = f"-{re.sub(r'[^\w-]+', '-', args.niche).strip('-')}" if args.niche else ""
    path = Path(args.out_dir) / f"{date.today().isoformat()}-{args.tab}{niche_slug}-analysis.json"
    write_json_atomic(path, report)
    status = "success" if report["status"] == "ok" else "insufficient_data"
    runlog.log_run(sheets, agent="batch-analyzer", status=status,
                   input_summary=f"{args.tab} niche={args.niche} rows={report['total_rows']} "
                                 f"passed={report['passed_threshold']}",
                   output_paths=[str(path)],
                   started_at=run_started(args))
    print(f"analysis report: {path}")
    print(f"status: {report['status']}")
    if report["status"] == "insufficient_data":
        print(f"  missing_rows: {report['missing_rows']}")
    return 0


def cmd_cluster_other(sheets, args):
    # dedupe ПЕРЕД фильтром/кластеризацией (как analyze_rows/_eligible_niches): дубли
    # одного source_url иначе считаются несколько раз и раздувают кластеры.
    rows = [r for r in dedupe_rows(sheets.read_rows(args.tab))
            if str(r.get("niche", "")).strip() == "other"]
    clusters = cluster_rows(rows, min_size=args.min_size)
    report = {"tab": args.tab, "generated_at": now_iso(), "total_other": len(rows),
              "clusters": clusters}
    path = Path(args.out_dir) / f"{date.today().isoformat()}-{args.tab}-clusters.json"
    write_json_atomic(path, report)
    print(f"clusters report: {path}")
    if clusters:
        print(f"clusters: {len(clusters)}, biggest: {clusters[0]['size']}")
    else:
        print("кластеров нет")
    return 0


def cmd_validate(sheets, args):
    if args.kind == "proposal":
        from cf.proposals import validate_proposal_text
        errors = validate_proposal_text(Path(args.file).read_text(encoding="utf-8"))
    else:
        from cf.validate import validate_json_file
        errors = validate_json_file(args.kind, args.file)
    if errors:
        print(f"INVALID {args.file}:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK {args.file}")
    return 0


FORMULA_STATUSES = ["proposed", "approved", "paused", "rejected"]


def _repo_root_from_index(index_path):
    """Корень репозитория из положения индекса formulas/_approved/index.json.

    Индекс всегда лежит на три уровня ниже корня, поэтому dirname×3 даёт корень
    независимо от CWD (устойчиво к запуску CLI из любой директории)."""
    abs_index = os.path.abspath(str(index_path))
    return os.path.dirname(os.path.dirname(os.path.dirname(abs_index)))


def _slash_path(path):
    """Backslash-пути (легаси Windows: старые записи индекса, входы агентов) ->
    прямые слэши. На POSIX backslash — обычный символ имени файла, без замены
    такой путь не резолвится."""
    return str(path).replace("\\", "/")


def _canonical_formula_path(path, index_path):
    """Любой вход (абсолютный, ./, backslash, уже относительный) -> канонический
    POSIX-путь формулы относительно корня репозитория.

    Хранимый entry["path"] и ключ дедупа/удаления считаются ОДНОЙ этой функцией,
    поэтому абсолютный путь дашборда, .\\formulas\\x.json и formulas/x.json
    схлопываются в одинаковую строку. Путь вне репозитория (relpath начинается с
    .. либо падает на разных дисках Windows) -> ValueError."""
    repo_root = _repo_root_from_index(index_path)
    try:
        rel = os.path.relpath(os.path.abspath(_slash_path(path)), repo_root)
    except ValueError as exc:  # разные диски на Windows -> относительного пути нет
        raise ValueError(f"путь формулы вне репозитория: {path}") from exc
    rel = rel.replace(os.sep, "/")
    if rel == ".." or rel.startswith("../"):
        raise ValueError(f"путь формулы вне репозитория: {path}")
    return rel


_APPROVED_PREFIX = "formulas/_approved/"

# name/niche/version пишут агенты по данным соцсетей — в путь снапшота они
# попадают только слугами (буквы/цифры/дефис/подчёркивание), иначе traversal
# вида niche="../../.." вывел бы запись за пределы formulas/_approved/.
_FORMULA_SLUG_RE = re.compile(r"^[\w-]+$", re.UNICODE)


def _is_snapshot_path(canonical):
    return canonical.startswith(_APPROVED_PREFIX)


def _snapshot_rel_path(formula):
    """Канонический путь иммутабельного снапшота одобренной версии."""
    name, niche = str(formula["name"]), str(formula["niche"])
    version = str(formula["version"])
    for label, value in (("name", name), ("niche", niche), ("version", version)):
        if not _FORMULA_SLUG_RE.fullmatch(value):
            raise ValueError(
                f"формула с недопустимым {label}={value!r}: в путь снапшота "
                f"допускаются только буквы/цифры/дефис/подчёркивание")
    return f"{_APPROVED_PREFIX}{niche}/{name}-v{version}.json"


def _working_rel_path(formula):
    name, niche = str(formula["name"]), str(formula["niche"])
    for label, value in (("name", name), ("niche", niche)):
        if not _FORMULA_SLUG_RE.fullmatch(value):
            raise ValueError(
                f"формула с недопустимым {label}={value!r}: в путь рабочего файла "
                f"допускаются только буквы/цифры/дефис/подчёркивание")
    return f"formulas/{niche}/{name}.json"


def _entry_matches(entry, canonical, formula):
    """Идентичность записи индекса: по (name, niche), c fallback на путь.

    Ключ по строке пути ломается, как только пути начинают различаться по
    версии снапшота или написанию (инцидент 8156c43: запись v2 молча выпала).
    Регрессии закреплены в tests/test_formula_status.py::
    test_legacy_absolute_entry_is_replaced_not_duplicated / _removed_on_pause.
    """
    if entry.get("path") == canonical:
        return True
    name, niche = formula.get("name"), formula.get("niche")
    return bool(name) and entry.get("name") == name and entry.get("niche") == niche


def set_formula_status(path, status, reason="", index_path="formulas/_approved/index.json"):
    """Единая логика lifecycle-статуса формулы: JSON + индекс approved атомарно.

    approved (только по рабочему файлу formulas/<ниша>/<имя>.json) -> копия
    формулы пишется иммутабельным снапшотом formulas/_approved/<ниша>/<имя>-vN.json,
    запись индекса указывает на снапшот (аудит C1/H1: рабочий файл — черновик,
    formula-writer может переписывать его свободно, одобренное тело неуязвимо).
    Иначе -> запись формулы убирается из индекса по идентичности (name, niche);
    снапшоты никогда не мутируются — статус/причина пишутся в рабочий файл, и
    только если его версия совпадает (более новый черновик не затираем).

    Read-modify-write индекса берётся под локом мутаций репозитория: конкурентные
    вызовы (потоки фан-аута × кнопка дашборда × CLI) иначе теряют обновления и
    наезжают на git-коммит, который держит тот же лок. Возвращает dict формулы.
    """
    path = Path(_slash_path(path))
    repo_root = _repo_root_from_index(index_path)
    # Один и тот же лок с git-коммиттерами — запись индекса и commit не пересекаются.
    with file_lock(repo_mutation_lock_path(repo_root)):
        formula = read_json(path)
        canonical = _canonical_formula_path(path, index_path)
        is_snapshot = _is_snapshot_path(canonical)
        if is_snapshot and status == "approved":
            raise ValueError(
                "approve работает только по рабочему файлу formulas/<ниша>/<имя>.json "
                f"(получен снапшот {canonical})")

        # Запись индекса строим ДО мутации файла: неполная формула (нет name/niche/
        # version) упадёт KeyError здесь, файл на диске останется нетронутым.
        entry = None
        if status == "approved":
            entry = {
                "name": formula["name"],
                "niche": formula["niche"],
                "path": _snapshot_rel_path(formula),
                "version": formula["version"],
                "approved_at": now_iso(),
            }

        if is_snapshot:
            # Снапшот иммутабелен: статус несём в рабочий файл той же версии.
            working = Path(repo_root) / _working_rel_path(formula)
            if working.is_file():
                draft = read_json(working)
                if str(draft.get("version")) == str(formula.get("version")):
                    draft["status"] = status
                    draft["status_reason"] = reason or ""
                    write_json_atomic(working, draft)
            formula = dict(formula, status=status, status_reason=reason or "")
        else:
            formula["status"] = status
            formula["status_reason"] = reason or ""
            write_json_atomic(path, formula)
            if status == "approved":
                write_json_atomic(Path(repo_root) / entry["path"], formula)

        index_path = Path(index_path)
        if status == "approved":
            index = read_json(index_path) if index_path.exists() else {}
            index["approved"] = [e for e in index.get("approved", [])
                                 if not _entry_matches(e, canonical, formula)]
            index["approved"].append(entry)
            write_json_atomic(index_path, index)
        elif index_path.exists():
            index = read_json(index_path)
            index["approved"] = [e for e in index.get("approved", [])
                                 if not _entry_matches(e, canonical, formula)]
            write_json_atomic(index_path, index)
    return formula


def cmd_approve_formula(sheets, args):
    formula = set_formula_status(args.path, "approved", index_path=args.index)
    print(f"approved: {formula['name']} v{formula['version']} -> {args.index}")
    return 0


def cmd_formula_status(sheets, args):
    formula = set_formula_status(args.path, args.status, reason=args.reason, index_path=args.index)
    note = f", index -> {args.index}" if args.status == "approved" else ""
    print(f"{formula['name']} -> {args.status}{note}")
    return 0


# Пункты чек-листа, которые в калибровке не участвуют: они про ЖИЗНЕННЫЙ ЦИКЛ
# черновика, а не про его качество. У уже решённого рецепта status всегда не
# proposed, у одобренного — его версия уже в индексе, а human_veto по построению
# повторяет решение оператора, которое мы и проверяем. Оставь их — калибровка
# показывала бы «машина отвергает всё» и ничего не измеряла.
CALIBRATION_SKIP = ("status", "duplicate", "human_veto")


def cmd_gate_calibration(sheets, args):
    """Насколько машинные ворота совпадают с решениями человека — на его же данных.

    Прогоняет чек-лист по всем РЕШЁННЫМ рецептам (approved/rejected/paused) и
    сверяет с тем, что решил оператор. Это тот самый замер, которым обоснован
    перевод ворот в авто-режим 2026-07-26, — и он воспроизводим в одну команду, а
    не живёт цитатой в докладе.

    Четыре исхода:
      совпало       — человек одобрил и чек-лист зелёный (или отклонил и красный);
      машина строже — человек одобрил, чек-лист красный: стоит одного решения
                      человека, продукт не страдает;
      ПРОПУСК       — человек отклонил, а чек-лист зелёный: ровно то, ради чего
                      над чек-листом стоит агент-судья. Единственный опасный класс.
    """
    from cf.dashboard.autogate import formula_checks

    root = Path(args.root or _repo_root_from_index(args.index))
    raw = _raw_index_for_checks(sheets)
    if raw is None:
        print("error: raw-вкладки недоступны — калибровать не по чему", file=sys.stderr)
        return 1

    rows, agree, stricter, misses = [], 0, 0, []
    base = root / "formulas"
    for niche_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if niche_dir.name == "_approved":
            continue
        for path in sorted(niche_dir.glob("*.json")):
            try:
                formula = read_json(path)
            except (OSError, ValueError):
                continue
            human = str(formula.get("status") or "")
            if human not in ("approved", "rejected", "paused"):
                continue
            checks = [c for c in formula_checks(root, formula, raw, schema_root=root)
                      if c["key"] not in CALIBRATION_SKIP]
            red = [c for c in checks if not c["ok"]]
            machine = "одобрил бы" if not red else "на ворота"
            if human == "approved" and not red:
                outcome, agree = "совпало", agree + 1
            elif human != "approved" and red:
                outcome, agree = "совпало", agree + 1
            elif human == "approved":
                outcome, stricter = "машина строже", stricter + 1
            else:
                outcome = "ПРОПУСК"
                misses.append((str(formula.get("name")), str(formula.get("niche"))))
            rows.append((human, str(formula.get("niche")), str(formula.get("name")),
                         machine, outcome,
                         "; ".join(c["label"] for c in red[:2])))

    if not rows:
        print("решённых рецептов нет — калибровать не по чему")
        return 0
    print(f"{'человек':9} {'тема':22} {'рецепт':32} {'машина':11} {'итог':14} причина")
    print("-" * 118)
    for human, niche, name, machine, outcome, why in rows:
        print(f"{human:9} {niche[:22]:22} {name[:32]:32} {machine:11} {outcome:14} {why}")
    total = len(rows)
    print(f"\nсовпало {agree} из {total}"
          f" · машина строже: {stricter}"
          f" · пропусков: {len(misses)}")
    if misses:
        print("пропуски (их закрывает агент-судья /cf-review-formula): "
              + ", ".join(f"{n} ({g})" for n, g in misses))
    return 0


def cmd_apply_agent_edit(sheets, args):
    """Применить правку действующего промпта/команды агента по черновику proposals/.

    Отдельно от install-agent: тот намеренно не пишет поверх работающего файла.
    Пути «поправить существующее» не было вовсе — а deny-правила харнесса не
    пускают в prompts/agents/** и .claude/commands/** Edit-инструменты, значит
    поправить инструкцию агента не мог никто, кроме человека вручную.
    """
    from cf.agentinstall import AgentInstallError, apply_edit
    root = Path(args.root or ".")
    filename = str(args.filename).replace("\\", "/").removeprefix("proposals/")
    try:
        written = apply_edit(root, filename, sheets=sheets)
    except AgentInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for rel in written:
        print(f"обновлён: {rel}")
    return 0


def cmd_install_agent(sheets, args):
    """Установить нового агента по черновику proposals/ (правило №3: жмёт человек)."""
    from cf.agentinstall import AgentInstallError, install
    root = Path(args.root or ".")
    # Оператор копирует путь из черновика целиком («proposals/…»), а install ждёт
    # голое имя: снимаем префикс, чтобы команда не отказывала на ровном месте.
    filename = str(args.filename).replace("\\", "/").removeprefix("proposals/")
    try:
        written = install(root, filename, sheets=sheets)
    except AgentInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for rel in written:
        print(f"установлен: {rel}")
    return 0


def cmd_codex_sync(sheets, args):
    """Перегенерировать Codex-обёртки слэш-команд (.agents/skills/) из канона
    .claude/commands/. --check — только сверка: дрейф это красный выход, его
    ловит тест-инвариант test_codex_compat."""
    from cf.codexwrap import CodexWrapError, sync
    try:
        report = sync(Path(args.root or "."), write=not args.check)
    except CodexWrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verb = "дрейфует" if args.check else "обновлена"
    for name in report["updated"]:
        print(f"{verb}: {name}")
    for name in report["orphaned"]:
        print(("генерат без команды (удалит codex-sync без --check): "
               if args.check else "удалён генерат без команды: ") + name)
    for reason in report["invalid"]:
        print(f"обёртка не собирается: {reason}", file=sys.stderr)
    print(f"итого: {len(report['updated'])} обновить, "
          f"{len(report['unchanged'])} совпадает, "
          f"{len(report['orphaned'])} осиротело, "
          f"{len(report['invalid'])} не собирается")
    if report["invalid"] or (args.check and (report["updated"] or report["orphaned"])):
        return 1
    return 0


def _raw_index_for_checks(sheets):
    """Индекс raw-строк {url: строка} для проверок evidence. None — не прочитано.

    Правило №2: недоступная вкладка обязана отличаться от «ссылок нет» — иначе
    рецепт с честным evidence уехал бы в отказ из-за сбоя Sheets."""
    from cf.dashboard.autogate import raw_index
    rows = []
    read_any = False
    for tab in ("raw_tiktok", "raw_instagram"):
        try:
            rows += sheets.read_rows(tab)
            read_any = True
        except Exception as exc:  # noqa: BLE001 — вызывающий решает по None
            print(f"  ВНИМАНИЕ: вкладка {tab} недоступна ({exc})")
    return raw_index(rows) if read_any else None


def _judge_check(args):
    """Вердикт агента-судьи как ещё один пункт чек-листа.

    Судья закрывает ровно то, чего детерминированные признаки не видят:
    исполним ли рецепт брендом, не повторяет ли он уже утверждённый по сути.
    Единственный отказ оператора в целевой теме (shopper-pov-store-find,
    26.07) был именно этого класса — evidence у рецепта чистый.
    """
    from cf.dashboard.autogate import make_check
    path = getattr(args, "review", "") or ""
    if not path:
        return make_check("judge", False, "вердикт судьи",
                      "нет файла ревью (--review); рецепт идёт к человеку")
    try:
        review = read_json(path)
    except (OSError, ValueError) as exc:
        return make_check("judge", None, "вердикт судьи", f"файл ревью не прочитан: {exc}")
    verdict_value = str(review.get("verdict") or "").strip()
    reason = str(review.get("reason") or "").strip()
    return make_check("judge", verdict_value == "recommend", "вердикт судьи",
                  verdict_value + (f" — {reason[:160]}" if reason else ""))


def cmd_auto_approve_formula(sheets, args):
    """Одобряет рецепт без оператора: машинный чек-лист + вердикт судьи.

    Ворота «Одобрение рецептов» в редакции 2026-07-26: продюсер — эксперт по
    контенту, а не по внутренностям завода, поэтому решает он сценарии, а рецепты
    решает машина по проверяемым признакам. Красный чек-лист ничего не меняет:
    рецепт остаётся черновиком и виден на воротах с названной причиной.

    Правило №6: одобрение — success, любой отказ — insufficient_data («условий не
    хватило, чтобы действовать»), как у cf auto-approve по сценариям.
    """
    from cf.dashboard.autogate import formula_checks, gate_policy, verdict

    root = _repo_root_from_index(args.index)
    try:
        formula = read_json(args.path)
    except (OSError, ValueError) as exc:
        print(f"error: рецепт не прочитан: {exc}", file=sys.stderr)
        return 1
    name = str(formula.get("name") or Path(args.path).stem)

    checks = formula_checks(root, formula, _raw_index_for_checks(sheets),
                            schema_root=root)
    if not getattr(args, "no_judge", False):
        checks.append(_judge_check(args))
    result = verdict(checks)

    for check in checks:
        mark = {True: "✓", False: "✗", None: "?"}[check["ok"]]
        print(f"  {mark} {check['label']}"
              + (f": {check['detail']}" if check["detail"] else ""))

    if getattr(args, "check", False):     # только показать чек-лист
        print(f"{name}: {'зелёный' if result['green'] else 'на ворота'}")
        return 0

    policy = gate_policy(sheets.config, "recipes")
    if policy != "auto":
        note = (f"не одобрен — ворота «Одобрение рецептов» в ручном режиме "
                f"(cf.config.json → gates.policy.recipes={policy!r})")
    elif not result["green"]:
        note = "не одобрен — " + "; ".join(result["reasons"][:3])
    else:
        note = None

    if note:
        print(f"{name}: {note}")
        runlog.log_run(sheets, agent="auto-approve-formula",
                       status="insufficient_data", input_summary=f"{name}: {note}",
                       started_at=run_started(args))
        return 0

    from cf.dashboard.decisions import FormulaAlreadyApproved, decide_formula
    rel = _canonical_formula_path(args.path, args.index)
    try:
        done = decide_formula(root, rel, "approved", sheets=sheets, actor="auto")
    except FormulaAlreadyApproved as exc:
        print(f"{name}: {exc}")
        runlog.log_run(sheets, agent="auto-approve-formula",
                       status="insufficient_data", input_summary=f"{name}: {exc}",
                       started_at=run_started(args))
        return 0
    if not done:
        print(f"error: {name}: запись решения не удалась", file=sys.stderr)
        runlog.log_run(sheets, agent="auto-approve-formula", status="failed",
                       input_summary=f"{name}: запись решения не удалась",
                       started_at=run_started(args))
        return 1
    print(f"{name}: авто-одобрен (чек-лист зелёный)")
    runlog.log_run(sheets, agent="auto-approve-formula", status="success",
                   input_summary=f"{name} v{formula.get('version')} "
                                 f"({formula.get('niche')}): чек-лист зелёный, "
                                 f"{len(checks)} проверок",
                   started_at=run_started(args))
    return 0


def _prompt_is_active(row):
    # ACTIVE_TOKENS — единый источник из cf.abtest (та же семантика, что
    # cf.dashboard.sections._is_active, который синхронизируется вручную с комментом).
    return str(row.get("active", "")).strip().upper() in ACTIVE_TOKENS


def parse_generated_at(value):
    """generated_at -> aware datetime; naive (ручная правка таблицы) считаем UTC; мусор -> None."""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def approve_cap(config):
    """Кап авто-одобрения на формулу за 7 дней: cf.config.json →
    dashboard.fanout.approve_cap (там же, где briefs_per_formula — все пороги
    конвейера в одном месте). Мусор/нет ключа/<=0 → дефолт FANOUT_DEFAULTS: битый
    конфиг не должен ронять команду (как weekly_target_from_config).

    Значение — бизнес-параметр (пропускная способность съёмки), поэтому живёт в
    конфиге, а не литералом в argparse: CLI перечитывает конфиг на каждом запуске,
    смена цифры не требует рестарта cf-dashboard. ВНИМАНИЕ: тот же порог продублирован
    в prompts/agents/brief-generator.md — двигать оба места синхронно (правило №3,
    промпт правится через proposal), иначе генератор продолжит пропускать формулу."""
    default = FANOUT_DEFAULTS["approve_cap"]
    try:
        value = int((config or {}).get("dashboard", {}).get("fanout", {})
                    .get("approve_cap", default))
    except (AttributeError, TypeError, ValueError):
        return default
    return value if value > 0 else default


def approved_in_cap_window(rows, formula_id, days=7, notify=None):
    """Сколько брифов формулы одобрено за последние `days` — знаменатель капа.

    Считается по моменту ОДОБРЕНИЯ (reviewed_at), а не генерации: бэклог pending
    старше окна иначе обходит кап целиком (аудит M31). Легаси-строки без
    reviewed_at — fallback на generated_at. Нечитаемая дата (ручная правка листа)
    считается «в окне» консервативно: иначе бриф с битой датой обходит кап и
    авто-одобрение становится безграничным.

    Общий код cf auto-approve и cf retry-deferred: два разных счёта одного капа
    разъехались бы при первой же правке окна.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    for b in rows:
        if str(b.get("formula_id", "")).strip() != formula_id:
            continue
        if norm_status(b.get("review_status")) != "approved":
            continue
        dt = parse_generated_at(str(b.get("reviewed_at") or "").strip()
                                or b.get("generated_at"))
        if dt is None:
            if notify:
                notify(f"approved-бриф {str(b.get('brief_id'))!r} с нечитаемой "
                       f"generated_at={b.get('generated_at')!r} засчитан в кап "
                       f"(консервативно)")
            count += 1
        elif dt >= cutoff:
            count += 1
    return count


def cmd_recheck_briefs(sheets, args):
    """Пересверить УЖЕ ОДОБРЕННЫЕ, но ещё не снятые сценарии новыми проверками ремесла
    и отправить дефектные в доработку.

    Зачем отдельная команда. Ворота `cf auto-approve` стоят на входе: они смотрят
    ТОЛЬКО брифы в статусе pending. Сценарии, одобренные до появления проверки, ею не
    проверяются никогда — они лежат в очереди съёмки как готовые к работе. На 04.08
    таких 49 из 114: с ремаркой вместо хука, с незаполненными `[подстановками]`, с
    капшеном против скрипта. Гейт их не блокирует — он про них не знает, и без этой
    команды первым, кто узнает о дефекте, был бы криэйтор на площадке.

    Почему статус `revised`, а не `rejected`. У завода уже есть звено, которое чинит
    такие брифы само: воркер «Доработка сценариев» берёт брифы `revised`, зовёт
    brief-fixer и применяет результат (`cf revise-brief`). Ему не хватало ровно одного
    — чтобы кто-то клал дефектные сценарии в его очередь. Эта команда и есть
    недостающее звено; переписывание после неё идёт само, без человека.

    Снятые не трогаем: ролик уже опубликован, переписывать его сценарий поздно, а
    метка сломала бы связку brief→reel→performance. Бриф, который завод уже правил
    (FIX_MARKER), тоже пропускаем — одна попытка на бриф, иначе фиксер и ревьюер
    пингуют друг друга вечно за платный вызов каждый цикл.

    По умолчанию НИЧЕГО не пишет: печатает разбор. Пишет только с --apply, потому что
    перевод N сценариев в доработку стоит N вызовов агента на следующем цикле.
    """
    from cf.craftcheck import craft_defects, parse_timing
    from cf.dashboard.data import brief_creator, shot_brief_ids
    from cf.reasons import format_reason

    index = read_json(args.index) if Path(args.index).exists() else {}
    entries = {str(e.get("name")): e for e in index.get("approved", [])}
    repo_root = _repo_root_from_index(Path(args.index))

    timings = {}
    for name, entry in entries.items():
        try:
            formula = read_json(Path(repo_root) / str(entry.get("path", "")))
        except (OSError, ValueError):
            continue          # снапшот недоступен -> окна нет, hook не судим
        timings[name] = parse_timing((formula.get("hook_structure") or {}).get("timing"))

    rows = sheets.read_rows("briefs")
    try:
        published = shot_brief_ids(sheets.read_rows("reels"), False)
    except Exception as exc:  # noqa: BLE001 — правило №2: не судим по непрочитанному
        print(f"error: вкладка reels недоступна ({exc}) — снятые сценарии неотличимы "
              f"от неснятых, пересверка отменена", file=sys.stderr)
        runlog.log_run(sheets, agent="recheck-briefs", status="insufficient_data",
                       input_summary=f"reels недоступны: {exc}",
                       started_at=run_started(args))
        return 0

    queue = [r for r in rows
             if norm_status(r.get("review_status")) == "approved"
             and str(r.get("brief_id")) not in published
             and not brief_was_fixed(r)]

    findings, skipped_fixed = [], sum(
        1 for r in rows
        if norm_status(r.get("review_status")) == "approved" and brief_was_fixed(r))
    for row in queue:
        formula_id = str(row.get("formula_id", "")).strip()
        defects = craft_defects(
            row, timings.get(formula_id),
            prior_scripts=_prior_scripts(rows, formula_id, str(row.get("brief_id")),
                                         before=row.get("generated_at")))
        if defects:
            findings.append((row, defects))

    if not findings:
        summary = (f"пересверено {len(queue)} одобренных сценариев, дефектов нет"
                   + (f"; пропущено уже правленных: {skipped_fixed}" if skipped_fixed else ""))
        runlog.log_run(sheets, agent="recheck-briefs", status="success",
                       input_summary=summary, started_at=run_started(args))
        print(summary)
        return 0

    limit = getattr(args, "limit", None)
    for row, defects in findings:
        print(f"{row.get('brief_id')}:")
        for d in defects:
            print(f"  - {d}")

    if not getattr(args, "apply", False):
        summary = (f"пересверено {len(queue)}, дефекты у {len(findings)} — "
                   f"НИЧЕГО не изменено (нужен --apply)")
        runlog.log_run(sheets, agent="recheck-briefs", status="success",
                       input_summary=summary, started_at=run_started(args))
        print(f"\n{summary}")
        return 0

    sent, assigned = 0, []
    for row, defects in findings:
        # Сценарий уже отдан криэйтору (ревью 14.09.2026): текст, по которому снимают,
        # автоматом не переписываем — иначе снятое разойдётся со сценарием в листе.
        if brief_creator(row):
            assigned.append(row)
            continue
        if limit is not None and sent >= limit:
            break
        brief_id = str(row.get("brief_id"))
        note = ("отправлен в доработку пересверкой 2026-08-04 (проверки ремесла): "
                + "; ".join(defects))
        sheets.update_row_fields("briefs", "brief_id", brief_id, {
            "review_status": "revised",
            "reviewer_notes": note,
            "rejection_reason": format_reason(_recheck_reason_code(defects)),
        })
        sent += 1
        print(f"{brief_id}: → доработка")

    held = len(findings) - sent - len(assigned)
    for row in assigned:
        print(f"{row.get('brief_id')}: дефект есть, но сценарий уже у "
              f"{brief_creator(row)} — в доработку не отправлен, решение за продюсером")
    summary = (f"пересверено {len(queue)}, в доработку отправлено {sent}"
               + (f", отложено лимитом {held}" if held else "")
               + (f", не тронуто — уже у криэйтора: {len(assigned)}" if assigned else "")
               + (f"; пропущено уже правленных: {skipped_fixed}" if skipped_fixed else ""))
    runlog.log_run(sheets, agent="recheck-briefs", status="success",
                   input_summary=summary, started_at=run_started(args))
    print(f"\n{summary}")
    print("Дальше сценарии переписывает воркер «Доработка сценариев» (brief-fixer) "
          "на ближайшем цикле — вмешательство не нужно.")
    return 0


# Дефект -> код причины. Порядок важен: у брифа их бывает несколько, а код в
# rejection_reason один, и группировка rejection-history идёт по нему. Берём САМЫЙ
# тяжёлый — тот, из-за которого сценарий вообще нельзя снять.
_RECHECK_CODES = (
    ("подстановки", "unfilled_placeholder"),
    ("соврав зрителю", "self_contradiction"),
    ("повторяет", "derivative_script"),
    ("пересобран", "derivative_script"),
    ("описание кадра", "weak_hook"),
    ("не произносим", "weak_hook"),
    ("сегмент", "weak_hook"),
)


def _recheck_reason_code(defects):
    """Код причины по списку дефектов — самый тяжёлый из найденных."""
    blob = " ".join(defects)
    for needle, code in _RECHECK_CODES:
        if needle in blob:
            return code
    return "other"


def cmd_retry_deferred(sheets, args):
    """Вернуться к сценариям, отложенным капом формулы, когда окно сдвинулось.

    Отложенный бриф прошёл ВСЕ условия авто-одобрения, кроме квоты: ревьюер сказал
    recommend, формула утверждена, промпт темы активен. Значит решать по нему
    человеку нечего — надо просто дождаться, пока освободится место в окне 7 дней.
    До 2026-07-27 никто не возвращался: три брифа от 25.07 висели в «ожидает»
    бессрочно и изображали очередь решений продюсера.
    """
    from cf.dashboard.data import brief_is_deferred

    rows = sheets.read_rows("briefs")
    queue = [r for r in rows
             if norm_status(r.get("review_status")) == "pending" and brief_is_deferred(r)]
    if not queue:
        runlog.log_run(sheets, agent="retry-deferred", status="success",
                       input_summary="отложенных сценариев нет",
                       started_at=run_started(args))
        print("отложенных сценариев нет")
        return 0

    index = read_json(args.index) if Path(args.index).exists() else {}
    approved_index = {str(e.get("name")): e for e in index.get("approved", [])}
    try:
        versions = sheets.read_rows("prompt_versions")
    except Exception as exc:  # noqa: BLE001 — правило №2: не судим по непрочитанному
        print(f"error: prompt_versions недоступны ({exc})", file=sys.stderr)
        runlog.log_run(sheets, agent="retry-deferred", status="insufficient_data",
                       input_summary=f"prompt_versions недоступны: {exc}",
                       started_at=run_started(args))
        return 0
    cap = args.cap if getattr(args, "cap", None) is not None else approve_cap(sheets.config)

    approved, held = [], []
    for row in queue:
        brief_id = str(row.get("brief_id"))
        formula_id = str(row.get("formula_id", "")).strip()
        entry = approved_index.get(formula_id)
        if entry is None:
            held.append(f"{brief_id}: формула {formula_id!r} больше не утверждена")
            continue
        prompt_id = f"brief-{entry.get('niche', '')}-reel"
        if not any(str(p.get("prompt_id")) == prompt_id and _prompt_is_active(p)
                   for p in versions):
            held.append(f"{brief_id}: промпт {prompt_id} не активен")
            continue
        # rows перечитывать не надо: одобренные в этом же прогоне уже учтены ниже.
        used = approved_in_cap_window(rows, formula_id)
        if used >= cap:
            held.append(f"{brief_id}: кап всё ещё занят ({used}/{cap})")
            continue
        sheets.update_row_fields("briefs", "brief_id", brief_id, {
            "review_status": "approved",
            "reviewer_notes": keep_fix_marker(
                row.get("reviewer_notes"),
                f"авто-одобрен после ожидания квоты (кап {used + 1}/{cap})"),
            "rejection_reason": "",
        }, optional_fields={"reviewed_at": now_iso()})
        # Локальный срез обновляем сразу: два отложенных брифа одной формулы не
        # должны оба проскочить в один прогон мимо капа.
        row["review_status"] = "approved"
        row["reviewed_at"] = now_iso()
        approved.append(brief_id)
        print(f"{brief_id}: одобрен — квота освободилась")

    for note in held:
        print(f"  ждёт: {note}")
    summary = (f"одобрено {len(approved)} из {len(queue)} отложенных"
               + (f"; ждут: {len(held)}" if held else ""))
    runlog.log_run(sheets, agent="retry-deferred", status="success",
                   input_summary=summary, started_at=run_started(args))
    print(summary)
    return 0


def _prior_scripts(rows, formula_id, brief_id, before=None):
    """Тексты УЖЕ ОДОБРЕННЫХ сценариев той же формулы за окно антидубликата.

    База сравнения — только approved: черновики и забракованные не задают нормы, а
    лишний брак в базе прятал бы повтор («это же уже отклонили, значит можно»). Бриф
    сравнивается сам с собой -> исключён по brief_id.

    `before` (generated_at проверяемого брифа) отсекает всё, что появилось ПОЗЖЕ.
    Без этого сравнение симметрично, и в паре дублей флаг получают оба — включая
    оригинал, который ни в чём не виноват. Воротам это безразлично (кандидат там
    всегда самый свежий), а пересверке корпуса — нет: на живых данных симметричный
    счёт дал 73 дефектных брифа против 55 при верном.
    """
    from cf.craftcheck import DUPLICATE_WINDOW_DAYS
    edge = today() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    cutoff = parse_generated_at(before) if before else None
    out = []
    for r in rows:
        if str(r.get("brief_id")) == brief_id:
            continue
        if str(r.get("formula_id", "")).strip() != formula_id:
            continue
        if norm_status(r.get("review_status")) != "approved":
            continue
        dt = parse_generated_at(r.get("reviewed_at")) or parse_generated_at(
            r.get("generated_at"))
        if dt is not None and dt.date() < edge:
            continue          # вне окна; нечитаемая дата — оставляем (fail-safe)
        if cutoff is not None:
            born = parse_generated_at(r.get("generated_at"))
            if born is not None and born >= cutoff:
                continue      # появился позже проверяемого — он не предшественник
        out.append(r.get("script_text") or r.get("script") or "")
    return out


def cmd_auto_approve(sheets, args):
    """Одобряет бриф без оператора: reviewer recommend + формула approved + активный
    промпт ниши + свободный кап формулы за 7 дней. Любое условие не выполнено —
    печатаем причину и ничего не меняем (это не ошибка пайплайна, exit 0).

    Железное правило №6: любой прогон оставляет след в Run Log. Одобрение —
    success; любой отказ — insufficient_data («условий не хватило, чтобы
    действовать», как profile/analyze-batch логируют неполный батч). Отказ — не
    сбой пайплайна, поэтому не failed."""
    def refuse(note):
        # единый выход по отказу: печать причины + строка insufficient_data в Run Log
        print(f"{args.brief_id}: {note}")
        runlog.log_run(sheets, agent="auto-approve", status="insufficient_data",
                       input_summary=f"{args.brief_id}: {note}",
                       started_at=run_started(args))
        return 0

    review = read_json(args.review)
    if str(review.get("brief_id", "")) != args.brief_id:
        return refuse(f"не одобрен — review-файл от другого брифа "
                      f"(в файле brief_id={review.get('brief_id')!r})")
    if review.get("verdict") != "recommend":
        return refuse(f"не одобрен — verdict={review.get('verdict')!r} (нужен recommend)")

    rows = sheets.read_rows("briefs")
    row = next((r for r in rows if str(r.get("brief_id")) == args.brief_id), None)
    if row is None:
        return refuse("не одобрен — бриф не найден в briefs")
    if norm_status(row.get("review_status")) != "pending":
        return refuse(f"не одобрен — review_status={row.get('review_status')!r} (не pending)")

    formula_id = str(row.get("formula_id", "")).strip()
    index_path = Path(args.index)
    index = read_json(index_path) if index_path.exists() else {}
    entry = next((e for e in index.get("approved", []) if e.get("name") == formula_id), None)
    if entry is None:
        return refuse(f"не одобрен — формула {formula_id!r} не утверждена "
                      f"(нет в {args.index})")

    # Гейт целостности: entry["path"] — снапшот утверждённой версии (аудит C1/H1),
    # он обязан существовать, быть approved и совпадать с индексом по версии и имени.
    # Дрейф РАБОЧЕГО файла (formula-writer уже пишет v+1 proposed) одобрению не
    # мешает: брифы генерируются по снапшоту. Расхождение здесь — порча/ручная
    # правка снапшота или полу-миграция — fail-safe, не одобряем.
    repo_root = _repo_root_from_index(index_path)
    formula_file = Path(repo_root) / str(entry.get("path", ""))
    try:
        formula = read_json(formula_file)
    except (OSError, ValueError):  # нет файла / битый JSON / путь-каталог — fail-safe
        return refuse(f"не одобрен — файл формулы {formula_id!r} недоступен "
                      f"({entry.get('path')!r})")
    if (formula.get("status") != "approved"
            or str(formula.get("version")) != str(entry.get("version"))
            or formula.get("name") != entry.get("name")):
        return refuse(f"не одобрен — снапшот формулы {formula_id!r} расходится с "
                      f"индексом: индекс v{entry.get('version')} approved, файл "
                      f"{formula.get('name')!r} v{formula.get('version')} "
                      f"{formula.get('status')!r} — нужен ре-апрув")

    # Проверки РЕМЕСЛА (2026-08-04). Семь проверок ревьюера смотрят на подлинность
    # референса и соответствие снапшоту — на сам текст не смотрит ни одна, и 8 из 12
    # смертельных дефектов корпуса прошли ворота насквозь. Здесь стоит арифметика:
    # незаполненные подстановки, самопротиворечие скрипта и капшена, ремарка вместо
    # хука, непроизносимый тайминг. Красный чек-лист — бриф остаётся pending и уходит
    # к человеку: ручная кнопка продюсера работает всегда и зелёного не требует.
    from cf.craftcheck import craft_defects, parse_timing
    defects = craft_defects(
        row,
        parse_timing((formula.get("hook_structure") or {}).get("timing")),
        prior_scripts=_prior_scripts(rows, formula_id, args.brief_id,
                                     before=row.get("generated_at")))
    if defects:
        return refuse("не одобрен — дефекты ремесла: " + "; ".join(defects))

    prompt_id = f"brief-{entry.get('niche', '')}-reel"
    prompt_active = any(str(p.get("prompt_id")) == prompt_id and _prompt_is_active(p)
                        for p in sheets.read_rows("prompt_versions"))
    if not prompt_active:
        return refuse(f"не одобрен — промпт {prompt_id} не активен")

    # Кап: явный --cap побеждает, иначе значение из конфига (dashboard.fanout).
    cap = args.cap if getattr(args, "cap", None) is not None else approve_cap(sheets.config)
    # M31 работает ТОЛЬКО если колонка reviewed_at заведена в листе: запись идёт
    # optional_fields (без колонки — no-op), и кап тогда молча считается по
    # generated_at. Деградацию не запрещаем (иначе конвейер встанет), но она
    # обязана быть слышной: строка в stdout + пометка в Run Log ниже.
    try:
        no_reviewed_at = bool(sheets.missing_columns("briefs", ("reviewed_at",)))
    except Exception:          # диагностика не имеет права мешать одобрению
        no_reviewed_at = False
    if no_reviewed_at:
        print(f"{args.brief_id}: ВНИМАНИЕ — в CF Creative Briefs нет колонки "
              f"reviewed_at, кап считается по generated_at (фикс M31 не работает; "
              f"колонку добавляет оператор, см. docs/RUNBOOK.md)")

    approved_recent = approved_in_cap_window(rows, formula_id, notify=print)
    if approved_recent >= cap:
        by = ("по дате генерации — нет колонки reviewed_at" if no_reviewed_at
              else "по дате одобрения")
        note = (f"не одобрен — кап формулы {formula_id!r} исчерпан "
                f"({approved_recent}/{cap} за 7 дней, {by}; порог — "
                f"cf.config.json → dashboard.fanout.approve_cap)")
        # Пометка «отложено заводом» (27.07): без неё бриф оставался в «ожидает» и
        # изображал решение продюсера, которого тот принять не может — сценарий
        # хороший, просто квота занята. С пометкой он уходит из очереди внимания, а
        # завод возвращается к нему сам (--retry-deferred), когда окно сдвинется.
        from cf.dashboard.data import DEFERRED_MARKER
        try:
            sheets.update_row_fields("briefs", "brief_id", args.brief_id, {
                "reviewer_notes": keep_fix_marker(
                    row.get("reviewer_notes"),
                    f"{DEFERRED_MARKER}: кап формулы {approved_recent}/{cap} за 7 дней")})
        except Exception:  # noqa: BLE001 — пометка не важнее самого отказа
            print(f"{args.brief_id}: пометку «отложено» записать не удалось")
        return refuse(note)

    sheets.update_row_fields("briefs", "brief_id", args.brief_id, {
        "review_status": "approved",
        "reviewer_notes": keep_fix_marker(
            row.get("reviewer_notes"),
            "авто-одобрен: recommend + формула approved + активный промпт + кап свободен"),
        "rejection_reason": "",
    }, optional_fields={"reviewed_at": now_iso()})
    runlog.log_run(sheets, agent="auto-approve", status="success",
                   input_summary=f"brief_id={args.brief_id} formula_id={formula_id} "
                                 f"кап {approved_recent + 1}/{cap}"
                                 + (" · без колонки reviewed_at" if no_reviewed_at else ""),
                   started_at=run_started(args))
    print(f"{args.brief_id}: авто-одобрен")
    return 0


# P5.10 performance-правило: пороги авто-паузы по замеренной результативности.
PERF_WINDOW_DAYS = 28          # окно замеров (по measured_at выбранного замера)
PERF_MIN_REELS = 3             # минимум замеренных reels формулы в окне
PERF_MEDIAN_RATIO = 0.5        # медиана формулы < ratio × общей -> кандидат на паузу


def _measured_rows(sheets, briefs, since=None):
    """Общий join замеров для perf-правила гвардии (P5.10) и витрины own_performance
    (P5.11): дедуплицированные reel-строки из build_eval_dataset (P5.8) с views > 0.

    Дедуп и связка reel→brief→formula — один замер на reel_id (ближайший к
    published_at+7д, fallback — последний measured_at). since (YYYY-MM-DD) отсекает
    замеры ПОСЛЕ дедупа по measured_at (окно гвардии); None — накопительно (витрина).
    reels даёт published_at для дедупа; недоступны -> fallback (последний measured_at).

    Замеры с views ≤ 0 (непарсибельные/нулевые →0.0) отсеиваются: битые views искажают
    медиану. Возврат:
      - None — СБОЙ чтения performance (Sheets недоступна): вызывающий отличает «не
        смогли посчитать» (витрину не трогаем, гвардия пропускает правило) от…
      - [] — вкладка читаема, но пуста / нет валидных замеров («посчитали, замеров нет»).

    Демо-замеры (reel_id с меткой DEMO_PREFIX) отсеиваются ЗДЕСЬ, в единственной точке
    join'а — иначе фикция доходит до решений. Аудит 2026-08-04: все 48 строк performance
    были демо-строками `cf demo-seed`, и на них авто-пауза сняла с производства два
    живых рецепта (store-native-skit «median 765», color-upgrade-ladder «median 740»),
    а own_performance показывал в /lab выдуманные медианы 8100-10400. Демо-петля обязана
    оставаться проверяемой (её смысл — приёмка eval на известном ответе), но контур
    РЕШЕНИЙ она кормить не должна: правило №1 (evidence-first) не различает выдуманные
    данные и отсутствующие, а правило №2 требует честного «данных нет».
    """
    from cf.evalprep import build_eval_dataset
    try:
        performance = sheets.read_rows("performance")
    except Exception as exc:  # noqa: BLE001 — best-effort, вызывающий решает по None
        print(f"  WARNING: вкладка performance недоступна ({exc})")
        return None
    if not performance:
        return []
    try:
        reels = sheets.read_rows("reels")
    except Exception:  # noqa: BLE001 — published_at best-effort, дедуп упадёт в fallback
        reels = None
    # Деньги (тикет 05) — best-effort как reels: вкладок может ещё не быть
    # (сбор Метрики не запускался), гвардия и витрина работают и без них.
    try:
        utm = sheets.read_rows("utm_traffic")
    except Exception:  # noqa: BLE001 — clicks/orders не роняют perf-правило
        utm = None
    try:
        orders = sheets.read_rows("orders")
    except Exception:  # noqa: BLE001
        orders = None
    from cf.demoseed import DEMO_PREFIX
    rows = [r for r in build_eval_dataset(performance, briefs, [], reels=reels,
                                          since=since, utm=utm,
                                          orders=orders)["rows"]
            if r["views"] > 0]
    live = [r for r in rows if not str(r.get("reel_id", "")).startswith(DEMO_PREFIX)]
    if rows and not live:
        # Слышная деградация вместо тихого нуля: оператор обязан видеть, что решения
        # не принимаются не из-за поломки, а из-за отсутствия боевых замеров.
        print(f"  ВНИМАНИЕ: боевых замеров нет — все {len(rows)} отфильтрованы как "
              f"демо ({DEMO_PREFIX}*); perf-правила пропущены")
    return live


def _performance_pause_reasons(sheets, briefs, names):
    """P5.10: {formula_name: reason} для формул, чья медиана просмотров за окно
    PERF_WINDOW_DAYS дн. < PERF_MEDIAN_RATIO× медианы ВСЕХ замеренных reels периода
    при ≥ PERF_MIN_REELS замеренных reels (пороги — из констант PERF_*).

    Дедуп/связку/окно/фильтр views берём из общего _measured_rows (since = сегодня −
    PERF_WINDOW_DAYS дн.). База сравнения — медиана по всем замеренным reels периода
    (по всем формулам), не только проверяемой.

    Best-effort: недоступная/пустая вкладка performance или нет валидных замеров ->
    пустой результат (reject-правило гвардии всё равно отработает)."""
    since = (today() - timedelta(days=PERF_WINDOW_DAYS)).isoformat()
    rows = _measured_rows(sheets, briefs, since=since)
    if not rows:  # None (сбой чтения) или [] (нет замеров) — паузить некого
        return {}
    overall_median = statistics.median([r["views"] for r in rows])  # > 0: все views > 0
    threshold = PERF_MEDIAN_RATIO * overall_median
    by_formula = {}
    for r in rows:
        fid = str(r.get("formula_id", "")).strip()
        if fid:
            by_formula.setdefault(fid, []).append(r["views"])
    reasons = {}
    for name in names:
        views = by_formula.get(name, [])
        if len(views) < PERF_MIN_REELS:
            continue
        fmedian = statistics.median(views)
        if fmedian < threshold:
            ratio = fmedian / overall_median
            reasons[name] = (
                f"авто-пауза по performance: {len(views)} reels, "
                f"median {fmedian:.0f} vs {overall_median:.0f} общая "
                f"({ratio:.2f}×), окно {PERF_WINDOW_DAYS} дн.")
    return reasons


def cmd_formula_guard(sheets, args):
    """Авто-пауза формулы двумя правилами -> формула выходит из approved:
      1) 3 reject из последних 5 брифов формулы (reviewer-сигнал);
      2) P5.10: ≥ PERF_MIN_REELS замеренных reels за PERF_WINDOW_DAYS дн. и median
         views < PERF_MEDIAN_RATIO× медианы всех замеренных reels периода (провал у
         аудитории — за дни, а не недели).

    Правило 1 приоритетнее (проверяется первым; при нарушении обоих perf-причина
    дописывается второй фразой в reason). Железное правило №6: любой прогон гвардии
    оставляет след в Run Log — с реальными цифрами причины. Пауза формулы — success
    (действие выполнено); сбой постановки на паузу — failed (реальная ошибка
    действия); прогон, где всё в норме, — одна success-строка."""
    index_path = Path(args.index)
    index = read_json(index_path) if index_path.exists() else {}
    entries = list(index.get("approved", []))
    if not entries:
        runlog.log_run(sheets, agent="formula-guard", status="success",
                       input_summary="нет approved-формул — проверять нечего",
                       started_at=run_started(args))
        print("все формулы в норме")
        return 0

    def dt_key(b):
        # unparseable даты в конец окна — та же семантика дат, что в auto-approve
        dt = parse_generated_at(b.get("generated_at"))
        return (dt is None, dt or datetime.min.replace(tzinfo=timezone.utc))

    briefs = sheets.read_rows("briefs")
    # Правило 2 считается один раз на прогон: общая медиана периода — по всем формулам.
    perf_reasons = _performance_pause_reasons(
        sheets, briefs, [str(e.get("name", "")) for e in entries])
    paused_any = False
    paused = []  # (имя, причина) — для одного сводного письма владельцу
    logged = 0  # сколько строк уже записали за прогон (паузы + сбои)
    for entry in entries:
        name = entry.get("name", "")
        rows = [b for b in briefs if str(b.get("formula_id", "")).strip() == name]
        # H14: окно ограничено текущей эпохой одобрения — ре-апрув оператора
        # обновляет approved_at и сбрасывает старые reject'ы, иначе гвардия
        # ежедневно перебивала бы осознанное решение оператора. Бриф с нечитаемой
        # generated_at при заданном approved_at исключается: неатрибутируемый бриф
        # не должен пере-паузить ре-одобренную формулу.
        approved_dt = parse_generated_at(entry.get("approved_at"))
        if approved_dt is not None:
            rows = [b for b in rows
                    if (d := parse_generated_at(b.get("generated_at"))) is not None
                    and d >= approved_dt]
        rows.sort(key=dt_key)
        window = rows[-5:]
        n = sum(1 for b in window if norm_status(b.get("review_status")) == "rejected")
        # Второй, более слабый порог (26.07, вместе с машинными воротами рецептов):
        # рецепт, который бесконечно даёт «на доработку», по правилу reject-3-из-5 не
        # тормозится ничем и не даёт ни одного сценария — а гвардия ЕДИНСТВЕННАЯ
        # страховка авто-одобренного рецепта, пока нет ни строки performance.
        # Порог выше (4 из 5): revise — «исправимо», и одинокая доработка не повод
        # снимать рецепт с производства.
        bad = sum(1 for b in window
                  if norm_status(b.get("review_status")) in ("rejected", "revised"))
        if n >= 3:
            reason = f"авто-пауза: {n} reject из последних {len(window)}"
            # формула нарушает оба правила: reject первичен, perf — второй фразой
            if name in perf_reasons:
                reason += f" · {perf_reasons[name]}"
        elif bad >= 4 and len(window) >= 4:
            reason = (f"авто-пауза: {bad} из последних {len(window)} сценариев "
                      f"забракованы (reject или доработка)")
            if name in perf_reasons:
                reason += f" · {perf_reasons[name]}"
        elif name in perf_reasons:
            reason = perf_reasons[name]
        else:
            print(f"{name}: в норме ({n}/{len(window)} reject, {bad} забраковано)")
            continue

        try:
            set_formula_status(entry["path"], "paused", reason=reason, index_path=args.index)
        except (OSError, KeyError, ValueError) as exc:
            # причина без имени формулы (имя уже в префиксе), без слова «paused»
            runlog.log_run(sheets, agent="formula-guard", status="failed",
                           input_summary=f"{name}: не удалось поставить на паузу: {reason}",
                           errors=[str(exc)],
                           started_at=run_started(args))
            logged += 1
            print(f"{name}: пропущена — не удалось поставить на паузу ({exc})")
            continue
        # Run Log несёт реальные цифры причины (боевой путь выбрасывает stdout)
        runlog.log_run(sheets, agent="formula-guard", status="success",
                       input_summary=f"{name}: {reason}",
                       started_at=run_started(args))
        logged += 1
        print(f"{name}: {reason}")
        paused_any = True
        paused.append((name, reason))

    if paused:
        # Спека §5 обещала это событие с самого начала, но кода у него не было
        # до 2026-08-10: машина снимала рецепт с производства, и человек, чьё
        # это производство, не узнавал об этом ниоткуда, кроме Run Log.
        # Одно письмо на прогон, а не на рецепт: их может быть несколько сразу.
        try:
            from cf.messages import formulas_paused
            from cf.notify import notify_telegram
            notify_telegram(formulas_paused(paused, sheets.config),
                            sheets.config)
        except Exception as exc:  # noqa: BLE001 — телеметрия не важнее гвардии
            print(f"уведомление не отправлено: {type(exc).__name__}",
                  file=sys.stderr)

    if not paused_any:
        print("все формулы в норме")
    # Прогон без единой записи (все формулы в норме, паузы/сбоя не было) — оставляем
    # одну сводную success-строку, чтобы «тихий» здоровый прогон тоже был в Run Log.
    if logged == 0:
        runlog.log_run(sheets, agent="formula-guard", status="success",
                       input_summary=f"все {len(entries)} формул в норме",
                       started_at=run_started(args))
    return 0


def _own_performance_by_formula(sheets, briefs, names):
    """P5.11: {formula_name: {reels, median_views, avg_er, last_measured_at}} —
    НАКОПИТЕЛЬНАЯ витрина собственных замеров формулы.

    Реюзает общий _measured_rows (дедуп, связка reel→brief→formula, фильтр views>0) —
    тот же путь, что гвардия (P5.10), без дубля join'а. Осознанные отличия от гвардии:
      - НЕТ окна: since=None, собираем ВСЕ замеры (гвардия смотрит только 28-дневное
        окно, потому что решает о паузе; витрина «накапливает результаты формулы»);
      - views<=0 отсекаются (в _measured_rows) — консистентно с гвардией.
    avg_er — среднее er учтённых reels (та же коэрция 0.0 для отсутствующего er, что в
    eval-агрегатах _aggregate_by_version); если НИ ОДИН reel не несёт er>0 — None
    («нет данных»: реальный engagement rate > 0, поэтому all-zero == er не замеряется).
    last_measured_at — максимум measured_at ТОЛЬКО по парсибельным датам (рукописный
    мусор вроде «нет данных» иначе выиграл бы лексикографику max()).

    Возврат:
      - None — СБОЙ чтения performance: витрину НЕ трогаем (вызывающий отличит от «нет
        замеров» и не сотрёт накопленное);
      - {} — вкладка читаема, но у approved-формул нет валидных замеров (витрина
        чистится). Считаем только формулы из names (approved), чужие reels отбросим.
    """
    from cf.evalprep import _parse_date
    rows = _measured_rows(sheets, briefs, since=None)  # накопительно (без окна)
    if rows is None:  # сбой чтения performance — витрину не обновляем
        return None
    wanted = set(names)
    by_formula = {}
    for r in rows:
        fid = str(r.get("formula_id", "")).strip()
        if fid and fid in wanted:
            by_formula.setdefault(fid, []).append(r)
    out = {}
    for name, frows in by_formula.items():
        views = [r["views"] for r in frows]
        ers = [r["er"] for r in frows]
        measured = [str(r.get("measured_at", "")) for r in frows
                    if _parse_date(r.get("measured_at")) is not None]
        out[name] = {
            "reels": len(frows),
            "median_views": round(statistics.median(views), 6),
            "avg_er": (round(sum(ers) / len(ers), 6) if any(e > 0 for e in ers) else None),
            "last_measured_at": max(measured) if measured else "",
        }
    return out


def _eval_is_demo(data):
    """Отчёт eval посчитан на демо-строках -> его вердиктам верить нельзя.

    Два признака, любой достаточен: честная пометка агента `dataset.provenance_warning`
    и метка DEMO_PREFIX на КАЖДОМ упомянутом reel_id (пометки может не быть у отчёта,
    написанного до её появления). Смешанный отчёт (есть хоть один боевой ролик)
    демо-отчётом не считается: его выводы уже опираются на реальность."""
    from cf.demoseed import DEMO_PREFIX
    warning = str(((data.get("dataset") or {}) if isinstance(data, dict) else {})
                  .get("provenance_warning", ""))
    if "демо" in warning.lower() or "demo" in warning.lower():
        return True
    ids = re.findall(r'"reel_id"\s*:\s*"([^"]+)"', json.dumps(data, ensure_ascii=False))
    return bool(ids) and all(i.startswith(DEMO_PREFIX) for i in ids)


def _formula_verdicts_from_eval(repo_root):
    """P5.11: {formula_id: verdict} из последнего *-weekly-eval.json (секция
    formula_performance, формат eval-agent.md: [{formula_id, reels, ..., verdict}]).

    Best-effort перенос ТОГО, ЧТО ФАКТИЧЕСКИ ЕСТЬ: нет каталога evals / нет файлов /
    нет секции formula_performance / у формулы нет непустого verdict -> ключ
    отсутствует (перенос — no-op). Новый формат eval-файла не изобретаем.

    Отчёт на демо-данных вердиктов НЕ отдаёт. Eval-агент сам помечает такой отчёт
    (`dataset.provenance_warning`), и в единственном существующем — 2026-07-27 — стоит
    «Все 35 замеренных роликов — строки демо-прогона». Перенос эту честную пометку
    игнорировал, и выдуманные медианы уезжали в status_reason формул: «держать,
    медиана 9350», «кандидат на паузу, медиана 765». Оператор читает именно эту
    строку — она обязана быть либо правдой, либо пустой (правило №2)."""
    evals_dir = Path(repo_root) / "agent-runtime" / "evals"
    if not evals_dir.is_dir():
        return {}
    reports = sorted(evals_dir.glob("*-weekly-eval.json"), reverse=True)
    if not reports:
        return {}
    try:
        data = read_json(reports[0])
    except (OSError, ValueError):
        return {}
    if _eval_is_demo(data):
        print(f"  ВНИМАНИЕ: {reports[0].name} посчитан на демо-данных — "
              f"вердикты формул не переносятся")
        return {}
    verdicts = {}
    for item in (data.get("formula_performance") or []):
        fid = str(item.get("formula_id", "")).strip()
        verdict = str(item.get("verdict", "")).strip()
        if fid and verdict:
            verdicts[fid] = verdict
    return verdicts


def _write_formula_perf(index_path, perf_by_name, verdicts):
    """P5.11: записать own_performance в индекс + продублировать eval-verdict в
    status_reason approved-формулы. Read-modify-write под тем же локом мутаций
    репозитория (P1.9), что set_formula_status и git-коммит — без гонок с фан-аутом,
    кнопкой дашборда и коммиттером.

    perf_by_name is None (СБОЙ чтения performance) -> перф-часть витрины не трогаем
    вовсе (накопленное не стираем); dict -> формула с замерами получает own_performance,
    формула без замеров теряет ключ (отсутствие -> /lab «нет данных», а не старые цифры).

    verdict — ПОЛНАЯ перезапись status_reason approved-формулы (протухший verdict живёт
    в карточке до следующего eval, это осознанно). Пишем, НЕ трогая status, и только
    если файл всё ещё status==approved: при рассинхроне файл/индекс (файл уже paused,
    а запись из индекса не убрана) не затираем pause-reason. Файл недоступен/битый ->
    verdict пропускаем. Возвращает (updated_perf, updated_verdicts) — считаем только
    фактические записи (no-op, когда verdict уже стоит, не в счёт)."""
    index_path = Path(index_path)
    repo_root = _repo_root_from_index(index_path)
    updated_perf = updated_verdicts = 0
    with file_lock(repo_mutation_lock_path(repo_root)):
        index = read_json(index_path) if index_path.exists() else {}
        for entry in index.get("approved", []):
            name = entry.get("name", "")
            if perf_by_name is not None:  # None = сбой чтения: витрину сохраняем как есть
                perf = perf_by_name.get(name)
                if perf is not None:
                    entry["own_performance"] = perf
                    updated_perf += 1
                elif "own_performance" in entry:
                    del entry["own_performance"]
            verdict = verdicts.get(name)
            if verdict:
                # Снапшоты иммутабельны (C1/H1): verdict пишем в РАБОЧИЙ файл,
                # и только если он той же версии — новый черновик не штампуем.
                rel = str(entry.get("path", ""))
                if _is_snapshot_path(rel):
                    rel = f"formulas/{entry.get('niche', '')}/{entry.get('name', '')}.json"
                fpath = Path(repo_root) / rel
                try:
                    formula = read_json(fpath)
                except (OSError, ValueError):
                    continue  # формула недоступна/битая — verdict молча пропускаем
                if formula.get("status") != "approved":
                    continue  # рассинхрон файл/индекс — не трогаем paused/rejected reason
                if str(formula.get("version")) != str(entry.get("version")):
                    continue  # рабочий файл уже новая версия — verdict не про неё
                if formula.get("status_reason") != verdict:
                    formula["status_reason"] = verdict
                    write_json_atomic(fpath, formula)
                    updated_verdicts += 1
        write_json_atomic(index_path, index)
    return updated_perf, updated_verdicts


def cmd_formula_perf(sheets, args):
    """P5.11: накопить own_performance approved-формул в индекс + перенести eval-verdict.

    Проблема: формула показывает только чужой evidence — собственные результаты не
    аккумулируются. По briefs→reels→performance собираем на каждую approved-формулу
    {reels, median_views, avg_er, last_measured_at} (тот же join/дедуп, что гвардия
    P5.10, но накопительно — без 28-дн окна) и пишем ключ own_performance в
    formulas/_approved/index.json под локом мутаций репозитория (P1.9). Формула без
    замеров -> ключ убран (/lab: «нет данных»). verdict из последнего *-weekly-eval.json
    (formula_performance) дублируется в status_reason формулы (best-effort). Железное
    правило №6: любой прогон оставляет одну строку в Run Log."""
    index_path = Path(args.index)
    index = read_json(index_path) if index_path.exists() else {}
    entries = list(index.get("approved", []))
    if not entries:
        runlog.log_run(sheets, agent="formula-perf", status="success",
                       input_summary="нет approved-формул — считать нечего",
                       started_at=run_started(args))
        print("нет approved-формул")
        return 0

    briefs = sheets.read_rows("briefs")
    names = [str(e.get("name", "")) for e in entries]
    perf_by_name = _own_performance_by_formula(sheets, briefs, names)  # None при сбое
    verdicts = _formula_verdicts_from_eval(_repo_root_from_index(index_path))
    try:
        updated_perf, updated_verdicts = _write_formula_perf(
            index_path, perf_by_name, verdicts)
    except (OSError, ValueError) as exc:
        runlog.log_run(sheets, agent="formula-perf", status="failed",
                       input_summary="не удалось записать own_performance формул",
                       errors=[str(exc)],
                       started_at=run_started(args))
        print(f"formula-perf: ошибка записи ({exc})", file=sys.stderr)
        return 1

    # Сбой чтения performance != «замеров нет»: честная сводка, а не «обновлён у 0/N».
    perf_note = ("витрина own_performance не обновлена (performance недоступна)"
                 if perf_by_name is None
                 else f"own_performance обновлён у {updated_perf}/{len(entries)} формул")
    summary = f"{perf_note}; eval-verdict перенесён у {updated_verdicts}"
    runlog.log_run(sheets, agent="formula-perf", status="success", input_summary=summary,
started_at=run_started(args))
    print(summary)
    return 0


def cmd_demo_seed(sheets, args):
    """Влить демо-ролики и демо-замеры по плану — приёмка eval-петли на данных.

    Обратная связь завода (eval, атрибуция, own_performance, гвардия P5.10, A/B)
    никогда не выполнялась: съёмки нет, замеров нет. Команда подставляет данные с
    заранее известным правильным ответом. Все строки помечены DEMO- и status=demo
    (см. cf.demoseed) и стираются `cf demo-wipe` в той же сессии."""
    from cf.demoseed import DemoSeedError, seed
    started = getattr(args, "started_at", None) or run_started(args)
    try:
        plan = read_json(Path(args.plan))
    except (OSError, ValueError) as exc:
        # Битый план — не прогон завода, а опечатка оператора; строку в Run Log
        # всё равно оставляем (правило №6), но лист не трогаем.
        runlog.log_run(sheets, agent="demo-seed", status="failed",
                       input_summary=f"DEMO: план {args.plan} не прочитан",
                       errors=[str(exc)], started_at=started)
        print(f"error: план не прочитан ({exc})", file=sys.stderr)
        return 1
    try:
        summary = seed(sheets, plan, started_at=started)
    except DemoSeedError as exc:
        runlog.log_run(sheets, agent="demo-seed", status="failed",
                       input_summary=f"DEMO: план {args.plan} отклонён проверками",
                       errors=[str(exc)], started_at=started)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"demo-seed: роликов {summary['reels']} (пропущено {summary['reels_skipped']}), "
          f"замеров {summary['performance']} (пропущено {summary['performance_skipped']})")
    return 0


def cmd_demo_wipe(sheets, args):
    """Стереть демо-строки из CF Published Reels и CF Performance.

    Фильтр по метке DEMO-, а не очистка вкладки: настоящая публикация, записанная
    между инъекцией и стиранием, обязана пережить уборку (см. cf.demoseed.wipe)."""
    from cf.demoseed import wipe
    started = getattr(args, "started_at", None) or run_started(args)
    removed = wipe(sheets, started_at=started)
    print(f"demo-wipe: стёрто роликов {removed['reels']}, замеров {removed['performance']}")
    return 0


def cmd_set_review(sheets, args):
    from cf.reasons import REASON_CODES, format_reason, is_reason_code
    # P5.12: код причины из enum — структурным префиксом в rejection_reason (ключ
    # группировки rejection-history). Свободный текст (--reason) идёт человекочитаемым
    # хвостом; без --reason-code — прежнее поведение (свободный текст как причина).
    reason_code = (getattr(args, "reason_code", "") or "").strip()
    if reason_code:
        # Код причины осмыслен только у reject/revise. У approved/pending он записал бы
        # мусорный [code] в rejection_reason одобренного брифа — отказываем явно.
        if args.status not in ("rejected", "revised"):
            print(f"--reason-code применим только к --status rejected/revised, "
                  f"а не {args.status!r}", file=sys.stderr)
            return 1
        if not is_reason_code(reason_code):
            print(f"неизвестный reason-code {reason_code!r}; допустимо: "
                  f"{', '.join(REASON_CODES)}", file=sys.stderr)
            return 1
        rejection_reason = format_reason(reason_code, args.reason)
    else:
        rejection_reason = args.reason
    # (разбор 2026-07-27) Стоп-кран «одна попытка фиксера на бриф» держится ТОЛЬКО
    # меткой в reviewer_notes, а set-review перезаписывал колонку целиком — и метка
    # исчезала на самом обычном пути (ревьюер снова ставит «доработка» переписанному
    # сценарию и пишет свои замечания). Дальше бриф опять попадал в очередь фиксера:
    # платный вызов агента каждый цикл, бессрочно. Читаем прежние notes и метку
    # сохраняем. Лишнее чтение листа здесь дешевле дырявого стоп-крана.
    prev = next((r for r in sheets.read_rows("briefs")
                 if str(r.get("brief_id")) == str(args.brief_id)), None)
    notes = keep_fix_marker(prev.get("reviewer_notes") if prev else "", args.notes)
    # reviewed_at — момент решения (M31: кап auto-approve считается по нему);
    # optional: колонка появляется в briefs миграцией оператора, до неё — no-op.
    found = sheets.update_row_fields("briefs", "brief_id", args.brief_id, {
        "review_status": args.status,
        "rejection_reason": rejection_reason,
        "reviewer_notes": notes,
    }, optional_fields={
        "reviewed_at": "" if args.status == "pending" else now_iso()})
    if not found:
        print(f"brief_id {args.brief_id} не найден в CF Creative Briefs", file=sys.stderr)
        return 1
    print(f"{args.brief_id}: review_status={args.status}")
    return 0


def cmd_mark_published(sheets, args):
    """Отметить бриф опубликованным: одна строка в CF Published Reels + Run Log.

    Замыкает контур brief->reel->performance (иначе cf trace показывает MISSING, а
    eval/атрибуция не работают). Логика — в cf.publish.mark_published (общая с формой
    дашборда), здесь только разбор ошибки валидации в внятный exit 1. --account
    сверяется с реестром accounts (тот же геттер, что у select формы дашборда):
    публикация на неизвестный или выключенный аккаунт — деньги UTM-контура мимо."""
    from cf.config import accounts_from_config
    from cf.publish import MarkPublishedError, mark_published
    account = str(args.account or "").strip()
    if account:
        active = sorted(a["slug"] for a in accounts_from_config(sheets.config)
                        if a["active"])
        if account not in active:
            print(f"error: неизвестный или выключенный аккаунт {account!r} — "
                  f"активные слаги реестра accounts (cf.config.json): "
                  f"{', '.join(active) or 'нет ни одного'}", file=sys.stderr)
            return 1
    try:
        row = mark_published(sheets, args.brief_id, args.url, notes=args.notes,
                             account=account)
    except MarkPublishedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.brief_id}: опубликован рил {row['reel_id']} -> {row['post_url']}"
          + (f" (аккаунт {account})" if account else ""))
    return 0


def cmd_add_brief(sheets, args):
    from cf.validate import validate_json_file
    errors = validate_json_file("brief", args.file)
    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    brief = read_json(args.file)
    brief_id = str(brief["brief_id"])
    # Pre-check + append неатомарны (две гонки: (1) потерянный ответ и ретрай того же
    # процесса — лечится идемпотентным append_row со сканом хвоста; (2) два процесса
    # одновременно). Здесь делаем add-brief идемпотентным: если brief_id уже виден —
    # это no-op УСПЕХ (желаемое состояние достигнуто), а не второй ряд и не ошибка.
    if any(str(b.get("brief_id")) == brief_id for b in sheets.read_rows("briefs")):
        print(f"бриф {brief_id} уже есть — пропущено (идемпотентно)")
        return 0
    sheets.append_row("briefs", {
        "brief_id": brief["brief_id"],
        "generated_at": now_iso(),
        "platform": args.platform,
        "hook": brief["hook"],
        "script": brief["script"],
        "visual_direction": brief["visual_direction"],
        "cta": brief["cta"],
        "caption": args.caption,
        "references": "; ".join(brief["references"]),
        "prompt_version": brief["prompt_version"],
        "formula_id": brief["formula_id"],
        "source_pattern_ids": ", ".join(brief["source_pattern_ids"]),
        "payload_json": json.dumps(brief, ensure_ascii=False),
        "review_status": "pending",
    })
    # Пост-аппенд перечитывание по brief_id: если ПОКА мы писали, параллельный процесс
    # вписал тот же id (оба прошли pre-check до записи) — трактуем дубль как успех
    # первого и НЕ пишем ещё раз (удаление лишнего ряда рискованно). Жёсткая гарантия
    # единственного ряда для двух ОДНОВРЕМЕННЫХ писателей — межпроцессный лок (P1.9).
    dupes = sum(1 for b in sheets.read_rows("briefs")
                if str(b.get("brief_id")) == brief_id)
    if dupes > 1:
        print(f"бриф {brief_id}: параллельная запись того же id — считаем успехом "
              f"первого (рядов: {dupes}); строгий дедуп даст межпроцессный лок")
        return 0
    print(f"бриф {brief_id} добавлен (pending)")
    return 0


# Метка машинной доработки в reviewer_notes. Она же — стоп-кран цикла «доработал →
# снова доработка»: бриф, уже переписанный заводом, второй раз в доработку не идёт,
# а уходит к человеку. Отдельной колонки под счётчик попыток в листе нет, а
# добавлять её ради одного числа дороже, чем прочитать метку.
FIX_MARKER = "доработано заводом"


# Хвост метки для брифа, который фиксер ЧЕСТНО отказался переписывать (сценарий
# требует несуществующего события и т.п.). Формулировка — часть контракта с
# раннером (dashboard/runner.py:_mark_refused_fixes), менять её в одиночку нельзя.
FIX_REFUSED_SUFFIX = "(не переписан заводом)"


def brief_was_fixed(row):
    """Бриф уже переписывался заводом по замечаниям ревьюера?"""
    return FIX_MARKER in str((row or {}).get("reviewer_notes") or "")


def keep_fix_marker(old_notes, new_notes):
    """Не потерять метку FIX_MARKER при перезаписи reviewer_notes.

    (разбор 2026-07-27) Метка — единственный носитель признака «завод уже пробовал»:
    отдельной колонки под счётчик попыток в листе нет. Любая перезапись notes без
    этой склейки открывает пинг-понг «фиксер ↔ ревьюер» заново, а он стоит платного
    вызова агента каждый цикл. Текст самой новой заметки не трогаем — она нужна
    человеку целиком; метку дописываем префиксом, как её пишет сам фиксер.
    """
    old, new = str(old_notes or ""), str(new_notes or "")
    if FIX_MARKER not in old or FIX_MARKER in new:
        return new
    return f"{FIX_MARKER}; {new}" if new.strip() else FIX_MARKER


def revise_brief_id(args):
    """brief_id для revise-brief: позиционный аргумент ИЛИ флаг --brief-id.

    (разбор 2026-07-27) Два входа — не каприз: обычный вызов исторически
    позиционный (`cf revise-brief b-001 --file …`), а режим --refused зовёт раннер
    флагом (контракт с dashboard/runner.py). Dest'ы РАЗНЫЕ намеренно: общий dest
    argparse затирает дефолтом пропущенного позиционного, и флаг молча пропадал бы.
    """
    return str(getattr(args, "brief_id", None)
               or getattr(args, "brief_id_flag", None) or "").strip()


def revise_brief_refused(sheets, args, brief_id):
    """Пометить бриф, который завод так и не переписал, и отдать его человеку.

    (разбор 2026-07-27) Отказ фиксера — штатный и правильный исход: сценарий может
    требовать события, которого у бренда нет. Но метку ставила ТОЛЬКО успешная
    правка, поэтому отказ не оставлял следа вовсе — бриф вечно возвращался в очередь
    доработки и жёг платный вызов агента каждый цикл, бессрочно. Так заперты
    b-own-event-announcement-20260726-founder-opening и …-20260727-popup-offers.

    Сценарий не трогаем: review_status остаётся «доработка», и бриф виден человеку
    в очереди внимания дашборда (dashboard/data.py:187 считает revised целиком, без
    оглядки на метку) — уходит он только из очереди фиксера.
    """
    row = next((r for r in sheets.read_rows("briefs")
                if str(r.get("brief_id")) == brief_id), None)
    if row is None:
        print(f"error: бриф {brief_id} не найден в CF Creative Briefs",
              file=sys.stderr)
        return 1

    def trace(note):
        """След в Run Log обязателен даже когда писать в лист нечего (правило №6)."""
        print(f"{brief_id}: {note}")
        runlog.log_run(sheets, agent="brief-fixer", status="insufficient_data",
                       input_summary=f"{brief_id}: {note}",
                       started_at=run_started(args))
        return 0

    if brief_was_fixed(row):
        # Метку мог поставить и сам агент. Переписав notes второй раз, мы затёрли бы
        # текст, который человеку уже показан, — состояние и так желаемое.
        return trace("метка доработки уже стоит — лист не трогаем")
    if norm_status(row.get("review_status")) != "revised":
        # Вне «доработки» метка не значит ничего, а вот навредить может: ревьюер
        # позже поставит «доработка», и фиксер пропустит бриф, решив, что уже пробовал.
        return trace(f"метка не поставлена — review_status="
                     f"{row.get('review_status')!r}, а не «доработка»")

    notes = f"{FIX_MARKER} {FIX_REFUSED_SUFFIX}: {args.notes}".strip().rstrip(":")
    # Замечания ревьюера — единственное объяснение, почему бриф вернули; человек
    # решает по ним. Дописываем, а не затираем (ревью 14.09.2026).
    previous = str(row.get("reviewer_notes") or "").strip()
    if previous:
        notes = f"{notes}; замечания ревьюера: {previous}"
    sheets.update_row_fields("briefs", "brief_id", brief_id,
                             {"reviewer_notes": notes})
    return trace(f"снят с очереди доработки к вам — {args.notes or notes}")


def cmd_revise_brief(sheets, args):
    """Переписать сценарий по замечаниям ревьюера и вернуть его на ревью.

    Замыкает единственный оставшийся тупик конвейера: вердикт `revise` был
    приговором — ревьюер называл, что поправить, а поправить было некому. Брифы
    копились в очереди продюсера, изображая решения, которых он принять не может
    («сюжет скопирован с референса» — это не его выбор, а работа для генератора).

    Одна попытка на бриф: переписанный и снова забракованный уходит к человеку
    (см. FIX_MARKER). Иначе завод и ревьюер могли бы пинговать друг друга вечно.

    Режим --refused (разбор 2026-07-27) — вторая половина той же петли: завод
    ОТКАЗАЛСЯ переписывать. Сценария нет, файла нет, есть только причина; см.
    revise_brief_refused.
    """
    from cf.validate import validate_json_file
    brief_id = revise_brief_id(args)
    refused = bool(getattr(args, "refused", False))
    path = getattr(args, "file", None)
    # --file/--brief-id больше не required у argparse (иначе --refused не выразить),
    # поэтому сочетания разбираем здесь и отвечаем внятно, а не падаем ниже по коду.
    if not brief_id:
        print("error: нужен brief_id (позиционным аргументом или --brief-id)",
              file=sys.stderr)
        return 1
    if refused:
        if path:
            print("error: --refused ничего не переписывает — уберите --file",
                  file=sys.stderr)
            return 1
        return revise_brief_refused(sheets, args, brief_id)
    if not path:
        print("error: нужен --file с переписанным брифом (или --refused, если "
              "завод его не переписал)", file=sys.stderr)
        return 1

    errors = validate_json_file("brief", path)
    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    brief = read_json(path)
    file_id = str(brief["brief_id"])
    if file_id != brief_id:
        print(f"error: в файле brief_id={file_id!r}, а правится {brief_id!r}",
              file=sys.stderr)
        return 1

    rows = sheets.read_rows("briefs")
    row = next((r for r in rows if str(r.get("brief_id")) == brief_id), None)
    if row is None:
        print(f"error: бриф {brief_id} не найден в CF Creative Briefs", file=sys.stderr)
        return 1

    def refuse(note):
        print(f"{brief_id}: {note}")
        runlog.log_run(sheets, agent="brief-fixer", status="insufficient_data",
                       input_summary=f"{brief_id}: {note}",
                       started_at=run_started(args))
        return 0

    if norm_status(row.get("review_status")) != "revised":
        return refuse(f"не переписан — review_status={row.get('review_status')!r} "
                      f"(переписываются только «доработка»)")
    if brief_was_fixed(row):
        return refuse("не переписан — завод уже правил его однажды, решение за вами")

    notes = f"{FIX_MARKER}: {args.notes}".strip().rstrip(":")
    sheets.update_row_fields("briefs", "brief_id", brief_id, {
        "hook": brief["hook"],
        "script": brief["script"],
        "visual_direction": brief["visual_direction"],
        "cta": brief["cta"],
        "references": "; ".join(brief["references"]),
        "prompt_version": brief["prompt_version"],
        "source_pattern_ids": ", ".join(brief["source_pattern_ids"]),
        "payload_json": json.dumps(brief, ensure_ascii=False),
        "review_status": "pending",
        "reviewer_notes": notes,
        "rejection_reason": "",
    }, optional_fields={"reviewed_at": ""})
    runlog.log_run(sheets, agent="brief-fixer", status="success",
                   input_summary=f"{brief_id}: переписан по замечаниям, снова pending",
                   started_at=run_started(args))
    print(f"{brief_id}: переписан и возвращён на ревью")
    return 0


def cmd_rejection_history(sheets, args):
    from cf.reasons import REASON_CODES, parse_reason_code
    # P5.12: группировка по КОДУ причины (стабильный ключ), а не по точному тексту —
    # иначе повторы не копились и evidence-петля оптимизатора была мертва. Каждая
    # запись отчёта несёт code (канонический код или None для legacy-строк без кода).
    reasons = {}
    for b in sheets.read_rows("briefs"):
        if norm_status(b.get("review_status")) in ("rejected", "revised"):
            raw = str(b.get("rejection_reason", "")).strip()
            code = parse_reason_code(raw)
            # Ключи группировки разведены по пространствам имён: код и дословно равный
            # ему legacy-текст ('other' vs '[other] …') не должны слиться в одну группу
            # и утопить code=None. Сам key наружу не отдаётся — на схему отчёта не влияет.
            if code:
                key, label = f"code:{code}", REASON_CODES[code]
            elif raw:
                # Старая запись без кода: группируем по её тексту (повторы такого
                # текста не копятся, но отчёт не ломается — graceful для legacy-данных).
                key, label = f"text:{raw.lower()}", raw.lower()
            else:
                key, label = "none", "(no reason logged)"
            item = reasons.setdefault(
                key, {"code": code, "reason": label, "count": 0, "brief_ids": []})
            item["count"] += 1
            item["brief_ids"].append(str(b.get("brief_id")))
    report = {
        "generated_at": now_iso(),
        "total_rejected_or_revised": sum(i["count"] for i in reasons.values()),
        "reasons": sorted(reasons.values(), key=lambda i: -i["count"]),
    }
    write_json_atomic(args.out, report)
    print(f"rejection history -> {args.out}")
    for item in report["reasons"][:5]:
        tag = f"[{item['code']}] " if item["code"] else ""
        print(f"  {item['count']}x {tag}{item['reason']}")
    return 0


def cmd_eval_prep(sheets, args):
    from cf.evalprep import build_eval_dataset
    # Вкладка reels даёт published_at для дедупа к 7-му дню. Best-effort: раньше eval-prep
    # работал и без неё — если её нет/недоступна, продолжаем с fallback (последний
    # measured_at), а не падаем.
    try:
        reels = sheets.read_rows("reels")
    except Exception as exc:  # noqa: BLE001 — best-effort, любой сбой чтения не должен ронять eval
        print(f"  WARNING: вкладка reels недоступна ({exc}) — "
              f"дедуп замеров по fallback (последний measured_at)")
        reels = None
    # Деньги (тикет 05): CF UTM Traffic и CF Orders обогащают строки датасета
    # clicks/orders. Best-effort как reels: вкладок может ещё не быть (сбор
    # Метрики не запускался) — датасет прежней формы плюс предупреждение ниже.
    money = {}
    for tab in ("utm_traffic", "orders"):
        try:
            money[tab] = sheets.read_rows(tab)
        except Exception as exc:  # noqa: BLE001 — деньги не роняют eval
            print(f"  WARNING: вкладка {tab} недоступна ({exc}) — "
                  f"датасет без её денег")
            money[tab] = None
    # --since применяется внутри build_eval_dataset ПОСЛЕ дедупа (не префильтр замеров).
    dataset = build_eval_dataset(sheets.read_rows("performance"), sheets.read_rows("briefs"),
                                 sheets.read_rows("prompt_versions"),
                                 reels=reels, since=args.since,
                                 utm=money["utm_traffic"], orders=money["orders"])
    if money["utm_traffic"] is None and money["orders"] is None:
        # Обе вкладки недоступны -> build_eval_dataset вернул датасет прежней
        # формы (обратная совместимость); предупреждение кладём здесь, чтобы в
        # JSON оно всё равно попало, а статус/гейты остались нетронутыми.
        dataset["warnings"].append(
            "вкладки CF UTM Traffic и CF Orders недоступны — датасет прежней "
            "формы, без clicks/orders (деньги в eval не участвуют)")
    dataset["generated_at"] = now_iso()
    dataset["since"] = args.since
    path = Path(args.out_dir) / f"{date.today().isoformat()}-eval-dataset.json"
    write_json_atomic(path, dataset)
    print(f"eval dataset -> {path}")
    jh = dataset["join_health"]
    pairs = [p for f in dataset["cohorts_by_formula"].values()
             for g in f["parallel_groups"] for p in g["comparable_pairs"]]
    pairs_ok = sum(1 for p in pairs if p["verdict_gate"] == "ok")
    print(f"  reels: {len(dataset['rows'])}, "
          f"versions: {len(dataset['by_prompt_version'])}, "
          f"comparable A/B pairs: {len(pairs)} (ok: {pairs_ok}), "
          f"pass rate: {dataset['reviewer_pass_rate']}, status: {dataset['status']}")
    print(f"  join_health: unknown_version_share={jh['unknown_version_share']}, "
          f"briefs_not_found={jh['briefs_not_found']}")
    for w in dataset["warnings"]:
        print(f"  WARNING: {w}")
    return 0


def log_prompt_version(sheets, prompt_id, version, path, changelog, candidate=False):
    """Записать версию промпта. active — дописать активную, погасив прежние; candidate
    (P5.13) — дописать кандидата для честного A/B, НЕ трогая active.

    Общий код CLI (cmd_log_prompt_version) и скаффолда промпта с дашборда
    (cf.dashboard.decisions.scaffold_prompt) — чтобы «активировать промпт» шло одним
    путём и не разъехалось.

    Статус хранится значением в колонке active ('TRUE'/'1'/'YES' — active, та же
    семантика, что _prompt_is_active; 'CANDIDATE' — кандидат; 'FALSE'/пусто —
    деактивирована). Новой колонки нет намеренно (append молча теряет колонки вне
    заголовков живого листа, update_* падает UnknownFieldsError — как P5.12).
    update_rows_where матчит по точному нормализованному значению, поэтому гасим по
    каждому токену отдельно.

    candidate=True: гасим лишь прежнего кандидата этого промпта (один кандидат за раз),
    active оставляем — версии идут ПАРАЛЛЕЛЬНО. candidate=False (активация): гасим и
    прежние active, и «повисшего» кандидата (сменилась база — прежний A/B устарел).

    Гард (candidate=True): github_path кандидата не должен совпадать с активным — иначе
    A/B двух ИДЕНТИЧНЫХ файлов (обе когорты по одному тексту, разнятся только
    prompt_version) и вердикт по шуму. Текст кандидата кладётся в ОТДЕЛЬНЫЙ файл
    (reel-v{N+1}.md). Совпадение -> ValueError (CLI вернёт exit 1).
    """
    from cf.abtest import CANDIDATE_TOKEN

    if candidate:
        active, _ = select_versions(sheets.read_rows("prompt_versions"), prompt_id)
        if active is not None and \
                str(active.get("github_path", "")).strip() == str(path).strip():
            raise ValueError(
                f"candidate github_path совпадает с активным ({path}): A/B двух "
                f"идентичных файлов даст вердикт по шуму. Положи текст кандидата в "
                f"ОТДЕЛЬНЫЙ файл (например reel-v{{N+1}}.md рядом) и укажи его путь.")

    # M36 (аудит 2026-07-24): сначала append новой строки, потом деактивация прежних
    # с exclude по свежему activated_at (метка уникальна с точностью до секунды —
    # version не годится: кандидат той же версии при активации гаситься ОБЯЗАН).
    # Крэш между шагами оставляет ДВЕ активные версии (безопасно: select_versions
    # берёт последнюю, _prompt_is_active — any()), а не ноль, как при старом
    # порядке «погасить → append», когда сбой append молча останавливал
    # auto-approve и брифы всей ниши.
    activated_at = now_iso()
    sheets.append_row("prompt_versions", {
        "prompt_id": prompt_id,
        "version": version,
        "github_path": path,
        "active": CANDIDATE_TOKEN if candidate else "TRUE",
        "activated_at": activated_at,
        "deactivated_at": "",
        "changelog": changelog,
    })
    deactivate = (CANDIDATE_TOKEN,) if candidate else ACTIVE_TOKENS + (CANDIDATE_TOKEN,)
    for token in deactivate:
        sheets.update_rows_where("prompt_versions",
                                 {"prompt_id": prompt_id, "active": token},
                                 {"active": "FALSE", "deactivated_at": now_iso()},
                                 exclude={"activated_at": activated_at})


def cmd_log_prompt_version(sheets, args):
    candidate = getattr(args, "candidate", False)
    try:
        log_prompt_version(sheets, args.prompt_id, args.version, args.path, args.changelog,
                           candidate=candidate)
    except ValueError as exc:  # гард candidate: одинаковый github_path и т.п.
        print(f"  - {exc}", file=sys.stderr)
        return 1
    if candidate:
        print(f"{args.prompt_id} {args.version} записана как candidate ({args.path}) — "
              f"active не тронут (честный A/B P5.13)")
    else:
        print(f"{args.prompt_id} {args.version} активирована ({args.path})")
    return 0


def cmd_apply_brief_prompt(sheets, args):
    """CLI-двойник кнопки «Включить промпт темы» на воротах дашборда.

    Тот же код (cf.dashboard.prompt_apply), та же конвенция, что у mark-published:
    маршрут и команда стоят за одной функцией. Нужен на случай, когда дашборд лежит,
    а тему расшить надо. `--show` печатает текст промпта и sha, ничего не меняя, —
    ревью перед применением остаётся обязательным (правило №3).
    """
    from cf.dashboard.prompt_apply import (PromptApplyError, apply_prompt,
                                           preflight, read_draft, reject_draft)
    # Корень репозитория — от индекса утверждённых рецептов, как у остальных
    # команд, работающих с деревом (_repo_root_from_index).
    root = _repo_root_from_index(args.index)
    auto = getattr(args, "auto", False)
    if getattr(args, "check", False) or auto:
        try:
            versions = sheets.read_rows("prompt_versions")
        except Exception as exc:  # noqa: BLE001 — правило №2: не судим по непрочитанному
            print(f"  ВНИМАНИЕ: prompt_versions недоступны ({exc})")
            versions = None
        report = preflight(root, args.filename, versions)
        for check in report["checks"]:
            mark = {True: "✓", False: "✗", None: "?"}[check["ok"]]
            print(f"  {mark} {check['label']}"
                  + (f": {check['detail']}" if check["detail"] else ""))
        if getattr(args, "check", False):
            print(f"{args.filename}: "
                  f"{'зелёный' if report['green'] else 'на ворота'}")
            return 0
    try:
        if args.show:
            draft = read_draft(root, args.filename)
            print(f"# тема: {draft['niche']} · статус: {draft['status']} · "
                  f"sha256: {draft['sha256']}")
            print(draft["prompt_text"], end="")
            return 0
        if args.reject:
            niche = reject_draft(root, args.filename, reason=args.reason,
                                 sheets=sheets)
            print(f"черновик промпта темы {niche} отклонён")
            return 0
        if auto:
            from cf.dashboard.autogate import gate_policy
            policy = gate_policy(sheets.config, "prompt")
            if policy != "auto":
                print(f"{args.filename}: ворота «Включение промпта темы» в ручном "
                      f"режиме (cf.config.json → gates.policy.prompt={policy!r})")
                runlog.log_run(sheets, agent="prompt-apply",
                               status="insufficient_data",
                               input_summary=f"{args.filename}: ручной режим ворот",
                               started_at=run_started(args))
                return 0
        niche = apply_prompt(root, args.filename, sha256=args.sha256 or None,
                             sheets=sheets, require_green=auto,
                             versions=versions if auto else None)
    except PromptApplyError as exc:
        print(f"  - {exc}", file=sys.stderr)
        if auto:
            # Авто-режим: не включили — это не сбой пайплайна, тема просто уходит
            # к человеку на ворота с названной причиной (как у cf auto-approve).
            runlog.log_run(sheets, agent="prompt-apply", status="insufficient_data",
                           input_summary=f"{args.filename}: {exc}",
                           started_at=run_started(args))
            return 0
        return 1
    print(f"промпт темы {niche} включён: prompts/briefs/{niche}/reel.md, "
          f"версия brief-{niche}-reel v1 активна")
    return 0


def cmd_apply_prompt_candidate(sheets, args):
    """Правка ДЕЙСТВУЮЩЕГО промпта темы — кандидатом в A/B, а не заменой.

    Активную версию не трогаем: по ней уже произведены сценарии, и подмена текста
    порвала бы связку «версия → текст → результат», на которой стоит eval. Кандидат
    встаёт рядом, генератор чередует версии между брифами одной формулы, и вердикт
    выносит результат, а не чтение. Это и есть новая редакция правила №3.
    """
    from cf.dashboard.prompt_apply import (PromptApplyError, apply_candidate,
                                           candidate_preflight)
    root = _repo_root_from_index(args.index)
    try:
        versions = sheets.read_rows("prompt_versions")
    except Exception as exc:  # noqa: BLE001 — правило №2: не судим по непрочитанному
        print(f"  ВНИМАНИЕ: prompt_versions недоступны ({exc})")
        versions = None
    report = candidate_preflight(root, args.filename, versions)
    for check in report["checks"]:
        mark = {True: "✓", False: "✗", None: "?"}[check["ok"]]
        print(f"  {mark} {check['label']}"
              + (f": {check['detail']}" if check["detail"] else ""))
    if args.check:
        print(f"{args.filename}: {'зелёный' if report['green'] else 'на ворота'}")
        return 0
    try:
        niche, target = apply_candidate(root, args.filename, sheets=sheets,
                                        versions=versions)
    except PromptApplyError as exc:
        print(f"  - {exc}", file=sys.stderr)
        runlog.log_run(sheets, agent="prompt-apply", status="insufficient_data",
                       input_summary=f"{args.filename}: {exc}",
                       started_at=run_started(args))
        return 1
    print(f"кандидат A/B для темы {niche} поставлен: {target}. "
          f"Генератор будет чередовать его с активной версией; "
          f"вердикт вынесет недельный eval.")
    return 0


def cmd_brief_version(sheets, args):
    """План версий для брифов одной формулы (P5.13 interleaving): JSON-список
    {index, prompt_version, github_path, cohort}. Есть candidate -> чередование
    active/candidate (~50/50), иначе все active. Зовётся генератором брифов."""
    plan = interleave_plan(sheets.read_rows("prompt_versions"), args.prompt_id, args.count)
    # count<2 не даёт пары active/candidate в одном прогоне; предупреждение в stderr,
    # чтобы stdout остался чистым JSON для агента. Пары надо копить между прогонами.
    if args.count < 2:
        print(f"WARNING: count={args.count} < 2 — план не даёт пары active/candidate в "
              f"одном прогоне; сохраняй пары между прогонами (см. brief-generator.md)",
              file=sys.stderr)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def cmd_check_commit(sheets, args):
    from cf.guard import check_staged
    # encoding: git отдаёт пути в UTF-8; локальная кодировка (cp1251) их ломает.
    # --diff-filter=d исключает staged-удаления — дальше любой сбой чтения blob = нарушение.
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=d", "-z"],
                         capture_output=True, encoding="utf-8", check=True).stdout
    paths = [p for p in out.split("\0") if p]

    def read_text(path):
        # Читаем staged blob, а не рабочую копию: коммитится именно индекс.
        res = subprocess.run(["git", "show", f":{path}"], capture_output=True)
        if res.returncode != 0:
            raise OSError(res.stderr.decode(errors="ignore"))
        return res.stdout.decode("utf-8", errors="ignore")

    violations = check_staged(paths, read_text)
    if violations:
        print("COMMIT BLOCKED:")
        for v in violations:
            print(f"  - {v}")
        return 1
    return 0


DEFAULT_DASHBOARD_PORT = 8787


def resolve_dashboard_port(args_port, config):
    """Порт дашборда: явный --port важнее конфига; иначе dashboard.port из
    cf.config.json, иначе дефолт 8787. Так ключ dashboard.port реально читается
    (раньше он висел в конфиге, но порт был захардкожен в argparse)."""
    if args_port is not None:
        return int(args_port)
    return int((config or {}).get("dashboard", {}).get("port", DEFAULT_DASHBOARD_PORT))


def cmd_dashboard(sheets, args):
    import uvicorn

    from cf.config import load_config
    from cf.dashboard.app import build_production_app
    config = load_config()
    port = resolve_dashboard_port(getattr(args, "port", None), config)
    # порт нужен и uvicorn, и CSRF-middleware (разрешённые Origin выводятся из него)
    app = build_production_app(sheets=sheets, config=config, port=port)
    print(f"CF Dashboard: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)
    return 0


def cmd_install_hooks(sheets, args):
    # Аудит 2026-07-24 (H18): путь venv — POSIX (.venv/bin), и без exec-бита
    # git молча пропускает хук (fail-open) — chmod обязателен.
    hook = Path(".git/hooks/pre-commit")
    hook.write_text("#!/bin/sh\nexec .venv/bin/python -m cf check-commit\n",
                    encoding="utf-8", newline="\n")
    hook.chmod(hook.stat().st_mode | 0o755)
    print(f"pre-commit hook installed: {hook}")
    return 0


def cmd_trace(sheets, args):
    from cf.trace import build_trace
    trace = build_trace(args.brief_id, sheets.read_rows("briefs"),
                        sheets.read_rows("reels"), sheets.read_rows("performance"))
    if not trace["found"]:
        print(f"brief {args.brief_id} не найден")
        return 1
    print(f"brief {trace['brief_id']}")
    print(f"  patterns: {trace['source_pattern_ids']}")
    print(f"  formula:  {trace['formula_id']}")
    print(f"  prompt:   {trace['prompt_version']}")
    for r in trace["reels"]:
        print(f"  reel {r['reel_id']} ({r['platform']}): {r['post_url']}")
    for p in trace["performance"]:
        print(f"    perf {p['measured_at']}: views={p['views']} er={p['er']}")
    if trace["missing_links"]:
        print(f"  MISSING: {', '.join(trace['missing_links'])}")
    return 0


def cmd_apply_sources(sheets, args):
    # C3.3: proposal применяется к реестру sources/<platform>.json; JS-эталоны
    # n8n не трогаем (n8n собирает по своим спискам до этапа 5, расхождение
    # реестра и JS допустимо и завершится переключением). Шаг push-n8n исчез.
    from cf.lock import LockTimeout
    from cf.pipeline_lock import registry_lock
    from cf.sourcepatch import apply_proposal_to_registry
    from cf.validate import validate_json_data, validate_json_file
    errors = validate_json_file("source-proposal", args.proposal)
    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    proposal = read_json(args.proposal)
    if proposal["status"] != "approved":
        print(f"proposal не approved (status={proposal['status']}) — сначала ревью оператора",
              file=sys.stderr)
        return 1
    reg_path = Path("sources") / (proposal["platform"] + ".json")
    try:
        # M37: collect держит этот лок весь прогон — применение в его окно молча
        # откатывалось бы save_registry устаревшей копией. Честный отказ лучше.
        with registry_lock(proposal["platform"], timeout=5.0):
            records, summary = apply_proposal_to_registry(
                read_json(reg_path), proposal, today=now_iso()[:10])
            # M38: валидация ДО записи — битый реестр не попадает на диск,
            # прод-таймеры продолжают читать прежний валидный файл.
            reg_errors = validate_json_data("sources", records)
            if reg_errors:
                for e in reg_errors:
                    print(f"  - {e}", file=sys.stderr)
                print(f"реестр {reg_path} после применения не прошёл бы схему — "
                      f"файл не тронут, проверь proposal", file=sys.stderr)
                return 1
            write_json_atomic(reg_path, records)
    except LockTimeout:
        print(f"реестр {reg_path} занят сбором (cf collect {proposal['platform']}) — "
              f"повтори после завершения", file=sys.stderr)
        return 1
    proposal["applied_at"] = now_iso()
    write_json_atomic(args.proposal, proposal)
    print(f"реестр обновлён: {reg_path} (+{summary['added']} / retired {summary['retired']}"
          f" / реактивировано {summary['reactivated']})")
    for src in summary["missing"]:
        print(f"  ! remove не нашёл в реестре: {src}")
    print("дальше: git commit " + str(reg_path))
    return 0


MAX_LOST_LINES = 6                 # хвост причин: экран отчёта, а не лог целиком


def collect_stdout_lines(stage, summary):
    """Что `cf collect` печатает в stdout: статус И причина деградации.

    (разбор 2026-07-27) Ночью Apify упёрся в потолок трат и перестал стартовать
    раны; гейт написал, что именно потеряно, — но на экран «Отчётов этапов»
    попало 25 символов «collect instagram: failed». Причина была в памяти
    процесса, ушла в Telegram и Run Log, а человек, открывший дашборд, читал
    отчёт этапа. Фолбэк раннера на stderr (runner.py:1321) не спасает: он берёт
    stderr, только когда stdout ПУСТ, а тут он непуст.

    Ключи читаются через .get(): сводки сборщиков доезжают до контракта
    (lost_reasons) не одновременно, и печать не должна падать на старой сводке.
    """
    summary = summary or {}
    head = f"collect {stage}: {summary.get('status')}"
    reason = str(summary.get("error") or "").strip()
    lines = [f"{head} — {reason}" if reason else head]
    lost = [str(x).strip() for x in (summary.get("lost_reasons") or [])
            if str(x).strip()]
    for item in lost[:MAX_LOST_LINES]:
        lines.append(f"  потеряно: {item}")
    if len(lost) > MAX_LOST_LINES:
        lines.append(f"  … ещё {len(lost) - MAX_LOST_LINES} причин(ы)")
    # sources_lost — второй сигнал того же отказа: батч не стартовал => источники
    # этой ночи не опрошены. Показываем число, имена уже перечислены в причинах.
    lost_sources = summary.get("lost_sources") or []
    if lost_sources:
        lines.append(f"  источников не опрошено: {len(lost_sources)}")
    # Счётчики медиа-контура (тикет 03 визуального контура) — в том же
    # гарантированном блоке статуса: сводка сборщика тоже печатается, но
    # именно эти строки «Отчёты этапов» показывают всегда (см. docstring).
    media = summary.get("media") or {}
    if media:
        lines.append("  медиа: сохранено={saved} частично={partial} "
                     "отказов={failed} кадров={frames} повторно={reused} "
                     "протухших={expired}".format(
                         **{k: media.get(k, 0) for k in
                            ("saved", "partial", "failed", "frames",
                             "reused", "expired")}))
    return lines


def _notify_collect(stage, summary, config):
    """Сказать владельцу про исход сбора — но не повторяться каждое утро.

    Зовётся на ЛЮБОМ исходе, включая success: иначе некому заметить, что
    поломка ушла, и тревога висела бы открытой вечно (cf.alerts).
    """
    try:
        from cf.alerts import track
        from cf.messages import collect_alert, collect_recovered
        from cf.notify import notify_telegram

        status = str((summary or {}).get("status") or "")
        broken = status in ("failed", "insufficient_data")
        send, day, recovered = track(f"collect:{stage}", broken, kind=status)
        if recovered:
            # Сводка починившегося прогона едет в «снова работает»: это
            # единственное сообщение успешного сбора, и факты про источники
            # (тикет 06 плана 2026-08-10-apify-costs) иначе не доехали бы.
            notify_telegram(collect_recovered(stage, config, summary=summary),
                            config)
        elif send:
            notify_telegram(collect_alert(stage, summary, config, day=day),
                            config)
    except Exception as exc:  # noqa: BLE001 — телеметрия не важнее сбора
        # Только тип: текст исключения может нести секрет (H9).
        print(f"уведомление не отправлено: {type(exc).__name__}",
              file=sys.stderr)


def _notify_collect_crash(stage, exc, config):
    """Сбор не смог даже стартовать (ключи, таблица, сеть)."""
    try:
        from cf.alerts import track
        from cf.messages import collect_crash_alert
        from cf.notify import notify_telegram

        send, day, _ = track(f"collect:{stage}", True, kind="crash")
        if send:
            notify_telegram(collect_crash_alert(stage, exc, config, day=day),
                            config)
    except Exception as notify_exc:  # noqa: BLE001
        print(f"уведомление не отправлено: {type(notify_exc).__name__}",
              file=sys.stderr)


# Цикл ходит раз в сутки в 07:30; 26 часов — запас на сдвиг таймера и долгий прогон.
CYCLE_STALE_HOURS = 26
CYCLE_PROGRESS_PATH = "agent-runtime/reports/pipeline-progress.json"


def _last_cycle_at(path):
    """Когда завод в последний раз доехал до конца (или None, если не понять)."""
    try:
        raw = ((read_json(path) or {}).get("__cycle__") or {}).get("at")
        at = datetime.fromisoformat(str(raw))
    except Exception:  # noqa: BLE001 — нет файла / битая отметка: считаем «не знаем»
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def cmd_heartbeat(sheets, args):
    """Сторож: заметить, что завод не отработал, и сказать владельцу.

    Живёт ОТДЕЛЬНЫМ процессом по своему таймеру — в этом весь смысл. Прежде
    единственный отправитель уведомлений сидел внутри дашборда, поэтому его
    смерть не мог заметить никто: сообщать было некому, и тишина в чате
    означала одновременно «всё хорошо» и «завод умер».

    Google Sheets не трогает намеренно — читается только локальный конфиг
    (хэндл в cf.sheets ленивый): сторож обязан работать, когда сломано всё
    остальное. По той же причине всегда выходит с кодом 0: ненулевой код
    поднял бы OnFailure= самого сторожа и удвоил сообщение.
    """
    from cf.alerts import track
    from cf.messages import cycle_back, cycle_missing
    from cf.notify import notify_telegram

    config = sheets.config
    limit = float(getattr(args, "max_hours", None) or CYCLE_STALE_HOURS)
    at = _last_cycle_at(Path(getattr(args, "progress", None)
                             or CYCLE_PROGRESS_PATH))
    hours = None if at is None else (
        datetime.now(timezone.utc) - at).total_seconds() / 3600.0
    stale = hours is None or hours > limit

    send, day, recovered = track("cycle:missing", stale)
    if recovered:
        notify_telegram(cycle_back(config), config)
        print("завод снова отрабатывает — тревога снята")
        return 0
    if not stale:
        print(f"завод отработал {hours:.1f} ч назад — всё в порядке")
        return 0
    if send:
        notify_telegram(cycle_missing(hours, config, day=day), config)
    when = "неизвестно когда" if hours is None else f"{hours:.1f} ч назад"
    print(f"завод не отработал (последний прогон: {when})"
          f"{'' if send else ' — уже сообщали'}")
    return 0


def cmd_alert_unit(sheets, args):
    """Сказать владельцу, что служба на сервере упала. Зовётся из OnFailure=.

    Отдельный процесс, поднимаемый самим systemd: сообщение уходит, даже если
    лежит пульт. До 2026-08-09 OnFailure= не было ни у одного юнита, и падения
    бэкапа, архивации и утреннего запуска не доходили ни до кого.
    """
    from cf.alerts import track
    from cf.messages import unit_failed
    from cf.notify import notify_telegram

    unit = str(getattr(args, "unit", "") or "").strip()
    if not unit:
        print("не указана служба", file=sys.stderr)
        return 1
    config = sheets.config
    send, day, _ = track(f"unit:{unit}", True)
    if not send:
        print(f"{unit}: уже сообщали ({day}-й день)")
        return 0
    ok = notify_telegram(unit_failed(unit, config, day=day), config)
    print(f"{unit}: уведомление {'отправлено' if ok else 'НЕ отправлено'}")
    return 0


def cmd_notify_test(sheets, args):
    """Проверить, что бот жив: отправить владельцу тестовое сообщение.

    Ответ на вопрос «а он вообще работает?», который раньше можно было задать
    только аварии: единственная проверка контура была разовой при миграции.
    """
    from cf.messages import notify_selftest
    from cf.notify import notify_telegram, telegram_configured

    config = sheets.config
    if not telegram_configured(config):
        print("Telegram не настроен: нет токена или chat_id в ~/.cf/secrets/",
              file=sys.stderr)
        return 1
    if notify_telegram(notify_selftest(config), config):
        print("сообщение отправлено — проверь чат с ботом")
        return 0
    print("отправить не удалось: причина в журнале (journalctl) — токен, "
          "chat_id или сеть", file=sys.stderr)
    return 1


def cmd_collect(sheets, args):
    # C3.1: сбор без n8n (спека §2). Exit 0 — success И insufficient_data
    # (0 строк — честный статус, не авария), 1 — failed.
    from cf.collect import instagram, metrika, performance, snowball, tiktok
    from cf.collect.apify import client_from_config
    from cf.pipeline_lock import (locks_held_by_parent, registry_lock,
                                  release_stage_locks, try_stage_locks)

    config = sheets.config
    dry = bool(getattr(args, "dry_run", False))

    # H8/H10/M7: между процессами действует тот же мьютекс raw↔factory, что у
    # StageRunner, — таймерный/ручной collect не въезжает в работающий фан-аут
    # и наоборот. Если локи уже держит родитель (дашборд запускает collect
    # subprocess'ом из захваченного звена), повторно не берём.
    locks = []
    if not locks_held_by_parent():
        stages = (("stats",) if args.stage in ("performance", "metrika")
                  else ("raw", "factory"))
        locks, busy = try_stage_locks(stages)
        if busy:
            print(f"collect {args.stage}: пропущено — конвейер занят (звено {busy})")
            try:
                runlog.log_run(sheets, agent=f"collect-{args.stage}", status="skipped",
                               input_summary=f"конвейер занят (звено {busy})",
                               started_at=run_started(args))
            except Exception:  # noqa: BLE001 — след важен, но пропуск не должен падать
                pass
            return 0
    try:
        # Внутри try (ревью аудита): чтение apify-токена падает FileNotFoundError
        # при ротации/кривых правах — без этого сбой миновал бы Run Log и Telegram.
        # Метрике Apify не нужен: её клиента собирает сам сборщик, а отсутствие
        # своего токена/счётчика он отвечает честным insufficient_data.
        client = None if args.stage == "metrika" else client_from_config(config)
        with contextlib.ExitStack() as stack:
            if args.stage in ("tiktok", "instagram"):
                # M37: реестр sources/<platform>.json заперт на весь прогон —
                # apply-sources в это окно честно откажет, а не потеряет правки.
                stack.enter_context(registry_lock(args.stage, timeout=30.0))
            # Приёмка этапа 4: --batches 0 снимает dry-run-лимит «1 батч» (полный
            # прогон без записи), --no-explore выключает exploration-батч — у n8n
            # его нет, сравнение должно быть 1:1.
            batches = getattr(args, "batches", None)
            limit = batches if batches is not None else (1 if dry else None)
            if args.stage == "metrika":
                summary = metrika.collect(sheets, config=config, dry_run=dry,
                                          log=print)
            elif args.stage == "performance":
                summary = performance.collect(sheets, client, dry_run=dry, log=print,
                                              limit=None if limit == 0 else limit)
            else:
                fn = {"tiktok": tiktok.collect, "instagram": instagram.collect,
                      "snowball": snowball.collect}[args.stage]
                kwargs = {}
                if args.stage != "snowball":
                    kwargs["explore"] = not getattr(args, "no_explore", False)
                summary = fn(sheets, client, config=config, dry_run=dry, log=print,
                             limit_batches=None if limit == 0 else limit,
                             max_workers=config.get("apify", {}).get("max_workers", 2),
                             **kwargs)
            if dry:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                out = Path("agent-runtime/collect") / f"dry-run-{args.stage}-{stamp}.jsonl"
                write_jsonl_atomic(out, summary.get("rows", []))
                print(f"dry-run: {len(summary.get('rows', []))} строк -> {out}")
            for line in collect_stdout_lines(args.stage, summary):
                print(line)
            if not dry:
                # спека §5: «сбор упал / 0 строк» — событие для оператора.
                # Зовём на любом статусе: success закрывает висящую тревогу.
                # Внутри свой try — вызов стоит В ОБЩЕМ try, и падение
                # уведомления внешний except записал бы в Run Log как
                # «инфраструктурный сбой», подменив штатный исход аварией.
                _notify_collect(args.stage, summary, config)
            return 0 if summary["status"] in ("success", "insufficient_data") else 1
    except Exception as exc:  # noqa: BLE001 — H3/M26: инфраструктурный сбой не молчит
        # Исключение вне гейта (Sheets, реестр, токен) раньше улетало в main():
        # «error:» в journalctl, ни строки в Run Log, ни Telegram — конвейер стоял
        # незаметно. След и алерт обязательны; сам сбой остаётся exit 1.
        try:
            runlog.log_run(sheets, agent=f"collect-{args.stage}", status="failed",
                           input_summary=f"инфраструктурный сбой: {exc}",
                           errors=[str(exc)],
                           started_at=run_started(args))
        except Exception:  # noqa: BLE001 — Sheets может лежать, алерт всё равно шлём
            pass
        if not dry:
            # Внутри свой try: вызов стоит В ВЕТКЕ except, и его падение
            # маскировало бы исходный сбой — print и return 1 ниже просто
            # не выполнились бы.
            _notify_collect_crash(args.stage, exc, config)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        release_stage_locks(locks)


def cmd_vision(sheets, args):
    # Тикет 05 визуального контура: платное и лимитное звено живёт отдельной
    # командой — его падение или лимит сбор не трогают. Exit 0 — success И
    # insufficient_data (пустая очередь — честный статус), 1 — failed.
    from cf import vision

    summary = vision.run(
        sheets, config=sheets.config, limit=args.limit,
        tabs=(args.tab,) if args.tab else vision.RAW_TABS,
        media_root=args.media_root, jsonl_path=args.jsonl,
        niche=args.niche, redo=args.redo, raw_ids=tuple(args.raw_ids),
        frames_cap=args.frames_cap,
        now=run_started(args), log=print)
    return 0 if summary["status"] in ("success", "insufficient_data") else 1


def cmd_export_seeds(sheets, args):
    index = read_json(args.index)
    sheets.ensure_tab("seeds", ["seed_url", "seed_type", "niche", "added_at", "active"])
    existing = {str(r.get("seed_url", "")).strip() for r in sheets.read_rows("seeds")}
    # Копим новые seed-ряды и шлём ОДНИМ batch-append. Цикл append_row делал 3 HTTP на
    # URL (метадата+чтение заголовков+append): 30 URL = 90+ вызовов -> квота Sheets
    # 60 read/min -> 429 -> with_retry растягивал экспорт на минуты. Дедуп по seed_url
    # оставляем здесь (append_rows не идемпотентен): фильтруем и уже присутствующие в
    # листе, и повторы внутри самого батча (existing пополняется по ходу).
    new_rows = []
    repo_root = _repo_root_from_index(args.index)
    for entry in index.get("approved", []):
        # Путь из индекса — канонически-относительный: резолвим от корня репо,
        # а не от CWD (аудит: export-seeds падал при запуске из чужой директории).
        try:
            formula = read_json(Path(repo_root) / _slash_path(str(entry.get("path", ""))))
        except (OSError, ValueError):
            print(f"  WARNING: файл формулы {entry.get('name')!r} недоступен "
                  f"({entry.get('path')!r}) — пропущен")
            continue
        for url in formula.get("evidence", {}).get("source_urls", []):
            url = str(url).strip()
            if "tiktok.com" in url and url not in existing:
                new_rows.append({
                    "seed_url": url, "seed_type": "winner",
                    "niche": formula.get("niche", ""),
                    "added_at": now_iso(), "active": "TRUE",
                })
                existing.add(url)
    sheets.append_rows("seeds", new_rows)
    print(f"seeds: добавлено {len(new_rows)}, всего {len(existing)}")
    return 0


def cmd_source_stats(sheets, args):
    from cf.sourcestats import build_source_stats
    rows = apply_filters(sheets.read_rows(args.tab), None, args.since)
    rows = dedupe_rows(rows)
    if args.target:
        targets = [t.strip() for t in args.target.split(",") if t.strip()]
    else:
        targets = (sheets.config.get("target_niches", [])
                   if hasattr(sheets, "config") else []) or ["мужские-образы"]
    stats = build_source_stats(rows, targets)
    stats["meta"] = {"tab": args.tab, "since": args.since, "generated_at": now_iso()}
    path = Path(args.out_dir) / f"{date.today().isoformat()}-{args.tab}-source-stats.json"
    write_json_atomic(path, stats)
    print(f"source stats -> {path}")
    print(f"{'source':44} {'rows':>5} {'target':>6} {'yield':>6} {'>=1k':>5} {'transcr':>7}")
    for s in stats["sources"]:
        print(f"{s['source'][:44]:44} {s['rows']:>5} {s['target_niche_rows']:>6} "
              f"{s['target_yield']:>6.2f} {s['views_1000_plus']:>5} {s['with_transcript']:>7}")
    if stats["candidate_hashtags"]:
        print("Кандидаты в хэштеги (из капшенов целевых ниш, views>=1000):")
        for c in stats["candidate_hashtags"][:10]:
            print(f"  {c['count']:>3}x {c['hashtag']}  {c['example_url']}")
    return 0


def cmd_backfill_attribution(sheets, args):
    from cf.attribution import source_query_from_raw_json

    def is_placeholder(value):
        v = str(value or "").strip()
        return not v or v == "managed_tiktok_keywords" or "," in v

    mapping = {}
    # include_heavy: этому потребителю нужен сам raw_json (по умолчанию проекция его не тянет).
    for r in sheets.read_rows(args.tab, include_heavy=True):
        raw_id = str(r.get("raw_id", "")).strip()
        if raw_id and is_placeholder(r.get("source_query")):
            mapping[raw_id] = source_query_from_raw_json(r.get("raw_json"))
    if getattr(args, "dry_run", False):
        print(f"would update {len(mapping)} rows in {args.tab}")
        return 0
    updated = sheets.set_column_by_key(args.tab, "raw_id", "source_query", mapping)
    print(f"source_query заполнен у {updated} строк в {args.tab}")
    return 0


def cmd_apply_niches(sheets, args):
    mapping = read_json(args.mapping)
    if not mapping:
        print(f"пустой mapping: {args.mapping}", file=sys.stderr)
        return 1
    updated = sheets.set_column_by_key(args.tab, args.key, "niche", mapping)
    # Массовая перезапись niche всегда оставляет след в Run Log сама — даже если агент
    # (ре-аудит/обычный режим) пропустил отдельный шаг `cf log-run` в промпте.
    runlog.log_run(sheets, agent="apply-niches", status="success",
                   input_summary=f"{args.tab}: обновлено {updated} строк "
                                 f"(mapping={len(mapping)})",
                   output_paths=[str(args.mapping)],
                   started_at=run_started(args))
    print(f"niche обновлена у {updated} строк в {args.tab} (в mapping: {len(mapping)})")
    return 0


BACKUP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-.+\.jsonl$")
BACKUP_RETENTION_DAYS = 30
# Сколько датированных снимков ротация оставляет в любом случае. Без минимума первый
# `cf backup` после долгой паузы (завод стоял с 13.08.2026) стёр бы всю допаузную
# историю: сегодняшний снимок свежий, всё прочее старше срока (ревью 14.09.2026).
BACKUP_MIN_DATES = 2


def _prune_backups(out_dir, today_date, keep_days=BACKUP_RETENTION_DAYS,
                   min_dates=BACKUP_MIN_DATES):
    """Удалить бэкапы старше keep_days по дате из имени файла.

    Считаем только файлы формата YYYY-MM-DD-<tab>.jsonl; посторонние файлы и снимки
    с непарсибельной датой не трогаем. Если в сроке осталось меньше min_dates дат,
    самые свежие из старых дат сохраняются до минимума. Возвращает имена удалённых.
    """
    cutoff = today_date - timedelta(days=keep_days)
    dated = []
    for f in sorted(Path(out_dir).glob("*.jsonl")):
        m = BACKUP_RE.match(f.name)
        if not m:
            continue
        try:
            dated.append((date.fromisoformat(m.group(1)), f))
        except ValueError:
            continue
    fresh = {d for d, _ in dated if d >= cutoff}
    stale = sorted({d for d, _ in dated if d < cutoff}, reverse=True)
    spared = set(stale[:max(0, min_dates - len(fresh))])
    removed = []
    for file_date, f in dated:
        if file_date < cutoff and file_date not in spared:
            f.unlink()
            removed.append(f.name)
    return removed


def cmd_backup(sheets, args):
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today_date = today()
    date_str = today_date.isoformat()
    # снимок ключей ДО логирования: log_run создаёт/дополняет run_log
    tab_keys = list(sheets.config["tabs"])
    counts, paths = {}, []
    for tab_key in tab_keys:
        # include_heavy: полный снимок вкладки обязан сохранять raw_json (проекция его прячет).
        rows = sheets.read_rows(tab_key, include_heavy=True)
        path = out_dir / f"{date_str}-{tab_key}.jsonl"
        write_jsonl_atomic(path, rows)
        counts[tab_key] = len(rows)
        paths.append(str(path))
    removed = _prune_backups(out_dir, today_date)

    note = ", ".join(f"{k}={v}" for k, v in counts.items())
    summary = f"{date_str} tabs: {note}" + (f"; pruned={len(removed)}" if removed else "")
    runlog.log_run(sheets, agent="backup", status="success",
                   input_summary=summary, output_paths=paths,
                   started_at=run_started(args))
    print(f"backup -> {out_dir} ({date_str})")
    for k in tab_keys:
        print(f"  {k}: {counts[k]} строк")
    if removed:
        print(f"удалено старых бэкапов (>{BACKUP_RETENTION_DAYS} дн.): {len(removed)}")
    return 0


def cmd_restore(sheets, args):
    path = Path(args.file)
    if not path.exists():
        print(f"файл не найден, откат отменён: {path}", file=sys.stderr)
        return 1
    # M14 (аудит 2026-07-24): имя снимка кодирует вкладку (YYYY-MM-DD-<tab>.jsonl) —
    # несовпадение с --tab затирало бы вкладку чужими данными с рапортом об успехе.
    m = re.match(r"^\d{4}-\d{2}-\d{2}-(?P<tab>.+)\.jsonl$", path.name)
    if m and m["tab"] != args.tab and not getattr(args, "force", False):
        print(f"файл от вкладки {m['tab']!r}, а --tab {args.tab!r} — откат отменён "
              f"(--force, чтобы пересилить осознанно)", file=sys.stderr)
        return 1
    if not m:
        print(f"предупреждение: имя {path.name!r} не в формате снимка "
              f"YYYY-MM-DD-<tab>.jsonl — сверка вкладки невозможна")
    rows = read_jsonl(path)
    if not rows:
        # восстановление «ничего» затёрло бы вкладку — это не откат, а потеря данных
        print(f"пустой бэкап-файл, откат отменён: {path}", file=sys.stderr)
        return 1
    if not getattr(args, "yes", False):
        print(f"restore ПЕРЕЗАПИШЕТ все строки вкладки '{args.tab}' "
              f"({len(rows)} строк из {path.name}).")
        print("Повторите с --yes для подтверждения.")
        return 1

    written = sheets.replace_rows(args.tab, rows)
    runlog.log_run(sheets, agent="restore", status="success",
                   input_summary=f"tab={args.tab} rows={written} file={path.name}",
                   started_at=run_started(args))
    print(f"restore: вкладка '{args.tab}' восстановлена из {path.name} ({written} строк)")
    return 0


# Вкладки ротации и колонка-дата каждой. raw-вкладки датируются моментом сбора
# (collected_at, пишет n8n-нормализатор); run_log — completed_at (тот же штамп, что
# показывает cf status и дашборд). run_log ИДЁТ ПОСЛЕДНЕЙ: собственную строку прогона
# archive логируем уже ПОСЛЕ перезаписи run_log, иначе replace_rows её бы затёр.
# utm_traffic — по дате визита (date): дневные строки копятся бесконечно, а месячные
# (row_kind=monthly, биллинговая цифра схемы оплаты) несут date="" ->
# _row_archive_date даёт None -> «недатированную не архивируем никогда» — они
# остаются в листе навсегда. CF Orders в ротацию НЕ входит вообще: реестр заказов —
# источник истины по деньгам (спека UTM-контура, тикет 06).
ARCHIVE_TABS = (
    ("raw_tiktok", "collected_at"),
    ("raw_instagram", "collected_at"),
    ("utm_traffic", "date"),
    ("run_log", "completed_at"),
)


def _parse_days(value):
    """'45d' или '45' -> 45 (int дней). Мусор -> ValueError (всплывёт как error оператору)."""
    s = str(value).strip().lower()
    if s.endswith("d"):
        s = s[:-1]
    return int(s)


def _row_archive_date(value):
    """Дата строки из ISO-штампа (collected_at/completed_at) -> date, иначе None.

    Берём первые 10 символов (YYYY-MM-DD): и '2026-05-01', и
    '2026-05-01T10:00:00.000Z', и '2026-05-01T10:00:00+00:00' дают одну дату.
    Пустое/нечитаемое -> None: такую строку НИКОГДА не архивируем (не удаляем то,
    что не смогли датировать)."""
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def cmd_archive(sheets, args):
    """Ротация raw-вкладок и run_log: строки строго старше N дней -> JSONL (формат
    бэкапа) + удаление из Sheets. Деструктивно -> требует --yes (иначе печатаем, что
    БЫЛО БЫ архивировано, и выходим с 1, как cmd_restore). Runs weekly ПОСЛЕ backup."""
    days = _parse_days(args.older_than)
    cutoff = today() - timedelta(days=days)   # тот же seam today(), что у backup — для тестов
    date_str = today().isoformat()
    tabs_cfg = sheets.config["tabs"]

    def partition(tab_key, date_col):
        # include_heavy для raw-вкладок: архив — исторический слепок, raw_json обязан
        # сохраниться; run_log тяжёлых колонок не имеет, читаем лёгким путём.
        rows = sheets.read_rows(tab_key, include_heavy=(tab_key != "run_log"))
        archive_rows, keep_rows = [], []
        for r in rows:
            d = _row_archive_date(r.get(date_col))
            if d is not None and d < cutoff:
                archive_rows.append(r)
            else:
                keep_rows.append(r)   # свежая ИЛИ недатированная -> остаётся
        return archive_rows, keep_rows

    # Партиции считаем ДО любых записей: dry-run и боевой прогон делят одну логику,
    # а run_log читается до того, как собственная строка archive появится в нём.
    plans = []  # (tab_key, date_col, archive_rows, keep_rows)
    for tab_key, date_col in ARCHIVE_TABS:
        if tab_key not in tabs_cfg:
            continue
        archive_rows, keep_rows = partition(tab_key, date_col)
        plans.append((tab_key, date_col, archive_rows, keep_rows))

    total = sum(len(a) for _, _, a, _ in plans)

    if not getattr(args, "yes", False):
        print(f"archive БЕЗ УДАЛЕНИЯ (старше {days}д, cutoff <{cutoff.isoformat()}):")
        for tab_key, _col, archive_rows, keep_rows in plans:
            print(f"  {tab_key}: архивировать {len(archive_rows)}, оставить {len(keep_rows)}")
        if total == 0:
            print("нечего архивировать.")
        print("Повторите с --yes для архивации и удаления строк из Sheets.")
        return 1

    if total == 0:
        # Никаких удалений и никакой записи в run_log: чистый no-op.
        print(f"нечего архивировать — все строки свежее порога ({days}д).")
        return 0

    # H8/M39: replace_rows затирает вкладку по снимку — берём те же stage-локи,
    # что конвейер (сбор/фан-аут не пишут в вкладки посреди архивации).
    from cf.pipeline_lock import release_stage_locks, try_stage_locks
    # stats тоже: collect performance пишет run_log — replace_rows затёр бы след.
    # Остаточное окно: ритуалы/mark-published логируют вне stage-локов — их
    # строки защищает только пере-чтение партиции непосредственно перед replace.
    locks, busy = try_stage_locks(("raw", "factory", "stats"))
    if busy:
        print(f"archive: пропущено — конвейер занят (звено {busy})")
        try:
            runlog.log_run(sheets, agent="archive", status="skipped",
                           input_summary=f"конвейер занят (звено {busy})",
                           started_at=run_started(args))
        except Exception:  # noqa: BLE001
            pass
        return 0
    counts, paths = {}, []
    try:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for tab_key, date_col, _planned_archive, _planned_keep in plans:
            # M39: между планированием и replace другие процессы могли дописать
            # строки — партицию пересчитываем по СВЕЖЕМУ чтению непосредственно
            # перед replace_rows (Sheets не транзакционен, окно сжимается до секунд).
            archive_rows, keep_rows = partition(tab_key, date_col)
            counts[tab_key] = (len(archive_rows), len(keep_rows))
            if not archive_rows:
                continue
            # Сначала пишем архив, ТОЛЬКО потом удаляем из Sheets: половина операции при
            # сбое -> строка есть и в архиве, и в листе (дубль), но никогда не потеряна.
            path = out_dir / f"{date_str}-{tab_key}.jsonl"
            existing = read_jsonl(path) if path.exists() else []   # тот же день -> дописываем, не затираем
            write_jsonl_atomic(path, existing + archive_rows)
            paths.append(str(path))
            sheets.replace_rows(tab_key, keep_rows)   # безопасное «удаление» пачкой (пере-запись хвоста)
    except Exception as exc:
        # Правило №6 (ревью 14.09.2026): часть вкладок уже удалена из листа — след
        # деструктивной операции обязан остаться, даже если дальше всё упало.
        done = ", ".join(f"{k}:arch={a},keep={b}" for k, (a, b) in counts.items()) or "—"
        try:
            runlog.log_run(sheets, agent="archive", status="failed",
                           input_summary=(f"{date_str} older_than={days}d прервано; "
                                          f"до сбоя: {done}"),
                           output_paths=paths, errors=[f"{type(exc).__name__}: {exc}"],
                           started_at=run_started(args))
        except Exception:  # noqa: BLE001 — не маскируем исходную ошибку сбоем лога
            pass
        raise
    finally:
        release_stage_locks(locks)

    # run_log уже перезаписан выше -> строка ниже приземлится ПОСЛЕ и переживёт прогон.
    note = ", ".join(f"{k}:arch={a},keep={b}" for k, (a, b) in counts.items())
    runlog.log_run(sheets, agent="archive", status="success",
                   input_summary=f"{date_str} older_than={days}d cutoff<{cutoff.isoformat()} {note}",
                   output_paths=paths,
                   started_at=run_started(args))
    print(f"archive -> {out_dir} ({date_str}, старше {days}д, cutoff <{cutoff.isoformat()})")
    for tab_key, (a, b) in counts.items():
        print(f"  {tab_key}: архивировано {a}, оставлено {b}")
    return 0


def cmd_hiring_check(_sheets, _args):
    """Validate access to the separate tracker and single application Form."""
    from cf.config import load_config
    from cf.hiring.google import FormsClient, HiringSheet
    from cf.hiring.spec import TRACKER_TABS

    config = load_config()
    store = HiringSheet.from_config(config)
    present = {ws.title for ws in store.book.worksheets()}
    missing = [title for title in TRACKER_TABS if title not in present]
    if missing:
        print(f"hiring: в трекере не хватает вкладок: {', '.join(missing)}")
        return 1

    resources = store.config_values()
    pending = [
        "APPLICATION_FORM_ID"
        for value in (resources.get("APPLICATION_FORM_ID", ""),)
        if value in ("", "PENDING_OWNER_SETUP")
    ]
    if pending:
        print("hiring: трекер доступен")
        print(
            "hiring: требуется применить cf.hiring.provision к одной "
            "human-owned Google Form"
        )
        print(f"hiring: ожидают заполнения: {', '.join(pending)}")
        return 2

    forms = FormsClient.from_config(config)
    key = "APPLICATION_FORM_ID"
    form = forms.get_form(resources[key])
    title = (form.get("info") or {}).get("title") or resources[key]
    print(f"hiring: {key} — доступна ({title})")
    print("hiring: готово к автоматической синхронизации")
    return 0


def cmd_hiring_sync(sheets, args):
    """Pull Form responses, apply rules/judges and create idempotent outbox rows."""
    from cf.config import load_config
    from cf.hiring.google import FormsClient, HiringSheet
    from cf.hiring.judge import judge_application
    from cf.hiring.rules import application_decision
    from cf.hiring.workflow import HiringWorkflow

    config = load_config()
    store = HiringSheet.from_config(config)
    workflow = HiringWorkflow(
        FormsClient.from_config(config),
        store,
        application_rule=application_decision,
        application_judge=None if args.no_agent else judge_application,
    )
    result = workflow.sync(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    total = sum(
        value
        for key, value in result["counts"].items()
        if key in ("application_created", "application_seen")
    )
    # Пустой poll не запускает смыслового агента и ничего не меняет, поэтому не
    # засоряет Run Log каждые 10 минут. Как только появился новый/изменённый
    # ответ либо ошибка судьи, прогон логируется по железному правилу №6.
    if total or result["warnings"]:
        status = "success" if total else "insufficient_data"
        hiring_config = config.get("hiring") or {}
        tracker_id = str(hiring_config.get("spreadsheet_id") or "")
        outputs = (
            [f"https://docs.google.com/spreadsheets/d/{tracker_id}/edit"]
            if tracker_id
            else []
        )
        runlog.log_run(
            sheets,
            agent="hiring-sync",
            status=status,
            input_summary=(
                f"dry_run={args.dry_run} agent={not args.no_agent} "
                f"responses={total}"
            ),
            output_paths=outputs,
            errors=result["warnings"],
            started_at=run_started(args),
        )
    return 0


def cmd_hiring_status(_sheets, _args):
    from cf.config import load_config
    from cf.hiring.google import HiringSheet
    from cf.hiring.workflow import status_summary

    summary = status_summary(HiringSheet.from_config(load_config()))
    resources = summary.pop("resources")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"tracker: {resources.get('TRACKER_URL', '')}")
    print(f"application: {resources.get('APPLICATION_FORM_URL', '')}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="cf", description="Content Factory CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("read", help="Прочитать вкладку Sheets, вывести/сохранить JSON")
    sp.add_argument("tab")
    sp.add_argument("--niche")
    sp.add_argument("--niche-empty", action="store_true",
                    help="только строки с пустой niche")
    sp.add_argument("--since")
    sp.add_argument("--out")
    sp.add_argument("--fields", help="оставить только эти колонки (через запятую)")
    sp.add_argument("--no-dedup", dest="dedup", action="store_false",
                    help="не схлопывать дубли source_url (по умолчанию остаётся свежая строка)")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("log-run", help="Записать запуск агента в CF Run Log")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--status", required=True, choices=sorted(runlog.VALID_STATUSES))
    sp.add_argument("--input")
    sp.add_argument("--outputs", nargs="*", default=[])
    sp.add_argument("--errors", nargs="*", default=[])
    sp.add_argument("--trigger", default="manual", choices=["manual", "scheduled", "event"])
    sp.add_argument("--started-at", dest="started_at", type=_started_at_arg,
                    metavar="ISO8601",
                    help="момент старта прогона в UTC, напр. "
                         "2026-07-26T09:15:00+00:00 (агент берёт его командой "
                         "date -u +%%Y-%%m-%%dT%%H:%%M:%%S+00:00 ПЕРЕД работой). Без "
                         "флага started_at = момент записи, длительность нулевая")
    sp.set_defaults(func=cmd_log_run)

    sp = sub.add_parser("status", help="Последние запуски агентов")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--proposals-dir", dest="proposals_dir", default="proposals")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("profile", help="Raw Batch Profiler: качество батча")
    sp.add_argument("tab")
    sp.add_argument("--niche")
    sp.add_argument("--since")
    # единый порог с очередью фан-аута (см. runner.PROFILE_MIN_ROWS) — не дать им разойтись
    sp.add_argument("--min-rows", dest="min_rows", type=int, default=PROFILE_MIN_ROWS)
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/profiles")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("analyze-batch",
                        help="Механика анализа v2: квартили/winners/losers для агента")
    sp.add_argument("tab")
    sp.add_argument("--niche", required=True)
    sp.add_argument("--since")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/analysis")
    sp.add_argument("--min-rows", dest="min_rows", type=int, default=12)
    sp.add_argument("--min-views", dest="min_views", type=int, default=1000)
    sp.set_defaults(func=cmd_analyze_batch)

    sp = sub.add_parser("cluster-other",
                        help="Кластеры «other» для предложений новых ниш")
    sp.add_argument("tab")
    sp.add_argument("--min-size", dest="min_size", type=int, default=30)
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/clusters")
    sp.set_defaults(func=cmd_cluster_other)

    sp = sub.add_parser("validate", help="Проверить артефакт по схеме")
    sp.add_argument("kind", choices=["patterns", "formula", "brief", "proposal",
                                     "source-proposal", "sources", "visual-facts"])
    sp.add_argument("file")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("vision", help="Визуальные факты: разбор скачанного "
                                       "медиа движком за конфигом "
                                       "(cf.config.json → vision)")
    sp.add_argument("--limit", type=int, default=None,
                    help="лимит партии (умолч.: vision.batch_limit)")
    sp.add_argument("--tab", choices=["raw_tiktok", "raw_instagram"],
                    help="одна вкладка (умолч.: обе)")
    sp.add_argument("--jsonl", help="режим пилота: строки dry-run батча; факты "
                                    "кладутся рядом (<имя>-visual.jsonl), "
                                    "Sheets не трогается")
    sp.add_argument("--niche", help="только строки этой ниши; лимит по "
                                    "умолчанию снимается — вся очередь ниши")
    sp.add_argument("--redo", action="store_true",
                    help="ре-разбор done/failed строк; требует адресации: "
                         "--raw-id (повторяемый) либо --niche")
    sp.add_argument("--raw-id", dest="raw_ids", action="append", default=[],
                    help="ограничить очередь конкретными строками "
                         "(повторяемый); адресация для --redo")
    sp.add_argument("--frames-cap", dest="frames_cap", type=int,
                    help="ВКЛЮЧИТЬ подачу кадров на этот прогон с этим капом "
                         "(путь пилота, поверх конфига; целое ≥ 2)")
    sp.add_argument("--media-root", dest="media_root",
                    default="agent-runtime/media")
    sp.set_defaults(func=cmd_vision)

    sp = sub.add_parser("collect", help="Сбор без n8n: tiktok|instagram|snowball|"
                                        "performance|metrika")
    sp.add_argument("stage", choices=["tiktok", "instagram", "snowball",
                                      "performance", "metrika"])
    sp.add_argument("--dry-run", action="store_true",
                    help="1 батч, реальный Apify, БЕЗ записи в Sheets "
                         "(строки в JSONL agent-runtime/collect/)")
    sp.add_argument("--batches", type=int, default=None,
                    help="сколько батчей гнать (0 = все; умолч.: 1 при "
                         "--dry-run, все в бою)")
    sp.add_argument("--no-explore", action="store_true",
                    help="без exploration-батча (приёмка: состав источников "
                         "1:1 с n8n)")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("approve-formula", help="Добавить формулу в approved inputs для n8n")
    sp.add_argument("path")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_approve_formula)

    sp = sub.add_parser("apply-brief-prompt",
                        help="Включить промпт темы по черновику (двойник кнопки "
                             "на воротах дашборда)")
    sp.add_argument("filename", help="имя файла черновика в proposals/")
    sp.add_argument("--show", action="store_true",
                    help="показать текст промпта и sha256, ничего не меняя")
    sp.add_argument("--sha256", default="",
                    help="отпечаток прочитанного текста; расхождение = отказ")
    sp.add_argument("--reject", action="store_true", help="отклонить черновик")
    sp.add_argument("--reason", default="", help="причина отклонения")
    sp.add_argument("--check", action="store_true",
                    help="показать чек-лист черновика и выйти")
    sp.add_argument("--auto", action="store_true",
                    help="включить только при полностью зелёном чек-листе "
                         "(авто-режим ворот)")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_apply_brief_prompt)

    sp = sub.add_parser("apply-prompt-candidate",
                        help="Правка действующего промпта темы — кандидатом в A/B "
                             "(активная версия не трогается)")
    sp.add_argument("filename", help="proposals/ГГГГ-ММ-ДД-brief-{тема}-reel-v{N}.md")
    sp.add_argument("--check", action="store_true",
                    help="показать чек-лист и выйти, ничего не меняя")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_apply_prompt_candidate)

    sp = sub.add_parser("gate-calibration",
                        help="Сверить машинный чек-лист рецептов с решениями "
                             "человека на его же данных")
    sp.add_argument("--root", default="")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_gate_calibration)

    sp = sub.add_parser("install-agent",
                        help="Установить нового агента по черновику proposals/ "
                             "(промпт + слэш-команда)")
    sp.add_argument("filename", help="имя файла черновика в proposals/")
    sp.add_argument("--root", default=".")
    sp.set_defaults(func=cmd_install_agent)

    sp = sub.add_parser("apply-agent-edit",
                        help="Применить правку ДЕЙСТВУЮЩЕГО промпта или команды "
                             "агента по черновику proposals/")
    sp.add_argument("filename", help="имя файла черновика в proposals/")
    sp.add_argument("--root", default=".")
    sp.set_defaults(func=cmd_apply_agent_edit)

    sp = sub.add_parser("codex-sync",
                        help="Перегенерировать Codex-обёртки слэш-команд "
                             "(.agents/skills/) из .claude/commands/")
    sp.add_argument("--root", default=".")
    sp.add_argument("--check", action="store_true",
                    help="только сверить, без записи; дрейф — код возврата 1")
    sp.set_defaults(func=cmd_codex_sync)

    sp = sub.add_parser("auto-approve-formula",
                        help="Одобрить рецепт машинным чек-листом (ворота "
                             "«Одобрение рецептов» в авто-режиме)")
    sp.add_argument("path", help="черновик formulas/<тема>/<имя>.json")
    sp.add_argument("--review", default="",
                    help="JSON с вердиктом судьи (/cf-review-formula)")
    sp.add_argument("--no-judge", action="store_true",
                    help="решать только по детерминированным проверкам")
    sp.add_argument("--check", action="store_true",
                    help="показать чек-лист и выйти, ничего не меняя")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_auto_approve_formula)

    sp = sub.add_parser("heartbeat",
                        help="Сторож: сказать владельцу, если завод не "
                             "отработал (зовётся своим таймером)")
    sp.add_argument("--max-hours", type=float, default=None,
                    help=f"через сколько часов без прогона бить тревогу "
                         f"(по умолчанию {CYCLE_STALE_HOURS})")
    sp.add_argument("--progress", default=CYCLE_PROGRESS_PATH)
    sp.set_defaults(func=cmd_heartbeat)

    sp = sub.add_parser("alert-unit",
                        help="Сказать владельцу, что служба упала "
                             "(зовётся из OnFailure= systemd)")
    sp.add_argument("unit", help="имя юнита, например cf-backup.service")
    sp.set_defaults(func=cmd_alert_unit)

    sp = sub.add_parser("notify-test",
                        help="Проверка связи: отправить себе тестовое "
                             "сообщение в Telegram")
    sp.set_defaults(func=cmd_notify_test)

    sp = sub.add_parser("formula-status", help="Сменить lifecycle-статус формулы (+ индекс approved)")
    sp.add_argument("path")
    sp.add_argument("status", choices=FORMULA_STATUSES)
    sp.add_argument("--reason", default="")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_formula_status)

    sp = sub.add_parser("auto-approve",
                        help="Авто-одобрение брифа: recommend + формула approved + "
                             "активный промпт + кап")
    sp.add_argument("brief_id")
    sp.add_argument("--review", required=True, help="JSON-файл ревью (verdict/reasons)")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.add_argument("--cap", type=int, default=None,
                    help="переопределить кап формулы за 7 дней (по умолчанию — "
                         "cf.config.json → dashboard.fanout.approve_cap)")
    sp.set_defaults(func=cmd_auto_approve)

    sp = sub.add_parser("recheck-briefs",
                        help="Пересверить уже одобренные, но не снятые сценарии "
                             "проверками ремесла и отправить дефектные в доработку")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.add_argument("--apply", action="store_true",
                    help="записать: перевести дефектные в доработку "
                         "(без флага — только разбор)")
    sp.add_argument("--limit", type=int, default=None,
                    help="сколько сценариев отправить за прогон (по умолчанию все)")
    sp.set_defaults(func=cmd_recheck_briefs)

    sp = sub.add_parser("retry-deferred",
                        help="Вернуться к сценариям, отложенным капом формулы, "
                             "когда окно 7 дней сдвинулось")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.add_argument("--cap", type=int, default=None,
                    help="переопределить кап формулы за 7 дней")
    sp.set_defaults(func=cmd_retry_deferred)

    sp = sub.add_parser("formula-guard",
                        help=f"Авто-пауза формул: 3 reject из 5 брифов ИЛИ медиана "
                             f"просмотров < {PERF_MEDIAN_RATIO}× общей за "
                             f"{PERF_WINDOW_DAYS} дн.")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_formula_guard)

    sp = sub.add_parser("formula-perf",
                        help="Накопить own_performance формул (reels/медиана/ER/дата) "
                             "в индекс + перенести eval-verdict в status_reason")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_formula_perf)

    sp = sub.add_parser("add-brief", help="Записать канонический бриф-JSON в CF Creative Briefs (pending)")
    sp.add_argument("file")
    sp.add_argument("--platform", default="tiktok")
    sp.add_argument("--caption", default="")
    sp.set_defaults(func=cmd_add_brief)

    sp = sub.add_parser("revise-brief",
                        help="Переписать сценарий по замечаниям ревьюера и вернуть "
                             "его на ревью (одна попытка на бриф); --refused — "
                             "пометить сценарий, который завод не переписал")
    # required=True снят с обоих аргументов (разбор 2026-07-27): режим --refused
    # правит только метку, файла у него нет. Сочетания проверяет cmd_revise_brief —
    # обычный вызов без --file по-прежнему отказ, но с внятным текстом.
    sp.add_argument("brief_id", nargs="?", default=None,
                    help="бриф, который правится (форма обычного вызова)")
    sp.add_argument("--brief-id", dest="brief_id_flag", metavar="BRIEF_ID",
                    default=None,
                    help="то же, что позиционный brief_id (форма для --refused)")
    sp.add_argument("--file", default=None, help="переписанный бриф-JSON")
    sp.add_argument("--refused", action="store_true",
                    help="завод НЕ переписал сценарий: пометить бриф и отдать "
                         "решение человеку (уходит из очереди доработки)")
    sp.add_argument("--notes", default="",
                    help="что именно исправлено (или почему не переписан)")
    sp.set_defaults(func=cmd_revise_brief)

    sp = sub.add_parser("mark-published",
                        help="Отметить бриф опубликованным: строка в CF Published Reels")
    sp.add_argument("brief_id")
    sp.add_argument("url", help="URL опубликованного рилса (http/https)")
    sp.add_argument("--notes", default="", help="production_notes: отступления при съёмке")
    sp.add_argument("--account", default="",
                    help="слаг аккаунта завода из реестра accounts (cf.config.json); "
                         "без него — публикация без привязки, как раньше")
    sp.set_defaults(func=cmd_mark_published)

    sp = sub.add_parser("demo-seed",
                        help="ДЕМО: влить фиктивные ролики и замеры по плану "
                             "(строки помечены DEMO-, стираются demo-wipe)")
    sp.add_argument("--plan", required=True,
                    help="JSON-план инъекции (agent-runtime/demo/plan.json)")
    sp.add_argument("--started-at", dest="started_at", type=_started_at_arg,
                    default=None, help="ISO8601-момент старта прогона для Run Log")
    sp.set_defaults(func=cmd_demo_seed)

    sp = sub.add_parser("demo-wipe",
                        help="ДЕМО: стереть строки с меткой DEMO- из CF Published "
                             "Reels и CF Performance")
    sp.add_argument("--started-at", dest="started_at", type=_started_at_arg,
                    default=None, help="ISO8601-момент старта прогона для Run Log")
    sp.set_defaults(func=cmd_demo_wipe)

    sp = sub.add_parser("set-review", help="Обновить review_status брифа")
    sp.add_argument("--brief-id", dest="brief_id", required=True)
    sp.add_argument("--status", required=True,
                    choices=["approved", "rejected", "revised", "pending"])
    sp.add_argument("--reason-code", dest="reason_code", default="",
                    help="код причины (enum): reference_mismatch, female_reference, "
                         "prohibition_violation, weak_hook, not_producible, other — "
                         "кладётся префиксом [code] в rejection_reason для группировки")
    sp.add_argument("--reason", default="",
                    help="свободный текст причины (человекочитаемый хвост после [code])")
    sp.add_argument("--notes", default="")
    sp.set_defaults(func=cmd_set_review)

    sp = sub.add_parser("rejection-history", help="Свод повторяющихся причин reject/revise")
    sp.add_argument("--out", default="agent-runtime/reviews/rejection_history.json")
    sp.set_defaults(func=cmd_rejection_history)

    sp = sub.add_parser("eval-prep", help="Собрать датасет для еженедельного eval")
    sp.add_argument("--since")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/evals")
    sp.set_defaults(func=cmd_eval_prep)

    sp = sub.add_parser("log-prompt-version", help="Зафиксировать новую версию промпта")
    sp.add_argument("--prompt-id", dest="prompt_id", required=True)
    sp.add_argument("--version", required=True)
    sp.add_argument("--path", required=True)
    sp.add_argument("--changelog", required=True)
    sp.add_argument("--candidate", action="store_true",
                    help="Записать как candidate для A/B (не деактивирует active) — P5.13")
    sp.set_defaults(func=cmd_log_prompt_version)

    sp = sub.add_parser("ab-plan",
                        help="План версий для брифов формулы (interleaving active/candidate) — P5.13")
    sp.add_argument("--prompt-id", dest="prompt_id", required=True)
    sp.add_argument("--count", type=int, default=2,
                    help="Число брифов формулы за прогон (dashboard.fanout.briefs_per_formula)")
    sp.set_defaults(func=cmd_brief_version)

    sp = sub.add_parser("check-commit", help="Проверить staged-файлы на секреты/runtime")
    sp.set_defaults(func=cmd_check_commit)

    sp = sub.add_parser("install-hooks", help="Установить pre-commit guard")
    sp.set_defaults(func=cmd_install_hooks)

    sp = sub.add_parser("dashboard", help="Локальный веб-дашборд CF")
    # default=None: без явного флага порт берётся из cf.config.json (dashboard.port),
    # иначе дефолт 8787 — см. resolve_dashboard_port
    sp.add_argument("--port", type=int, default=None)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("trace", help="Цепочка pattern->formula->brief->reel->performance")
    sp.add_argument("brief_id")
    sp.set_defaults(func=cmd_trace)

    sp = sub.add_parser("apply-sources", help="Применить approved source-proposal к n8n-спискам")
    sp.add_argument("proposal", help="JSON-файл proposal (status=approved)")
    sp.set_defaults(func=cmd_apply_sources)

    sp = sub.add_parser("export-seeds", help="Выгрузить winner-URL из approved формул в CF Seeds")
    sp.add_argument("--index", default="formulas/_approved/index.json")
    sp.set_defaults(func=cmd_export_seeds)

    sp = sub.add_parser("source-stats", help="Скоринг источников по source_query")
    sp.add_argument("tab")
    sp.add_argument("--since")
    sp.add_argument("--target", help="целевые ниши через запятую (default: config target_niches)")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/source-stats")
    sp.set_defaults(func=cmd_source_stats)

    sp = sub.add_parser("backfill-attribution",
                        help="Заполнить source_query из raw_json (только заглушки)")
    sp.add_argument("tab")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true")
    sp.set_defaults(func=cmd_backfill_attribution)

    sp = sub.add_parser("backup",
                        help="Датированные JSONL-снимки всех вкладок (ретенция 30 дней)")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/backups")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore",
                        help="Точечный откат вкладки из JSONL-снимка (ПЕРЕЗАПИСЬ)")
    sp.add_argument("--tab", required=True, help="ключ вкладки (как в config tabs)")
    sp.add_argument("--file", required=True, help="JSONL-снимок из cf backup")
    sp.add_argument("--yes", action="store_true", help="подтвердить перезапись вкладки")
    sp.add_argument("--force", action="store_true",
                    help="разрешить несовпадение вкладки в имени файла и --tab (M14)")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("archive",
                        help="Ротация старых raw/run_log строк в JSONL + удаление из Sheets")
    sp.add_argument("--older-than", dest="older_than", default="45d",
                    help="порог возраста: '45d' или '45' (дни); строки строго старше -> в архив")
    sp.add_argument("--out-dir", dest="out_dir", default="agent-runtime/archive")
    sp.add_argument("--yes", action="store_true",
                    help="подтвердить архивацию и УДАЛЕНИЕ строк из Sheets")
    sp.set_defaults(func=cmd_archive)

    sp = sub.add_parser("apply-niches", help="Записать классифицированные ниши в raw-вкладку")
    sp.add_argument("mapping", help="JSON-файл {raw_id: niche}")
    sp.add_argument("--tab", default="raw_tiktok")
    sp.add_argument("--key", default="raw_id")
    sp.set_defaults(func=cmd_apply_niches)

    sp = sub.add_parser(
        "hiring-check",
        help="Проверить доступ к трекеру и Google Forms найма",
    )
    sp.set_defaults(func=cmd_hiring_check)

    sp = sub.add_parser(
        "hiring-sync",
        help="Синхронизировать единую анкету, скоринг и очередь писем",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="прочитать и посчитать, но не менять hiring-таблицу",
    )
    sp.add_argument(
        "--no-agent",
        action="store_true",
        help="только объективные проверки; смысловые ответы оставить на review",
    )
    sp.set_defaults(func=cmd_hiring_sync)

    sp = sub.add_parser("hiring-status", help="Статусы кандидатов, оценок и outbox")
    sp.set_defaults(func=cmd_hiring_status)

    return p


def main(argv=None):
    force_utf8_stdio()
    # Момент старта процесса — источник started_at для команд, которые пишут в
    # Run Log сами (profile, analyze-batch, collect, backup, restore, archive,
    # auto-approve, formula-guard/perf, apply-niches). Берём ДО parse_args:
    # «прогон начался» — это запуск процесса, а не момент записи строки.
    started = now_iso()
    args = build_parser().parse_args(argv)
    args.run_started_at = started
    try:
        return args.func(Sheets(), args)
    except Exception as exc:  # оператору нужна причина, а не трейсбек
        print(f"error: {exc}", file=sys.stderr)
        return 1
