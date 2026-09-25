"""Датасет для еженедельного eval-агента: performance × briefs × prompt_versions.

Честность датасета (P5.8):
- один замер на reel_id — ближайший к 7-му дню после публикации (fallback: последний
  по measured_at, если published_at нет/не парсится). Иначе ежедневные замеры одного
  ролика попадали бы в средние по многу раз и раздували вес долгоживущих роликов;
- --since применяется ПОСЛЕ дедупа к measured_at выбранного замера: старый рил, чей
  7-дневный замер вне недельного окна, честно выпадает из когорты (а не пролезает
  поздним 44-дневным замером рядом с 7-дневными новыми — иначе перекос avg/median
  старых версий); строки несут days_after_publish (окно проверяемо, ось когорт P5.13);
- median_views рядом с avg — просмотры имеют тяжёлый хвост (один вирусный ролик
  сносит среднее, медиана устойчива);
- агрегат by_prompt_version ключуется НЕ голой версией, а парой «промпт + версия»
  (см. _prompt_key): 'v2' пишут брифы разных тем, и без пространства имён их замеры
  склеились бы в одно ведро — главная ось eval и A/B стала бы бессмысленной;
- join_health = {unknown_version_share, briefs_not_found} как guard качества связки;
  при unknown > 20% датасет помечается insufficient_data (порог unknown-доли
  eval-agent.md + железное правило №2 CLAUDE.md): если связка
  performance→prompt_version дырявая, сравнивать версии нельзя;
- статусы/active сравниваются с нормализацией как в дашборде (значения набиваются в
  Sheets руками: 'Approved ', 'TRUE', '1', 'yes').
"""
import statistics
from datetime import datetime, timedelta

from cf.abtest import ACTIVE_TOKENS  # единый источник токенов active (см. cf.abtest)
from cf.attribution import attribute_clicks
from cf.collect.performance import FAILED_MEASUREMENT_MARKER

# Порог доли unknown-связок, выше которого датасет недостоверен (строго больше).
UNKNOWN_VERSION_THRESHOLD = 0.20

# Порог per-version для вердикта A/B-когорты (P5.9): версия с < 5 замеренных reels
# не участвует в вердиктах. Держим синхронно с eval-agent.md.
COHORT_MIN_REELS = 5

# Метка нерезолвленной версии (замер без prompt_version и брифа/версии). Не настоящая
# версия — не участвует в A/B-когортах (её доля уже сторожится join_health).
# Теоретическая коллизия: prompt_version, БУКВАЛЬНО названный "unknown", смешается с
# нерезолвленными. Версии именуются vN (log-prompt-version), поэтому риск нулевой; если
# когда-нибудь понадобится — заменить на непечатный сентинел (напр. object()).
UNKNOWN_VERSION = "unknown"


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _norm_status(value):
    """review_status как в дашборде (cf.dashboard.data.brief_status / cli.norm_status):
    strip+lower. Держим синхронно вручную — импорт из dashboard в этот низкоуровневый
    модуль тянул бы презентационный слой в ядро eval."""
    return str(value).strip().lower()


def _is_active(value):
    """active как в дашборде (cf.dashboard.sections._is_active / cli._prompt_is_active):
    strip+upper. Токены — единый ACTIVE_TOKENS из cf.abtest (без дублей)."""
    return str(value).strip().upper() in ACTIVE_TOKENS


def _parse_date(value):
    """Первые 10 символов как YYYY-MM-DD (как cf.dashboard.data._parse_date).

    published_at несёт время ('2026-07-01T09:00:00+00:00') — берём только дату;
    мусор/пусто -> None (замер уходит в fallback по measured_at)."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _days_after_publish(published_at, measured_at):
    """Сколько дней прошло от публикации до выбранного замера (проверяемое окно + ось
    когорт для P5.13). None, если published_at или measured_at не парсится."""
    published = _parse_date(published_at)
    measured = _parse_date(measured_at)
    if published is None or measured is None:
        return None
    return (measured - published).days


def _latest_by_measured_at(measurements):
    """Fallback-выбор: самый свежий парсибельный measured_at; если ни один не парсится —
    последний по порядку ввода (детерминированно, без падения)."""
    dated = [(m, _parse_date(m.get("measured_at"))) for m in measurements]
    parseable = [(m, d) for m, d in dated if d is not None]
    if parseable:
        return max(parseable, key=lambda item: item[1])[0]
    return measurements[-1]


def _pick_measurement(measurements, published_at):
    """Один замер на ролик: ближайший к published_at + 7 дней.

    При равном расстоянии предпочитаем замер НА или ПОСЛЕ 7-го дня (ранний недосчитывает
    просмотры). Нет валидного published_at или ни один measured_at не парсится ->
    fallback: последний по measured_at."""
    if len(measurements) == 1:
        return measurements[0]
    published = _parse_date(published_at)
    if published is not None:
        target = published + timedelta(days=7)
        dated = [(m, _parse_date(m.get("measured_at"))) for m in measurements]
        dated = [(m, d) for m, d in dated if d is not None]
        if dated:
            def key(item):
                delta = (item[1] - target).days
                return (abs(delta), delta < 0)  # ничья -> предпочесть >= 7 дней
            return min(dated, key=key)[0]
    return _latest_by_measured_at(measurements)


def _resolve_version(measurement, brief):
    """prompt_version замера: со строки замера, иначе из брифа, иначе 'unknown'.
    Пустые/пробельные значения не считаются версией."""
    for candidate in (measurement.get("prompt_version"),
                      (brief or {}).get("prompt_version")):
        version = str(candidate or "").strip()
        if version:
            return version
    return UNKNOWN_VERSION


def _resolve_prompt_id(measurement, brief):
    """prompt_id замера: со строки замера, иначе из брифа, иначе '' (нет связки).

    Колонки prompt_id в живых листах пока нет (заголовки CF Creative Briefs /
    CF Performance на 26.07.2026 её не содержат) — читаем мягко, чтобы связка
    заработала сама, как только оператор добавит колонку, а генератор начнёт её
    писать. До тех пор пространство имён даёт formula_id (см. _prompt_key)."""
    for candidate in (measurement.get("prompt_id"), (brief or {}).get("prompt_id")):
        prompt_id = str(candidate or "").strip()
        if prompt_id:
            return prompt_id
    return ""


def _prompt_key(prompt_id, formula_id, version):
    """Ключ ведра агрегации: версия ВНУТРИ своего промпта, а не голая 'v2'.

    Голая версия — не идентификатор: prompt_version брифа пишется как 'v1'/'v2' без
    имени промпта (см. schemas/brief.schema.json), поэтому в CF Creative Briefs на
    26.07.2026 лежат 26 строк 'v2' темы мужские-образы и 16 строк 'v1' темы
    бренды-магазины. mark_published копирует версию в строку рила — как только выйдут
    обе темы, их замеры сложились бы в одно ведро by_prompt_version, и сравнение
    версий (главная ось eval и A/B) сравнивало бы разные промпты.

    Пространство имён берём максимально точное из того, что есть в строке:
      1) prompt_id — истинная ось A/B (версии ОДНОГО промпта темы, ab-plan чередует
         active/candidate внутри prompt_id);
      2) formula_id — рецепт принадлежит ровно одной теме (formulas/_approved/
         index.json: name -> niche), поэтому две темы им не склеить; это же
         пространство у cohorts_by_formula, где и выносится вердикт (P5.13);
      3) ничего (замер без брифа) — голая версия; такое ведро может смешивать
         промпты, поэтому build_eval_dataset говорит о нём предупреждением, а не
         молчит.
    Обратная совместимость: старые строки без prompt_id не падают и попадают в ведро
    своей формулы — с новыми (prompt_id-ведро) они не сливаются, но и не теряются.
    unknown-версия остаётся под своим ключом: это не версия, её доля — в join_health.
    """
    if version == UNKNOWN_VERSION:
        return version
    namespace = str(prompt_id or "").strip() or str(formula_id or "").strip()
    return f"{namespace}:{version}" if namespace else version


def _ambiguous_key_warning(rows):
    """Предупреждение о вёдрах, которым не нашлось пространства имён (ни prompt_id,
    ни formula_id): их агрегат может смешать одинаковые версии разных промптов."""
    bare = sorted({r["prompt_key"] for r in rows
                   if r["prompt_key"] == r["prompt_version"]
                   and r["prompt_version"] != UNKNOWN_VERSION})
    if not bare:
        return []
    return [f"версии без промпта/формулы: {', '.join(bare)} — ведро by_prompt_version "
            "может смешивать одинаковые версии разных промптов, вердикт по нему не "
            "выносить (правило №2)"]


def _rows_carry_money(rows):
    """Обогащены ли строки деньгами (тикет 05): utm/orders переданы в датасет.
    Строки однородны — ключ либо у всех, либо ни у одной (см. build_eval_dataset)."""
    return bool(rows) and "clicks_exact" in rows[0]


def _money_sums(group):
    """Деньги ведра — те же поля, что в строках, СУММАМИ (клики и заказы
    аддитивны, в отличие от views, где нужны avg/median)."""
    return {
        "clicks_exact": round(sum(r["clicks_exact"] for r in group), 2),
        "clicks_estimated": round(sum(r["clicks_estimated"] for r in group), 2),
        "orders": sum(r["orders"] for r in group),
        "revenue": round(sum(r["revenue"] for r in group), 2),
    }


def _aggregate_by_version(rows):
    """Агрегаты по ключу «промпт + версия» (_prompt_key): reels, avg_views, avg_er,
    median_views (тяжёлый хвост); у обогащённых деньгами строк (тикет 05) — ещё
    clicks_exact/clicks_estimated/orders/revenue суммами."""
    with_money = _rows_carry_money(rows)
    by_version = {}
    for row in rows:
        by_version.setdefault(row["prompt_key"], []).append(row)
    result = {}
    for v, group in by_version.items():
        views = [r["views"] for r in group]
        result[v] = {
            "reels": len(group),
            "avg_views": round(sum(views) / len(views), 6),
            "avg_er": round(sum(r["er"] for r in group) / len(group), 6),
            "median_views": round(statistics.median(views), 6),
        }
        if with_money:
            result[v].update(_money_sums(group))
    return result


def _join_health(rows):
    """Guard качества связки performance→prompt_version.

    Возвращает (health, status, warnings): при доле unknown выше порога — status
    insufficient_data и предупреждение (правило №2), иначе ok."""
    total = len(rows)
    unknown = sum(1 for r in rows if r["prompt_version"] == UNKNOWN_VERSION)
    briefs_not_found = sum(1 for r in rows if not r["brief_found"])
    unknown_share = (unknown / total) if total else 0.0
    health = {
        "unknown_version_share": round(unknown_share, 6),
        "briefs_not_found": briefs_not_found,
    }
    warnings, status = [], "ok"
    if unknown_share > UNKNOWN_VERSION_THRESHOLD:
        status = "insufficient_data"
        warnings.append(
            f"unknown_version_share {unknown_share:.0%} > "
            f"{UNKNOWN_VERSION_THRESHOLD:.0%}: связка performance→prompt_version "
            f"ненадёжна, сравнивать версии нельзя (правило №2)")
    return health, status, warnings


def _verdict_gate(version_reels, min_reels=COHORT_MIN_REELS):
    """Гейт A/B-вердикта параллельной когорты (P5.13, порог P5.9).

    Сравнивать версии можно, только если их не меньше двух и у КАЖДОЙ >= min_reels
    замеренных reels. Иначе insufficient_data (напр. 4 vs 6 -> версия с 4 не участвует).
    Возвращает (status, insufficient_versions) — версии ниже порога перечислены, чтобы
    eval-агент сказал, каких reels ждать.
    """
    insufficient = sorted(v for v, n in version_reels.items() if n < min_reels)
    if insufficient or len(version_reels) < 2:
        return "insufficient_data", insufficient
    return "ok", []


def _windows_overlap(a, b):
    """Пересекаются ли [min, max] окна публикации (включительно)."""
    return a[0] <= b[1] and b[0] <= a[1]


def _parallel_groups(windows):
    """windows: {version: (min_date, max_date) | None} -> список групп версий с
    пересекающимися окнами публикации (связные компоненты по пересечению).

    Interleaving даёт версиям одной формулы пересекающиеся окна -> одна группа
    (параллельная когорта). before/after (версия сменила активную) -> непересекающиеся
    окна -> разные группы. Версия без окна (published_at не парсится) — своя одиночная
    группа: не с чем честно сопоставить период.
    """
    versions = sorted(windows)
    parent = {v: v for v in versions}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, va in enumerate(versions):
        wa = windows[va]
        if wa is None:
            continue
        for vb in versions[i + 1:]:
            wb = windows[vb]
            if wb is not None and _windows_overlap(wa, wb):
                parent[find(va)] = find(vb)

    groups = {}
    for v in versions:
        groups.setdefault(find(v), []).append(v)
    # стабильный порядок групп: по минимальной версии внутри группы
    return [sorted(g) for g in sorted(groups.values(), key=lambda g: sorted(g)[0])]


def _comparable_pairs(versions, windows, counts):
    """Внутри группы вердикт выносится ПОПАРНО (P5.13): union-find связывает версии
    транзитивно (v1∩v2, v2∩v3), но при этом v1∩v3 может быть пусто — сравнивать v1 и v3
    нельзя. Сравнимой считаем пару с ПЕРЕСЕКАЮЩИМИСЯ окнами публикации; её verdict_gate —
    порог P5.9 (>=5 замеренных reels у обеих). Пары без пересечения окон не выводим —
    их сравнение было бы before/after внутри группы.
    """
    pairs = []
    for i, a in enumerate(versions):
        for b in versions[i + 1:]:
            wa, wb = windows[a], windows[b]
            if wa is None or wb is None or not _windows_overlap(wa, wb):
                continue
            status, insufficient = _verdict_gate({a: counts[a], b: counts[b]})
            pairs.append({"versions": [a, b], "verdict_gate": status,
                          "insufficient_versions": insufficient})
    return pairs


def _cohorts_by_formula(rows):
    """Разметить строки признаком параллельной когорты (P5.13) и вернуть агрегат.

    Для каждой формулы: окно публикации каждой РЕЗОЛВЛЕННОЙ версии (min/max по
    published_at строк), группы параллельности по пересечению окон, per-версия reels/окно
    и comparable_pairs (см. _comparable_pairs — попарный гейт по пересечению окон + порог
    P5.9). Каждой строке проставляется row['cohort'] = '<formula_id>#g<N>' — по нему
    eval-агент отличает параллельную когорту (interleaving) от before/after.

    unknown-версия (замер без связки) НЕ участвует в окнах/группах: иначе её строки с
    широким окном склеили бы непересекающиеся before/after в одну группу (её доля уже
    сторожится join_health). Строки unknown и строки без формулы несут пустой cohort.

    Версии внутри когорт остаются ГОЛЫМИ ('v2', не 'formula:v2'): пространство имён
    здесь уже задано самой формулой (рецепт принадлежит одной теме), а читаемые метки
    нужны eval-агенту для вердикта по паре. Пространство имён проставляется только в
    плоском by_prompt_version, где формулы смешаны (см. _prompt_key).

    Ключи когорт '<formula_id>#g<N>' стабильны ТОЛЬКО внутри одного датасета: N — индекс
    группы в текущем разбиении, между неделями/прогонами он может смениться. Не
    использовать эти ключи для сравнения когорт МЕЖДУ датасетами (для этого — версии/окна).
    """
    with_money = _rows_carry_money(rows)
    by_formula = {}
    for r in rows:
        fid = str(r.get("formula_id") or "").strip()
        if fid:
            by_formula.setdefault(fid, []).append(r)

    result = {}
    for fid in sorted(by_formula):
        frows = by_formula[fid]
        windows, counts, by_version = {}, {}, {}
        for r in frows:
            v = r["prompt_version"]
            if v == UNKNOWN_VERSION:
                continue  # не настоящая версия — вне A/B-когорт (см. docstring)
            counts[v] = counts.get(v, 0) + 1
            by_version.setdefault(v, []).append(r)
            windows.setdefault(v, None)
            d = _parse_date(r.get("published_at"))
            if d is not None:
                cur = windows[v]
                windows[v] = [d, d] if cur is None else [min(cur[0], d), max(cur[1], d)]

        groups = _parallel_groups(windows)
        version_to_cohort, parallel_groups = {}, []
        for gi, group in enumerate(groups):
            cohort_key = f"{fid}#g{gi}"
            for v in group:
                version_to_cohort[v] = cohort_key
            parallel_groups.append({
                "cohort": cohort_key,
                "versions": {
                    v: {"reels": counts[v],
                        "window": ([windows[v][0].strftime("%Y-%m-%d"),
                                    windows[v][1].strftime("%Y-%m-%d")]
                                   if windows[v] else None),
                        # деньги версии (тикет 05) — только у обогащённых строк
                        **(_money_sums(by_version[v]) if with_money else {})}
                    for v in group},
                "comparable_pairs": _comparable_pairs(group, windows, counts),
            })
        for r in frows:
            r["cohort"] = version_to_cohort.get(r["prompt_version"], "")
        result[fid] = {"parallel_groups": parallel_groups}
    return result


def build_eval_dataset(performance, briefs, prompt_versions, reels=None, since=None,
                       utm=None, orders=None):
    """Собрать eval-датасет из замеров, брифов, версий промптов и опубликованных роликов.

    reels (CF Published Reels) — источник published_at по reel_id для дедупа к 7-му дню;
    None/пусто -> дедуп по fallback (последний measured_at). since (YYYY-MM-DD) отсекает
    ролики ПОСЛЕ дедупа по measured_at выбранного замера. Каждая строка несёт
    prompt_version, prompt_id и prompt_key (ключ ведра by_prompt_version — версия
    внутри своего промпта, см. _prompt_key) и days_after_publish; метаданные —
    join_health и status.

    utm (CF UTM Traffic) и orders (CF Orders) — деньги (тикет 05), опциональны по
    образцу reels: оба None -> датасет ПРЕЖНЕЙ формы без новых ключей и новых
    предупреждений (совместимость со старыми вызовами). Хотя бы один передан ->
    строки несут clicks_exact/clicks_estimated (округлены до 2 знаков), orders,
    revenue (cf.attribution.attribute_clicks, окно ВСЕГО датасета — границы не
    передаются: просмотры в строках тоже кумулятивные), агрегаты по версиям и
    когорты — те же поля суммами; пустая/непереданная вкладка — предупреждение
    в warnings, но статус и гейты не трогаются (деньги не роняют eval).
    """
    briefs_by_id = {str(b.get("brief_id")): b for b in briefs}
    published_at_by_reel = {}
    for r in (reels or []):
        rid = str(r.get("reel_id", ""))
        if rid:
            published_at_by_reel[rid] = r.get("published_at")

    # Группируем замеры по reel_id (порядок первого появления сохраняется). Пустой
    # reel_id — каждая строка сама по себе (не схлопывать разные ролики без ключа).
    groups, order = {}, []
    for i, p in enumerate(performance):
        rid = str(p.get("reel_id", ""))
        key = rid or f"__empty__{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    rows = []
    for key in order:
        measurements = groups[key]
        rid = str(measurements[0].get("reel_id", ""))
        published_at = published_at_by_reel.get(rid)
        if not published_at:  # нет/пусто в reels: published_at мог лежать на строке замера
            published_at = next((m.get("published_at") for m in measurements
                                 if m.get("published_at")), None)
        # H16 (аудит 2026-07-24): замер из упавшего Apify-рана (views=0 с маркером
        # в eval_notes) не должен вытеснять честный замер близостью к 7-му дню.
        # Все замеры сбойные -> берём как есть (даунстрим-фильтры views>0 решают).
        valid = [m for m in measurements
                 if FAILED_MEASUREMENT_MARKER not in str(m.get("eval_notes", ""))]
        chosen = _pick_measurement(valid or measurements, published_at)
        measured_at = chosen.get("measured_at", "")
        # --since ПОСЛЕ дедупа: старый рил, чей канонический 7-дневный замер вне окна,
        # выпадает целиком (а не пролезает поздним замером).
        if since and str(measured_at) < since:
            continue
        brief = briefs_by_id.get(str(chosen.get("brief_id")))
        version = _resolve_version(chosen, brief)
        prompt_id = _resolve_prompt_id(chosen, brief)
        formula_id = (brief or {}).get("formula_id", "")
        rows.append({
            "reel_id": rid,
            "brief_id": str(chosen.get("brief_id", "")),
            "prompt_version": version,
            "prompt_id": prompt_id,
            # ключ агрегации: версия внутри своего промпта (см. _prompt_key)
            "prompt_key": _prompt_key(prompt_id, formula_id, version),
            "views": _num(chosen.get("views")),
            "er": _num(chosen.get("er")),
            "measured_at": measured_at,
            "published_at": published_at or "",
            "days_after_publish": _days_after_publish(published_at, measured_at),
            "formula_id": formula_id,
            "source_pattern_ids": (brief or {}).get("source_pattern_ids", ""),
            "brief_found": brief is not None,
            "cohort": "",  # проставляется _cohorts_by_formula (ось параллельности P5.13)
        })

    money_warnings = []
    if utm is not None or orders is not None:
        per_reel = attribute_clicks(utm or [], reels or [], performance,
                                    orders or [])["reels"]
        for row in rows:
            money = per_reel.get(row["reel_id"], {})
            row["clicks_exact"] = round(_num(money.get("clicks_exact")), 2)
            row["clicks_estimated"] = round(_num(money.get("clicks_estimated")), 2)
            row["orders"] = int(_num(money.get("orders")))
            row["revenue"] = _num(money.get("revenue"))
        if not utm:
            money_warnings.append(
                "вкладка CF UTM Traffic пуста/не передана — clicks_exact и "
                "clicks_estimated в датасете нулевые")
        if not orders:
            money_warnings.append(
                "вкладка CF Orders пуста/не передана — orders и revenue "
                "в датасете нулевые")

    versions = _aggregate_by_version(rows)
    # Метит строки признаком параллельной когорты (мутирует rows['cohort']) и агрегирует.
    cohorts = _cohorts_by_formula(rows)
    join_health, status, warnings = _join_health(rows)
    warnings = warnings + _ambiguous_key_warning(rows) + money_warnings

    reviewed = [b for b in briefs
                if _norm_status(b.get("review_status")) in ("approved", "rejected", "revised")]
    approved = [b for b in reviewed if _norm_status(b.get("review_status")) == "approved"]
    active = [
        {"prompt_id": v.get("prompt_id"), "version": v.get("version"),
         "github_path": v.get("github_path")}
        for v in prompt_versions
        if _is_active(v.get("active"))
    ]
    return {
        "status": status,
        "by_prompt_version": versions,
        "cohorts_by_formula": cohorts,
        "rows": rows,
        "join_health": join_health,
        "warnings": warnings,
        "reviewer_pass_rate": (len(approved) / len(reviewed)) if reviewed else None,
        "reviewed_briefs": len(reviewed),
        "active_prompt_versions": active,
    }
