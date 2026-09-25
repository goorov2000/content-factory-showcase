"""Прогресс этапов конвейера для вертикальной ленты (Фаза 2б).

Спека: docs/superpowers/specs/2026-07-25-cf-pipeline-timeline-design.md.

Чистые функции (без состояния и без часов): раннер инструментирует счётчики,
здесь — только математика прогресса и медиан. Так расчёт легко тестируется, а
раннер остаётся источником истины по состоянию.
"""

import math
from datetime import datetime

from cf.dashboard.labels import plural_ru

# Симулированный бар не доходит до 100% сам — только по факту завершения этапа.
SIM_CAP = 0.92

# Прогресс v2 (спека §7): полоска не превышает этой доли, пока сервер не подтвердил
# завершение шага. Требование оператора «не выше 90–95% до подтверждения».
GLOBAL_CAP = 0.95

# Внутри одного сегмента (единицы работы / фазы) кривая тоже не добегает до конца —
# иначе бар «прилипал» бы к границе следующего якоря и тот не давал бы движения.
SEGMENT_CAP = 0.9

# Нет истории в Run Log → пологая дефолт-кривая (спека §2): считаем этап «средним».
DEFAULT_ETA_SEC = 90.0

# Фазы единицы работы шага «Сбор» (спека §7.2): вес = доля полоски, которую фаза
# занимает внутри одной платформы. prepare весит 0.15 не «для красоты»: к моменту
# первого маркера реестр уже прочитан и батчи собраны — это сделанная работа,
# поэтому бар честно стартует с 15%, а не с нуля («быстрые первые 15–25%»).
COLLECT_PHASES = (
    ("prepare", 0.15, "Готовим источники"),
    ("fetch", 0.60, "Собираем ролики"),
    ("gate", 0.15, "Отбираем годные"),
    ("write", 0.10, "Записываем в базу"),
)

# Instagram собирается в две выкачки (хэштеги → сами ролики), поэтому у него свой
# план: иначе после первой стадии полоска дошла бы до конца fetch, а потом вторая
# стадия либо стояла бы, либо поехала назад от роста total.
COLLECT_IG_PHASES = (
    ("prepare", 0.10, "Ищем хэштеги"),
    ("fetch", 0.35, "Собираем по хэштегам"),
    ("gate", 0.10, "Отбираем годные"),
    ("reels", 0.35, "Забираем ролики"),
    ("write", 0.10, "Записываем в базу"),
)

# Планы фаз по единице работы (платформе). Раннер кладёт имя плана в состояние
# шага, когда стартует конкретную команду сбора. Только те платформы, что ведёт
# шаг с полоской: у «Статистики» producing-шага нет (она живой бэклог «ждут
# данных»), snowball звенья дашборда не запускают — добавлять их сюда значит
# рисовать проводку, которой нет.
PHASE_PLANS = {"tiktok": COLLECT_PHASES, "instagram": COLLECT_IG_PHASES}

# Шаги, у которых единица работы разбита на фазы по умолчанию. Остальные (claude)
# фаз не имеют: внутри единицы наблюдаемых границ нет, работает кривая по времени.
STEP_PHASES = {"collect": COLLECT_PHASES}


def phase_plan(key, state=None):
    """Фазы текущей единицы работы: план из состояния (платформа) → план шага → ().

    Единый доступ для сервера, шаблона и тестов.
    """
    name = str((state or {}).get("phase_plan") or "").strip()
    if name in PHASE_PLANS:
        return PHASE_PLANS[name]
    return STEP_PHASES.get(key, ())


def _phase_bounds(phases, name):
    """(floor, weight, label) фазы: floor — сумма весов предыдущих фаз.

    Незнакомая фаза → (0.0, 0.0, "") : неизвестный маркер не должен двигать бар
    и уж точно не должен ронять рендер ленты.
    """
    floor = 0.0
    for phase_name, weight, label in phases:
        if phase_name == name:
            return floor, weight, label
        floor += weight
    return 0.0, 0.0, ""

# 8 шагов вертикальной ленты (спека §1). producing=True — шаг обнуляется за прогон
# и ведёт счётчик/полоску; producing=False — живой бэклог из overview_metrics
# («сколько ждёт прямо сейчас»), не обнуляется. actor — имя инструмента/роли
# (решение оператора: имена инструментов сохраняем, N8N→Apify). progress:
# "real" — честный done/total; "sim" — симулированный по времени; "none" — бэклог.
# Лента конвейера. `kind` — что это за строка для оператора:
#   auto   — делает машина (агент или CLI), идёт само;
#   gate   — ВОРОТА: конвейер здесь не идёт дальше без вашего решения;
#   manual — делаете вы руками (съёмка), автомата нет и не планируется.
#
# Ворота появились 2026-07-26 вместе с queues.py. До них две операторские развилки —
# одобрение рецепта и включение промпта темы — жили только в лаборатории и на ленте
# не показывались вовсе: цикл шёл «Анализ → Сценарии» встык, а генератор молча
# пропускал тему без промпта, при этом шаг закрывался как done. Из-за этого 10 из 14
# утверждённых рецептов не давали ни одного сценария каждый прогон.
PIPELINE_STEPS = [
    {"key": "collect", "label": "Сбор роликов", "actor": "Apify", "stage": "raw",
     "agent": "collect-tiktok", "producing": True, "progress": "real",
     "kind": "auto", "href": "/sources", "open": "Референсы"},
    {"key": "classify", "label": "Разметка тем", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "niche-classifier", "producing": True,
     "progress": "real", "kind": "auto", "href": "/lab", "open": "Лаборатория"},
    {"key": "analyze", "label": "Анализ приёмов", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "niche-pipeline", "producing": True,
     "progress": "real", "kind": "auto", "href": "/lab", "open": "Лаборатория"},
    # Черновики рецептов пишет тот же прогон темы, что ведёт анализ, — отдельной
    # строкой они стоят потому, что под ними ворота: без своей строки ворота
    # «Одобрение рецептов» висели бы в ленте без видимого источника работы.
    {"key": "formulas", "label": "Черновики рецептов", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "formula-writer", "producing": True,
     "progress": "real", "kind": "auto", "href": "/lab", "open": "Лаборатория"},
    # Ворота рецептов стали машинными вечером 2026-07-26: чек-лист из 11
    # детерминированных проверок плюс вердикт судьи. Своя строка ленты у них
    # потому же, почему она есть у черновиков: под ними стоят ворота, и без
    # видимого источника работы ворота висели бы ниоткуда.
    {"key": "formula-review", "label": "Проверка рецептов", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "formula-reviewer", "producing": True,
     "progress": "sim", "kind": "auto", "href": "/lab", "open": "Лаборатория"},
    # Строка ворот осталась, но смысл у неё другой: это очередь ИСКЛЮЧЕНИЙ —
    # рецепты, по которым машина решать отказалась и назвала причину.
    # actor здесь — только ЗАПАСНОЕ значение: живую подпись даёт сам gate
    # (queues.gate_actor по политике ворот). «вы» — безопасная сторона: пока
    # ворота не посчитаны, обещать, что их закроет машина, нельзя (правило №2).
    {"key": "gate-recipes", "label": "Рецепты на ваше решение", "actor": "вы",
     "stage": None, "agent": None, "producing": False, "progress": "none",
     "kind": "gate", "gate": "recipes", "href": "/lab", "open": "Лаборатория"},
    # Главная правка порядка: промпт темы пишется ВНУТРИ цикла, а не ручным
    # ритуалом в лаборатории. Агент отдаёт только черновик — включает его человек
    # на следующих воротах (правило №3).
    # «Промпт темы» непонятен непогружённому (решение владельца 2026-07-28,
    # элемент 17б): это правила, по которым пишутся сценарии этой темы. Слово
    # меняется в трёх строках — черновик, включение и подпись самих ворот
    # (queues.prompt_gate), чтобы на экране не осталось двух имён одного шага.
    {"key": "prompt-draft", "label": "Черновик правил сценариев темы",
     "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "brief-prompt-writer", "producing": True,
     "progress": "sim", "kind": "auto", "href": "/lab", "open": "Лаборатория"},
    {"key": "prompt-apply", "label": "Включение правил сценариев темы",
     "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "prompt-apply", "producing": True,
     "progress": "sim", "kind": "auto", "href": "/lab", "open": "Лаборатория"},
    {"key": "gate-prompt", "label": "Темы на ваше решение", "actor": "вы",
     "stage": None, "agent": None, "producing": False, "progress": "none",
     "kind": "gate", "gate": "prompt", "href": "/lab", "open": "Лаборатория"},
    {"key": "briefs", "label": "Сценарии", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "brief-generator", "producing": True,
     "progress": "sim", "kind": "auto", "href": "/briefs", "open": "Сценарии"},
    # Доработка стоит ДО ревью намеренно: один прогон ревьюера покрывает и новые
    # сценарии, и переписанные, без второго платного вызова. До 2026-07-27 вердикт
    # «доработка» был приговором — ревьюер называл, что поправить, а поправить было
    # некому, и брифы копились в очереди продюсера как ложные решения.
    {"key": "fix", "label": "Доработка сценариев", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "brief-fixer", "producing": True,
     "progress": "sim", "kind": "auto", "href": "/briefs", "open": "Сценарии"},
    {"key": "review", "label": "Ревью сценариев", "actor": "CLAUDE CODE",
     "stage": "factory", "agent": "brief-reviewer", "producing": True,
     "progress": "sim", "kind": "auto", "href": "/briefs", "open": "Сценарии"},
    # Эти ворота машину не держат: продукт цикла уже произведён. Держат съёмку.
    {"key": "gate-briefs", "label": "Одобрение сценариев", "actor": "вы",
     "stage": None, "agent": None, "producing": False, "progress": "none",
     "kind": "gate", "gate": "briefs", "href": "/briefs",
     "open": "Очередь сценариев"},
    # Актор — только продюсер: n8n списан 2026-07-24, и контур публикации ручной
    # с самого начала (форма «опубликован» в очереди сценариев -> cf.publish
    # .mark_published, тот же код у `cf mark-published`). Подпись «· N8N» осталась
    # с той поры, когда звено собирались вешать на вебхук, и вводила в заблуждение:
    # оператор ждал автоматики, которой нет и не планируется.
    # Подпись — «вы», как у всех человеческих строк (решение владельца
    # 2026-07-28, элемент 17в): «ПРОДЮСЕР» звучал как третье лицо, хотя это и
    # есть тот, кто смотрит на экран.
    {"key": "publish", "label": "Съёмка и публикация", "actor": "вы",
     "stage": "publish", "agent": None, "producing": False, "progress": "none",
     "kind": "manual", "href": "/performance", "open": "Результаты",
     "backlog": "reels_published",
     "unit": ("ролик вышел", "ролика вышло", "роликов вышло")},
    {"key": "stats", "label": "Статистика", "actor": "Apify", "stage": "stats",
     "agent": None, "producing": False, "progress": "none",
     "kind": "auto", "href": "/performance", "open": "Результаты",
     "backlog": "awaiting_stats",
     "unit": ("ждёт данных", "ждут данных", "ждут данных")},
]

# Ключи производящих шагов (обнуляются за прогон) в порядке ленты.
PRODUCING_STEPS = [s["key"] for s in PIPELINE_STEPS if s["producing"]]

# Ключи строк-ворот в порядке ленты.
GATE_STEPS = [s["key"] for s in PIPELINE_STEPS if s["kind"] == "gate"]


def simulated_fraction(elapsed, eta_median, cap=SIM_CAP):
    """Асимптотическая доля 1 − exp(−elapsed/eta): плавно ползёт, никогда не
    достигает 1.0 сама (кэп на ``cap``). Невалидные вход/eta → 0.0.

    «Детерминированно-выглядящий» бар: не врёт обратным отсчётом, честно
    добивается до 100% только когда этап реально завершился (status=done).
    """
    if not eta_median or eta_median <= 0:
        return 0.0
    if elapsed is None or elapsed <= 0:
        return 0.0
    frac = 1.0 - math.exp(-elapsed / eta_median)
    return min(frac, cap)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None


def median_duration(rows, agent, last_n=5):
    """Медиана длительности последних ``last_n`` успешных запусков агента из Run
    Log (сек), или None, если истории нет.

    Длительность = completed_at − started_at (оба ISO, один хост/формат). Строки
    в порядке добавления (хронологические) — берём хвост last_n, затем медиану.
    """
    durations = []
    for row in rows or ():
        if str(row.get("agent", "")).strip() != agent:
            continue
        if str(row.get("status", "")).strip().lower() not in ("success", "ok"):
            continue
        started = _parse_ts(row.get("started_at"))
        completed = _parse_ts(row.get("completed_at"))
        if started and completed and completed >= started:
            durations.append((completed - started).total_seconds())
    if not durations:
        return None
    durations = sorted(durations[-last_n:])
    n = len(durations)
    mid = n // 2
    return durations[mid] if n % 2 else (durations[mid - 1] + durations[mid]) / 2.0


_SIM_STATUS_LABEL = {"running": "идёт…", "done": "готово",
                     "error": "ошибка", "warn": "с изъянами",
                     "interrupted": "прервано"}

# Пояснение под полоской для не-бегущих состояний: без него недозаполненный бар
# прерванного прогона выглядит как зависшая работа (замечание оператора).
STATUS_NOTES = {
    "error": "прогон завершился ошибкой — подробности в «Отчётах этапов»",
    "warn": "прогон прошёл с изъянами — подробности в «Отчётах этапов»",
}


def _stage_entry_label(stage):
    """Подпись шага, на котором живёт ▶ этого звена (там же и перезапуск)."""
    for step in PIPELINE_STEPS:
        if step.get("stage") == stage and _STAGE_ENTRIES.get(step["key"]):
            return step["label"]
    return ""


def status_note(key, status, stage=None):
    """Подпись под полоской для не-бегущего шага — с конкретным следующим шагом.

    У прерванного прогона важно не только сказать «прервано», но и куда нажать:
    ▶ живёт на первом шаге звена, поэтому «Анализ тем» отправляет оператора к
    «Разметке тем», а не к собственной (отсутствующей) кнопке.
    """
    if status != "interrupted":
        return STATUS_NOTES.get(status, "")
    entry = _stage_entry_label(stage) if stage else ""
    if entry and entry != next((s["label"] for s in PIPELINE_STEPS
                                if s["key"] == key), ""):
        return f"прогон прерван — запустите этап заново кнопкой ▶ у «{entry}»"
    return "прогон прерван — запустите этап заново кнопкой ▶"


def _int_or_none(value):
    return value if isinstance(value, (int, float)) else None


def _count_label(key, state):
    """Человеческая подпись счёта шага за прогон (idle показывает прошлый прогон,
    т.к. счётчики не обнуляются до старта следующего прогона).

    Считаем в том, ради чего этап существует: ролики, темы, сценарии. «2/2
    платформ» и «2/2 вкладок» — это единицы обхода машины, оператору они не
    говорят ничего (правка оператора 2026-07-25). Пока шаг не дал результата,
    показываем его состояние, а не ноль: «ничего не нашли» и «ещё не искали» —
    разные вещи.
    """
    produced = _int_or_none(state.get("produced"))
    left = _int_or_none(state.get("left"))
    status = state.get("status")
    running = status == "running"
    if key == "collect":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        base = f"{int(produced)} {plural_ru(produced, 'ролик', 'ролика', 'роликов')}"
        new = _int_or_none(state.get("produced_new"))
        if new:
            return f"{base} · {int(new)} {plural_ru(new, 'новый', 'новых', 'новых')}"
        return base if produced else "новых роликов нет"
    if key == "classify":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        # часть raw-вкладок не прочиталась — сумму «без темы» называть нельзя
        # (правило №2 CLAUDE.md): молчание про остаток читается как «не осталось»
        unknown = bool(state.get("left_unknown"))
        if produced:
            base = (f"{int(produced)} "
                    f"{plural_ru(produced, 'ролик размечен', 'ролика размечено', 'роликов размечено')}")
            if unknown:
                return f"{base} · остаток неизвестен"
            return f"{base} · {int(left)} без темы" if left else base
        if left:
            return f"{int(left)} без темы — не размечено"
        return "не удалось посчитать" if unknown else "размечать было нечего"
    if key == "analyze":
        total = _int_or_none(state.get("total"))
        if total is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        if not total:
            return "тем в очереди нет"
        done, count = state.get("done") or 0, state.get("count") or 0
        base = f"{int(done)} из {int(total)} {plural_ru(total, 'темы', 'тем', 'тем')}"
        return (f"{base} · {int(count)} "
                f"{plural_ru(count, 'рецепт', 'рецепта', 'рецептов')}") if count else base
    if key == "formulas":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        return (f"{int(produced)} "
                f"{plural_ru(produced, 'черновик', 'черновика', 'черновиков')}"
                if produced else ("пишем…" if running else "новых рецептов нет"))
    if key == "prompt-draft":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        return (f"{int(produced)} "
                f"{plural_ru(produced, 'черновик', 'черновика', 'черновиков')}"
                if produced else ("пишем…" if running else "тем в очереди нет"))
    if key == "briefs":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        return (f"{int(produced)} "
                f"{plural_ru(produced, 'сценарий', 'сценария', 'сценариев')}"
                if produced else ("пишем…" if running else "сценариев нет"))
    if key == "review":
        if produced is None:
            return _SIM_STATUS_LABEL.get(status, "ждёт")
        return (f"{int(produced)} "
                f"{plural_ru(produced, 'проверен', 'проверено', 'проверено')}"
                if produced else ("проверяем…" if running else "проверять было нечего"))
    return _SIM_STATUS_LABEL.get(status, "ждёт")


# Подписи строк-ворот: что именно ждёт оператора и как выглядит закрытое
# состояние. Закрытые ворота говорят «разобрано», а не молчат: пустая строка
# посреди ленты читается как «шаг не работает».
_GATE_EMPTY_LABEL = {
    "recipes": "все рецепты разобраны",
    "prompt": "правила сценариев включены",
    "briefs": "очередь пуста",
}


def _gate_count_label(gate_key, gate):
    """Счётчик строки-ворот. None (ворота не посчитаны) → «—», не ноль."""
    if not gate:
        return "—"
    count = gate.get("count") or 0
    if not count:
        return _GATE_EMPTY_LABEL.get(gate_key, "разобрано")
    if gate_key == "briefs":
        return (f"{count} "
                f"{plural_ru(count, 'ждёт решения', 'ждут решения', 'ждут решения')}")
    return (f"{count} {plural_ru(count, 'тема', 'темы', 'тем')} "
            f"{plural_ru(count, 'ждёт вас', 'ждут вас', 'ждут вас')}")


def _stage_entries():
    """key → True, если это ПЕРВЫЙ шаг своего звена (на нём рисуется ▶ запуска
    звена: collect→raw, classify→factory, stats→stats, publish→publish)."""
    seen = set()
    entries = {}
    for step in PIPELINE_STEPS:
        stage = step.get("stage")
        entries[step["key"]] = bool(stage) and stage not in seen
        if stage:
            seen.add(stage)
    return entries


_STAGE_ENTRIES = _stage_entries()


def _duration_of(state, running):
    """Сколько шаг ШЁЛ (сек) — у бегущего None: там тикает живой таймер.

    Раннер проставляет `duration` в момент завершения шага; после рестарта она
    приезжает из снимка прогресса, поэтому «прошлый прогон шёл 18:03» переживает
    и перезапуск сервиса.
    """
    if running:
        return None
    value = state.get("duration")
    # меньше секунды — «шёл 0:00» только зашумляет строку (так завершается,
    # например, «Анализ тем» с пустой очередью: там всё сказано счётчиком)
    if isinstance(value, (int, float)) and value >= 1:
        return round(value, 1)
    return None


def build_timeline(run_progress, metrics, now, gates=None):
    """Строки вертикальной ленты для шаблона (спека §4).

    Производящие шаги берут статус/счётчик/долю из ``run_progress`` (``now`` —
    монотонные часы раннера для elapsed симулированного бара); строки-ворота —
    из ``gates`` (queues.build_gates); съёмка и статистика — живой бэклог из
    ``metrics``.

    ``gates=None`` означает «ворота не посчитаны» (сбой чтения формул или Sheets):
    строка показывает «—», а не успокоительный ноль — правило №2.
    """
    run_progress = run_progress or {}
    metrics = metrics or {}
    gates = gates or {}
    rows = []
    for meta in PIPELINE_STEPS:
        key = meta["key"]
        row = {"key": key, "label": meta["label"], "actor": meta["actor"],
               "href": meta["href"], "open": meta["open"],
               "producing": meta["producing"], "progress": meta["progress"],
               "kind": meta["kind"], "gate_key": meta.get("gate"),
               "stage": meta.get("stage"), "stage_entry": _STAGE_ENTRIES[key]}
        if meta["producing"]:
            state = run_progress.get(key, {})
            started = state.get("started_at")
            # elapsed шага — для подписи «идёт 2:14»; elapsed фазы — для кривой
            # внутри текущего сегмента (фаза/единица начались позже самого шага).
            phase_started = state.get("phase_started_at")
            if phase_started is None:
                phase_started = started
            elapsed = (now - started) if (started is not None
                                          and now is not None) else None
            phase_elapsed = (now - phase_started) if (phase_started is not None
                                                      and now is not None) else None
            running = state.get("status") == "running"
            row.update(
                status=state.get("status", "idle"),
                fraction=round(bar_fraction(state, phase_elapsed, key), 4),
                # прогресс v2: потолок и τ уезжают в data-атрибуты — по ним
                # клиентский тикер двигает бар между опросами htmx
                ceiling=round(bar_ceiling(state, key), 4),
                tau=round(phase_tau(key, state), 2),
                # метка запуска шага: меняется — значит полоска новая и клиенту
                # надо забыть накопленную ширину, а не продолжать прошлый прогон
                run_token=("" if started is None else round(started, 3)),
                elapsed=round(elapsed, 1) if elapsed is not None else None,
                # сколько шаг ШЁЛ: таймер не исчезает вместе с завершением —
                # «сколько это заняло» оператор спрашивает уже после (правка 25.07)
                duration=_duration_of(state, running),
                phase_label=phase_label(key, state),
                # idle_reason ставит раннер, когда воркер НЕ запускался из-за
                # пустой очереди («не запускается: 3 темы ждут включения промпта»).
                # Без него пустая полоска читается как «зависло», а не как
                # «работы не было и платный вызов сэкономлен».
                note=(str(state.get("idle_reason") or "")
                      or status_note(key, state.get("status"), meta.get("stage"))),
                count_label=_count_label(key, state))
        elif meta["kind"] == "gate":
            gate = gates.get(meta["gate"])
            # "passed", а не "open": класс .tl-open уже занят ссылкой «открыть →»
            # (.tl-open { display:inline-flex; width:32px }), и статус с тем же
            # именем накрыл бы <li> строки геометрией иконки.
            # Подпись актора берём У САМИХ ВОРОТ: она следует политике
            # (gates.policy.<ключ>), а не константе строки. Ворота не посчитаны —
            # остаётся запасное «вы» из PIPELINE_STEPS.
            row.update(
                actor=(gate or {}).get("actor") or meta["actor"],
                status=("blocked" if (gate or {}).get("blocking") else "passed"),
                fraction=0.0, ceiling=0.0, tau=None, run_token="", elapsed=None,
                duration=None, phase_label="",
                note=("" if gate else "состояние ворот не посчитано"),
                gate=gate,
                count_label=_gate_count_label(meta["gate"], gate))
        else:
            backlog = metrics.get(meta["backlog"]) if meta.get("backlog") else None
            # «1 ждут решения» читается как машинный вывод — склоняем и здесь
            unit = plural_ru(backlog or 0, *meta["unit"]) if meta.get("unit") else ""
            row.update(
                status="backlog", fraction=0.0, ceiling=0.0, tau=None,
                run_token="", elapsed=None,
                duration=None, phase_label="",
                note="",
                count_label=(f"{backlog} {unit}".strip() if backlog is not None
                             else "—"))
        rows.append(row)
    return rows


def unit_span(state):
    """Длина одной единицы работы в долях полоски (2 платформы → 0.5)."""
    total = state.get("total")
    if isinstance(total, (int, float)) and total > 0:
        return 1.0 / float(total)
    return 1.0


def _unit_base(state):
    """Подтверждённый факт: сколько полоски закрыто завершёнными единицами."""
    done = state.get("done")
    if not isinstance(done, (int, float)) or done <= 0:
        return 0.0
    return max(0.0, min(done * unit_span(state), 1.0))


def phase_facts(state):
    """(done, total) текущей фазы, если она управляется фактами, иначе None.

    Единый признак «фаза с якорями» для доли, потолка и τ: расхождение между
    ними и означало бы, что клиент целится не туда, куда придёт сервер.
    """
    state = state or {}
    done, total = state.get("phase_done"), state.get("phase_total")
    if (isinstance(total, (int, float)) and total > 0
            and isinstance(done, (int, float))):
        return float(done), float(total)
    return None


def inner_fraction(key, state, elapsed_in_phase=None):
    """Доля внутри ТЕКУЩЕЙ (незавершённой) единицы работы, [0, 1).

    Где есть факт — берём факт: у фазы ``fetch`` шага «Сбор» это k/N батчей Apify.
    Где факта нет (подготовка, гейт, запись, весь claude-шаг) — кривая
    ``1 − exp(−t/τ)``: быстрый старт, замедление к концу, кэп ``SEGMENT_CAP``,
    чтобы бар не прилипал к границе следующего якоря.
    """
    state = state or {}
    phases = phase_plan(key, state)
    # eta_median измерен по ВСЕМУ шагу (обе платформы сбора, все ниши анализа), а
    # кривая рисуется внутри ОДНОЙ единицы работы. Без нормировки на их число τ
    # завышена в `total` раз и полоска почти стоит.
    eta = (state.get("eta_median") or DEFAULT_ETA_SEC) * unit_span(state)
    if phases and state.get("phase"):
        floor, weight, _ = _phase_bounds(phases, state.get("phase"))
        facts = phase_facts(state)
        if facts:
            done, total = facts
            within = max(0.0, min(done / total, 1.0))          # факт — без кэпа
        else:
            # τ фазы пропорциональна её весу: длинная фаза ползёт медленнее
            within = simulated_fraction(elapsed_in_phase, eta * weight,
                                        cap=SEGMENT_CAP)
        return min(floor + weight * within, 1.0)
    return simulated_fraction(elapsed_in_phase, eta, cap=SEGMENT_CAP)


def bar_fraction(step_state, elapsed=None, key=None):
    """Доля заполнения полоски шага: факт как якорь + кривая внутри единицы.

    - ``done`` → 1.0 (добивка только по подтверждению завершения);
    - иначе ``base`` (завершённые единицы) + доля внутри текущей единицы,
      всё вместе не выше ``GLOBAL_CAP`` — требование «не выше 90–95% до
      подтверждения сервером»;
    - простаивающий/пустой шаг → 0.0.

    Монотонность по построению: внутри единицы кривая не превышает её длины, а
    следующий якорь всегда выше достигнутого потолка предыдущего сегмента.
    """
    state = step_state or {}
    if state.get("status") == "done":
        return 1.0
    if state.get("status") != "running":
        # не бегущий шаг с фактом (error/warn после части единиц) — показываем факт
        base = _unit_base(state)
        return min(base, GLOBAL_CAP) if base else 0.0
    base = _unit_base(state)
    inner = inner_fraction(key, state, elapsed)
    return min(base + unit_span(state) * inner, GLOBAL_CAP)


def bar_ceiling(step_state, key=None):
    """Докуда полоска вправе доехать БЕЗ новых фактов от сервера.

    Потолок — это ближайшая точка, которую сервер реально способен показать:
    - фаза с якорями (fetch/reels: k из N батчей) → СЛЕДУЮЩИЙ якорь, (k+1)/N.
      Целиться в конец фазы нельзя: между батчами сервер стоит на месте, и
      каждый опрос htmx тянул бы разогнавшийся бар назад («полоску отталкивает»,
      замечание оператора 2026-07-25);
    - фаза без якорей → предел её собственной кривой, floor + weight·SEGMENT_CAP;
      сервер выше не поднимется, значит и клиенту выше нельзя.
    """
    state = step_state or {}
    if state.get("status") == "done":
        return 1.0
    if state.get("status") != "running":
        return bar_fraction(state, None, key)
    phases = phase_plan(key, state)
    if phases and state.get("phase"):
        floor, weight, _ = _phase_bounds(phases, state.get("phase"))
        facts = phase_facts(state)
        if facts:
            done, total = facts
            inner_cap = floor + weight * min((done + 1.0) / total, 1.0)
        else:
            inner_cap = floor + weight * SEGMENT_CAP
    else:
        inner_cap = SEGMENT_CAP
    return min(_unit_base(state) + unit_span(state) * inner_cap, GLOBAL_CAP)


def phase_tau(key, state):
    """τ (сек) для клиентского тикера: сколько живёт кривая до следующего якоря.

    Клиент двигает бар к потолку по exp-кривой с этой τ — та же математика, что
    на сервере, поэтому опрос htmx не «дёргает» полоску назад или вперёд. В фазе
    с якорями отрезок кривой — ОДИН батч, поэтому τ делится на их число: иначе
    бар полз бы к следующему факту темпом всей фазы и висел бы почти на месте.
    """
    state = state or {}
    # та же нормировка, что в inner_fraction: сервер и клиент обязаны считать
    # одну и ту же кривую, иначе тикер расходится с якорями
    eta = (state.get("eta_median") or DEFAULT_ETA_SEC) * unit_span(state)
    phases = phase_plan(key, state)
    if phases and state.get("phase"):
        _, weight, _ = _phase_bounds(phases, state.get("phase"))
        facts = phase_facts(state)
        if facts:
            return max(eta * weight / facts[1], 1.0)
        return max(eta * weight, 1.0)
    return max(eta, 1.0)


def phase_label(key, state):
    """Что происходит прямо сейчас, словами проекта (спека §7.2).

    Пусто — если шаг не бежит: подпись «идёт …» без бегущего шага только мешает.
    """
    state = state or {}
    if state.get("status") != "running":
        return ""
    parts = []
    unit = str(state.get("unit_label") or "").strip()
    if unit:
        parts.append(unit)
    phases = phase_plan(key, state)
    if phases and state.get("phase"):
        _, _, label = _phase_bounds(phases, state.get("phase"))
        if label:
            parts.append(label)
        done, total = state.get("phase_done"), state.get("phase_total")
        if (state.get("phase") in ("fetch", "reels")
                and isinstance(total, (int, float))
                and total and isinstance(done, (int, float))):
            parts.append(f"{int(done)}/{int(total)} батчей")
    elif not parts:
        parts.append(_STEP_RUNNING_LABEL.get(key, "идёт"))
    return " · ".join(parts)


# Подписи бегущих шагов без фаз и без единиц (один вызов claude — внутри не видно).
_STEP_RUNNING_LABEL = {
    "classify": "Размечаем темы",
    "analyze": "Разбираем тему",
    "briefs": "Пишем сценарии",
    "review": "Проверяем сценарии",
}
