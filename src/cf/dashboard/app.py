import html
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cf.dashboard import decisions
from cf.dashboard.actions import (VALID_DECISIONS, VALID_ORDER_STATUSES,
                                  add_manual_order, assign_creator,
                                  review_brief, set_order_status)
from cf.dashboard.data import (DataCache, approved_niche_map, assignment_label,
                               brief_niche, brief_peek, brief_status,
                               creator_slots_from_config,
                               demo_as_published_from_config, overview_metrics,
                               shot_brief_ids, sort_briefs_by_date,
                               weekly_target_from_config)
from cf.dashboard.labels import (RING_STROKE, RING_VIEWBOX, agent_label,
                                 brief_status_label, confidence_label,
                                 evidence_hint, evidence_label,
                                 fmt_date, fmt_datetime, fmt_duration, fmt_pct,
                                 fmt_views, link_label, niche_label, plural_ru,
                                 report_view, ring_path, tempo_rings,
                                 translate_terms)
from cf.dashboard.progress import build_timeline
from cf.dashboard.prompt_apply import (PromptApplyError, apply_prompt,
                                       read_draft, reject_draft)
from cf.dashboard.queues import (PROMPT_DRAFT, STALE_TEXT, build_gates,
                                 gate_banner, notifications, prompt_gate)
from cf.dashboard.runner import STAGES
from cf.payout import fmt_money, sheet_to_md
from cf.publish import MarkPublishedError, mark_published
from cf.reasons import REASON_CODES
from cf.sheets import UnknownFieldsError
from cf.dashboard.sections import (ORDER_DECISION_BUTTONS, ORDER_STATUS_LABELS,
                                   brief_origin, lab_context, money_context,
                                   performance_context, runs_context,
                                   sources_context)

BASE_DIR = Path(__file__).parent

logger = logging.getLogger(__name__)

# Человекочитаемые названия звеньев для заметок оператору (совпадают с overview.html
# и partials/stages.html — §2 UX-спеки: RAW → «собранные ролики»).
STAGE_NAMES = {"raw": "Собранные ролики", "factory": "Контент-завод",
               "publish": "Съёмка и публикация", "stats": "Статистика"}

# Статусы ревью, которые дашборд умеет фильтровать/считать. revised («доработка»)
# — валидный исход cf-review-brief; без него брифы «на доработку» пропадали из виду.
BRIEF_STATUSES = ("pending", "approved", "rejected", "revised")

# Порядок очереди сценариев. По умолчанию свежие сверху (решение владельца
# 2026-07-28): порядок листа шёл от самых старых, и очередь приходилось мотать.
BRIEF_SORTS = ("date-desc", "date-asc")
DEFAULT_BRIEF_SORT = "date-desc"


def template_filters():
    """Фильтры формата и словаря для Jinja — одним списком на всё приложение.

    Числа/даты/термины выводятся одинаково во всех разделах (Фаза 3 — «числа
    единообразны»), перевод живёт в labels.py. Список общий и для боевого
    окружения, и для прогрева шаблонов: Jinja проверяет имена фильтров при
    КОМПИЛЯЦИИ, поэтому разъехавшиеся списки означали бы «шаблон падает только в
    одном из двух мест».
    """
    return {
        "fmt_views": fmt_views, "fmt_pct": fmt_pct, "fmt_date": fmt_date,
        "fmt_datetime": fmt_datetime, "niche_label": niche_label,
        "confidence_label": confidence_label, "agent_label": agent_label,
        "evidence_label": evidence_label, "evidence_hint": evidence_hint,
        "brief_status_label": brief_status_label,
        "link_label": link_label, "plural_ru": plural_ru,
        "duration": fmt_duration, "term": translate_terms,
        # Деньги (страница «Деньги» и расчётный лист): формат один на HTML и
        # md-экспорт, живёт рядом с расчётом в cf.payout.
        "fmt_money": fmt_money,
    }


# Этап 3б: маршруты-РЕШЕНИЯ — те, что меняют версионируемое состояние по воле
# оператора. Их нельзя дёрнуть без Origin/Referer, в отличие от маршрутов запуска
# (/cycle/run, /stages/*/run, /rituals/*/run), которыми пользуется cf-cycle.timer
# через curl. Список — префиксами, а не регулярками: новый маршрут решений под
# /prompts/ или /formulas/ попадает под защиту сам, а не забывается.
DECISION_ROUTE_PREFIXES = ("/prompts/apply", "/prompts/reject", "/formulas/decision",
                           "/niches/decision", "/briefs/", "/orders")


def _is_decision_route(path):
    return any(path.startswith(p) for p in DECISION_ROUTE_PREFIXES)


def _precompile_templates(env):
    """Скомпилировать все шаблоны на старте и закэшировать в процессе.

    Без этого шаблон подхватывается с диска при ПЕРВОМ рендере — то есть уже
    после правок в рабочем каталоге, с несовместимым кодом. Битый шаблон здесь не
    роняет старт сервиса (иначе одна опечатка = дашборд не поднимается): пишем
    предупреждение, а 500 придёт только на своей странице.
    """
    root = BASE_DIR / "templates"
    for path in sorted(root.rglob("*.html")):
        name = path.relative_to(root).as_posix()
        try:
            env.get_template(name)
        except Exception:
            logger.warning("шаблон %s не скомпилирован на старте", name,
                           exc_info=True)


def create_app(sheets=None, cache=None, runner=None, health=None, lab_root=None,
               port=8787, weekly_target=70, public_origin="", creator_slots=(),
               accounts=(), demo_as_published=False, payout=None,
               site_base_url=""):
    @asynccontextmanager
    async def lifespan(app):
        yield
        # При остановке сервера гасим фоновый health-поток, чтобы daemon-поток
        # не оставался висеть (см. HealthMonitor.stop).
        if health is not None:
            health.stop()

    app = FastAPI(title="CF Dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    # CSRF-защита: браузерный form-POST обязан прийти со страницы самого дашборда.
    # Разрешённые origin выводятся из порта, на котором сервер реально слушает
    # (порт пробрасывается из cmd_dashboard, а не захардкожен здесь).
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    # За reverse-proxy (Caddy, этап 3) браузер шлёт Origin публичного домена —
    # без него все POST с https://cf.example.com ловили бы 403.
    if public_origin:
        allowed_origins.add(str(public_origin).rstrip("/"))
    allowed_referer_prefixes = tuple(f"{a}/" for a in allowed_origins)
    # Исключений нет: любой браузерный form-POST обязан прийти со страницы
    # дашборда, иначе чужая страница дёргала бы платные запуски и решения.

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin is not None:
                # Только точное совпадение, не startswith: иначе пройдёт
                # http://127.0.0.1:8787.evil.example
                if origin not in allowed_origins:
                    return PlainTextResponse(
                        "Запрос отклонён: чужой Origin", status_code=403)
            elif referer is not None:
                if (referer not in allowed_origins
                        and not referer.startswith(allowed_referer_prefixes)):
                    return PlainTextResponse(
                        "Запрос отклонён: чужой Referer", status_code=403)
            elif _is_decision_route(request.url.path):
                # Этап 3б: РЕШЕНИЯ оператора (включение промпта темы, решение по
                # рецепту/теме, ревью сценария) обязаны приходить со страницы
                # дашборда. Ослабление «нет заголовков -> пропускаем» остаётся
                # только у маршрутов ЗАПУСКА: их дёргает cf-cycle.timer через
                # curl без Origin/Referer, и это единственный легальный
                # безголовый клиент. Решение же безголовым быть не может по
                # определению — за ним стоит человек у браузера.
                return PlainTextResponse(
                    "Запрос отклонён: решение оператора принимается только со "
                    "страницы дашборда", status_code=403)
            # Остальное без Origin и Referer -> пропускаем: современные браузеры
            # ВСЕГДА шлют Origin на кросс-сайтовый POST, поэтому запрос без обоих
            # заголовков — не-браузерный клиент (curl, планировщик, TestClient).
        return await call_next(request)

    if cache is None:
        cache = DataCache(sheets)

    def factory_state(request):
        """(metrics, gates) — ОДИН раз на запрос, дальше из request.state.

        Их спрашивают двое: сам «Обзор» (плитки, кольца, лента) и сайдбар
        («Уведомления», он есть на всех семи экранах). Без памятки на запросе
        каждая полная страница считала бы ворота дважды — а это чтение
        index.json, обход proposals/ и текстов промптов на покрытие."""
        cached = getattr(request.state, "cf_factory_state", None)
        if cached is None:
            try:
                metrics = overview_metrics(cache, days=OVERVIEW_DAYS, root=lab_root,
                                           weekly_target=weekly_target,
                                           count_demo=demo_as_published)
                error = None
            except Exception as exc:
                metrics, error = None, str(exc)
            cached = (metrics, pipeline_gates(metrics), error)
            request.state.cf_factory_state = cached
        return cached

    def sidebar_notifications(request):
        """Строки «Уведомлений» — производная от состояния завода, не журнал.

        Функция, а не готовый список: сайдбар есть только у полных страниц, а
        context_processors выполняются и для партиалов. Считать ворота каждые
        2 секунды ради блока, которого в ответе нет, — впустую."""
        _metrics, gates, _error = factory_state(request)
        return notifications(gates, stale=cache.stale)

    def nav_badges(request):
        """Счётчик pending-брифов в сайдбаре; сбой Sheets бейдж не рисует."""
        try:
            pending = sum(1 for b in cache.rows("briefs")
                          if brief_status(b) == "pending")
        except Exception:
            pending = 0
        return {"nav_briefs_pending": pending,
                "notifications": lambda: sidebar_notifications(request),
                # Текст один на тост и на строку уведомлений: два источника
                # одной фразы — два места, где её потом поправят по-разному.
                "stale_text": STALE_TEXT}

    templates = Jinja2Templates(directory=BASE_DIR / "templates",
                                context_processors=[nav_badges])
    # Прод-осторожность (инцидент 2026-07-25): рабочий каталог сервиса — он же
    # репозиторий. Jinja по умолчанию перечитывает шаблоны с диска, а Python-модуль
    # остаётся тем, с которым процесс стартовал: правка шаблона + нового фильтра
    # роняла ЖИВОЙ дашборд в 500 («No filter named …») каждые 2 секунды поллинга,
    # пока оператор не рестартанёт. Пинуем разметку к процессу — код и шаблоны
    # всегда из одного состояния, а деплой остаётся «git pull + рестарт».
    templates.env.auto_reload = False
    # §2 UX-спеки: единая точка перевода значений из данных в язык продюсера.
    templates.env.globals["report_view"] = report_view
    # Реестр аккаунтов завода (UTM-контур): формам публикации предлагаются только
    # включённые слоты — active: false это заготовка, а не вариант выбора, и «все
    # выключены» для форм равно пустому реестру (они показывают причину). Глобал,
    # а не поле контекста: форма живёт в двух шаблонах и четырёх точках рендера,
    # включая фолбэк-контексты ошибок, — проброс по одному терял бы реестр.
    active_accounts = [dict(a) for a in (accounts or ()) if a.get("active")]
    account_slugs = {str(a.get("slug")) for a in active_accounts}
    templates.env.globals["accounts"] = active_accounts
    templates.env.filters.update(template_filters())
    app.state.sheets = sheets
    app.state.templates = templates
    app.state.cache = cache
    app.state.health = health
    lab_root = Path(lab_root or ".")
    decision_git = decisions._default_git(lab_root)  # единственная точка git для кнопок решений

    def configured_stages():
        """Звенья с настроенным раннером — только у них рисуется ▶ (P4.1).

        Настроенным считается звено, чей kind в STAGES самодостаточен — "fanout"
        (конвейер claude) или "cli" (subprocess `python -m cf`, C3.2: raw/stats
        без вебхука) — ЛИБО у которого задан вебхук dashboard.workflows (n8n;
        после C3.2 так идёт только publish). Ключи ВЫВОДЯТСЯ из STAGES, а не
        хардкодятся: новое звено под другим ключом не потеряет ▶ молча. publish
        без вебхука runnable, но ▶ была бы мёртвой кнопкой, всегда падающей
        «webhook не настроен», поэтому у него рендерится неинтерактивная →."""
        workflows = (runner.config.get("dashboard", {}).get("workflows", {})
                     if runner is not None else {}) or {}
        self_run = {s for s, spec in STAGES.items()
                    if spec.get("kind") in ("fanout", "cli")}
        return self_run | set(workflows)

    def pipeline_gates(metrics):
        """Ворота оператора для ленты и баннера — единый предикат из queues.py.

        Сбой чтения (формулы/Sheets) → None, а не пустые ворота: строка ленты
        покажет «—», а не успокоительное «всё разобрано» по непрочитанным данным
        (правило №2)."""
        try:
            return build_gates(lab_root, cache.rows("prompt_versions"), metrics)
        except Exception:
            logger.warning("ворота не посчитаны", exc_info=True)
            return None

    def _autocontinue(reason):
        """Ворота закрыты решением оператора → производство сценариев едет дальше.

        Выбор оператора 2026-07-26: «ехать сразу» вместо кнопки «Продолжить».
        Цена выбора названа прямо — решение по рецепту/промпту НЕМЕДЛЕННО тратит
        деньги на вызовы агентов, поэтому здесь три страховки: выключатель в
        конфиге (dashboard.fanout.autocontinue), отказ при занятом конвейере (тот
        же мьютекс, что у ▶) и запуск ТОЛЬКО сценариев с ревью — без повторного
        платного сбора и анализа.
        """
        if runner is None:
            return
        try:
            runner.continue_after_gate(reason)
        except Exception:
            logger.warning("автопродолжение после ворот не запущено", exc_info=True)

    def timeline_steps(metrics, gates=None):
        """Строки вертикальной ленты конвейера (Фаза 2б): производящие шаги — из
        per-run прогресса раннера, ворота — из queues.build_gates, съёмка и
        статистика — живой бэклог из metrics."""
        run_progress = runner.run_progress if runner is not None else {}
        now = runner.now() if runner is not None else None
        return build_timeline(run_progress, metrics, now, gates=gates)

    # Период «Обзора» — жёстко 30 дней (решение владельца 2026-07-28, элемент 3):
    # селектор 7/30/90 менял ровно одну плитку и один график, а само число средней
    # вовлечённости считалось по всем замерам за всё время. Аналитика по периодам
    # живёт на «Результатах». Параметр ?days= маршрут больше не читает вовсе —
    # мусор в нём страницу не роняет по построению.
    OVERVIEW_DAYS = 30

    @app.get("/")
    def root():
        # Стартовый экран — сценарии, а не внутренности завода (решение владельца
        # 2026-07-26). Конечный пользователь дашборда — SMM-продюсер: он эксперт по
        # контенту и судит сценарий, а рецепты, промпты и покрытие завод решает сам.
        # «Обзор» остался на расстоянии одного клика в навигации.
        return RedirectResponse("/briefs")

    def _overview_context(request):
        # Метрики и ворота — из памятки запроса: их же читает сайдбар.
        metrics, gates, error = factory_state(request)
        # Кусочки сценариев (2026-07-28): своя защита от сбоя Sheets. Метрики
        # уже пойманы factory_state, и падение соседнего блока не должно
        # ронять весь экран — плитки с ошибкой чтения показывают свой баннер.
        try:
            peek = brief_peek(cache, root=lab_root, count_demo=demo_as_published)
        except Exception:
            peek = []
        return {
            "title": "Обзор", "active": "overview",
            "metrics": metrics, "error": error,
            # Геометрия полуколец «Темпа недели» считается в labels.tempo_rings —
            # там же живёт кламп «значение выше цели не рисует дугу за габарит».
            "rings": tempo_rings(metrics),
            "ring_path": ring_path, "ring_viewbox": RING_VIEWBOX,
            "ring_stroke": RING_STROKE,
            "brief_peek": peek,
            "stale": cache.stale, "runner": runner, "health": health,
            "configured": configured_stages(),  # overview включает partials/stages.html
            "steps": timeline_steps(metrics if error is None else None, gates),
            # Красная полоса НАД «Конвейером»: ответ на «что от меня требуется»
            # должен читаться до прокрутки и до чтения двенадцати строк ленты.
            "gate_banner": gate_banner(gates or {}),
        }

    @app.get("/overview")
    def overview(request: Request):
        return templates.TemplateResponse(request, "overview.html",
                                          _overview_context(request))

    # Маршрута /refresh больше нет (решение владельца 2026-07-28, С3): кнопка
    # «Обновить данные» освободила место под «Уведомления», а принудительный
    # сброс кеша повторяется обычным F5 — вкладки и так протухают за 60 и 180
    # секунд (DataCache). Сам DataCache.refresh остался: им пользуется запуск
    # звена, чтобы лента показала свежие счётчики сразу после прогона.

    def _briefs_context(request, status_filter, selected_id, sort=DEFAULT_BRIEF_SORT):
        briefs = [dict(b) for b in cache.rows("briefs")]
        # Ниша не хранится в листе briefs — выводим из formula_id по index.json
        # утверждённых формул (одно чтение файла на построение списка).
        niche_map = approved_niche_map(lab_root)
        # Третье состояние «вышел»: approved-бриф, у которого уже есть НАСТОЯЩИЙ
        # рил — отличаем «одобрен, ждёт съёмки» от «опубликован». Предикат один на
        # весь дашборд (data.shot_brief_ids): фиктивные рилы демо-петли стоят на
        # реальных сценариях, и сырой join объявлял снятым то, что не снимали.
        # Рилы читаем один раз на построение списка.
        reels = cache.rows("reels")
        published = shot_brief_ids(reels, demo_as_published)
        for b in briefs:
            b["review_status"] = brief_status(b)
            b["niche"] = brief_niche(b, niche_map)
            b["published"] = (b["review_status"] == "approved"
                              and str(b.get("brief_id")) in published)
        counts = {"all": len(briefs)}
        for s in BRIEF_STATUSES:
            counts[s] = sum(1 for b in briefs if b["review_status"] == s)
        shown = [b for b in briefs
                 if status_filter == "all" or b["review_status"] == status_filter]
        # Свежие сверху (решение владельца 2026-07-28): до этого очередь шла в
        # порядке листа, то есть от самых старых, и продюсер каждый раз мотал её
        # вниз. Направление переключается заголовком колонки «Дата».
        shown = sort_briefs_by_date(shown, newest_first=(sort != "date-asc"))
        # Явный выбор — только по НЕпустому id: с пустым selected_id совпадение
        # str(brief_id)=="" выбрало бы бриф с пустым brief_id вместо первого pending.
        selected = None
        if selected_id:
            selected = next(
                (b for b in shown if str(b.get("brief_id")) == selected_id), None)
        if selected is None:
            selected = next((b for b in shown if b["review_status"] == "pending"), None) \
                or (shown[0] if shown else None)
        references = []
        if selected:
            references = [u.strip() for u in
                          re.split(r"[;\s]+", str(selected.get("references") or ""))
                          if u.strip()]
        origin = brief_origin(selected, references, root=lab_root)
        # Рилы выбранного брифа — для карточки состояния «опубликован» (ссылка + дата).
        selected_reels = []
        if selected:
            selected_reels = [r for r in reels
                              if str(r.get("brief_id")) == str(selected.get("brief_id"))]
        return {"briefs": shown, "counts": counts, "selected": selected,
                "status_filter": status_filter, "sort": sort,
                "references": references,
                "origin": origin, "selected_reels": selected_reels,
                # P5.12: коды причин reject для дропдауна в форме ревью (partials/briefs_review.html).
                "reason_codes": REASON_CODES}

    @app.get("/briefs")
    def briefs(request: Request, status: str = "all", id: str = "",
               sort: str = DEFAULT_BRIEF_SORT):
        if status != "all" and status not in BRIEF_STATUSES:
            status = "all"
        # Мусор в параметре не роняет страницу и не оставляет очередь в
        # непонятном порядке — молча возвращаемся к порядку по умолчанию.
        if sort not in BRIEF_SORTS:
            sort = DEFAULT_BRIEF_SORT
        try:
            ctx = _briefs_context(request, status, id, sort)
            error = None
        except Exception as exc:
            ctx, error = {"briefs": [], "counts": {}, "selected": None,
                          "status_filter": status, "sort": sort, "references": [],
                          "origin": None, "selected_reels": []}, str(exc)
        return templates.TemplateResponse(request, "briefs.html", {
            "title": "Сценарии", "active": "briefs", "stale": cache.stale,
            "error": error, "health": health, **ctx})

    def _shooting_list_context():
        """Одобренные брифы без опубликованного рила, сгруппированные по нише (P5.4).

        Фильтр «approved и нет в reels»: одобрен ревьюером И brief_id ещё не встречается
        в reels (тот же join, что у плитки «Ждут назначения» — data.shot_brief_ids).
        Референсы разбиваются как в _briefs_context; ниша выводится через
        brief_niche. Данные — из тех же вкладок briefs/reels, что и очередь брифов."""
        briefs = [dict(b) for b in cache.rows("briefs")]
        niche_map = approved_niche_map(lab_root)
        published = shot_brief_ids(cache.rows("reels"), demo_as_published)
        shooting = []
        for b in briefs:
            if brief_status(b) != "approved":
                continue
            if str(b.get("brief_id")) in published:
                continue
            b["niche"] = brief_niche(b, niche_map)
            b["reference_urls"] = [u.strip() for u in
                                   re.split(r"[;\s]+", str(b.get("references") or ""))
                                   if u.strip()]
            # Метка исполнителя: назначенный сценарий из очереди НЕ уходит, он
            # получает подпись «криэйтор-1, с 28.07» (решение владельца 28.07).
            b["assignment"] = assignment_label(b)
            shooting.append(b)
        groups = {}
        for b in shooting:
            groups.setdefault(b["niche"] or "—", []).append(b)
        ordered = sorted(groups.items(), key=lambda kv: kv[0])
        # Сценарии под площадку, где нет активного аккаунта: снять можно, выложить
        # некуда. Предупреждение, не ворота — решение владельца 2026-08-04.
        from cf.dashboard.queues import (briefs_for_inactive_platforms,
                                         inactive_platform_warning)
        platform_warning = inactive_platform_warning(
            briefs_for_inactive_platforms(shooting, accounts))
        return {"groups": ordered, "total": len(shooting),
                "platform_warning": platform_warning,
                "creator_slots": list(creator_slots)}

    def _shooting_list_md(groups):
        """Тот же контент, что печатная страница, но текстовым markdown для скачивания."""
        lines = ["# Очередь съёмки", ""]
        for niche, group in groups:
            lines.append(f"## {niche or '—'}")
            lines.append("")
            for b in group:
                lines.append(f"### {b.get('hook') or b.get('brief_id') or '—'}")
                lines.append(f"- ID: {b.get('brief_id') or '—'}")
                lines.append(f"- Ниша: {b.get('niche') or '—'}")
                lines.append(f"- Формула: {b.get('formula_id') or '—'}")
                # Выгрузку раздают криэйторам — без исполнителя список снова
                # становится «разберитесь сами», ради чего назначение и делалось.
                lines.append(f"- Исполнитель: {b.get('assignment') or 'не назначен'}")
                lines.append("")
                lines.append("**Скрипт:**")
                lines.append("")
                lines.append(str(b.get("script") or ""))
                lines.append("")
                refs = b.get("reference_urls") or []
                if refs:
                    lines.append("**Референсы:**")
                    for u in refs:
                        lines.append(f"- {u}")
                    lines.append("")
        return "\n".join(lines)

    def _shooting_list_render_ctx(warning=None):
        try:
            ctx, error = _shooting_list_context(), None
        except Exception as exc:
            ctx = {"groups": [], "total": 0, "platform_warning": "",
                   "creator_slots": list(creator_slots)}
            error = str(exc)
        return {"title": "Очередь съёмки", "active": "shooting", "stale": cache.stale,
                "error": error, "health": health, "runlog_warning": warning, **ctx}

    @app.get("/briefs/shooting-list")
    def shooting_list(request: Request, format: str = ""):
        ctx = _shooting_list_render_ctx()
        if format == "md":
            body = _shooting_list_md(ctx["groups"])
            return PlainTextResponse(body, media_type="text/markdown; charset=utf-8",
                                     headers={"Content-Disposition":
                                              'attachment; filename="shooting-list.md"'})
        return templates.TemplateResponse(request, "shooting-list.html", ctx)

    @app.post("/briefs/{brief_id}/assign")
    def post_assign(request: Request, brief_id: str, slot: str = Form(""),
                    unassign: str = Form("")):
        """Отдать сценарий в работу слоту исполнителя (или снять назначение).

        Слот — значение из cf.config.json → dashboard.creator_slots. Чужое
        значение отбивается 422: список слотов правится в одном месте, и
        произвольная строка из формы обошла бы это правило."""
        slot = "" if unassign else str(slot or "").strip()
        if slot and slot not in creator_slots:
            raise HTTPException(422, f"неизвестный слот исполнителя: {slot!r}")
        try:
            result = assign_creator(sheets, brief_id, slot)
        except UnknownFieldsError as exc:
            # fail-loud: назначение НЕ сохранено, в листе сценариев нет колонок.
            raise HTTPException(500, "Назначение не сохранено — в таблице сценариев "
                                     f"нет колонок исполнителя: {exc}")
        if not result.found:
            raise HTTPException(404, f"сценарий {brief_id} не найден")
        cache.invalidate("briefs")
        cache.invalidate("run_log")
        if not result.logged:
            # Назначение применено, но следа в Run Log нет — говорим об этом, а не
            # прячем за немым редиректом (тот же приём, что у решения по сценарию).
            return templates.TemplateResponse(request, "shooting-list.html",
                _shooting_list_render_ctx(
                    "Назначение сохранено, но запись в Run Log не удалась "
                    "— следа этого решения в CF Run Log не появилось."))
        return RedirectResponse("/briefs/shooting-list", status_code=303)

    @app.post("/briefs/{brief_id}/review")
    def post_review(request: Request, brief_id: str, decision: str = Form(...),
                    notes: str = Form(""), reason_code: str = Form(""),
                    sort: str = Form(DEFAULT_BRIEF_SORT)):
        if sort not in BRIEF_SORTS:
            sort = DEFAULT_BRIEF_SORT
        if decision not in VALID_DECISIONS:
            raise HTTPException(422, f"decision должен быть одним из: {', '.join(sorted(VALID_DECISIONS))}")
        # P3.5: убран бесполезный pre-write refresh — review_brief читает Sheets
        # напрямую, кеш ему не нужен, а полный сброс тут заставлял следующий /overview
        # заново тянуть все 5+ вкладок.
        try:
            # P5.12: reason_code из дропдауна reject — код причины в rejection_reason.
            result = review_brief(sheets, brief_id, decision, notes=notes,
                                  reason_code=reason_code)
        except UnknownFieldsError as exc:
            # fail-loud, но внятно: решение не сохранено, схема таблицы разошлась
            raise HTTPException(500, f"Решение не сохранено — схема таблицы briefs расходится: {exc}")
        if not result.found:
            raise HTTPException(404, f"бриф {brief_id} не найден")
        # P3.5: точечная инвалидация — решение меняет только briefs (статус) и run_log
        # (след ревью). Медленные raw/reels/performance остаются в кеше, чтобы
        # следующий /overview или HTMX-поллинг не перечитывал их зря.
        cache.invalidate("briefs")
        cache.invalidate("run_log")
        warning = None
        if not result.logged:
            # P2.14: решение применено в Sheets, но запись в CF Run Log сорвалась. Не
            # прячем это за немым редиректом (как runner._finish показывает сбой лога в
            # UI): показываем оператору предупреждением, что след в Run Log не появился,
            # хотя решение сохранено.
            warning = ("Решение сохранено, но запись в Run Log не удалась "
                       "— след этого решения в CF Run Log не появился.")
        # P5.5: очередь внимания после решения — следующий pending выбирается сам
        # (пустой id -> первый pending в _briefs_context). Инвалидация выше точечная
        # (briefs+run_log), медленные вкладки остаются в кеше, партиал их не перечитывает.
        # Порядок очереди переживает решение: сортировка приезжает скрытым полем
        # формы, иначе после каждого одобрения список прыгал бы к умолчанию.
        try:
            ctx = _briefs_context(request, "pending", "", sort)
        except Exception:  # noqa: BLE001 — решение уже в Sheets, падать 500 нельзя
            # Ревью 14.09.2026: после invalidate запасной копии в кеше нет, и сбой
            # перечитывания давал 500 — htmx не свопает 5xx, карточка «не менялась»,
            # и продюсер жал кнопку второй раз поверх сохранённого решения.
            note = "Решение сохранено, список обновить не удалось — обновите страницу."
            if request.headers.get("hx-request"):
                return HTMLResponse(
                    f'<div id="briefs-review" class="banner warn" role="status">'
                    f'{html.escape(note)}</div>')
            return RedirectResponse(f"/briefs?status=pending&sort={sort}", status_code=303)
        if request.headers.get("hx-request"):
            # htmx-инлайн: вместо полного редиректа+перерисовки страницы отдаём партиал
            # очереди (список + карточка) для свопа на месте (#briefs-review, outerHTML).
            return templates.TemplateResponse(request, "partials/briefs_review.html", {
                "stale": cache.stale, "health": health, "error": None,
                "runlog_warning": warning, **ctx})
        # не-htmx (JS отключён) — прогрессивное улучшение: прежнее поведение. Полный
        # рендер с предупреждением при сбое Run Log, иначе редирект в очередь pending.
        if warning:
            return templates.TemplateResponse(request, "briefs.html", {
                "title": "Сценарии", "active": "briefs", "stale": cache.stale,
                "error": None, "health": health, "runlog_warning": warning, **ctx})
        return RedirectResponse(f"/briefs?status=pending&sort={sort}", status_code=303)

    @app.post("/briefs/{brief_id}/published")
    def post_published(request: Request, brief_id: str, url: str = Form(...),
                       notes: str = Form(""), account: str = Form(""),
                       return_to: str = Form("")):
        # Та же логика, что у `cf mark-published` (cf.publish.mark_published) — CLI и
        # дашборд не расходятся. same-origin form проходит CSRF-middleware.
        account = str(account or "").strip()
        # Как слот исполнителя в post_assign: реестр правится в одном месте
        # (cf.config.json → accounts), произвольная строка формы его не обходит.
        # Пустой account легален — публикация без привязки, как до UTM-контура.
        if account and account not in account_slugs:
            raise HTTPException(
                422, f"неизвестный или выключенный аккаунт: {account!r}")
        try:
            mark_published(sheets, brief_id, url, notes=notes, account=account,
                           trigger_type="dashboard")
        except MarkPublishedError as exc:
            # неизвестный brief_id или URL не http(s) — внятная 422, ничего не записано
            raise HTTPException(422, str(exc))
        except UnknownFieldsError as exc:
            raise HTTPException(500, f"Рил не сохранён — схема таблицы reels расходится: {exc}")
        # Публикация меняет reels (новый рил) + run_log (след); briefs не трогаем —
        # состояние «опубликован» выводится из reels. Точечно инвалидируем оба.
        cache.invalidate("reels")
        cache.invalidate("run_log")
        # Возврат туда, откуда пришли (фиксированная карта, не URL из формы —
        # open redirect не пролезает): очередь съёмки шлёт return_to=shooting,
        # карточка сценария — ничего, и редирект остаётся прежним.
        if return_to == "shooting":
            return RedirectResponse("/briefs/shooting-list", status_code=303)
        return RedirectResponse(f"/briefs?id={brief_id}", status_code=303)

    @app.post("/formulas/decision")
    def formula_decision(path: str = Form(...), decision: str = Form(...),
                         reason: str = Form("")):
        try:
            ok = decisions.decide_formula(lab_root, path, decision, reason=reason,
                                          git=decision_git, sheets=sheets)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not ok:
            raise HTTPException(404, f"формула {path} не найдена")
        if decision == "approved":
            # Ворота №1 могли закрыться: у темы появился первый утверждённый
            # рецепт, значит ей теперь можно писать промпт и дальше сценарии.
            _autocontinue(f"утверждён рецепт {path}")
        return RedirectResponse("/lab", status_code=303)

    @app.post("/niches/decision")
    def niche_decision(filename: str = Form(...), decision: str = Form(...)):
        try:
            ok = decisions.decide_niche(lab_root, filename, decision,
                                        git=decision_git, sheets=sheets)
        except json.JSONDecodeError as exc:
            # битая таксономия — НЕ 422-опечатка формы: файл-истина требует ручной
            # правки, решение по нише не применено (аудит M25)
            raise HTTPException(500, "prompts/agents/niche-taxonomy.json битый — "
                                     f"исправьте вручную: {exc}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if not ok:
            raise HTTPException(404, f"предложение {filename} не найдено")
        return RedirectResponse("/lab", status_code=303)

    def _prompt_gate_rows():
        """Ворота «Включение промпта темы» с полным текстом черновика для ревью.

        Текст читается ЗДЕСЬ, а не в ленте конвейера: partials/stages.html
        опрашивается htmx раз в 2 с, и 163 строки промпта на каждую тему и
        схлопывали бы раскрытый <details> при каждом свопе, и гоняли бы килобайты
        по кругу. Точка действия одна — эта карточка; лента и баннер ведут сюда.
        """
        try:
            gate = prompt_gate(lab_root, cache.rows("prompt_versions"))
        except Exception:
            logger.warning("ворота промпта темы не посчитаны", exc_info=True)
            return None
        rows = []
        for row in gate["rows"]:
            item = dict(row, prompt_text="", sha256="", draft_error="")
            if row["state"] == PROMPT_DRAFT and row["draft_name"]:
                try:
                    draft = read_draft(lab_root, row["draft_name"])
                    item["prompt_text"] = draft["prompt_text"]
                    item["sha256"] = draft["sha256"]
                except PromptApplyError as exc:
                    # Битый черновик не прячем: без объяснения кнопка просто
                    # исчезает и тема выглядит «застрявшей без причины».
                    item["draft_error"] = str(exc)
            rows.append(item)
        return rows

    def _lab_render_ctx():
        try:
            ctx, error = lab_context(cache, root=lab_root), None
        except Exception as exc:
            ctx, error = {"versions": None, "formulas": [], "patterns": [],
                          "proposals": [], "eval_report": None,
                          "formula_queue": [], "paused_formulas": [],
                          "niche_queue": [], "rituals": [],
                          "pending_source_proposals": []}, str(exc)
        ctx["prompt_gate"] = _prompt_gate_rows()
        # runner нужен /lab только для кнопок ▶ ритуалов (P5.7): без раннера
        # (тесты секций без него) кнопки просто не рисуются.
        return {"title": "Лаборатория", "active": "lab", "stale": cache.stale,
                "error": error, "health": health, "runner": runner, **ctx}

    @app.post("/prompts/write")
    def write_brief_prompt(niche: str = Form(...)):
        # Запуск агента brief-промпта по нише через runner — тем же путём, что
        # ритуалы. Пишет он proposal, а не промпт: активацию версии и коммит делает
        # оператор после ревью (правило №3). Отчёт идёт в «Отчёты этапов» на
        # /runs, туда и ведём — там он виден живым (поллинг активен).
        if runner is None:
            raise HTTPException(404, "раннер недоступен")
        try:
            runner.run_prompt_writer(niche)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return RedirectResponse("/runs", status_code=303)

    @app.post("/prompts/apply")
    def apply_brief_prompt(filename: str = Form(...), sha256: str = Form("")):
        """Ворота №2: включить промпт темы по черновику (решение оператора).

        sha256 приходит из формы — это отпечаток текста, который оператор ВИДЕЛ.
        Расхождение отдаёт 409, а не тихо применяет свежую редакцию черновика."""
        try:
            niche = apply_prompt(lab_root, filename, sha256=sha256 or None,
                                 git=decision_git, sheets=sheets)
        except PromptApplyError as exc:
            raise HTTPException(exc.status, str(exc))
        # Строка версии и след решения только что записаны: без сброса /lab до
        # 180 с (TTL prompt_versions) показывал «версия не включена» и советовал
        # команду, которая ставит вторую строку v1 (ревью 14.09.2026).
        cache.invalidate("prompt_versions")
        cache.invalidate("run_log")
        # Ворота закрыты -> производство сценариев может ехать дальше (этап 6).
        _autocontinue(f"включён промпт темы {niche}")
        return RedirectResponse("/lab", status_code=303)

    @app.post("/prompts/reject")
    def reject_brief_prompt(filename: str = Form(...), reason: str = Form("")):
        try:
            reject_draft(lab_root, filename, reason=reason.strip(),
                         git=decision_git, sheets=sheets)
        except PromptApplyError as exc:
            raise HTTPException(exc.status, str(exc))
        return RedirectResponse("/lab", status_code=303)

    @app.get("/lab")
    def lab(request: Request):
        return templates.TemplateResponse(request, "lab.html", _lab_render_ctx())

    def _runs_context(reply_notice=None):
        """Контекст «Истории задач»: журнал + отчёты этапов с формой ответа агенту.

        Отчёты переехали сюда с «Обзора» 2026-07-28 (решение владельца, элемент
        19): всё про прогоны собирается на одном экране, а канал ответа агенту
        (`runner.reply`) — единственный, и он должен быть там же, где отчёт, на
        который отвечают. Сбой чтения Run Log отчёты не уносит: `runner` кладётся
        в контекст независимо от `error`."""
        try:
            ctx, error = runs_context(cache), None
        except Exception as exc:
            ctx, error = {"runs": [], "total": 0}, str(exc)
        return {"title": "История задач", "active": "runs", "stale": cache.stale,
                "error": error, "health": health, "runner": runner,
                "reply_notice": reply_notice, **ctx}

    @app.get("/runs")
    def runs(request: Request):
        return templates.TemplateResponse(request, "runs.html", _runs_context())

    # ── Страница «Деньги» (UTM-контур, тикет 04) ─────────────────────────────
    # Общая правда владельца и продюсера: расчётный лист месяца, переходы по
    # аккаунтам, реестр заказов (партиал тикета 03) и ссылки для bio. Ставки —
    # только из cf.config.json → payout, расчёт — чистая функция cf.payout.

    def _money_render_ctx(month="", warning=None):
        try:
            ctx, error = money_context(cache, list(accounts or ()), payout,
                                       site_base_url, month,
                                       datetime.now(timezone.utc)), None
        except Exception as exc:
            ctx, error = {"month": month, "months": [], "sheet": None,
                          "transitions": [], "per_reel": None,
                          "links": [], "links_reason": "",
                          "reg_empty": not accounts, "lower_bound_note": "",
                          "orders": [], "counts": {},
                          "status_labels": ORDER_STATUS_LABELS,
                          "decision_buttons": ORDER_DECISION_BUTTONS}, str(exc)
        return {"title": "Деньги", "active": "money", "stale": cache.stale,
                "error": error, "health": health, "runlog_warning": warning,
                **ctx}

    @app.get("/money")
    def money(request: Request, month: str = "", format: str = ""):
        ctx = _money_render_ctx(month=month)
        if format == "md" and not ctx["error"] and ctx["sheet"] is not None:
            # Тот же лист markdown'ом — переслать продюсеру как есть (образец
            # жанра — выгрузка очереди съёмки выше).
            body = sheet_to_md(ctx["sheet"])
            return PlainTextResponse(
                body, media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition":
                         f'attachment; filename="money-{ctx["month"]}.md"'})
        return templates.TemplateResponse(request, "money.html", ctx)

    @app.get("/orders")
    def orders_page():
        # Временная страница тикета 03 переехала сюда целиком — старые ссылки
        # не должны падать в 404.
        return RedirectResponse("/money")

    @app.post("/orders/manual")
    def post_manual_order(request: Request, order_date: str = Form(...),
                          revenue: str = Form(...), account: str = Form(...),
                          notes: str = Form("")):
        """Ручной заказ: e-commerce событие не дошло до Метрики, а 10% продюсера
        с продажи всё равно считаются. Сразу confirmed — владелец вносит то,
        что уже проверил руками."""
        account = str(account or "").strip()
        # Как аккаунт публикации в post_published: реестр правится в одном месте
        # (cf.config.json → accounts), произвольная строка формы его не обходит.
        # Пустого варианта нет: ручной заказ без аккаунта не привязать к
        # расчётному листу, ради которого реестр и заведён.
        if account not in account_slugs:
            raise HTTPException(
                422, f"неизвестный или выключенный аккаунт: {account!r}")
        try:
            result = add_manual_order(sheets, order_date, revenue, account,
                                      notes=notes)
        except ValueError as exc:
            # битая дата / сумма не больше нуля — внятная 422, ничего не записано
            raise HTTPException(422, str(exc))
        except UnknownFieldsError as exc:
            raise HTTPException(500, "Заказ не сохранён — схема таблицы заказов "
                                     f"расходится: {exc}")
        cache.invalidate("orders")
        cache.invalidate("run_log")
        if not result.logged:
            # Заказ уже в Sheets — сбой лога не прячем за немым редиректом (тот
            # же приём, что у решения по сценарию).
            return templates.TemplateResponse(
                request, "money.html", _money_render_ctx(warning=(
                    "Заказ сохранён, но запись в Run Log не удалась — следа "
                    "этого решения в CF Run Log не появилось.")))
        return RedirectResponse("/money", status_code=303)

    @app.post("/orders/{order_id}/status")
    def post_order_status(request: Request, order_id: str,
                          status: str = Form(...)):
        """Решение по заказу — только человек: сборщик решённые строки не трогает,
        так что кнопка здесь — единственный путь смены статуса."""
        if status not in VALID_ORDER_STATUSES:
            raise HTTPException(422, "status должен быть одним из: "
                                     f"{', '.join(VALID_ORDER_STATUSES)}")
        try:
            result = set_order_status(sheets, order_id, status)
        except UnknownFieldsError as exc:
            # fail-loud: решение о деньгах НЕ сохранено, в листе нет колонок.
            raise HTTPException(500, "Решение не сохранено — схема таблицы "
                                     f"заказов расходится: {exc}")
        if not result.found:
            raise HTTPException(404, f"заказ {order_id} не найден")
        cache.invalidate("orders")
        cache.invalidate("run_log")
        if not result.logged:
            return templates.TemplateResponse(
                request, "money.html", _money_render_ctx(warning=(
                    "Решение сохранено, но запись в Run Log не удалась — следа "
                    "этого решения в CF Run Log не появилось.")))
        return RedirectResponse("/money", status_code=303)

    @app.get("/performance")
    def performance(request: Request, tab: str = "reels"):
        try:
            ctx, error = performance_context(cache, tab), None
        except Exception as exc:
            ctx, error = {"tab": "reels", "counts": {}, "rows": []}, str(exc)
        return templates.TemplateResponse(request, "performance.html", {
            "title": "Результаты", "active": "performance", "stale": cache.stale,
            "error": error, "health": health, **ctx})

    @app.get("/sources")
    def sources(request: Request, tab: str = "tiktok", sort: str = "date"):
        try:
            ctx, error = sources_context(cache, tab, sort), None
        except Exception as exc:
            ctx, error = {"tab": "tiktok", "counts": {}, "rows": [], "total": 0,
                          "sort": "date", "has_hooks": False}, str(exc)
        return templates.TemplateResponse(request, "sources.html", {
            "title": "Референсы", "active": "sources", "stale": cache.stale,
            "error": error, "health": health, **ctx})

    def stages_context(request):
        error = None
        try:
            metrics = overview_metrics(cache, root=lab_root,
                                       weekly_target=weekly_target,
                                       count_demo=demo_as_published)
        except Exception as exc:
            error = exc
            metrics = {"raw_total": "—", "briefs_created": "—", "briefs_pending": "—",
                       "reels_published": "—", "awaiting_stats": "—"}
        gates = pipeline_gates(metrics)
        # Как на «Обзоре»: строки-прочерки в ленту не пускаем — build_timeline считает
        # по ним склонения через int() и отдавал 500, пока Sheets лежит (ревью 14.09.2026).
        return {"request": request, "metrics": metrics, "runner": runner,
                "configured": configured_stages(),
                "steps": timeline_steps(metrics if error is None else None, gates)}

    @app.get("/partials/stages")
    def stages_partial(request: Request):
        return templates.TemplateResponse(request, "partials/stages.html",
                                          stages_context(request))

    def reports_context(request):
        return {"request": request, "runner": runner}

    @app.get("/partials/reports")
    def reports_partial(request: Request):
        return templates.TemplateResponse(request, "partials/reports.html",
                                          reports_context(request))

    @app.get("/partials/health")
    def health_partial(request: Request):
        return templates.TemplateResponse(request, "partials/health.html",
                                          {"request": request, "health": health})

    @app.post("/stages/{stage}/run")
    def run_stage(stage: str, request: Request):
        if runner is None or stage not in runner.state:
            raise HTTPException(404, f"звено «{stage}» не найдено")
        runner.start(stage)
        cache.refresh()
        return templates.TemplateResponse(request, "partials/stages.html",
                                          stages_context(request))

    @app.post("/stages/{stage}/reply")
    def reply_stage(request: Request, stage: str, session_id: str = Form(...),
                    answer: str = Form(...)):
        if runner is None or stage not in runner.state:
            raise HTTPException(404, f"звено «{stage}» не найдено")
        session_id = session_id.strip()
        answer = answer.strip()
        if not session_id or not answer:
            raise HTTPException(422, "session_id и answer обязательны")
        try:
            replied = runner.reply(stage, session_id, answer)
        except ValueError as exc:   # M22: session_id не прошёл формат-гард
            raise HTTPException(422, str(exc))
        if not replied:
            # Звено занято (running / захвачено другим процессом) — ответ НЕ ушёл.
            # Не теряем его молча: рендерим «Историю задач» с заметкой и текстом
            # обратно в форму — там же, где живут сами отчёты (элемент 19).
            name = STAGE_NAMES.get(stage, stage)
            return templates.TemplateResponse(request, "runs.html", _runs_context(
                reply_notice={
                    "stage": stage, "session_id": session_id, "answer": answer,
                    "message": (f"Этап «{name}» сейчас занят — ответ не отправлен. "
                                "Отправьте его ещё раз, когда этап освободится."),
                }))
        return RedirectResponse("/runs", status_code=303)

    @app.post("/rituals/{ritual}/run")
    def run_ritual(ritual: str):
        # P5.7: запуск недельного ритуала (cf-eval / cf-tune-sources) через runner —
        # отчёт идёт в «Отчёты этапов» на /runs, туда и ведём оператора, чтобы он
        # сразу увидел живой отчёт (поллинг активен, пока ритуал выполняется).
        if runner is None or ritual not in runner.ritual_state:
            raise HTTPException(404, f"ритуал «{ritual}» не найден")
        runner.run_ritual(ritual)
        return RedirectResponse("/runs", status_code=303)

    @app.post("/cycle/run")
    def run_cycle(request: Request):
        # start_cycle() возвращает False, если конвейер занят. Отказ не немой:
        # браузерная форма (несёт Origin/Referer — та же эвристика, что CSRF-гард)
        # получает 303 + cycle_note на обзоре; headless-клиент (systemd curl -fsS,
        # заголовков нет) — 409, чтобы юнит стал failed и пропуск утреннего цикла
        # был виден оператору, а не маскировался зелёным 303 (аудит M13).
        if runner is not None and not runner.start_cycle():
            if not (request.headers.get("origin") or request.headers.get("referer")):
                return PlainTextResponse("цикл не запущен: конвейер занят",
                                         status_code=409)
            runner.cycle_note = "цикл не запущен: звено уже выполняется"
        return RedirectResponse("/overview", status_code=303)

    return app


def build_production_app(sheets=None, config=None, health_autostart=True,
                         port=8787):
    from cf.config import accounts_from_config, load_config
    from cf.dashboard.health import HealthMonitor, default_checks
    from cf.dashboard.runner import (PROGRESS_STATE_PATH, REPORTS_STATE_PATH,
                                     StageRunner)

    from cf.sheets import Sheets
    if sheets is None:
        sheets = Sheets()
    config = config or load_config()
    cache = DataCache(sheets)
    # M24: у раннера СВОЙ gspread-клиент — requests.Session не потокобезопасна,
    # а фоновые потоки фан-аута делили её с threadpool-хендлерами запросов
    # (та же причина, по которой health.py заводит отдельный инстанс).
    # reports_path только у боевого раннера: «Отчёты звеньев» переживают рестарт
    # (после рестарта 2026-07-24 17:10 пропали единственные следы блокировки
    # claude-звеньев — отчёты жили лишь в памяти процесса).
    runner = StageRunner(Sheets(config=sheets.config), config,
                         reports_path=REPORTS_STATE_PATH,
                         progress_path=PROGRESS_STATE_PATH)
    health = HealthMonitor(default_checks(sheets, config))
    if health_autostart:
        health.start()
    app = create_app(sheets=sheets, cache=cache, runner=runner, health=health,
                     port=port, weekly_target=weekly_target_from_config(config),
                     public_origin=(config.get("dashboard", {}) or {})
                     .get("public_origin", ""),
                     creator_slots=creator_slots_from_config(config),
                     accounts=accounts_from_config(config),
                     demo_as_published=demo_as_published_from_config(config),
                     payout=config.get("payout"),
                     site_base_url=(config.get("site", {}) or {})
                     .get("base_url", ""))
    # Прогрев только у боевого сервиса: он живёт часами в каталоге, который
    # одновременно является рабочим деревом репозитория (инцидент 2026-07-25 —
    # правка шаблона + нового фильтра уронила живой дашборд в 500). Компиляция
    # ПОСЛЕ create_app: фильтры уже зарегистрированы. В тестах не нужен — там
    # приложение живёт один запрос, а прогрев стоил бы ~280 мс на создание.
    _precompile_templates(app.state.templates.env)
    return app
