import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from cf.dashboard.queues import niches_ready_for_briefs
from cf.demoseed import DEMO_STATUS
from cf.trace import published_brief_ids

# P5.6: недельная цель одобрений (кольца «Темпа недели»), если dashboard.weekly_target нет.
DEFAULT_WEEKLY_TARGET = 70
# P5.6: порог старения approved-без-съёмки (одобрен, но рил не вышел > N дней).
DEFAULT_AGING_DAYS = 7

# Окно недельных чисел «Обзора» — СКОЛЬЗЯЩИЕ 7 дней, одно на весь экран
# (решение владельца 2026-07-28). Календарная неделя давала 0/70 во вторник:
# все 54 сценария были одобрены в воскресенье 26.07, а неделя уже новая — экран
# показывал остановившийся завод там, где завод работал.
LINK_WINDOW_DAYS = 7

# Демо-строки eval-петли (`cf demo-seed`) в счёт результатов не идут: 35 таких
# роликов в снимке 28.07 изобразили бы съёмку, которой не было. Признак берём У
# САМОЙ ПЕТЛИ (cf.demoseed.DEMO_STATUS), а не своей копией строки "demo": вторая
# копия того же токена — ровно тот способ, которым в этом проекте уже разъезжались
# признаки (см. docstring queues.py).


# P3.5: вкладки, меняющиеся на решении оператора (ревью брифа, решение по
# заказу) — короткий TTL, чтобы правка сразу отражалась. Остальные
# (raw_*/reels/performance/seeds) меняются медленно (сбор/публикация раз в
# цикл) — длинный TTL, чтобы фоновый поллинг не перечитывал их каждые 60 с и
# не жёг квоту чтений Sheets.
SHORT_TTL_TABS = frozenset({"briefs", "run_log", "orders"})


class DataCache:
    """TTL-кеш вкладок Sheets. При ошибке чтения отдаёт старые данные и ставит stale.

    Флаг stale — ПО ВКЛАДКЕ: успешное чтение одной вкладки не гасит stale,
    выставленный сбоем другой. Свойство stale — агрегат для баннера дашборда.

    TTL — per-tab: короткий (`ttl`) для быстро меняющихся вкладок из `short_tabs`
    (briefs/run_log — меняются на ревью), длинный (`long_ttl`) для медленных."""

    def __init__(self, sheets, ttl=60.0, long_ttl=180.0, short_tabs=SHORT_TTL_TABS,
                 clock=time.monotonic):
        self.sheets = sheets
        self.ttl = ttl
        self.long_ttl = long_ttl
        self.short_tabs = frozenset(short_tabs)
        self.clock = clock
        self._stale = {}  # tab_key -> bool: последняя выдача вкладки — из кеша после сбоя
        self._store = {}  # tab_key -> (fetched_at, rows)

    def ttl_for(self, tab_key):
        """TTL вкладки: короткий для briefs/run_log, длинный для медленных вкладок."""
        return self.ttl if tab_key in self.short_tabs else self.long_ttl

    @property
    def stale(self):
        """Хоть одна вкладка сейчас отдаёт данные из кеша после сбоя Sheets."""
        return any(self._stale.values())

    def stale_for(self, tab_key):
        return bool(self._stale.get(tab_key))

    def rows(self, tab_key):
        now = self.clock()
        cached = self._store.get(tab_key)
        if cached and now - cached[0] < self.ttl_for(tab_key):
            return cached[1]
        try:
            rows = self.sheets.read_rows(tab_key)
        except Exception:
            if cached:
                self._stale[tab_key] = True
                return cached[1]
            raise
        self._store[tab_key] = (now, rows)
        self._stale[tab_key] = False
        return rows

    def invalidate(self, tab_key):
        """Точечный сброс кеша ОДНОЙ вкладки (не всего стора, как refresh).

        После решения по брифу перечитать нужно только briefs+run_log; медленные
        raw/reels/performance остаются в кеше, чтобы следующий /overview или 2-сек
        HTMX-поллинг не выкачивал 5+ вкладок заново."""
        self._store.pop(tab_key, None)
        self._stale.pop(tab_key, None)

    def refresh(self):
        self._store.clear()
        self._stale.clear()


def _parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def brief_status(row):
    return str(row.get("review_status", "")).strip().lower()


# Метка отложенного заводом сценария (кап формулы исчерпан). Живёт в reviewer_notes,
# а не в отдельном статусе, намеренно: пятый статус пришлось бы протаскивать через
# norm_status, фильтры дашборда, гвардию и eval, а смысл у него один — «это ждёт
# квоты, а не человека». Пишет её cf auto-approve, снимает он же при одобрении.
DEFERRED_MARKER = "отложено заводом"


def brief_is_deferred(row):
    """Сценарий отложен заводом до освобождения квоты формулы?"""
    return DEFERRED_MARKER in str((row or {}).get("reviewer_notes") or "")


def demo_as_published_from_config(config):
    """Режим показа: считать ли демо-ролики выкладкой (dashboard.demo_as_published).

    Выключен по умолчанию и включается ОДНОЙ строкой конфига — это презентационный
    режим, а не молчаливая подмена данных. Владельцу он нужен, чтобы показать, как
    «Обзор» выглядит при работающей выкладке, пока отдела съёмки ещё нет; после
    показа флаг снимается, и экран возвращается к правде без правки кода.

    Демо-строки при этом остаются помеченными (`DEMO-`, `status=demo`): предохранитель
    от ночного `cf collect performance` держится на самой метке, а не на этом флаге,
    и фиктивные URL за деньги в Apify не уходят ни в каком режиме."""
    try:
        raw = ((config or {}).get("dashboard") or {}).get("demo_as_published", False)
    except AttributeError:
        return False
    return str(raw).strip().lower() in ("true", "1", "yes")


def reel_is_published(row, count_demo=False):
    """Ролик действительно вышел: есть ссылка и это не строка демо-петли.

    Ссылка — и есть признак «вышел»: строка рила без неё описывает намерение, а
    не результат. Демо-строки (`cf demo-seed`, status=demo) отсекаются по тому же
    маркеру, которым метит их сама петля: в снимке 28.07 таких было 35, и без
    фильтра плитка «Опубликованы» показала бы съёмку, которой не было.

    ``count_demo=True`` — презентационный режим (см. demo_as_published_from_config):
    демо-ролик со ссылкой считается выкладкой."""
    row = row or {}
    if str(row.get("status") or "").strip().lower() == DEMO_STATUS and not count_demo:
        return False
    return bool(str(row.get("post_url") or "").strip())


def shot_brief_ids(reels, count_demo=False):
    """brief_id сценариев, по которым ролик ДЕЙСТВИТЕЛЬНО вышел.

    Тот же join, что `trace.published_brief_ids`, но по настоящим строкам рилов.
    Разница не косметическая: демо-петля (`cf demo-seed`) ставит фиктивные рилы
    на РЕАЛЬНЫЕ одобренные сценарии, и сырой join объявлял их снятыми. На боевом
    28.07 так пропало 35 сценариев из 64 — они исчезли и из плитки «Ждут
    назначения», и из «Очереди съёмки», хотя их никто не снимал. Признак «ролик
    вышел» в дашборде должен быть ОДИН, иначе экраны отвечают на один вопрос
    по-разному (то же правило, что у queues.py)."""
    return published_brief_ids(
        [r for r in reels or () if reel_is_published(r, count_demo)])


def approved_formulas_in_window(root, since, until):
    """Сколько рецептов утверждено в окне — по approved_at из индекса снапшотов.

    Без approved_at рецепт в окно не попадает: «дата неизвестна» — это не «на
    этой неделе» (правило №2). Индекс тот же, что у approved_niche_map."""
    path = Path(root) / "formulas" / "_approved" / "index.json"
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    count = 0
    for entry in index.get("approved", []) or []:
        when = _parse_date(entry.get("approved_at", ""))
        if when is not None and since <= when <= until:
            count += 1
    return count


def approved_niche_map(root="."):
    """name -> niche из formulas/_approved/index.json (одно дешёвое чтение файла)."""
    path = Path(root) / "formulas" / "_approved" / "index.json"
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(e.get("name")): str(e.get("niche", ""))
            for e in index.get("approved", []) if e.get("name")}


def weekly_target_from_config(config, default=DEFAULT_WEEKLY_TARGET):
    """Недельная цель одобрений из cf.config.json dashboard.weekly_target (дефолт 70).

    Мусор/отсутствие ключа → дефолт: плитка X/70 не должна падать из-за конфига."""
    try:
        return int((config or {}).get("dashboard", {}).get("weekly_target", default))
    except (TypeError, ValueError):
        return default


# ── Исполнители съёмки ───────────────────────────────────────────────────────
# Слоты — ЗНАЧЕНИЯ ПОЛЯ, а не люди: «криэйтор-1», «криэйтор-2». При найме слот
# переименовывается одной правкой в одном месте (решение владельца 2026-07-28).
# Держать их в конфиге, а не в отдельном листе, — сознательный выбор той же
# природы: список из двух строк не стоит вкладки, схемы и синхронизации.
def creator_slots_from_config(config):
    """Слоты исполнителей из cf.config.json → dashboard.creator_slots.

    Мусор/отсутствие ключа → пустой список: экран от этого не падает, просто
    назначать некого — и «Очередь съёмки» говорит об этом прямо, а не прячет
    кнопку без объяснения."""
    try:
        raw = ((config or {}).get("dashboard") or {}).get("creator_slots") or []
    except AttributeError:
        return []
    if isinstance(raw, (str, bytes)):
        return []
    try:
        return [str(s).strip() for s in raw if str(s).strip()]
    except TypeError:
        return []


def brief_creator(row):
    """Слот исполнителя сценария ("" — не назначен)."""
    return str((row or {}).get("creator_slot") or "").strip()


def assignment_label(row):
    """Метка исполнителя для очереди: «криэйтор-1, с 28.07».

    Без даты (колонки нет, дата битая) — один слот: назначение важнее, чем
    красивая подпись, и терять его из-за пустой даты нельзя."""
    slot = brief_creator(row)
    if not slot:
        return ""
    when = _parse_date(row.get("assigned_at", ""))
    return f"{slot}, с {when.strftime('%d.%m')}" if when else slot


def niches_with_prompt(root="."):
    """Ниши, у которых есть prompts/briefs/{niche}/reel.md.

    Дешёвый скан каталога один раз на построение метрик: наличие reel.md и есть
    сигнал «у ниши готов бриф-промпт». _shared/ отсеивается сам — там нет reel.md."""
    base = Path(root) / "prompts" / "briefs"
    try:
        return sorted(p.parent.name for p in base.glob("*/reel.md"))
    except OSError:
        return []


def brief_niche(brief, niche_map):
    """Ниша брифа: явная колонка -> ниша формулы по index.json -> имя формулы -> ''.

    У живого брифа нет колонки niche, но есть formula_id; утверждённая формула
    несёт нишу (index.json). Fallback — само имя формулы, затем пусто (шаблон даст «—»)."""
    explicit = str(brief.get("niche") or "").strip()
    if explicit:
        return explicit
    formula_id = str(brief.get("formula_id") or "").strip()
    if not formula_id:
        return ""
    return niche_map.get(formula_id) or formula_id


# ── Кусочки сценариев для «Обзора» (решение владельца 2026-07-28) ────────────
# Под плитками оставался пустой экран, и по нему нельзя было понять, ЧТО завод
# написал: до текста сценария продюсер добирался только с /briefs. Здесь именно
# КУСОЧКИ — заголовок и пара строк: решение по сценарию принимается там, где
# рядом обоснование и кнопки, а не на обзорном экране.
BRIEF_PEEK_LIMIT = 6
BRIEF_PEEK_CHARS = 170


def brief_snippet(text, limit=BRIEF_PEEK_CHARS):
    """Начало сценария одной строкой; обрыв — по границе слова.

    Режем на сервере, а не многоточием CSS: сценарий бывает на несколько
    экранов, и гонять его целиком в разметку ради двух строк — впустую."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def brief_peek(cache, root=".", limit=BRIEF_PEEK_LIMIT, count_demo=False):
    """Свежие сценарии кусочками: сначала ждущие решения, дальше — по дате.

    Ждущие идут первыми не ради красоты: это единственное, что на заводе
    остаётся за человеком. Отложенные капом (`brief_is_deferred`) ждущими НЕ
    считаются — тем же предикатом, что и плитка очереди внимания, иначе экран
    звал бы к решению там, где решать нечего."""
    niche_map = approved_niche_map(root)
    published = shot_brief_ids(cache.rows("reels"), count_demo)
    cards = []
    for brief in cache.rows("briefs"):
        status = brief_status(brief)
        brief_id = str(brief.get("brief_id") or "")
        waiting = status in ("pending", "revised") and not brief_is_deferred(brief)
        when = _parse_date(brief.get("generated_at", ""))
        cards.append({
            "brief_id": brief_id,
            "hook": str(brief.get("hook") or "").strip(),
            "snippet": brief_snippet(brief.get("script")),
            "niche": brief_niche(brief, niche_map),
            "review_status": status,
            "generated_at": brief.get("generated_at", ""),
            "published": status == "approved" and brief_id in published,
            "waiting": waiting,
            "order": (0 if waiting else 1, -(when.toordinal() if when else 0)),
        })
    cards.sort(key=lambda c: c["order"])
    for card in cards:
        card.pop("order")
    return cards[:limit]


def sort_briefs_by_date(rows, newest_first=True):
    """Очередь сценариев по дате генерации; строки без даты — всегда в конце.

    «Дата неизвестна» — не «самый старый» и не «самый новый» (правило №2):
    в обоих направлениях такие строки уходят вниз, а не изображают край списка.
    Сортировка устойчивая — сценарии одного дня сохраняют порядок листа."""
    def key(row):
        when = _parse_date((row or {}).get("generated_at", ""))
        if when is None:
            return (1, 0)
        return (0, -when.toordinal() if newest_first else when.toordinal())
    return sorted(rows, key=key)


def overview_metrics(cache, days=30, today=None, root=".",
                     weekly_target=DEFAULT_WEEKLY_TARGET, aging_days=DEFAULT_AGING_DAYS,
                     window_days=LINK_WINDOW_DAYS, count_demo=False):
    today = today or datetime.now()
    # Границы периода — по датам: строки Sheets парсятся в полночь, поэтому нижнюю
    # границу тоже нормализуем к полуночи, иначе ряд ровно `days` дней назад
    # (00:00) выпадает из окна, когда у today есть время суток.
    since = (today - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    # Скользящее окно недельных чисел — той же нормализацией к полуночи, что и
    # период: строка ровно 7 дней назад лежит в окне независимо от времени суток,
    # с которым открыли экран. Календарного «this_week» больше нет: он давал 0/70
    # во вторник при 54 сценариях, одобренных в воскресенье (решение 2026-07-28).
    window_since = (today - timedelta(days=window_days)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    def in_period(row, field):
        d = _parse_date(row.get(field, ""))
        return d is not None and since <= d <= today

    def in_window(row, field):
        d = _parse_date(row.get(field, ""))
        return d is not None and window_since <= d <= today

    raw_rows = cache.rows("raw_tiktok") + cache.rows("raw_instagram")
    raw_tt = [r for r in cache.rows("raw_tiktok") if in_period(r, "posted_at")]
    raw_ig = [r for r in cache.rows("raw_instagram") if in_period(r, "posted_at")]

    briefs = cache.rows("briefs")
    # Отложенные заводом (кап формулы исчерпан) в очередь внимания НЕ идут: сценарий
    # хороший, решать по нему человеку нечего — он ждёт, пока освободится квота, и
    # завод сам вернётся к нему следующим прогоном (cf auto-approve --retry-deferred).
    # До 27.07 такие брифы висели в «ожидает» вечно и изображали решения: 3 из 9
    # карточек очереди продюсера были именно ими.
    pending = [b for b in briefs
               if brief_status(b) == "pending" and not brief_is_deferred(b)]
    deferred = [b for b in briefs
                if brief_status(b) == "pending" and brief_is_deferred(b)]
    approved = [b for b in briefs if brief_status(b) == "approved"]
    # P5.5: revised («доработка», P2.13) — тоже требует внимания ревьюера. Считаем
    # его в «очередь внимания» рядом с pending, чтобы доработочные брифы не забывались.
    revised = [b for b in briefs if brief_status(b) == "revised"]
    # Живой конвейер пишет generated_at (cf add-brief), а не created_at.
    created = [b for b in briefs if in_period(b, "generated_at")]
    oldest_pending = min(
        (d for d in (_parse_date(b.get("generated_at", "")) for b in pending) if d),
        default=None)
    # Одобрено в окне. approved_at в листе нет, поэтому аппроксимируем: статус
    # approved + generated_at в окне (бриф генерят и ревьюят в рамках одного цикла,
    # так что дата генерации близка к дате одобрения).
    briefs_approved_week = sum(1 for b in approved if in_window(b, "generated_at"))
    # Назначено исполнителю в окне — среднее полукольцо «Темпа недели».
    assigned_week = sum(1 for b in briefs
                        if brief_creator(b) and in_window(b, "assigned_at"))

    reels = cache.rows("reels")
    perf = cache.rows("performance")
    measured_ids = {str(p.get("reel_id")) for p in perf}
    awaiting = [r for r in reels if str(r.get("reel_id")) not in measured_ids]

    # Тот же предикат, что у плитки «Опубликованы»: иначе лента конвейера рядом
    # рисовала бы «35 роликов вышло» из демо-строк eval-петли, пока плитка честно
    # показывает 0. Второй способ считать «ролик вышел» здесь не заводится.
    published = [r for r in reels
                 if reel_is_published(r, count_demo) and in_period(r, "published_at")]
    # «Опубликованы» и внутреннее полукольцо: ролик СО ССЫЛКОЙ, не демо-строка.
    # Ссылка — и есть признак «вышел»: строка рила без неё описывает намерение.
    reels_published_week = sum(1 for r in reels
                               if reel_is_published(r, count_demo)
                               and in_window(r, "published_at"))

    # Воронка темы→рецепты (P5.6): из M тем с утверждённым рецептом у N тема реально
    # производит сценарии. N считается ЕДИНЫМ предикатом queues.niches_ready_for_briefs
    # (файл промпта И активная строка prompt_versions) — тем же, по которому решает
    # генератор. Раньше здесь был третий, свой способ счёта: пересечение множества
    # «есть файл reel.md» с множеством «есть approved-формула». Он не проверял
    # активность версии, поэтому тема в состоянии «файл сохранён, версию включить
    # забыли» попадала в N и цифра «Темы в работе N из M» показывала благополучие,
    # пока сценарии по теме не производились вовсе.
    niche_map = approved_niche_map(root)            # name -> niche (одно чтение index)
    formula_niches = {n for n in niche_map.values() if n}
    ready_niches = set(niches_ready_for_briefs(root, cache.rows("prompt_versions")))

    # Aging: approved-бриф без опубликованного рила старше N дней (join по brief_id).
    published_ids = shot_brief_ids(reels, count_demo)
    approved_no_reel_over_ndays = 0
    # «Ждут назначения исполнителя» — БЭКЛОГ, а не окно: это очередь работы, и
    # сценарий недельной давности из неё никуда не девается. Скользящее окно тут
    # спрятало бы ровно то, ради чего плитка заведена. Опубликованные исключаем:
    # ролик уже вышел, назначать некого — тот же фильтр, что у «Очереди съёмки».
    awaiting_assignment = 0
    for b in approved:
        if str(b.get("brief_id")) in published_ids:
            continue                                # рил уже вышел — не «завис»
        if not brief_creator(b):
            awaiting_assignment += 1
        d = _parse_date(b.get("generated_at", ""))
        if d is not None and (today - d).days > aging_days:
            approved_no_reel_over_ndays += 1

    # Звенья завода за окно: собрано → размечено тем → утверждено рецептов →
    # написано сценариев. Одно окно на весь экран (решение владельца 28.07).
    links_week = {
        "raw": sum(1 for r in raw_rows if in_window(r, "collected_at")),
        "niches": len({str(r.get("niche") or "").strip() for r in raw_rows
                       if in_window(r, "collected_at")
                       and str(r.get("niche") or "").strip()}),
        "formulas": approved_formulas_in_window(root, window_since, today),
        "briefs": sum(1 for b in briefs if in_window(b, "generated_at")),
    }

    return {
        "raw_total": len(raw_tt) + len(raw_ig),
        "raw_tiktok": len(raw_tt),
        "raw_instagram": len(raw_ig),
        "briefs_pending": len(pending),
        "briefs_revised": len(revised),                     # P5.5: «на доработку»
        "briefs_deferred": len(deferred),                   # отложены капом формулы
        "briefs_attention": len(pending) + len(revised),    # P5.5: очередь внимания
        "oldest_pending_days": (today - oldest_pending).days if oldest_pending else None,
        "briefs_created": len(created),
        "awaiting_stats": len(awaiting),
        "reels_published": len(published),
        # Звенья завода и кольца «Темпа недели» — всё за одно скользящее окно.
        "links_week": links_week,
        "window_days": window_days,
        "briefs_approved_week": briefs_approved_week,       # внешнее полукольцо
        "assigned_week": assigned_week,                     # среднее полукольцо
        "reels_published_week": reels_published_week,       # внутреннее полукольцо
        "weekly_target": weekly_target,
        "awaiting_assignment": awaiting_assignment,         # бэклог, не окно
        "niches_with_prompt": len(ready_niches),            # N: тема реально производит
        "niches_with_formula": len(formula_niches),         # M
        "formulas_approved": len(niche_map),                # K
        "approved_no_reel_over_ndays": approved_no_reel_over_ndays,
        "aging_days": aging_days,
    }
