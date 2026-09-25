"""Контексты разделов этапа 2: Источники, Перформанс, Запуски, Лаборатория.

Чистые функции поверх DataCache; Лаборатория дополнительно читает файлы
репозитория (read-only: промпты и формулы меняются только через git-ревью).
"""
import datetime
import json
import re
from pathlib import Path

from gspread.exceptions import WorksheetNotFound

from cf.attribution import WINDOW_DAYS, attribute_clicks, month_bounds
from cf.collect.util import num
from cf.dashboard.labels import run_view
from cf.payout import LOWER_BOUND_NOTE, bio_links, build_payout_sheet
# Единый источник признаков готовности темы (queues.py). Имена ре-экспортируются:
# на них ссылаются тесты и вызывающий код, а определение теперь ровно одно.
from cf.dashboard.queues import (brief_prompt_path, fanout_exclude_niches,  # noqa: F401
                                 is_valid_niche_name, niche_has_prompt,
                                 version_is_active)

SOURCES_LIMIT = 200
RUNS_LIMIT = 100

# Сортировки ленты референсов (Фаза 3): ключ из ?sort= → (поле, числовое ли).
# Дефолт — по дате: свежесобранное сверху, как было до появления сортировки.
SOURCES_SORTS = {"date": ("posted_at", False), "views": ("views", True),
                 "saves": ("saves", True), "likes": ("likes", True)}

# P5.7 «Ритуалы недели»: сколько дней без запуска ритуала считается просрочкой.
RITUAL_STALE_DAYS = 7

# Ритуалы: key совпадает с ключами runner.RITUALS (маршрут /rituals/<key>/run),
# command — слэш-команда, которую дёргает runner тем же путём, что _fanout_claude.
# path/glob — где искать отчёты ритуала (дата в префиксе имени файла).
RITUAL_SPECS = [
    {"key": "eval", "label": "Недельный eval", "command": "/cf-eval",
     "path": ("agent-runtime", "evals"), "glob": "*-weekly-eval.json"},
    {"key": "tune-sources", "label": "Тюнинг источников", "command": "/cf-tune-sources",
     "path": ("agent-runtime", "source-stats"), "glob": "*-source-stats.json"},
]

_DATE_PREFIX_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# Пороги ширины доказательной базы рецепта — по числу проверенных референсов.
# Обоснование в данных, а не на глаз: паттерн считается валидным от 3 winners
# (меньше — insufficient_data у pattern-analyzer), значит рецепт на 3 URL стоит
# ровно на нижней границе: один отвалившийся референс делает его невалидным.
# Удвоенный минимум (6+) — уже осмысленный запас. На срезе 26.07 из 22 рецептов:
# 6 широких, 12 достаточных, 4 на минимуме — метка остаётся различающей, а не
# красит всю очередь одним цветом.
EVIDENCE_MIN_URLS = 3      # ниже — паттерн вообще не считается валидным
EVIDENCE_WIDE_URLS = 6     # от — «широкая база»


def evidence_level(url_count):
    """Ширина доказательной базы рецепта: 'wide' | 'ok' | 'thin'.

    Считается по ЧИСЛУ референсов, а не по confidence: confidence ставит агент
    суждением (на срезе 26.07 при 5 URL встречаются и high, и medium), а
    оператору в очереди одобрения нужен объективный, сравнимый между рецептами
    признак — на сколько роликов приём реально опирается.
    """
    try:
        n = int(url_count)
    except (TypeError, ValueError):
        return "thin"
    if n >= EVIDENCE_WIDE_URLS:
        return "wide"
    return "ok" if n > EVIDENCE_MIN_URLS else "thin"


def _sort_desc(rows, field):
    return sorted(rows, key=lambda r: str(r.get(field, "")), reverse=True)


def _sort_desc_num(rows, field):
    """Сортировка по числовой колонке метрик: мусор/пусто уезжает вниз, не наверх."""
    return sorted(rows, key=lambda r: _as_int(r.get(field), default=-1), reverse=True)


def sources_context(cache, tab, sort="date"):
    """Лента собранных референсов: вкладка платформы + сортировка из ?sort=.

    has_hooks (Фаза 3): собирается ли вообще hook_text. Пока не собирается, колонка
    «Заход» не рисуется — 200 прочерков подряд читаются как поломка, а не как
    «данных нет»."""
    if tab not in ("tiktok", "instagram"):
        tab = "tiktok"
    if sort not in SOURCES_SORTS:
        sort = "date"
    counts = {"tiktok": len(cache.rows("raw_tiktok")),
              "instagram": len(cache.rows("raw_instagram"))}
    field, numeric = SOURCES_SORTS[sort]
    all_rows = cache.rows(f"raw_{tab}")
    rows = (_sort_desc_num(all_rows, field) if numeric
            else _sort_desc(all_rows, field))[:SOURCES_LIMIT]
    has_hooks = any(str(r.get("hook_text") or "").strip() for r in rows)
    return {"tab": tab, "counts": counts, "sort": sort,
            "rows": rows, "total": counts[tab], "has_hooks": has_hooks}


def _brief_hooks(cache):
    """brief_id -> хук сценария: подпись строки результатов вместо голого ID."""
    hooks = {}
    for b in cache.rows("briefs"):
        brief_id = str(b.get("brief_id") or "").strip()
        hook = str(b.get("hook") or "").strip()
        if brief_id and hook:
            hooks[brief_id] = hook
    return hooks


def performance_context(cache, tab):
    """Вышедшие ролики и их показатели.

    К машинным ID добавляем человеческую подпись (Фаза 3): хук сценария для обеих
    вкладок, а для «Показателей» — ещё платформа и дата публикации из reels, чтобы
    строку можно было опознать без похода в другой раздел."""
    if tab not in ("reels", "metrics"):
        tab = "reels"
    reels = cache.rows("reels")
    perf = cache.rows("performance")
    hooks = _brief_hooks(cache)
    if tab == "reels":
        rows = [dict(r, brief_hook=hooks.get(str(r.get("brief_id") or "").strip(), ""))
                for r in _sort_desc(reels, "published_at")]
    else:
        by_reel = {str(r.get("reel_id") or "").strip(): r for r in reels}
        rows = []
        for p in _sort_desc(perf, "measured_at"):
            reel = by_reel.get(str(p.get("reel_id") or "").strip(), {})
            brief_id = str(p.get("brief_id") or reel.get("brief_id") or "").strip()
            rows.append(dict(p, brief_hook=hooks.get(brief_id, ""),
                             platform=reel.get("platform", ""),
                             published_at=reel.get("published_at", ""),
                             post_url=reel.get("post_url", "")))
    return {"tab": tab, "counts": {"reels": len(reels), "metrics": len(perf)},
            "rows": rows}


# ── Реестр заказов (UTM-контур, тикеты 03–04) ────────────────────────────────
# Секция живёт на странице «Деньги» (/money, money_context ниже); контекст
# собран отдельной чистой функцией, а разметка — в partials/orders_section.html.
ORDER_STATUS_LABELS = {"candidate": "Кандидат", "confirmed": "Оплачен, не возвращён",
                       "returned": "Возврат", "rejected": "Не наш"}
# Кнопки решений: в шаблоне рендерятся ВСЕ, кроме текущего статуса, — у
# кандидата три решения, у решённого два других плюс возврат в кандидаты
# («передумать» доступно всегда, статусы меняет только человек).
ORDER_DECISION_BUTTONS = [("confirmed", "Оплачен, не возвращён"),
                          ("returned", "Возврат"),
                          ("rejected", "Не наш"),
                          ("candidate", "Вернуть в кандидаты")]


def orders_context(cache):
    """Реестр заказов: кандидаты сверху (они ждут решения), внутри — свежие первыми.

    Вкладки CF Orders может ещё не быть (её создаёт первый сбор Метрики или
    первый ручной заказ) — это не сбой, а честное «заказов пока нет»."""
    try:
        raw = cache.rows("orders")
    except WorksheetNotFound:
        raw = []
    rows = []
    for r in raw:
        row = dict(r)
        row["status_norm"] = str(r.get("status") or "").strip().lower()
        rows.append(row)
    # Двухпроходная устойчивая сортировка: сначала свежие сверху по дате заказа
    # (ISO-строки сравниваются лексикографически; пустая дата уходит вниз),
    # затем кандидаты поднимаются над решёнными.
    rows.sort(key=lambda r: str(r.get("order_date") or ""), reverse=True)
    rows.sort(key=lambda r: 0 if r["status_norm"] == "candidate" else 1)
    counts = {"total": len(rows)}
    for key in ORDER_STATUS_LABELS:
        counts[key] = sum(1 for r in rows if r["status_norm"] == key)
    return {"orders": rows, "counts": counts,
            "status_labels": ORDER_STATUS_LABELS,
            "decision_buttons": ORDER_DECISION_BUTTONS}


# ── Страница «Деньги» (UTM-контур, тикет 04) ─────────────────────────────────

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _month_key(value):
    """'2026-07' из значения (месяц строки или префикс даты), мусор -> ''."""
    text = str(value or "").strip()[:7]
    return text if _MONTH_RE.fullmatch(text) else ""


def _money_months(utm_rows, orders, payout, current):
    """Месяцы селектора листа: где есть данные (или задан фикс), плюс текущий.

    Свежие первыми — как везде в дашборде. Текущий месяц есть всегда: нулевой
    месяц — состояние, а не ошибка, и дефолт селектора обязан существовать."""
    months = {current}
    for r in utm_rows:
        if str(r.get("row_kind") or "").strip() == "monthly":
            months.add(_month_key(r.get("month")))
    for o in orders:
        months.add(_month_key(o.get("order_date")))
    for key in ((payout or {}).get("monthly_fix_rub") or {}):
        months.add(_month_key(key))
    return sorted((m for m in months if m), reverse=True)


def _days_with_traffic(utm_rows, month):
    """slug -> число дней месяца с дневными строками переходов (динамика счётчиком).

    Месячные уники суммой дневных не считаются никогда (биллинговая цифра) —
    дневные строки дают только счётчик «в скольких днях аккаунт вёл на сайт»."""
    days = {}
    for r in utm_rows:
        if str(r.get("row_kind") or "").strip() != "daily":
            continue
        if _month_key(r.get("month")) != month:
            continue
        slug = str(r.get("account") or "").strip()
        day = str(r.get("date") or "").strip()
        if slug and day:
            days.setdefault(slug, set()).add(day)
    return {slug: len(seen) for slug, seen in days.items()}


def _per_reel_context(cache, utm_rows, orders, month):
    """Секция «По роликам» (тикет 05): точные и оценочные переходы месяца + заказы.

    Оценка не хранится — cf.attribution.attribute_clicks пересчитывает её на
    лету по дневным строкам выбранного месяца. Вкладок reels/performance может
    ещё не быть (свежая таблица) — это «оценивать нечем», а не сбой: остаток
    уходит в unattributed, точные клики по меткам остаются. clicks_estimated
    округляется до 2 знаков только здесь, на выводе."""
    bounds = month_bounds(month)
    if bounds is None:                            # мусорный месяц -> пустая секция
        return {"rows": [], "unattributed": 0, "window_days": WINDOW_DAYS}
    try:
        reels = cache.rows("reels")
    except WorksheetNotFound:
        reels = []
    try:
        performance = cache.rows("performance")
    except WorksheetNotFound:
        performance = []
    result = attribute_clicks(utm_rows, reels, performance, orders,
                              date_from=bounds[0], date_to=bounds[1])
    rows = [dict(item, reel_id=rid,
                 clicks_estimated=num(round(item["clicks_estimated"], 2)))
            for rid, item in result["reels"].items()]
    # больше переходов — выше; при равенстве — по reel_id (детерминизм)
    rows.sort(key=lambda r: (-(r["clicks_exact"] + r["clicks_estimated"]),
                             r["reel_id"]))
    return {"rows": rows, "unattributed": result["unattributed_total"],
            "window_days": WINDOW_DAYS}


def money_context(cache, accounts, payout, base_url, month, now):
    """Контекст «Денег»: расчётный лист месяца, переходы, по-роликовая сводка
    (тикет 05), заказы, ссылки для bio.

    По контракту queues.py: момент формирования (now) — аргумент, часов внутри
    нет; деньги считает чистая функция cf.payout.build_payout_sheet. accounts —
    ПОЛНЫЙ реестр (переходы считаются по нему), активность фильтруют только
    ссылки. Вкладки CF UTM Traffic может ещё не быть (сбор Метрики не
    запускался) — это не сбой, а нулевой месяц."""
    try:
        utm_rows = cache.rows("utm_traffic")
    except WorksheetNotFound:
        utm_rows = []
    orders_ctx = orders_context(cache)
    current = now.strftime("%Y-%m")
    month = _month_key(month) or current
    months = _money_months(utm_rows, orders_ctx["orders"], payout, current)
    if month not in months:                      # ?month= на месяц без данных
        months = sorted(months + [month], reverse=True)
    sheet = build_payout_sheet(utm_rows, orders_ctx["orders"], accounts,
                               payout, month, now)
    days = _days_with_traffic(utm_rows, month)
    transitions = [dict(a, days_active=days.get(a["slug"], 0))
                   for a in sheet["accounts"]]
    links = bio_links(accounts, base_url)
    links_reason = ""
    if not links:
        links_reason = ("site.base_url не задан в cf.config.json — ссылки "
                        "собирать не от чего"
                        if not str(base_url or "").strip() else
                        "все аккаунты реестра выключены (active: false) — "
                        "включи аккаунт в cf.config.json")
    return {"month": month, "months": months, "sheet": sheet,
            "transitions": transitions,
            "per_reel": _per_reel_context(cache, utm_rows, orders_ctx["orders"],
                                          month),
            "links": links,
            "links_reason": links_reason, "reg_empty": not accounts,
            "lower_bound_note": LOWER_BOUND_NOTE, **orders_ctx}


def read_frontmatter(text):
    """Мини-парсер YAML-шапки proposal-файла: только строки «ключ: значение»."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


def _first_heading(text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_json_cached(path, cache):
    """_load_json с памятью в пределах запроса: один файл читается с диска один раз.

    Утверждённая формула лежит в niche-каталоге, поэтому её JSON нужен и списку
    formulas, и _formula_queues — без кеша он читался бы дважды за один /lab (P3.6).
    None (битый/отсутствующий файл) тоже кешируется, чтобы не перечитывать."""
    if path in cache:
        return cache[path]
    data = _load_json(path)
    cache[path] = data
    return data


def runs_context(cache):
    """Журнал задач: строки run_log, переведённые в язык продюсера (labels.run_view).

    Слаг агента, сырой input_summary и текст ошибок остаются в строке — шаблон
    прячет их под «детали», но не выбрасывает."""
    all_runs = cache.rows("run_log")
    runs = [run_view(r) for r in _sort_desc(all_runs, "completed_at")[:RUNS_LIMIT]]
    return {"runs": runs, "total": len(all_runs)}


# Токен активной версии — из queues.version_is_active (он же тянет ACTIVE_TOKENS из
# ядра A/B). Собственная копия здесь была третьей в проекте и ровно из-за таких
# копий разъезжались признаки готовности темы.
_is_active = version_is_active


def _is_candidate(row):
    return str(row.get("active", "")).strip().upper() == "CANDIDATE"


def lab_context(cache, root=".", today=None):
    root = Path(root)

    try:
        # is_candidate (P5.13): кандидат A/B отличим от погашенной версии; в сортировке
        # active -> candidate -> прочие (по дате).
        versions = sorted(
            (dict(v, is_active=_is_active(v), is_candidate=_is_candidate(v))
             for v in cache.rows("prompt_versions")),
            key=lambda v: (v["is_active"], v["is_candidate"], str(v.get("activated_at", ""))),
            reverse=True)
    except Exception:
        versions = None

    # P3.6: кеш прочитанных formula-JSON в пределах одного запроса — approved-детали
    # ниже и _formula_queues дальше читают одни и те же файлы, диск дёргаем один раз.
    json_cache = {}
    # P5.2: niche -> есть ли brief-промпт. Общий дедуп stat на весь /lab: одна и та же
    # ниша в approved/proposed/paused проверяется один раз. Так бейдж «ниша без
    # brief-промпта» переживает approve (виден и в карточке утверждённых формул).
    prompt_cache = {}
    # нецелевые ниши (dashboard.fanout.exclude_niches) — читаем один раз на /lab
    exclude = fanout_exclude_niches(root)
    formulas = []
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    for entry in index.get("approved", []):
        detail = _load_json_cached(root / str(entry.get("path", "")), json_cache) or {}
        niche = entry.get("niche", "")
        if niche not in prompt_cache:
            prompt_cache[niche] = niche_has_prompt(root, niche)
        formulas.append({
            "name": entry.get("name", ""),
            "niche": niche,
            "version": entry.get("version", ""),
            "approved_at": str(entry.get("approved_at", ""))[:10],
            "confidence": detail.get("confidence", ""),
            "conditions": detail.get("conditions", ""),
            "path": entry.get("path", ""),
            "niche_has_prompt": prompt_cache[niche],
            "niche_excluded": niche in exclude,
            # P5.11: собственные замеры формулы (нормализованы) или None -> «нет данных».
            "own_performance": _norm_own_performance(entry.get("own_performance")),
        })

    patterns = []
    patterns_dir = root / "agent-runtime" / "patterns"
    if patterns_dir.is_dir():
        for path in sorted(patterns_dir.glob("*.json"), reverse=True):
            data = _load_json(path)
            if not data:
                continue
            meta = data.get("meta", {}) or {}
            items = []
            for p in data.get("patterns", []) or []:
                ev = p.get("evidence", {}) or {}
                urls = len(_url_list(ev.get("source_urls")))
                items.append({
                    "pattern_id": p.get("pattern_id", ""),
                    "description": p.get("description", ""),
                    "confidence": p.get("confidence", ""),
                    "avg_views": _as_float(ev.get("avg_views")),
                    "avg_er": _as_float(ev.get("avg_er")),
                    "urls": urls,
                    "evidence_level": evidence_level(urls),
                })
            patterns.append({
                "file": path.name,
                "niche": meta.get("niche", ""),
                "source_tab": _source_tab(meta, path.name),
                "generated_at": str(meta.get("generated_at", ""))[:10],
                "items": items,
            })

    proposals = []
    proposals_dir = root / "proposals"
    if proposals_dir.is_dir():
        for path in sorted(proposals_dir.glob("*.md"), reverse=True):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = read_frontmatter(text)
            proposals.append({
                "file": path.name,
                "title": _first_heading(text) or path.stem,
                "status": meta.get("status", "proposed"),
                "prompt_id": meta.get("prompt_id", ""),
                "created": meta.get("created", ""),
            })
    proposals.sort(key=lambda p: p["status"] != "proposed")  # proposed — первыми

    eval_report = None
    evals_dir = root / "agent-runtime" / "evals"
    if evals_dir.is_dir():
        # Детальная карточка «Последний eval» берёт свежайший файл лексикографически.
        # Карточка «Ритуалы недели» (_ritual_status) считает свежесть по дате в имени —
        # для конвенции YYYY-MM-DD-weekly-eval.json обе дают ОДИН файл (префикс-дата
        # монотонна), поэтому дублированием источника «последнего eval» не считаем.
        reports = sorted(evals_dir.glob("*-weekly-eval.json"), reverse=True)
        if reports:
            data = _load_json(reports[0])
            if data:
                eval_report = {
                    "file": reports[0].name,
                    "period": data.get("period", {}) or {},
                    "by_prompt_version": data.get("by_prompt_version", {}) or {},
                    "success_criteria": data.get("success_criteria", {}) or {},
                    "insights": data.get("insights", []) or [],
                }

    formula_queue, paused_formulas = _formula_queues(root, json_cache, prompt_cache,
                                                     exclude)
    niche_queue = _niche_queue(root)
    rituals = rituals_context(root, today=today)

    return {"versions": versions, "formulas": formulas, "patterns": patterns,
            "proposals": proposals, "eval_report": eval_report,
            "formula_queue": formula_queue, "paused_formulas": paused_formulas,
            "niche_queue": niche_queue, "rituals": rituals["rituals"],
            "pending_source_proposals": rituals["pending_source_proposals"]}


def _hook_structure_text(value):
    return (value if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False))[:200]


def _str_list(value, limit=6):
    """Список строк из значения формулы: list → элементы, str → один элемент."""
    if isinstance(value, list):
        return [str(v) for v in value[:limit]]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _url_list(value):
    """source_urls как список URL: list → сам список, str → один элемент, иначе →
    пусто. Защита от строкового source_urls: без неё len/итерация идут по символам."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _as_int(value, default=0):
    """Число из значения агента: '7' → 7, мусор → default (для сортировки очередей)."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _hook_parts(value):
    """(timing, элементы) из hook_structure — dict по схеме или произвольный текст."""
    if isinstance(value, dict):
        return str(value.get("timing", "")), _str_list(value.get("elements"))
    return "", _str_list(value)


# Ключи solution_structure, под которыми лежит посекундная структура ролика. Схема
# описывает поле как «просто объект» без обязательных ключей, и авторы называют его
# по-разному — на 19 формулах встретилось 9 разных наборов (format+beats,
# steps+duration_sec, format+flow+voiceover, …). Берём первый непустой список.
_SOLUTION_STEP_KEYS = ("beats", "steps", "flow")
# Ручная нумерация в начале шага: «1. », «2) ». Снимается — нумерует разметка.
_STEP_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)]\s*")
# Человеческие подписи для остальных ключей; незнакомый ключ выводится как есть,
# иначе новая находка агента молча исчезала бы с карточки.
_SOLUTION_LABELS = {
    "per_brand": "по каждому бренду", "positioning_device": "приём позиционирования",
    "category_variant": "вариант категории", "framing_rule": "правило рамки",
    "items_count": "число позиций", "voiceover": "войсовер", "caption": "капшен",
    "specifics": "конкретика", "answer_objection": "работа с возражением",
    "argument": "аргумент", "resolution": "развязка", "tone": "тон",
    "story_amplifier": "усилитель истории", "concreteness": "конкретность",
    "occasion_framing": "рамка повода", "product_integration": "интеграция товара",
    "serial": "сериальность",
}


def _solution_parts(value):
    """Разбор solution_structure на части, которые показывает карточка очереди.

    До правки карточка читала ровно `format` — а у трёх черновиков очереди
    (contrarian-verdict, specific-picks-list, star-peak-moment) этого ключа нет, и
    строка «Формат» молча исчезала. Сами шаги ролика не показывались НИКОГДА, ни у
    одной формулы: продюсер утверждал рецепт ролика, не видя структуры ролика.
    Поэтому берём всё: формат, шаги под любым из известных имён, длительность и
    остаток ключей — ничего из solution_structure не остаётся невидимым."""
    if not isinstance(value, dict):
        text = str(value or "").strip()
        return {"format": text, "steps": [], "duration": "", "extra": []}
    fmt = str(value.get("format", "") or "").strip()
    steps, steps_key = [], None
    for key in _SOLUTION_STEP_KEYS:
        found = _str_list(value.get(key), limit=12)
        if found:
            # Часть авторов нумерует шаги прямо в тексте («1. Вердикт-запрет…»), часть
            # нет («0-5с: рамка словами…»). Список нумерует разметка, поэтому ручной
            # префикс снимаем — иначе у первых выходит «1. 1. Вердикт-запрет».
            steps = [_STEP_PREFIX_RE.sub("", s, count=1).strip() for s in found]
            steps_key = key
            break
    duration = str(value.get("duration_sec", "") or "").strip()
    extra = []
    for key, raw in value.items():
        if key in ("format", "duration_sec") or key == steps_key:
            continue
        text = ("; ".join(str(v) for v in raw) if isinstance(raw, list)
                else str(raw or "")).strip()
        if text:
            extra.append({"label": _SOLUTION_LABELS.get(key, key), "text": text})
    return {"format": fmt, "steps": steps, "duration": duration, "extra": extra}


def _formula_queue_item(data, path, root, prompt_cache=None, exclude=None):
    evidence = data.get("evidence", {}) or {}
    urls = evidence.get("source_urls")
    hook_timing, hook_elements = _hook_parts(data.get("hook_structure", ""))
    solution = _solution_parts(data.get("solution_structure"))
    niche = data.get("niche", "")
    # P5.2: есть ли у ниши brief-промпт. Дедуп по нише через prompt_cache — один
    # stat на distinct-нишу за построение очереди, даже если формул в нише много.
    if prompt_cache is None:
        prompt_cache = {}
    if niche not in prompt_cache:
        prompt_cache[niche] = niche_has_prompt(root, niche)
    return {
        "name": data.get("name", ""),
        "niche": niche,
        "version": data.get("version", ""),
        "status_reason": str(data.get("status_reason") or ""),
        "hook_structure": _hook_structure_text(data.get("hook_structure", "")),
        "hook_timing": hook_timing,
        "hook_elements": hook_elements,
        # Очередь одобрения — поверхность go/no-go решения оператора: описательные
        # поля (проблема/формат/условия) показываем ЦЕЛИКОМ. Обрезка резала гипотезу
        # формулы на полуслове и читалась как «бред в конце» (store-native-skit:
        # «…магазин и товар — часть сюжета, а не» ← оборвано на «а не объект продажи»).
        "problem": str(data.get("problem_definition", "") or "").strip(),
        "solution_format": solution["format"],
        # Структура ролика — то, ради чего рецепт и утверждают. Раньше не доходила
        # до карточки ни у одной формулы (карточка читала только `format`).
        "solution_steps": solution["steps"],
        "duration": solution["duration"],
        "solution_extra": solution["extra"],
        "visual_requirements": _str_list(data.get("visual_requirements"), limit=8),
        "cta_type": str(data.get("cta_type", "") or "").strip(),
        "conditions": str(data.get("conditions", "") or "").strip(),
        "prohibitions": _str_list(data.get("prohibitions")),
        "confidence": str(data.get("confidence", "")),
        "evidence_urls": list(urls) if isinstance(urls, list) else [],
        "evidence_level": evidence_level(len(urls) if isinstance(urls, list) else 0),
        "avg_views": _as_float(evidence.get("avg_views")),
        "avg_er": _as_float(evidence.get("avg_er")),
        "path": path.relative_to(root).as_posix(),
        "niche_has_prompt": prompt_cache[niche],
        "niche_excluded": niche in (exclude or set()),
    }


def _formula_queues(root, cache=None, prompt_cache=None, exclude=None):
    """Сканирует formulas/*/*.json (без _approved) на proposed/paused формулы.

    cache — общий с lab_context кеш прочитанных JSON: approved-формулы, лежащие в
    niche-каталогах, уже прочитаны выше, повторно с диска их не тянем (P3.6).
    prompt_cache — общий с lab_context дедуп niche -> есть ли brief-промпт (P5.2):
    ниша, уже проверенная у approved-формулы, повторного stat не делает.
    exclude — нецелевые ниши (dashboard.fanout.exclude_niches) для бейджа «вне
    производства»; None -> пустое множество (обратная совместимость тестов)."""
    cache = cache if cache is not None else {}
    prompt_cache = prompt_cache if prompt_cache is not None else {}
    proposed, paused = [], []
    formulas_dir = root / "formulas"
    if formulas_dir.is_dir():
        for niche_dir in sorted(formulas_dir.iterdir()):
            if not niche_dir.is_dir() or niche_dir.name == "_approved":
                continue
            for path in sorted(niche_dir.glob("*.json")):
                data = _load_json_cached(path, cache)
                if not data:
                    continue
                status = data.get("status")
                if status == "proposed":
                    proposed.append(_formula_queue_item(data, path, root, prompt_cache,
                                                        exclude))
                elif status == "paused":
                    paused.append(_formula_queue_item(data, path, root, prompt_cache,
                                                      exclude))
    proposed.sort(key=lambda f: (f["niche"], f["name"]))
    paused.sort(key=lambda f: f["name"])
    return proposed, paused


def _niche_queue(root):
    """Сканирует agent-runtime/niche-proposals/*.json на предложения ниш."""
    queue = []
    proposals_dir = root / "agent-runtime" / "niche-proposals"
    if proposals_dir.is_dir():
        for path in sorted(proposals_dir.glob("*.json")):
            data = _load_json(path)
            if not data or data.get("status") != "proposed":
                continue
            queue.append({
                "name": data.get("name", ""),
                "title": data.get("title", ""),
                "description": data.get("description", ""),
                "cluster_size": _as_int(data.get("cluster_size", 0)),
                "examples": data.get("examples", []) or [],
                "filename": path.name,
            })
    queue.sort(key=lambda n: -n["cluster_size"])
    return queue


def _ritual_file_date(path):
    """Дата ритуального файла. Первичный источник — дата YYYY-MM-DD в имени файла
    (её пишет сам агент), fallback — mtime. Имя приоритетно намеренно: Я.Диск умеет
    портить mtime при синхронизации, поэтому mtime только когда даты в имени нет."""
    match = _DATE_PREFIX_RE.search(path.name)
    if match:
        try:
            return datetime.date.fromisoformat(match.group(1))
        except ValueError:
            pass
    try:
        return datetime.date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _ritual_status(dir_path, glob, today):
    """Свежесть ритуала: дата последнего отчёта + бейдж просрочки (>7 дней).

    Файлов нет (каталог отсутствует в свежем клоне или ритуал ещё не запускался) —
    last_date None, бейдж «нет данных»: это тоже повод запустить, поэтому overdue.
    """
    files = list(dir_path.glob(glob)) if dir_path.is_dir() else []
    dated = []
    for path in files:
        date = _ritual_file_date(path)
        if date is not None:
            dated.append((date, path))
    if not dated:
        return {"last_date": None, "last_file": None, "days_ago": None,
                "overdue": True, "badge": "нет данных"}
    # самый свежий отчёт: по дате, при равенстве — по имени (детерминизм)
    date, path = max(dated, key=lambda item: (item[0], item[1].name))
    # клампим на 0: будущая дата в имени (клок-скью/опечатка) не должна давать
    # «-N дн. назад» — трактуем как «только что», просрочки нет
    days_ago = max(0, (today - date).days)
    overdue = days_ago > RITUAL_STALE_DAYS
    return {"last_date": date.isoformat(), "last_file": path.name,
            "days_ago": days_ago, "overdue": overdue,
            "badge": "просрочено" if overdue else None}


def _pending_source_proposals(root):
    """Source-proposals (proposals/*.json), которые ещё ждут `cf apply-sources`.

    Статусы — из schemas/source-proposal.schema.json: pending/approved/rejected.
    Ждут apply два случая: (1) pending — свежий, ждёт ревью и применения; (2) approved
    БЕЗ applied_at — уже одобрен, но ещё не применён (cf apply-sources проставляет
    applied_at при применении, cli.py). applied approved и rejected — уже закрыты.
    Прочие proposals промптов — .md, под этот glob не попадают.
    """
    pending = []
    proposals_dir = root / "proposals"
    if proposals_dir.is_dir():
        for path in sorted(proposals_dir.glob("*.json")):
            data = _load_json(path)
            if not isinstance(data, dict):
                continue
            status = str(data.get("status", "")).strip().lower()
            applied = str(data.get("applied_at", "")).strip()
            awaiting = status == "pending" or (status == "approved" and not applied)
            if not awaiting:
                continue
            pending.append({
                "file": path.name,
                "platform": data.get("platform", ""),
                "generated_at": str(data.get("generated_at", ""))[:10],
                "remove": len(data.get("remove", []) or []),
                "add": len(data.get("add", []) or []),
            })
    return pending


def rituals_context(root=".", today=None):
    """Карточка «Ритуалы недели» (P5.7): свежесть eval и source-tuner с бейджем
    просрочки (>7 дней) плюс pending source-proposals, ждущие apply-sources.

    today — инъекция ради тестов; по умолчанию текущая дата. Чтение файлов
    best-effort, отсутствие agent-runtime карточку не роняет."""
    root = Path(root)
    today = today or datetime.date.today()
    rituals = []
    for spec in RITUAL_SPECS:
        status = _ritual_status(root.joinpath(*spec["path"]), spec["glob"], today)
        rituals.append({"key": spec["key"], "label": spec["label"],
                        "command": spec["command"], **status})
    return {"rituals": rituals,
            "pending_source_proposals": _pending_source_proposals(root)}


PLATFORM_LABELS = {"raw_tiktok": "TikTok", "raw_instagram": "Instagram"}
ORIGIN_PROBLEM_LIMIT = 300


def _source_tab(meta, filename):
    """Вкладка-источник паттерна: meta.source_tab, а если его нет (старые файлы и
    фан-аут) — вывод из имени по конвенции ...-<tab>-patterns.json."""
    tab = str((meta or {}).get("source_tab", "")).strip()
    if tab:
        return tab
    lower = filename.lower()
    if "tiktok" in lower:
        return "raw_tiktok"
    if "instagram" in lower:
        return "raw_instagram"
    return ""


def _norm_url(url):
    return str(url or "").strip().rstrip("/")


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_own_performance(raw):
    """P5.11: терпимая нормализация own_performance из индекса (рукоправка/дрейф схемы).

    Строка вместо dict, отсутствующие/нечисловые reels|median_views -> None («нет
    данных»): иначе битое значение клало бы весь /lab в 500 при рендере. avg_er ->
    float или None; last_measured_at -> строка."""
    if not isinstance(raw, dict):
        return None
    reels = _as_float(raw.get("reels"))
    median = _as_float(raw.get("median_views"))
    if reels is None or median is None:
        return None
    return {
        "reels": int(reels),
        "median_views": median,
        "avg_er": _as_float(raw.get("avg_er")),
        "last_measured_at": str(raw.get("last_measured_at", "") or ""),
    }


def _shorten(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _origin_formula(formula_id, root):
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    entry = next((e for e in index.get("approved", [])
                  if str(e.get("name")) == formula_id), None)
    detail = _load_json(root / str(entry.get("path", ""))) if entry else None
    if detail is None:
        return {"name": formula_id, "missing": True}
    return {
        "name": formula_id,
        "missing": False,
        "version": entry.get("version", ""),
        "niche": entry.get("niche", ""),
        "approved_at": str(entry.get("approved_at", ""))[:10],
        "confidence": detail.get("confidence", ""),
        "problem": _shorten(detail.get("problem_definition", ""), ORIGIN_PROBLEM_LIMIT),
    }


def _origin_patterns(pattern_ids, refs_normed, root):
    found = {}
    needed = set(pattern_ids)
    patterns_dir = root / "agent-runtime" / "patterns"
    if patterns_dir.is_dir():
        # имена начинаются с даты: обратная сортировка = свежайший файл побеждает
        for path in sorted(patterns_dir.glob("*.json"), reverse=True):
            data = _load_json(path)
            if not data:
                continue
            tab = _source_tab(data.get("meta"), path.name)
            for p in data.get("patterns", []) or []:
                pid = str(p.get("pattern_id", ""))
                if pid in needed and pid not in found:
                    ev = p.get("evidence", {}) or {}
                    found[pid] = {
                        "pattern_id": pid,
                        "description": p.get("description", ""),
                        "confidence": p.get("confidence", ""),
                        "platform": PLATFORM_LABELS.get(tab, tab),
                        "avg_views": _as_float(ev.get("avg_views")),
                        "avg_er": _as_float(ev.get("avg_er")),
                        "evidence": [
                            {"url": u, "is_reference": _norm_url(u) in refs_normed}
                            for u in _url_list(ev.get("source_urls"))],
                    }
            # P3.6: все нужные id найдены — остальные (более старые) файлы не читаем
            if len(found) == len(needed):
                break
    return ([found[pid] for pid in pattern_ids if pid in found],
            [pid for pid in pattern_ids if pid not in found])


def brief_origin(selected, references, root="."):
    """Цепочка происхождения брифа: формула → паттерны → evidence.

    None — агент не записал происхождение (старые брифы). Чтение файлов
    best-effort: битые/отсутствующие файлы не роняют страницу.
    Матчинг URL точный (strip + без трейлинг-слэша): реф с иным
    протоколом/параметрами честно попадёт в unmatched_refs.
    """
    if not selected:
        return None
    formula_id = str(selected.get("formula_id") or "").strip()
    pattern_ids = list(dict.fromkeys(
        p.strip() for p in
        str(selected.get("source_pattern_ids") or "").split(",") if p.strip()))
    if not formula_id and not pattern_ids:
        return None
    root = Path(root)
    refs_normed = {_norm_url(u) for u in references or []}
    patterns, missing = _origin_patterns(pattern_ids, refs_normed, root)
    matched = {_norm_url(e["url"])
               for p in patterns for e in p["evidence"] if e["is_reference"]}
    return {
        "formula": _origin_formula(formula_id, root) if formula_id else None,
        "patterns": patterns,
        "missing_pattern_ids": missing,
        "unmatched_refs": [u for u in (references or [])
                           if _norm_url(u) not in matched],
    }
