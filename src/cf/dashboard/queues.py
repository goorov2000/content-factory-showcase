"""Очереди конвейера и ворота оператора — единственный источник истины.

Чистые функции без состояния, без часов и без побочных эффектов (тот же контракт,
что у ``progress.py``): всё считается от файлов репозитория и строк Sheets,
переданных аргументом. Поэтому один и тот же предикат обслуживает и дашборд
(«что показать оператору»), и раннер («запускать ли платный вызов»).

Зачем модуль появился. До 2026-07-26 готовность темы к производству сценариев
считалась в ТРЁХ местах по-разному:

* дашборд смотрел только файл ``prompts/briefs/{тема}/reel.md``
  (``sections.niche_has_prompt``);
* генератор сценариев — только активную строку ``prompt_versions``
  (``prompts/agents/brief-generator.md``: «нет активного промпта ниши»);
* метрика воронки — пересечение файла и наличия утверждённого рецепта
  (``data.overview_metrics``).

Из-за этого состояние «файл сохранён, версия не включена» было невидимо нигде:
карточка в лаборатории гасла, а сценарии по теме по-прежнему не производились.
``prompt_state`` сводит признак к одному значению из четырёх и делает это
состояние явным (``PROMPT_FILE_ONLY``).

Тот же скип нашёлся этажом ниже — на уровне РЕЦЕПТА: промпт темы включён, а
рецепты, которых он не называет, генератор молча пропускает. Ответ на это —
``prompt_coverage``/``formula_prompt_state`` (пятое значение
``PROMPT_UNCOVERED``): они стоят НА ``prompt_state``, а не рядом с ним, и ничего
не блокируют — только называют счёт «промпт называет N рецептов из M».

Ворота — не статус в базе, а предикат от состояния. Ничего не персистится:
решение оператора меняет файлы рецептов и строки ``prompt_versions``, а ворота
пересчитываются от них. Поэтому «продолжить после закрытия ворот» не требует
сохранённой позиции прогона.
"""
import json
import re
from pathlib import Path

# Токены статуса версии промпта. Импортируются из ядра A/B, которое само не
# зависит от cf.* — это и есть «не плодить дубли токенов» (см. cf/abtest.py).
# До появления queues.py копий было три: cli._prompt_is_active,
# sections._is_active и evalprep._is_active.
from cf.abtest import ACTIVE_TOKENS

# ── Состояние промпта темы ───────────────────────────────────────────────────
# Порядок значений — это порядок продвижения темы к производству.
PROMPT_NONE = "none"            # ни файла, ни черновика: цикл ещё не писал
PROMPT_DRAFT = "draft"          # черновик лежит в proposals/, ждёт вашего чтения
PROMPT_FILE_ONLY = "file_only"  # файл есть, активной версии нет — сценарии НЕ идут
PROMPT_READY = "ready"          # файл + активная версия: тема производит
# Пятое значение — только для КОНКРЕТНОГО рецепта (formula_prompt_state): промпт
# темы включён, но этого рецепта не называет. Ворота им не держатся (см. блок
# «Покрытие рецептов» ниже), поэтому в PROMPT_BLOCKING его нет.
PROMPT_UNCOVERED = "uncovered"

# Подписи состояний языком оператора (единственное место, где они формулируются).
PROMPT_STATE_LABELS = {
    PROMPT_NONE: "черновик ещё не написан — его напишет ближайший прогон",
    PROMPT_DRAFT: "черновик готов — прочитайте и включите",
    PROMPT_FILE_ONLY: "файл промпта есть, но версия не включена — сценарии не идут",
    PROMPT_READY: "промпт включён",
    PROMPT_UNCOVERED: "промпт темы включён, но этого рецепта не называет — "
                      "сценариев по нему может не быть",
}

# Состояния, при которых тема НЕ производит сценарии (то есть держит ворота).
PROMPT_BLOCKING = (PROMPT_NONE, PROMPT_DRAFT, PROMPT_FILE_ONLY)

# P2.9-зеркало: имя темы идёт в путь prompts/briefs/{тема}/reel.md, поэтому
# допускаем только безопасные символы — буквы (вкл. кириллицу), цифры, дефис,
# подчёркивание. Слэши/точки/пробелы отсекаются, path-инъекция не пройдёт.
_NICHE_NAME_RE = re.compile(r"[\w-]+", re.UNICODE)

# Черновик промпта темы: proposals/YYYY-MM-DD-brief-{тема}-reel.md.
_DRAFT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-brief-(?P<niche>.+)-reel\.md$",
                            re.UNICODE)
# Черновик ПРАВКИ действующего промпта: …-reel-v{N}.md. Отдельное имя — отдельный
# путь применения: первое включение темы заменяет пустоту, а правка работающего
# промпта заменяла бы текст, по которому уже произведены сценарии. Поэтому правка
# идёт КАНДИДАТОМ в A/B (cf.abtest): обе версии работают параллельно, выбирает
# результат, а не чтение (правило №3 в редакции 2026-07-26).
_CANDIDATE_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-brief-(?P<niche>.+)-reel-v(?P<version>\d+)\.md$", re.UNICODE)


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def is_valid_niche_name(niche):
    """Имя темы безопасно для пути (буквы вкл. кириллицу, цифры, дефис, _)."""
    return bool(_NICHE_NAME_RE.fullmatch(str(niche or "")))


def brief_prompt_id(niche):
    """Идентификатор строки версии промпта темы в CF Prompt Versions."""
    return f"brief-{niche}-reel"


def brief_prompt_path(root, niche):
    """Путь промпта темы: prompts/briefs/{тема}/reel.md."""
    return Path(root) / "prompts" / "briefs" / str(niche) / "reel.md"


def niche_has_prompt(root, niche):
    """Есть ли ФАЙЛ промпта темы. Это лишь половина готовности — см. prompt_state.

    Дешёвая проверка существования (один stat на тему; вызывающий дедупит).
    Небезопасное имя (path-инъекция) → False без выхода за пределы дерева."""
    niche = str(niche or "")
    if not is_valid_niche_name(niche):
        return False
    return brief_prompt_path(root, niche).is_file()


def repo_config(root):
    """cf.config.json репозитория ({} , если файла нет или он битый)."""
    return _load_json(Path(root) / "cf.config.json") or {}


def fanout_exclude_niches(root, config=None):
    """Нецелевые темы из cf.config.json → dashboard.fanout.exclude_niches.

    Тот же файл, что читает StageRunner (единый источник политики): бренд по этим
    темам контент не выпускает, поэтому они не попадают ни в очереди, ни в ворота."""
    cfg = repo_config(root) if config is None else config
    raw = ((cfg.get("dashboard") or {}).get("fanout") or {}).get("exclude_niches") or []
    return {str(n).strip() for n in raw if str(n).strip()}


# ── Кто закрывает ворота ─────────────────────────────────────────────────────
# Подпись актора — ПРОИЗВОДНАЯ от политики ворот, а не константа шаблона. До
# 2026-07-28 здесь в трёх местах стояло жёсткое "вы", хотя ворота рецептов и
# правил сценариев закрывает машина с вечера 26.07 (CLAUDE.md, «Ворота
# конвейера»): лента подписывала человеком то, чего он не делает, и обещала
# работу там, где её нет. Вернули ворота в ручной режим одной строкой конфига —
# подпись меняется вместе с ними, без правки кода.
AUTO_ACTOR = "CLAUDE CODE"
HUMAN_ACTOR = "вы"


def gate_actor(root, gate_key, config=None):
    """Кто закрывает эти ворота при действующей политике: CLAUDE CODE или «вы».

    Политику читает `autogate.gate_policy` — единственный предикат режима ворот
    в проекте; своего разбора `gates.policy` здесь не заводится. Импорт локальный
    по той же причине, что у `stale_prompt_snapshots` ниже: autogate стоит НА
    queues, и обратная связь на уровне модуля дала бы цикл."""
    from cf.dashboard.autogate import GATE_AUTO, gate_policy
    if config is None:
        config = repo_config(root)
    return AUTO_ACTOR if gate_policy(config, gate_key) == GATE_AUTO else HUMAN_ACTOR


def version_is_active(row):
    """Активна ли строка prompt_versions (TRUE/1/YES в колонке active/status)."""
    return str(row.get("active", "")).strip().upper() in ACTIVE_TOKENS


def has_active_version(versions, niche):
    """Есть ли активная строка brief-{тема}-reel — то, что проверяет генератор.

    versions — строки CF Prompt Versions (cache.rows / sheets.read). None означает
    «лист не прочитан»: тогда судить нельзя и мы возвращаем None, а не False
    (правило №2 — недостаток данных не выдаётся за отрицательный ответ)."""
    if versions is None:
        return None
    prompt_id = brief_prompt_id(niche)
    return any(str(v.get("prompt_id") or "").strip() == prompt_id
               and version_is_active(v) for v in versions)


def prompt_drafts(root):
    """Черновики промптов тем, ждущие решения: {тема: путь}.

    Черновик — файл proposals/YYYY-MM-DD-brief-{тема}-reel.md со `status: proposed`
    во frontmatter. Применённый (`status: approved`) и отклонённый в очередь не
    попадают: иначе агент писал бы новый черновик по той же теме каждый прогон."""
    out = {}
    base = Path(root) / "proposals"
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for path in entries:
        match = _DRAFT_NAME_RE.match(path.name)
        if not match:
            continue
        niche = match.group("niche")
        if not is_valid_niche_name(niche):
            continue
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        if re.search(r"^status:\s*proposed\s*$", head, re.MULTILINE):
            # Свежий черновик перекрывает старый по той же теме (сортировка по имени
            # = по дате в префиксе), чтобы кнопка вела на актуальный текст.
            out[niche] = path
    return out


def prompt_candidate_drafts(root):
    """Черновики ПРАВОК действующих промптов тем: {тема: (версия, путь)}.

    Имя — proposals/YYYY-MM-DD-brief-{тема}-reel-v{N}.md, статус `proposed`. Тот же
    обход, что prompt_drafts, но очередь другая: этот черновик не включает тему, а
    добавляет кандидата в A/B к уже работающему промпту. Свежий по имени (дата в
    префиксе) перекрывает старый по той же теме."""
    out = {}
    base = Path(root) / "proposals"
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for path in entries:
        match = _CANDIDATE_NAME_RE.match(path.name)
        if not match:
            continue
        niche = match.group("niche")
        if not is_valid_niche_name(niche):
            continue
        try:
            head = path.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        if re.search(r"^status:\s*proposed\s*$", head, re.MULTILINE):
            out[niche] = (int(match.group("version")), path)
    return out


def prompt_state(root, niche, versions, drafts=None):
    """Готовность темы к производству сценариев — одно значение из четырёх.

    Единый предикат вместо трёх разошедшихся признаков (см. docstring модуля).
    ``versions is None`` (лист не прочитан) трактуется как «версия не активна»:
    для ворот это безопасная сторона — покажем тему как ждущую, а не выдадим
    молчаливое «всё хорошо» по непрочитанным данным."""
    if drafts is None:
        drafts = prompt_drafts(root)
    if niche_has_prompt(root, niche):
        return PROMPT_READY if has_active_version(versions, niche) else PROMPT_FILE_ONLY
    return PROMPT_DRAFT if niche in drafts else PROMPT_NONE


# ── Рецепты по темам ─────────────────────────────────────────────────────────

def approved_formulas_by_niche(root):
    """{тема: [имена утверждённых рецептов]} из formulas/_approved/index.json.

    Имена нужны покрытию (см. ниже): счётчика «4 рецепта» мало, чтобы сказать,
    КАКОЙ из них промпт темы не называет. Пустое имя (битая строка индекса) в
    список попадает — иначе счёт рецептов разошёлся бы с самим индексом."""
    data = _load_json(Path(root) / "formulas" / "_approved" / "index.json") or {}
    out = {}
    for entry in data.get("approved") or []:
        niche = str(entry.get("niche") or "").strip()
        if niche:
            out.setdefault(niche, []).append(str(entry.get("name") or "").strip())
    return out


def approved_by_niche(root):
    """{тема: число утверждённых рецептов} из formulas/_approved/index.json."""
    return {niche: len(names)
            for niche, names in approved_formulas_by_niche(root).items()}


def proposed_by_niche(root):
    """{тема: число черновиков рецептов} — formulas/*/*.json со status=proposed.

    Каталог _approved пропускается: там иммутабельные снапшоты утверждённых."""
    out = {}
    base = Path(root) / "formulas"
    if not base.is_dir():
        return out
    for niche_dir in sorted(base.iterdir()):
        if not niche_dir.is_dir() or niche_dir.name == "_approved":
            continue
        for path in sorted(niche_dir.glob("*.json")):
            data = _load_json(path)
            if data and data.get("status") == "proposed":
                niche = str(data.get("niche") or niche_dir.name).strip()
                if niche:
                    out[niche] = out.get(niche, 0) + 1
    return out


# ── Покрытие рецептов промптом темы ──────────────────────────────────────────
# Второй экземпляр того же молчаливого скипа, ради которого делались ворота, —
# только опустившийся с уровня темы на уровень РЕЦЕПТА. Тема «мужские-образы»
# 2026-07-26: промпт включён, ворота зелёные, niches_ready_for_briefs её отдаёт,
# а сценариев она даёт ноль. Слова самого генератора (отчёт этапа 16:13:54,
# agent-runtime/reports/stage-reports.json): «grwm-interactive-frame,
# reference-recreation-fit, style-manifesto-statement — активный промпт
# brief-мужские-образы-reel v2 написан только под short-styling-idea-reel».
# Единственный след — проза input_summary, на ленте не видно ничего.
#
# ВНИМАНИЕ: признак ниже — ЭВРИСТИКА, а не строгий критерий. Машинного критерия
# покрытия в системе нет: prompts/agents/brief-generator.md знает только «есть ли
# активный промпт темы» (правило 3), а решение «этот рецепт промптом не
# обслуживается» принимает сам агент, читая текст промпта. Поэтому берём самый
# консервативный машинный признак — имя рецепта упомянуто в тексте промпта (так и
# было в разобранном случае: заголовок называл ровно один рецепт из четырёх, а
# черновик v3, который их закрывает, называет все четыре). Чем он ошибается:
#   * ложное «покрыт» — имя названо в запрете («не по формуле X») или как часть
#     имени другого рецепта (`voiced-expert-review` внутри `…-review-long`);
#   * ложное «не покрыт» — промпт написан на тему целиком и имён не называет;
#     тот же промпт мужских-образов 26.07 в 04:49 сценарии всем четырём рецептам
#     всё-таки дал, а в 16:13 трём отказал — поведение агента не детерминировано.
# Поэтому покрытие НИЧЕГО не блокирует (решение разбора: ворота не судят о
# качестве, docs/plans/2026-07-26-cycle-gates.md — «Открытый вопрос») и подаётся
# счётом «промпт называет N рецептов из M», а не приговором.
COVERAGE_FULL = "full"        # промпт называет все утверждённые рецепты темы
COVERAGE_PARTIAL = "partial"  # часть рецептов в тексте промпта не названа


def prompt_covers_formula(text, formula):
    """Называет ли ТЕКСТ промпта темы этот рецепт (эвристика — см. блок выше)."""
    formula = str(formula or "").strip().lower()
    return bool(formula) and formula in str(text or "").lower()


def coverage_label(covered, total):
    """Подпись покрытия языком оператора — в стиле PROMPT_STATE_LABELS.

    Формулировка осторожная («может не идти»), потому что признак эвристический:
    обещать «сценариев не будет» по упоминанию имени в тексте нельзя."""
    if covered >= total:
        return f"промпт называет все {total} {_plural_recipe(total)}"
    return (f"промпт включён, но называет {covered} {_plural_recipe(covered)} "
            f"из {total} — по остальным производство может не идти")


def prompt_coverage(root, niche, formulas=None):
    """Покрытие утверждённых рецептов темы текстом её промпта.

    Возвращает ``{niche, approved, covered, uncovered, state, state_label}`` или
    ``None``, если судить не по чему: у темы нет названных утверждённых рецептов,
    файла промпта нет или он не прочитался. None — это «не знаю», а не «ничего не
    покрыто» (правило №2), и вызывающий обязан различать их."""
    if formulas is None:
        formulas = approved_formulas_by_niche(root).get(niche, [])
    names = [n for n in formulas if n]
    if not names or not niche_has_prompt(root, niche):
        return None
    try:
        text = brief_prompt_path(root, niche).read_text(encoding="utf-8")
    except OSError:
        return None
    covered = [n for n in names if prompt_covers_formula(text, n)]
    uncovered = [n for n in names if n not in covered]
    state = COVERAGE_FULL if not uncovered else COVERAGE_PARTIAL
    return {"niche": niche, "approved": len(names),
            "covered": covered, "uncovered": uncovered, "state": state,
            "state_label": coverage_label(len(covered), len(names))}


def formula_prompt_state(root, niche, formula, versions, drafts=None):
    """Готовность к производству КОНКРЕТНОГО рецепта — тот же предикат, что у темы.

    Ровно prompt_state темы, плюс пятое значение ``PROMPT_UNCOVERED``: промпт
    включён, но этого рецепта не называет. Отдельного признака «готова ли тема»
    здесь не заводится (CLAUDE.md: три копии этого признака и породили дефект) —
    функция стоит НА prompt_state и лишь уточняет его для одного рецепта.
    Текст промпта не прочитан → возвращаем PROMPT_READY: непрочитанные данные не
    повод объявить рецепт непокрытым (правило №2)."""
    state = prompt_state(root, niche, versions, drafts)
    if state != PROMPT_READY:
        return state
    coverage = prompt_coverage(root, niche, [formula])
    if coverage is None:
        return state
    return PROMPT_READY if coverage["covered"] else PROMPT_UNCOVERED


def niches_with_uncovered_recipes(root, versions, exclude=None, drafts=None):
    """Темы с ВКЛЮЧЁННЫМ промптом, который называет не все утверждённые рецепты.

    Список словарей prompt_coverage. Тема, стоящая на воротах промпта, сюда не
    попадает: она и так видна строкой ворот, а два сообщения об одной теме
    заставляют оператора решать, какое из них главное."""
    exclude = exclude if exclude is not None else fanout_exclude_niches(root)
    drafts = prompt_drafts(root) if drafts is None else drafts
    by_niche = approved_formulas_by_niche(root)
    out = []
    for niche in sorted(by_niche):
        if niche in exclude:
            continue
        if prompt_state(root, niche, versions, drafts) != PROMPT_READY:
            continue
        coverage = prompt_coverage(root, niche, by_niche[niche])
        if coverage and coverage["uncovered"]:
            out.append(coverage)
    return out


# ── Очереди воркеров (их читает раннер, чтобы не жечь платные вызовы) ────────

def niches_awaiting_prompt_draft(root, versions, exclude=None):
    """Темы, которым пора писать ЧЕРНОВИК промпта: очередь одноимённого воркера.

    Условия ровно те же, что у гейтов самого агента (brief-prompt-writer):
    тема вне exclude_niches, есть хотя бы один утверждённый рецепт, промпта ещё
    нет и черновик по ней не лежит. Тема с одними черновиками рецептов сюда НЕ
    попадает: промпт выводится из утверждённого рецепта, писать не по чему."""
    exclude = exclude if exclude is not None else fanout_exclude_niches(root)
    drafts = prompt_drafts(root)
    return [n for n in sorted(approved_by_niche(root))
            if n not in exclude
            and prompt_state(root, n, versions, drafts) == PROMPT_NONE]


def niches_ready_for_briefs(root, versions, exclude=None):
    """Темы, по которым сценарии производить МОЖНО: промпт включён + есть рецепт.

    Пустой список — законный повод не запускать генератор вовсе: платный вызов,
    который заведомо вернёт «нет работы», не делается (до 2026-07-26 он делался
    каждый прогон и уходил в skipped)."""
    exclude = exclude if exclude is not None else fanout_exclude_niches(root)
    drafts = prompt_drafts(root)
    return [n for n in sorted(approved_by_niche(root))
            if n not in exclude
            and prompt_state(root, n, versions, drafts) == PROMPT_READY]


# ── Ворота ───────────────────────────────────────────────────────────────────

def recipe_gate(root, versions=None, exclude=None, config=None):
    """Ворота «Одобрение рецептов». Держат ПО ТЕМЕ, а не глобально.

    Решение оператора 2026-07-26: блокирует только тема, у которой БЕЗ решения
    нет ни одного утверждённого рецепта — ей нечем начать производство. Черновик
    по уже работающей теме показывается пометкой «не блокирует» и производство не
    гасит: прогоны приносят черновики почти каждый день, и грубый вариант
    останавливал бы завод целиком, включая единственную живую тему."""
    exclude = exclude if exclude is not None else fanout_exclude_niches(root, config)
    approved = approved_by_niche(root)
    proposed = proposed_by_niche(root)
    blocking, informational = [], []
    for niche in sorted(proposed):
        if niche in exclude:
            continue
        row = {"niche": niche, "proposed": proposed[niche],
               "approved": approved.get(niche, 0)}
        (blocking if not row["approved"] else informational).append(row)
    return {
        "key": "recipes",
        "label": "Одобрение рецептов",
        "actor": gate_actor(root, "recipes", config),
        "href": "/lab",
        "blocking": bool(blocking),
        "count": len(blocking),
        "rows": blocking,
        "informational": informational,
    }


def prompt_gate(root, versions, exclude=None, config=None):
    """Ворота «Включение правил сценариев темы». Держат производство по теме.

    Тема без включённого промпта не попадает в очередь сценариев. Если сюда
    упёрлись ВСЕ темы — шаг «Сценарии» не запускается вовсе (см.
    niches_ready_for_briefs).

    Ключ ``coverage`` — темы, ПРОШЕДШИЕ эти ворота, но с промптом, который
    называет не все их рецепты. Ворота они не держат (blocking/count/rows их не
    видят, производство не гаснет): это предупреждение о втором экземпляре той же
    мёртвой зоны, только внутри работающей темы."""
    exclude = exclude if exclude is not None else fanout_exclude_niches(root, config)
    approved = approved_by_niche(root)
    drafts = prompt_drafts(root)
    rows = []
    for niche in sorted(approved):
        if niche in exclude:
            continue
        state = prompt_state(root, niche, versions, drafts)
        if state == PROMPT_READY:
            continue
        draft = drafts.get(niche)
        rows.append({
            "niche": niche,
            "approved": approved[niche],
            "state": state,
            "state_label": PROMPT_STATE_LABELS[state],
            "draft_path": draft.as_posix() if draft else "",
            "draft_name": draft.name if draft else "",
        })
    # Протухшие ссылки на снапшоты у УЖЕ включённых промптов: ворота проверяют
    # свежесть в момент применения, а индекс живёт дальше (27.07: машина одобрила
    # v2 рецепта через час после применения промпта, и текст стал указывать на v1).
    # Импорт локальный: autogate стоит НА queues, обратная связь на уровне модуля
    # дала бы цикл.
    from cf.dashboard.autogate import stale_prompt_snapshots
    try:
        stale = [s for s in stale_prompt_snapshots(root, versions=versions)
                 if s["niche"] not in exclude]
    except Exception:      # noqa: BLE001 — предупреждение не важнее самих ворот
        stale = []
    return {
        "key": "prompt",
        # «Промпт темы» непонятен непогружённому (решение владельца 2026-07-28,
        # элемент 17б): это правила, по которым пишутся сценарии темы.
        "label": "Включение правил сценариев темы",
        "actor": gate_actor(root, "prompt", config),
        "href": "/lab",
        "blocking": bool(rows),
        "count": len(rows),
        "rows": rows,
        "informational": [],
        "coverage": niches_with_uncovered_recipes(root, versions, exclude, drafts),
        "stale_snapshots": stale,
    }


def brief_gate(metrics):
    """Ворота «Одобрение сценариев». Машину НЕ блокируют — продукт уже произведён.

    Держат съёмку: сценарий в статусе «ожидает»/«доработка» в съёмочный лист не
    идёт. Поэтому и подаются спокойным счётчиком, без красного: если красным горят
    все ворота сразу, красное становится фоном и новая важная строка теряется."""
    metrics = metrics or {}
    count = metrics.get("briefs_attention")
    return {
        "key": "briefs",
        "label": "Одобрение сценариев",
        # Единственные ворота, актор которых НЕ следует политике: сценарии
        # остаются за человеком по решению владельца — дальше идёт съёмка, то
        # есть его время и деньги (CLAUDE.md, «Кто их закрывает»).
        "actor": HUMAN_ACTOR,
        "href": "/briefs",
        "blocking": False,
        "count": count if isinstance(count, int) else 0,
        "rows": [],
        "informational": [],
    }


def build_gates(root, versions, metrics=None, exclude=None, config=None):
    """Все ворота в порядке конвейера: {ключ: описание}.

    cf.config.json читается ОДИН раз на построение: из него выводятся и
    нецелевые темы, и подпись актора каждых ворот."""
    config = repo_config(root) if config is None else config
    exclude = exclude if exclude is not None else fanout_exclude_niches(root, config)
    return {
        "recipes": recipe_gate(root, versions, exclude, config),
        "prompt": prompt_gate(root, versions, exclude, config),
        "briefs": brief_gate(metrics),
    }


def blocking_gates(gates):
    """Ворота, которые реально держат конвейер — в порядке ленты."""
    order = ("recipes", "prompt", "briefs")
    return [gates[k] for k in order if k in gates and gates[k]["blocking"]]


def gate_banner(gates):
    """Текст красной полосы над «Конвейером» — или "" , если конвейер едет.

    Одна строка, читаемая ДО прокрутки: имя держащих ворот и что именно встало.
    Ответ на «что от меня требуется» не должен требовать чтения двенадцати строк
    ленты."""
    holding = blocking_gates(gates)
    if not holding:
        return ""
    gate = holding[0]
    if gate["key"] == "recipes":
        names = ", ".join(r["niche"] for r in gate["rows"][:4])
        tail = " и др." if len(gate["rows"]) > 4 else ""
        return (f"Конвейер стоит на воротах «{gate['label']}»: "
                f"{gate['count']} {_plural_theme(gate['count'])} "
                f"без единого утверждённого рецепта — {names}{tail}")
    names = ", ".join(r["niche"] for r in gate["rows"][:4])
    tail = " и др." if len(gate["rows"]) > 4 else ""
    return (f"Конвейер стоит на воротах «{gate['label']}»: "
            f"{gate['count']} {_plural_theme(gate['count'])} "
            f"дают рецепты, но не дают сценариев — {names}{tail}")


def _plural(n, one, few, many):
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _plural_theme(n):
    return _plural(n, "тема", "темы", "тем")


def _plural_recipe(n):
    return _plural(n, "рецепт", "рецепта", "рецептов")


def gate_summary_rows(gates):
    """Сводка состояния завода строками с адресом действия.

    Каждая строка — ПРОИЗВОДНАЯ от состояния, а не запись журнала: она живёт
    ровно пока живо условие и исчезает сама, когда условие расшилось. Отметки
    «прочитано» нет и хранилища нет — рестарт дашборда ничего не теряет
    (решение владельца 2026-07-28, С2)."""
    gates = gates or {}
    out = []
    recipes = gates.get("recipes") or {}
    if recipes.get("count"):
        out.append({"key": "recipes", "href": recipes.get("href") or "/lab",
                    "text": f"{recipes['count']} {_plural_theme(recipes['count'])} "
                            f"без утверждённого рецепта"})
    prompt = gates.get("prompt") or {}
    if prompt.get("count"):
        out.append({"key": "prompt", "href": prompt.get("href") or "/lab",
                    "text": f"{prompt['count']} {_plural_theme(prompt['count'])} "
                            f"без включённых правил сценариев"})
    # Непокрытые рецепты — не ворота, но и не молчание: без этой строки тема с
    # включённым промптом выглядит полностью производящей, хотя три её рецепта
    # из четырёх сценариев не дают (мужские-образы, 2026-07-26).
    uncovered = sum(len(c.get("uncovered") or ())
                    for c in (prompt.get("coverage") or ()))
    if uncovered:
        out.append({"key": "coverage", "href": "/lab",
                    "text": f"{uncovered} {_plural_recipe(uncovered)} "
                            f"{_plural(uncovered, 'не назван', 'не названы', 'не названы')}"
                            f" правилами своей темы"})
    # Тот же класс молчаливого расхождения, только по ВЕРСИИ, а не по имени:
    # промпт называет рецепт, но ведёт на вытесненный снапшот.
    stale = sum(len(s.get("stale") or ()) for s in (prompt.get("stale_snapshots") or ()))
    if stale:
        out.append({"key": "snapshots", "href": "/lab",
                    "text": f"{stale} {'ссылка' if stale == 1 else 'ссылки'} правил "
                            f"ведут на устаревшие версии рецептов"})
    briefs = gates.get("briefs") or {}
    if briefs.get("count"):
        out.append({"key": "briefs", "href": briefs.get("href") or "/briefs",
                    "text": f"{briefs['count']} сценариев ждут решения"})
    return out


# Строка про устаревшие данные стоит ПОСЛЕДНЕЙ: это состояние показа, а не
# работа завода, и вытеснять ею настоящие задачи нельзя.
STALE_TEXT = "Данные могли устареть — показываем последнюю сохранённую копию."
STALE_NOTIFICATION = {"key": "stale", "href": "", "text": STALE_TEXT}

# Ворота не посчитаны (сбой чтения формул или Sheets) — это НЕ «всё разобрано».
UNKNOWN_NOTIFICATION = {
    "key": "unknown", "href": "/lab",
    "text": "Состояние ворот не посчитано — что требует действия, сейчас неизвестно",
}


def notifications(gates, stale=False):
    """Содержимое раздела «Уведомления» — всё, что требует действия сейчас.

    ``gates is None`` означает «ворота не посчитаны», и это отдельная строка, а
    не пустой список: пустой список рисует успокоительное «ничего не требует
    действия» по НЕпрочитанным данным — ровно то, что запрещает правило №2."""
    rows = [dict(UNKNOWN_NOTIFICATION)] if gates is None else gate_summary_rows(gates)
    if stale:
        rows = rows + [dict(STALE_NOTIFICATION)]
    return rows


def briefs_for_inactive_platforms(briefs, accounts):
    """Сколько одобренных сценариев написано под площадку, где нет активного аккаунта.

    Возврат: {платформа: количество} по УБЫВАНИЮ количества, пусто — всё в порядке.

    Зачем. Аудит 2026-08-04: 113 из 114 сценариев написаны под TikTok, а в
    cf.config.json все три tiktok-слота — handle=PLACEHOLDER, active=false; активен
    один instagram-1 (@by.jelapeche). Снять корпус было некуда, и об этом не говорил
    ни один экран: очередь съёмки показывала работу как готовую к выдаче.

    Это ПРЕДУПРЕЖДЕНИЕ, а не ворота (решение владельца 2026-08-04: аккаунты будут).
    Производство не блокируется — продюсер просто видит цифру и планирует под неё.
    Аккаунтов не задано вовсе -> пусто: судить по НЕзаданному нельзя (правило №2).
    """
    live = {str(a.get("platform") or "").strip().lower()
            for a in (accounts or ()) if a.get("active")}
    if not live:
        return {}
    counts = {}
    for b in briefs or ():
        platform = str(b.get("platform") or "").strip().lower()
        if platform and platform not in live:
            counts[platform] = counts.get(platform, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def inactive_platform_warning(counts):
    """Строка предупреждения для экрана — или пустая строка, если всё в порядке."""
    if not counts:
        return ""
    parts = [f"{platform}: {n} {_plural(n, 'сценарий', 'сценария', 'сценариев')}"
             for platform, n in counts.items()]
    return ("Написано под площадку без активного аккаунта — "
            + ", ".join(parts)
            + ". Снять можно, выложить пока некуда: включите аккаунт в "
              "cf.config.json → accounts.")
