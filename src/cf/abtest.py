"""Честный A/B промпт-версий через interleaving (P5.13).

Проблема: в CF Prompt Versions активна всегда одна версия, поэтому «сравнение версий»
было before/after со смешанными факторами (сменилась не только версия, но и период,
формулы, сезон) — статистически пустым.

Решение: рядом с active держим `candidate` (log-prompt-version --candidate — не
деактивирует active). Генератор брифов при наличии candidate ЧЕРЕДУЕТ версии между
брифами одной формулы (чётный индекс — active, нечётный — candidate), так обе версии
идут ПАРАЛЛЕЛЬНО в один период по одним формулам. Eval сравнивает только такие
параллельные когорты (см. cf.evalprep).

Статус версии выражен ЗНАЧЕНИЕМ в существующей колонке active (на живом листе она
называется status, см. column_aliases): TRUE/1/YES — active, CANDIDATE — кандидат,
FALSE/пусто — деактивирована. Новых колонок нет намеренно: append_row молча теряет
колонки вне заголовков живого листа, а update_* падает UnknownFieldsError — как и
P5.12, кладём новую семантику в уже существующую колонку.
"""
import hashlib

# Этот модуль — низкоуровневое ядро A/B без зависимостей от cf.*: cli и evalprep
# импортируют отсюда ACTIVE_TOKENS/CANDIDATE_TOKEN, чтобы не плодить дубли токенов.
# Токены активной версии — та же семантика, что cli._prompt_is_active /
# dashboard.sections._is_active / evalprep._is_active.
ACTIVE_TOKENS = ("TRUE", "1", "YES")
# Значение статуса кандидата в колонке active/status.
CANDIDATE_TOKEN = "CANDIDATE"


def _start_offset(prompt_id):
    """Детерминированная стартовая чётность (0/1) плана от prompt_id.

    Без этого план ВСЕГДА стартует с active: при count=1 кандидат стерилен навсегда, при
    нечётном count — систематический перекос к active (у плана нет памяти между
    прогонами). Хеш prompt_id стабилен между прогонами ОДНОГО промпта (в отличие от
    встроенного hash(), рандомизированного PYTHONHASHSEED), но разные промпты стартуют
    по-разному — так по многим промптам кандидат честно получает ~половину брифов даже
    при count=1.
    """
    digest = hashlib.sha1(str(prompt_id).encode("utf-8")).hexdigest()
    return int(digest, 16) % 2


def _status(row):
    return str(row.get("active", "")).strip().upper()


def select_versions(prompt_versions, prompt_id):
    """(active_row | None, candidate_row | None) для prompt_id.

    Строк каждого статуса может быть несколько (ручные правки листа) — выигрывает
    ПОСЛЕДНЯЯ по порядку: log_prompt_version дописывает свежую версию в конец, поэтому
    последняя active/candidate — самая новая.
    """
    active = candidate = None
    for row in prompt_versions:
        if str(row.get("prompt_id")) != str(prompt_id):
            continue
        status = _status(row)
        if status in ACTIVE_TOKENS:
            active = row
        elif status == CANDIDATE_TOKEN:
            candidate = row
    return active, candidate


def interleave_plan(prompt_versions, prompt_id, count):
    """План версий для `count` брифов одной формулы (детерминированный, тестируемый).

    Есть candidate -> чередуем active/candidate со стартовой чётностью от prompt_id
    (_start_offset): при чётном count ровно 50/50, при нечётном/count=1 перекос усреднён
    по промптам. Нет candidate -> все брифы несут active. Нет активной версии -> пустой
    план (генерировать нельзя, брифу нечего нести — brief-generator и так требует
    активный промпт ниши).

    Возвращает список {index, prompt_version, github_path, cohort} — агент пишет каждый
    бриф по github_path его версии и проставляет prompt_version в бриф-JSON.
    """
    active, candidate = select_versions(prompt_versions, prompt_id)
    if active is None:
        return []
    offset = _start_offset(prompt_id)
    plan = []
    for i in range(count):
        if candidate is not None and (i + offset) % 2 == 1:
            chosen, cohort = candidate, "candidate"
        else:
            chosen, cohort = active, "active"
        plan.append({
            "index": i,
            "prompt_version": str(chosen.get("version", "")),
            "github_path": str(chosen.get("github_path", "")),
            "cohort": cohort,
        })
    return plan
