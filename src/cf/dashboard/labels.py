"""Единый слой «система → пользователь» для дашборда.

§2 UX-спеки (docs/superpowers/specs/2026-07-25-cf-dashboard-ux-design.md), словарь
утверждён оператором 2026-07-25. Здесь живут переводы значений из данных и единые
форматы чисел/дат, чтобы копирайт не рассыпался по 9 шаблонам. Расширяется по фазам
UX-доводки (Фаза 2 — отчёты этапов, Фаза 3 — разделы).
"""
import json
import math
import re

# Англоязычные отказы Claude по лимитам: продюсеру показываем человеческую строку,
# сырой текст остаётся доступным под <details> (правило «прятать, не удалять»).
_LIMIT_MARKERS = ("session limit", "usage limit", "rate limit",
                  "reached your", "hit your")

# ...но ТОЛЬКО в сообщении агента, не в сводке сбора (разбор 2026-07-27). С тех
# пор как причина отказа Apify доезжает до input_summary, «monthly usage limit
# exceeded» попадал под те же маркеры — и авария оплаты показывалась продюсеру
# как «Система была занята, повторит позже», то есть «само пройдёт». Не пройдёт:
# сбор стоит, пока владелец не поднимет потолок трат. Сводку сбора узнаём по
# счётчикам: их не бывает в человеческом отказе Claude.
_COLLECT_MARKERS = ("batches=", "kept=", "rows=")


def _is_collect_dump(text):
    """Это машинная сводка звена сбора, а не сообщение агента о лимите?"""
    return any(marker in str(text or "") for marker in _COLLECT_MARKERS)

# Маркеры машинного дампа: такой отчёт сворачиваем под <details>, чтобы вопрос
# агента (человеческий текст без этих маркеров) не тонул в технике.
_DUMP_MARKERS = ("batches=", "rows=", "kept=", "NICHE_RESULT",
                 "agent-runtime/", "session_id=")

# Записи диалога с продюсером (reply-поток) — всегда на виду, их не сворачиваем.
_NEVER_COLLAPSE_TITLES = ("вопрос продюсера", "ответ агента")

_LIMIT_BANNER = "Система была занята, повторит позже."

# Неразрывный пробел: разряды чисел не рвутся переносом строки в узкой колонке.
NBSP = " "


def report_view(entry):
    """Как показать запись «Отчётов этапов» продюсеру (§2 спеки, Фаза 2).

    Возвращает dict:
    - ``banner``: человеческая строка вместо англоязычного отказа Claude по лимиту
      (сырой текст остаётся под <details>), иначе ``None``;
    - ``text``: сам текст отчёта (обрезанный по краям);
    - ``collapse``: ``True`` — свернуть текст под <details> (машинный дамп:
      ``batches=…``, ``NICHE_RESULT``, пути ``agent-runtime/…`` или отказ по лимиту),
      чтобы вопрос агента не тонул в технике. Диалог с продюсером не сворачивается.
    """
    entry = entry or {}
    text = str(entry.get("text") or "").strip()
    title = str(entry.get("title") or "").strip()
    low = text.lower()
    limited = (not _is_collect_dump(text)
               and any(marker in low for marker in _LIMIT_MARKERS))
    banner = _LIMIT_BANNER if limited else None
    collapse = False
    if title not in _NEVER_COLLAPSE_TITLES:
        collapse = limited or any(marker in text for marker in _DUMP_MARKERS)
    return {"banner": banner, "text": text, "collapse": collapse}


# --- Числа и даты: единый формат на весь дашборд (§2, «одна сущность — один вид») --

def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ".").replace(NBSP, "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def fmt_views(value, dash="—"):
    """Просмотры/лайки/сохранения: 705906 → «705 906» (неразрывные разряды).

    Нечисловое или пустое — прочерк: сырой мусор в колонке метрик читается как сбой.
    """
    number = _as_float(value)
    if number is None:
        return dash
    return f"{int(number):,}".replace(",", NBSP)


def fmt_pct(value, dash="—"):
    """Вовлечённость: доля 0.078 → «7,8%» (русская запятая, один знак).

    ER хранится долей (data.py), проценты — только на выводе. Значение > 1.5
    трактуем как уже-проценты (рукоправка в Sheets), чтобы не показать «780%».
    """
    number = _as_float(value)
    if number is None:
        return dash
    if number > 1.5:
        number = number / 100
    return f"{number * 100:.1f}".replace(".", ",") + "%"


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def fmt_date(value, dash="—"):
    """ISO-дата → «24.07.2026». Не-ISO строку отдаём как есть (не врём формой)."""
    text = str(value or "").strip()
    if not text:
        return dash
    match = _DATE_RE.match(text)
    if not match:
        return text
    year, month, day = match.groups()
    return f"{day}.{month}.{year}"


def fmt_datetime(value, dash="—"):
    """ISO-метка → «24.07.2026 22:25» (без секунд и таймзоны — как было в срезе)."""
    text = str(value or "").strip()
    if not text:
        return dash
    date = fmt_date(text, dash=dash)
    time = text[11:16] if len(text) >= 16 and text[10] in ("T", " ") else ""
    return f"{date} {time}".strip()


def plural_ru(n, one, few, many):
    """Русское склонение счётного слова: 1 сценарий / 2 сценария / 5 сценариев."""
    return _plural(n, one, few, many)


def fmt_duration(seconds, dash="—"):
    """Секунды → «2:14» (и «1:05:20» на длинных прогонах).

    Подпись «идёт …» рядом с полоской: без неё бегущий этап молчит минутами и
    выглядит зависшим (замечание оператора на живом прогоне).
    """
    number = _as_float(seconds)
    if number is None or number < 0:
        return dash
    total = int(number)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- Словарь значений из данных ------------------------------------------------

# «other» — служебная категория классификатора, продюсеру она ничего не говорит.
_NICHE_OVERRIDES = {"other": "без категории", "": "без категории"}


def niche_label(value):
    """Ниша → тема на языке продюсера: «мужские-образы» → «мужские образы»."""
    text = str(value or "").strip().lower()
    if text in _NICHE_OVERRIDES:
        return _NICHE_OVERRIDES[text]
    return text.replace("-", " ")


_HOST_LABELS = (("tiktok.", "TikTok"), ("instagram.", "Instagram"),
                ("youtube.", "YouTube"), ("youtu.be", "YouTube"))
_HANDLE_RE = re.compile(r"@([\w.\-]+)")


def link_label(url, limit=48):
    """Ссылка-референс → читаемый ярлык «TikTok · @dancox_7» вместо сырого URL.

    Не-URL и незнакомые площадки отдаём как есть (обрезая хвост): подменять адрес
    выдуманным названием нельзя — по нему продюсер и открывает ролик.
    """
    text = str(url or "").strip()
    if not text:
        return "—"
    body = text.split("://", 1)[-1]
    host = body.split("/", 1)[0].lower()
    platform = next((label for prefix, label in _HOST_LABELS if prefix in host), "")
    handle = _HANDLE_RE.search(body)
    if platform and handle:
        return f"{platform} · @{handle.group(1)}"
    if platform:
        return platform
    short = body[3:] if body.startswith("www.") else body
    return short if len(short) <= limit else short[:limit - 1] + "…"


CONFIDENCE_LABELS = {"low": "низкая", "medium": "средняя", "high": "высокая"}


def confidence_label(value):
    """confidence формулы → слово по-русски; незнакомое значение — как есть."""
    text = str(value or "").strip().lower()
    return CONFIDENCE_LABELS.get(text, text)


# Статус сценария словом — те же четыре слова, что в очереди ревью.
BRIEF_STATUS_LABELS = {"pending": "Ожидает", "approved": "Одобрен",
                       "rejected": "Отклонён", "revised": "Доработка"}


def brief_status_label(value):
    """review_status → слово продюсера; незнакомый статус — как есть."""
    text = str(value or "").strip().lower()
    return BRIEF_STATUS_LABELS.get(text, text)


# Ширина доказательной базы (sections.evidence_level) словами. Подпись обязательна
# рядом с цветом: цвет один смысл не несёт (кит, раздел доступности), да и
# «широкая/на минимуме» оператору понятнее оттенка.
EVIDENCE_LABELS = {"wide": "широкая база", "ok": "база достаточна",
                   "thin": "база на минимуме"}
EVIDENCE_HINTS = {
    "wide": "Приём опирается на 6 и более проверенных роликов — вдвое выше "
            "минимума, при котором паттерн вообще считается валидным.",
    "ok": "Проверенных роликов достаточно: приём подтверждён, но запас "
          "небольшой.",
    "thin": "Всего 3 проверенных ролика — нижняя граница. Если хотя бы один "
            "референс отвалится, приём перестанет быть валидным.",
}


def evidence_label(value):
    return EVIDENCE_LABELS.get(str(value or "").strip().lower(), "")


def evidence_hint(value):
    return EVIDENCE_HINTS.get(str(value or "").strip().lower(), "")


# Человекочитаемые имена задач (agent из CF Run Log). Продюсер не обязан знать
# слаги агентов, но сам слаг остаётся в интерфейсе мелкой служебной строкой —
# по нему оператор ищет прогон в логах (правило «прятать, не удалять»).
AGENT_LABELS = {
    # сбор
    "collect-tiktok": "Сбор роликов из TikTok",
    "collect-instagram": "Сбор роликов из Instagram",
    "collect-snowball": "Поиск новых источников",
    "collect-performance": "Сбор статистики наших роликов",
    "collect-metrika": "Сбор переходов на сайт из Метрики",
    # разметка и анализ
    "niche-classifier": "Разметка тем",
    "raw-batch-profiler": "Проверка качества собранных роликов",
    "batch-analyzer": "Анализ приёмов",
    "pattern-analyzer": "Анализ приёмов",
    "niche-pipeline": "Разбор темы",
    "formula-writer": "Черновик рецепта ролика",
    "formula-guard": "Проверка рецептов роликов",
    "formula-perf": "Пересчёт результатов рецептов",
    "apply-niches": "Обновление списка тем",
    # сценарии
    "brief-generator": "Написание сценариев",
    "brief-reviewer": "Проверка сценариев",
    "auto-approve": "Автоматическое одобрение сценариев",
    "dashboard-review": "Решение по сценарию",
    "dashboard-assign": "Назначение исполнителя",
    "dashboard-order": "Решение по заказу",
    "dashboard-formula": "Решение по рецепту ролика",
    "dashboard-niche": "Решение по теме",
    "mark-published": "Отметка о публикации",
    # ритуалы и служебное
    "eval-agent": "Недельная оценка результатов",
    "source-tuner": "Тюнинг источников",
    "prompt-optimizer": "Предложение правки промпта",
    "brief-prompt-writer": "Черновик правил сценариев темы",
    # Ворота «Включение правил сценариев темы»: решение, не работа агента.
    "prompt-apply": "Решение по правилам сценариев темы",
    "backup": "Резервная копия данных",
    "restore": "Восстановление из копии",
    "archive": "Архивация старых записей",
}

# Раннер пишет прогон этапа как f"dashboard-{stage}" — имена совпадают с плитками
# конвейера на «Обзоре» (partials/stages.html), чтобы журнал и лента звучали одинаково.
_DASHBOARD_STAGES = {
    "raw": "Этап «Сбор роликов»",
    "factory": "Этап «Контент-завод»",
    "publish": "Этап «Съёмка и публикация»",
    "stats": "Этап «Статистика»",
}


def agent_label(agent):
    """Слаг агента → человеческое имя задачи; незнакомый слаг — как есть.

    Незнакомое не переводим наугад: выдумывать имя для нового агента опаснее, чем
    показать слаг (правило честности, а заодно сигнал «словарь пора дополнить»).
    """
    key = str(agent or "").strip()
    if not key:
        return "—"
    if key in AGENT_LABELS:
        return AGENT_LABELS[key]
    if key.startswith("dashboard-"):
        return _DASHBOARD_STAGES.get(key[len("dashboard-"):], key)
    return key


TRIGGER_LABELS = {
    "dashboard": "с дашборда",
    "manual": "вручную",
    "cli": "по расписанию",     # таймерные юниты дёргают cf collect как cli
    "scheduled": "по расписанию",
    "timer": "по расписанию",
    "event": "по событию",
    "dry-run": "тест",
}


def trigger_label(trigger):
    """Триггер прогона → человеческая подпись; незнакомое значение — как есть."""
    key = str(trigger or "").strip().lower()
    if not key:
        return "—"
    return TRIGGER_LABELS.get(key, key)


# Строка уже человеческая, если в ней нет ни одного машинного маркера: key=value,
# счётчика ×N или латинского идентификатора (own_performance, eval-verdict).
# Такую сводку («конвейер занят — звено raw») показываем как есть, переведя термины.
_MACHINE_RE = re.compile(r"[\w-]+[=:]\S|×\d|[a-z]{3,}[_-][a-z]{3,}")

# Термины §2, встречающиеся внутри человеческих сводок агентов: статусы решения и
# пара внутренних понятий. Перевод пословный — на язык продюсера.
_TERMS = {
    "approved": "утверждено", "rejected": "отклонено", "revised": "доработать",
    "pending": "ожидает", "recommend": "рекомендовать", "revise": "доработать",
    "insufficient_data": "данных мало", "skipped": "пропущено", "paused": "в паузе",
    # статус proposal'а: в «Аналитике» бейдж печатался как есть, латиницей
    "proposed": "предложено",
}
_TERMS_RE = re.compile(r"\b(" + "|".join(sorted(_TERMS, key=len, reverse=True)) + r")\b")


def translate_terms(text):
    """Латинские статусы внутри готовой фразы → слова словаря §2."""
    return _TERMS_RE.sub(lambda m: _TERMS[m.group(1)], str(text or ""))


# Сводка прогона: из машинного input_summary вытаскиваем факты, которые продюсер
# может прочитать. Порядок правил = порядок фраз в сводке. Сырой текст всегда
# остаётся под <details> — ничего не теряем, только перестаём показывать по умолчанию.
_SUMMARY_RULES = (
    # решения дашборда пишутся как «<слаг>: approved» — показываем само решение,
    # слаг остаётся в сыром тексте под катом
    (re.compile(r"^\S+:\s*(approved|rejected|paused|revised|pending)\b"),
     lambda m: _TERMS.get(m.group(1), m.group(1))),
    (re.compile(r"\bniche=([\w-]+)"),
     lambda m: f"тема «{niche_label(m.group(1))}»"),
    (re.compile(r"\brows=(\d+)"),
     lambda m: f"просмотрено {m.group(1)} "
               f"{_plural(m.group(1), 'ролик', 'ролика', 'роликов')}"),
    (re.compile(r"\bpassed=(\d+)"),
     lambda m: f"прошло проверку {m.group(1)}"),
    (re.compile(r"\bkept=(\d+)/(\d+)"),
     lambda m: f"отобрано {m.group(1)} из {m.group(2)}"),
    (re.compile(r"\bkept=(\d+)(?!/)"),
     lambda m: f"отобрано {m.group(1)}"),
    (re.compile(r"\bappended=(\d+)"),
     lambda m: f"новых {m.group(1)}"),
    (re.compile(r"\brequests=(\d+)"),
     lambda m: f"замеров {m.group(1)}"),
    (re.compile(r"\bpending:(\d+)"),
     lambda m: f"на проверке {m.group(1)}"),
    (re.compile(r"auto-approved×(\d+)"),
     lambda m: f"одобрено автоматически {m.group(1)}"),
    (re.compile(r"\bruns_failed=([1-9]\d*)"),
     lambda m: f"сбоев {m.group(1)}"),
    # (разбор 2026-07-27) Instagram печатает счётчики С ПРЕФИКСОМ СТАДИИ, и общее
    # правило ниже их не ловило НИКОГДА: \b между «_» и «b» не срабатывает — оба
    # word-символы. Из-за этого прогон 27.07 01:21 с reel_batches_failed=2 висел
    # на пульте чисто зелёным. Стадии называем раздельно: у них разная цена —
    # потеря хэштега это недобор кандидатов, потеря рилса это недобор материала.
    (re.compile(r"\bhashtag_batches_failed=([1-9]\d*)"),
     lambda m: f"неудачных батчей по хэштегам {m.group(1)}"),
    (re.compile(r"\breel_batches_failed=([1-9]\d*)"),
     lambda m: f"неудачных батчей по рилсам {m.group(1)}"),
    # \b здесь корректен и НУЖЕН: он же отсекает префиксные формы выше, чтобы
    # одна и та же потеря не называлась дважды.
    (re.compile(r"\bbatches_failed=([1-9]\d*)"),
     lambda m: f"неудачных батчей {m.group(1)}"),
    (re.compile(r"\bsources_lost=([1-9]\d*)"),
     lambda m: f"потеряно источников {m.group(1)}"),
    (re.compile(r"\breels_lost=([1-9]\d*)"),
     lambda m: f"потеряно рилсов {m.group(1)}"),
)

def run_summary(input_summary):
    """Машинный input_summary → одна человеческая фраза (или '' — фраз не нашлось).

    Не пытается перевести всё: непонятный дамп честно даёт '', и шаблон показывает
    только имя задачи, а сырой текст оставляет под «деталями».
    """
    text = str(input_summary or "").strip()
    if not text:
        return ""
    # отказ по лимиту — тот же случай, что в отчётах этапов: продюсеру нужна
    # человеческая строка, а сырой английский остаётся под «деталями». Сводку
    # сбора этот баннер не подменяет: см. _is_collect_dump.
    if not _is_collect_dump(text) \
            and any(marker in text.lower() for marker in _LIMIT_MARKERS):
        return _LIMIT_BANNER
    if not _MACHINE_RE.search(text):
        return translate_terms(text)     # уже человеческая строка — только термины
    parts = []
    for pattern, render in _SUMMARY_RULES:
        match = pattern.search(text)
        if match:
            parts.append(render(match))
    return " · ".join(parts)


RUN_BADGES = {"success": ("success", "Успех"),
              "failed": ("failed", "Ошибка"),
              "insufficient_data": ("insufficient", "Мало данных"),
              "skipped": ("insufficient", "Пропущено")}


def run_view(row):
    """Строка журнала задач на языке продюсера (Фаза 3 спеки).

    Возвращает поля поверх сырой строки run_log: имя задачи, человеческая сводка,
    подпись триггера, пометка теста (dry-run ничего не сохраняет), бейдж статуса и
    дата. «Успех» с непустыми errors становится «Успех с ошибками»: зелёный бейдж
    над красным текстом ошибки внутри строки — прямая дезинформация.
    """
    row = dict(row or {})
    status = str(row.get("status", "")).strip().lower()
    errors = _errors_list(row.get("errors"))
    badge_class, badge_label = RUN_BADGES.get(status, ("insufficient", status or "—"))
    if status == "success" and errors:
        badge_class, badge_label = "insufficient", "Успех с ошибками"
    agent = str(row.get("agent", "")).strip()
    trigger = str(row.get("trigger_type", "")).strip().lower()
    raw = str(row.get("input_summary", "") or "").strip()
    summary = run_summary(raw)
    row.update({
        "task": agent_label(agent),
        "agent_id": agent,
        "trigger_label": trigger_label(trigger),
        "dry_run": trigger == "dry-run",
        "summary": summary,
        # Сырой дамп прячем под «детали» только когда он не равен показанной сводке.
        "raw_summary": raw if raw and raw != summary else "",
        "errors_list": errors,
        "badge_class": badge_class,
        "badge_label": badge_label,
        "when": fmt_datetime(row.get("completed_at")),
    })
    return row


def _errors_list(value):
    """errors из Sheets (JSON-массив или свободный текст) → список строк."""
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return [text]
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [str(parsed)] if str(parsed).strip() else []


# ── «Темп недели»: три полукольца ────────────────────────────────────────────
# Решение владельца 2026-07-28 (элемент 9): вместо средней вовлечённости — три
# вложенных полукольца, у каждого цель 70. Конверсия читается как разница колец,
# отдельной строки для неё больше нет.
#
# Геометрия живёт здесь, а не в шаблоне, ровно ради одного: клампа. Значение выше
# цели обязано остановиться на полном полукольце, иначе dash уезжает за дугу и
# ломает габарит плитки — а это как раз тот случай, который случится первым, когда
# завод разгонится.
RING_CENTER = (60, 58)          # центр дуг в координатах viewBox
RING_VIEWBOX = "0 0 120 64"
RING_STROKE = 9
RING_RADII = (52, 38, 24)       # внешнее → среднее → внутреннее, зазор 5px


def tempo_rings(metrics, target=None):
    """Три полукольца «Темпа недели» для шаблона.

    Порядок — порядок конвейера: одобрено → назначен исполнитель → выложено.
    Возвращает список словарей с готовой геометрией дуги (radius, length, dash) и
    самими числами: подписи и значения рендерятся ТЕКСТОМ рядом с рисунком, чтобы
    кольца не были единственным носителем смысла.
    """
    metrics = metrics or {}
    goal = target if target is not None else metrics.get("weekly_target") or 0
    rows = (("approved", "одобрено", metrics.get("briefs_approved_week")),
            ("assigned", "назначен исполнитель", metrics.get("assigned_week")),
            ("published", "выложено", metrics.get("reels_published_week")))
    out = []
    for radius, (key, label, value) in zip(RING_RADII, rows):
        value = value if isinstance(value, (int, float)) else 0
        length = math.pi * radius
        # Клампим ОБЕ стороны: перелёт цели рисует полное полукольцо и не уходит
        # за дугу, отрицательное значение (мусор в данных) не рисует ничего.
        share = 0.0 if not goal else max(0.0, min(float(value) / float(goal), 1.0))
        out.append({
            "key": key, "label": label, "value": value, "target": goal,
            "radius": radius, "length": round(length, 2),
            "dash": f"{length * share:.2f} {length:.2f}",
            "over": bool(goal) and value > goal,
        })
    return out


def ring_path(radius, center=RING_CENTER):
    """SVG-путь полукольца заданного радиуса (дуга сверху, слева направо)."""
    cx, cy = center
    return f"M {cx - radius} {cy} A {radius} {radius} 0 0 1 {cx + radius} {cy}"
