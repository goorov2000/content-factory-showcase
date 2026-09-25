"""Реестр источников сбора (sources/*.json) и шардер батчей — C1.7.

Замена SOURCES-блокам в JS: списки — данные, JS-эталоны заморожены до этапа 6
(parity держит tests/test_collect_sources.py). Шардинг — порт P5.14 из
Build-TikTok-Actor-Input.js / Normalize-Instagram-Hashtags.js: вход дробится
на батчи по <=batch_size источников, каждый уходит отдельным Apify-раном,
чтобы один медленный/битый батч не ронял весь сбор.
"""
import hashlib
import json
from pathlib import Path

from cf.io import write_json_atomic

# Поля акторов — значения из замороженных JS Build-файлов (не менять без эталона).
RESULTS_PER_PAGE = 20        # clockworks/tiktok-scraper (Build-TikTok-Actor-Input.js)
SEARCH_LIMIT_PER_QUERY = 4   # instagram-search-scraper, discovery (Build-Instagram-Search-Input.js)
RESULTS_LIMIT = 10           # instagram-hashtag-scraper (Build-Instagram-Search-Input.js)
BATCH_SIZE = 4               # источников на батч (Build-TikTok / Normalize-Instagram-Hashtags)
REEL_BATCH_SIZE = 6          # reel-URL на батч (Prepare-Instagram-Reel-Transcript-Input.js)

# Корень репо: src/cf/collect/sources.py -> три уровня вверх.
_ROOT = Path(__file__).resolve().parents[3]


def load_registry(platform, root=None):
    """Записи sources/<platform>.json: {query, kind, niche, status, origin, added_at}."""
    path = Path(root or _ROOT) / "sources" / (platform + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def active_sources(registry):
    """Активные источники в порядке реестра, раздельно по kind
    (соответствует SOURCES.hashtags / SOURCES.searchQueries в JS)."""
    active = [r for r in registry if r.get("status") == "active"]
    return {
        "hashtags": [r["query"] for r in active if r.get("kind") == "hashtag"],
        "search_queries": [r["query"] for r in active if r.get("kind") == "search"],
    }


def save_registry(platform, records, root=None):
    path = Path(root or _ROOT) / "sources" / (platform + ".json")
    write_json_atomic(path, records)


# Пороги exploration-контура (C4.1-C4.3). Переопределяются в cf.config.json:
# sources.exploration. Первичные значения — калибровать после месяца данных
# (решение плана). Дефолты равны прежним хардкодам: без ключей в конфиге
# поведение то же.
EXPLORATION_DEFAULTS = {
    "min_runs": 2,           # прогонов, прежде чем судить кандидата
    "retire_below_rows": 3,  # суммарно строк через гейт меньше -> retired
    "promote_min_rows": 8,   # строк через гейт от -> кандидат на повышение
    "er_factor": 0.8,        # median ER >= er_factor * медианы active-источников
    "window_days": 14,       # окно строк raw для сравнения ER
    "batch": BATCH_SIZE,     # кандидатов в exploration-батче за прогон
    "harvest_top_n": 5,      # новых кандидатов из капшенов за прогон
    "mature_share": 0.5,     # доля батча под дозревающих (см. exploration_pick)
    # Потолок очереди зондирования (Д4). 40 = ~20 прогонов ожидания: за прогон
    # уходит batch зондов, вердикт стоит min_runs зондов -> 2 вердикта за
    # прогон, а сбор идёт раз в сутки (cf-collect-*.timer). Больше — и вердикт
    # кандидату приходит через квартал, что уже неотличимо от «никогда».
    "pool_cap": 40,
}

# Отсутствие потолка выражаем числом, а не None: room участвует в min()/вычитании.
_NO_CAP = 10 ** 9


def _runs(record):
    return int(record.get("runs_count") or 0)


def exploration_cfg(config=None):
    """Пороги exploration из cf.config.json (sources.exploration) поверх дефолтов.

    Одно место сборки на пайплайн: раньше конфиг доезжал только до
    age_candidates, а pick/harvest жили на хардкоде — крутить контур из
    конфига было нечем (Д4)."""
    return {**EXPLORATION_DEFAULTS,
            **((config or {}).get("sources", {}).get("exploration") or {})}


def candidate_room(registry, cfg=None):
    """Сколько НОВЫХ кандидатов ещё влезает в очередь зондирования (Д4).

    Потолок pool_cap считает только кандидатов БЕЗ вердикта
    (runs_count < min_runs): длина именно этой очереди = время до вердикта
    (batch зондов за прогон, min_runs зондов на вердикт). Добравшие min_runs
    ждут решения оператора (promotable) или новых строк и очередь не удлиняют —
    иначе застрявшая пачка promotable заперла бы приток навсегда.
    Потолок только ТОРМОЗИТ приток: ничего уже добавленного он не трогает
    (ретайр необратим внутри контура — обратно в active только через
    proposal + cf apply-sources). pool_cap <= 0 — «без потолка»."""
    cfg = {**EXPLORATION_DEFAULTS, **(cfg or {})}
    cap = int(cfg["pool_cap"])
    if cap <= 0:
        return _NO_CAP
    queued = sum(1 for r in registry if r.get("status") == "candidate"
                 and _runs(r) < int(cfg["min_runs"]))
    return max(0, cap - queued)


def _rotation_key(record):
    """Порядок ротации: меньше прогонов -> дольше ждёт последнего зонда ->
    старее added_at -> алфавит. last_run_at держит FIFO внутри когорты
    дозревающих; у новичков он пуст, и ключ вырождается в прежний (added_at, query)."""
    return (_runs(record), str(record.get("last_run_at") or ""),
            str(record.get("added_at") or ""), str(record.get("query")))


def exploration_pick(registry, limit=None, kinds=None, cfg=None):
    """Кандидаты на exploration-прогон (спека §2.1): детерминированная ротация
    без LLM. После прогона счётчики растут (mark_explored) — следующий прогон
    сам выбирает следующую порцию.

    Д4 (26.07): прежний порядок «сначала наименьший runs_count» голодил
    дозревающих. Приток новичков (harvest_top_n=5 за прогон) больше батча
    (batch=4), поэтому когорта runs_count=0 не кончалась никогда, когорта
    runs_count=1 второго зонда не получала, порог min_runs=2 был недостижим:
    115 кандидатов, ноль с runs_count>=2, ноль вердиктов age_candidates с 23.07.
    Лечится не порогом, а очередью — часть батча резервируется дозревающим
    (1 <= runs_count < min_runs) в порядке FIFO по last_run_at.

    Доля квоты mature_share=1/2 и берётся с округлением вверх: при квоте q
    дозревающих прибывает batch-q за прогон (столько новичков зондируется),
    а уходит q — очередь дозревающих не растёт только при q >= batch/2.
    Половина — наименьшая доля, гарантирующая сходимость, и она же оставляет
    вторую половину батча на разведку новых тегов. Пустая когорта дозревающих
    отдаёт весь батч новичкам — холодный старт как был."""
    import math

    cfg = {**EXPLORATION_DEFAULTS, **(cfg or {})}
    limit = int(cfg["batch"] if limit is None else limit)
    min_runs = int(cfg["min_runs"])
    cands = [r for r in registry if r.get("status") == "candidate"
             and (kinds is None or r.get("kind") in kinds)]
    maturing = sorted((r for r in cands if 1 <= _runs(r) < min_runs),
                      key=_rotation_key)
    quota = min(len(maturing), math.ceil(limit * float(cfg["mature_share"])))
    picked = maturing[:quota]
    reserved = {id(r) for r in picked}
    # Остаток батча — прежним порядком: новички, потом недобравшие квоту
    # дозревающие, потом уже судимые (их черёд, когда очередь пуста).
    rest = sorted((r for r in cands if id(r) not in reserved), key=_rotation_key)
    return picked + rest[:max(0, limit - len(picked))]


def mark_explored(records, queries, when, rows_by_query=None):
    """Инкремент счётчиков прогона у записей с query из queries (мутирует records):
    runs_count += 1, last_run_at = when, rows_passed_gate += rows_by_query[query].
    Считается только УСПЕШНЫЙ ран — сбой батча сигнала кандидату не даёт."""
    qset = set(queries)
    for r in records:
        if r.get("query") in qset:
            r["runs_count"] = int(r.get("runs_count") or 0) + 1
            r["last_run_at"] = when
            n = (rows_by_query or {}).get(r["query"], 0)
            if n:
                r["rows_passed_gate"] = int(r.get("rows_passed_gate") or 0) + n
    return records


def age_candidates(registry, raw_rows, collected_at, cfg=None):
    """Взросление/отсев кандидатов с runs_count >= min_runs (спека §2.1, C4.3).

    (а) rows_passed_gate < retire_below_rows -> status=retired автоматически
    (мутирует registry); (б) rows_passed_gate >= promote_min_rows И median ER
    строк кандидата за окно >= er_factor * медианы active-источников -> запись
    в promotable (сам реестр НЕ меняется: перевод в active — только через
    proposal + approve оператора). Между порогами — ждёт следующих прогонов.
    Возвращает (retired, promotable); promotable несут цифры для evidence."""
    import statistics
    from cf.collect.util import _parse_date_string
    from datetime import timedelta

    cfg = {**EXPLORATION_DEFAULTS, **(cfg or {})}
    now = _parse_date_string(collected_at)
    cutoff = now - timedelta(days=cfg["window_days"]) if now else None

    def in_window(row):
        dt = _parse_date_string(str(row.get("collected_at") or ""))
        return dt is not None and cutoff is not None and dt >= cutoff

    window = [r for r in raw_rows if in_window(r)]
    ers_by_marker = {}
    for row in window:
        er = row.get("engagement_rate")
        if isinstance(er, (int, float)):
            ers_by_marker.setdefault(str(row.get("source_query") or ""), []).append(er)
    active_markers = {source_marker(r) for r in registry if r.get("status") == "active"}
    active_ers = [er for m in active_markers for er in ers_by_marker.get(m, [])]
    active_median = statistics.median(active_ers) if active_ers else 0

    retired, promotable = [], []
    today = str(collected_at)[:10]
    for rec in registry:
        if rec.get("status") != "candidate" \
                or int(rec.get("runs_count") or 0) < cfg["min_runs"]:
            continue
        rows_n = int(rec.get("rows_passed_gate") or 0)
        if rows_n < cfg["retire_below_rows"]:
            rec["status"] = "retired"
            rec["retired_at"] = today
            rec["retired_reason"] = (f"yield: rows_passed_gate={rows_n} "
                                     f"за {rec.get('runs_count')} прогонов")
            retired.append(rec)
            continue
        if rows_n < cfg["promote_min_rows"]:
            continue
        cand_ers = ers_by_marker.get(source_marker(rec), [])
        cand_median = statistics.median(cand_ers) if cand_ers else 0
        if cand_median and (not active_median
                            or cand_median >= cfg["er_factor"] * active_median):
            promotable.append({"record": dict(rec), "median_er": cand_median,
                               "active_median_er": active_median,
                               "rows_passed_gate": rows_n})
    return retired, promotable


def promote_composition(promotable):
    """Состав промоута — отсортированные маркеры источников.

    (разбор 2026-07-27) Тождество promote-proposal'а определяется составом, а не
    датой: человек решает «брать ли эти теги», и цифры ER, поехавшие за сутки на
    третьем знаке, того же решения не отменяют."""
    return sorted(source_marker(p["record"]) for p in promotable)


def promote_stem(platform, promotable, generated_at):
    """Имя пары файлов proposal'а: дата + платформа + отпечаток состава.

    (разбор 2026-07-27) Прежний stem состоял из ОДНОЙ даты, поэтому два разных
    решения за сутки писались в один путь и второе затирало первое. sha1 берётся
    от отсортированного состава — имя детерминировано (тесты не зависят от
    времени), но разный состав больше не делит файл.

    Отпечаток вставлен ПОСЛЕ даты, а не в хвост: суффикс
    '-sources-<платформа>-promote.json' — сложившееся имя, по которому файлы
    ищут глобом (tests/test_collect_aging.py) и глазами."""
    digest = hashlib.sha1(
        "\n".join(promote_composition(promotable)).encode("utf-8")).hexdigest()
    return f"{str(generated_at)[:10]}-{digest[:8]}-sources-{platform}-promote"


def _read_proposal(path):
    """proposal с диска или None: чужой битый JSON не должен ронять сбор."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_open_promote_proposal(proposals_dir, platform, promotable):
    """Уже лежащий proposal того же состава, по которому apply-sources НЕ отработал.

    (разбор 2026-07-27) Без этой сверки каждый прогон выписывал новый файл с тем
    же решением: на 27.07 в репозитории лежали 2026-07-26- и 2026-07-27-
    sources-tiktok-promote с байт-в-байт одинаковым составом (#mensfashion,
    #fashion), оба pending. Дашборд держал на каждый вечную карточку «ждёт
    применения», а `cf status` — строку про отсутствующий frontmatter.

    Признак «решение ещё не отработано» — отсутствие applied_at (его проставляет
    cf apply-sources, cli.py). Под него попадают и pending (ждёт человека), и
    rejected: отказ человека по ЭТОМУ составу помним, как autogate помнит отказ
    по рецепту (CLAUDE.md, журнал решений). Изменится состав — изменится и
    отпечаток, и proposal выпишется заново."""
    wanted = promote_composition(promotable)
    if not wanted:
        return None
    proposals_dir = Path(proposals_dir)
    if not proposals_dir.is_dir():
        return None
    for path in sorted(proposals_dir.glob("*.json")):
        data = _read_proposal(path)
        if data is None or data.get("platform") != platform:
            continue
        if str(data.get("applied_at", "")).strip():
            continue
        add = sorted(str(a.get("source", "")) for a in (data.get("add") or []))
        if add == wanted:
            return path
    return None


def save_promote_proposal(platform, promotable, generated_at, root=None):
    """Промоут-proposal + человекочитаемое обоснование рядом.

    Обоснование пишется тем же вызовом, а не «когда-нибудь потом»: правило №1
    требует, чтобы у любой правки была evidence, которую человек может прочитать,
    а тест репозитория (test_shipped_source_proposals_have_rationale_doc) держит
    это как инвариант. До 2026-07-27 контур эксплорейшна стоял (дефект Д4), и
    первый же живой promote-proposal этот инвариант нарушил — JSON был, .md не было.

    (разбор 2026-07-27) Ничего чужого не перезаписываем. Прежняя версия писала
    файл безусловно и со status:"pending", а cf apply-sources проставляет
    applied_at в ЭТОТ ЖЕ файл — второй прогон тех же суток возвращал применённый
    proposal обратно в pending, то есть стирал решение человека. Теперь:
    (а) файл с чужим решением (status != pending или applied_at) не трогаем;
    (б) двойник того же состава заново не выписываем. Возвращаем путь в обоих
    случаях — звенья сбора кладут его имя в сводку прогона, и «уже лежит вот
    этот» им сказать честнее, чем упасть на None."""
    root = Path(root or _ROOT)
    proposals = root / "proposals"
    twin = find_open_promote_proposal(proposals, platform, promotable)
    if twin is not None:
        return twin
    stem = promote_stem(platform, promotable, generated_at)
    path = proposals / f"{stem}.json"
    existing = _read_proposal(path)
    if existing is not None and (
            str(existing.get("status", "")).strip().lower() != "pending"
            or str(existing.get("applied_at", "")).strip()):
        # Тот же состав в тот же день, но решение по нему уже принято
        # (одобрено/применено/отклонено) — переиздание отменило бы его молча.
        return path
    proposals.mkdir(parents=True, exist_ok=True)
    doc_rel = f"proposals/{stem}.md"
    (root / doc_rel).write_text(
        build_promote_rationale(platform, promotable, generated_at, stem=stem),
        encoding="utf-8")
    write_json_atomic(path, build_promote_proposal(platform, promotable, generated_at,
                                                   rationale_doc=doc_rel))
    return path


def build_promote_proposal(platform, promotable, generated_at, rationale_doc=None):
    """source-proposal (schemas/source-proposal.schema.json) на перевод
    кандидатов в active; статус pending — ждёт approve оператора."""
    proposal = {
        "platform": platform,
        "generated_at": generated_at,
        "status": "pending",
        "remove": [],
        "add": [{
            "source": source_marker(p["record"]),
            "kind": "hashtag" if p["record"].get("kind") == "hashtag" else "query",
            "evidence": (f"exploration: rows_passed_gate={p['rows_passed_gate']} "
                         f"за {p['record'].get('runs_count')} прогонов, "
                         f"median_er={p['median_er']:.4f} против "
                         f"{p['active_median_er']:.4f} у active"),
        } for p in promotable],
    }
    if rationale_doc:
        proposal["rationale_doc"] = rationale_doc
    return proposal


def build_promote_rationale(platform, promotable, generated_at, stem=None):
    """Обоснование промоута языком оператора: что, откуда и почему проходит порог.

    (разбор 2026-07-27) Frontmatter обязателен и повторяет формат остальных
    source-proposal'ов репозитория (см. proposals/2026-07-26-sources-instagram.md):
    `cf status` считает .md-proposal'ы по ключу status и на файл без него печатает
    «не попал в сводку» — на каждый прогон, вечно. Словарь статусов .md
    (proposed/approved/rejected) НЕ совпадает со словарём JSON-схемы
    (pending/approved/rejected), поэтому здесь именно proposed (cli.py, cmd_status).
    stem связывает пару файлов: он несёт отпечаток состава, и без него ссылка на
    машиночитаемую часть указывала бы на несуществующее имя-по-дате."""
    day = str(generated_at)[:10]
    stem = stem or promote_stem(platform, promotable, generated_at)
    lines = [
        "---",
        "status: proposed",
        "kind: source-proposal",
        f"platform: {platform}",
        f"target: sources/{platform}.json",
        f"machine_readable: proposals/{stem}.json",
        f"created: {day}",
        "---",
        f"# Промоут источников {platform} — {day}", "",
        "Кандидаты, которые за время зондирования дали материал не хуже активных",
        "источников. Написано автоматически звеном сбора (`cf collect "
        f"{platform}`), применяется человеком:", "",
        "```bash",
        f".venv/bin/python -m cf apply-sources proposals/{stem}.json",
        "```", "",
        "| Источник | Строк через гейт | Прогонов | Медиана ER | Медиана ER активных |",
        "|---|---|---|---|---|",
    ]
    for p in promotable:
        lines.append(
            f"| `{source_marker(p['record'])}` | {p['rows_passed_gate']} | "
            f"{p['record'].get('runs_count')} | {p['median_er']:.4f} | "
            f"{p['active_median_er']:.4f} |")
    lines += [
        "", "## Как это посчитано", "",
        "Кандидат попадает сюда, когда его медиана ER не ниже `er_factor` от медианы",
        "активных источников площадки (`cf.config.json` → `sources.exploration`), а",
        "материала он дал не меньше `promote_min_rows` строк, прошедших гейт качества.",
        "Обратное решение — ретайр — считается тем же проходом и в этот файл не входит.",
        "",
    ]
    return "\n".join(lines)


# Generic-мусор, который не станет кандидатом (не ниша, а алгоритм-приманка).
STOP_TAGS = {"fyp", "fypage", "foryou", "foryoupage", "viral", "viralvideo",
             "trending", "reels", "reel", "video", "tiktok", "instagram",
             "explore", "рек", "врек", "хочуврек", "рекомендации"}


def harvest_candidates(registry, rows, today, top_n=None, min_er=0.05,
                       stop_tags=None, origin="harvest", cfg=None):
    """Детерминированная выжимка кандидатов из капшенов (спека §2.1, C4.2).

    Участвуют строки с сильным сигналом: ER >= min_er ИЛИ views >= p75 прогона.
    Минус уже известные (любой status, регистронезависимо) и стоп-лист; топ-N
    по частоте (тай-брейк — алфавит). Новые записи добавляются в registry
    (мутирует) и возвращаются; повторный вызов идемпотентен.

    Д4: приток режется потолком очереди (candidate_room) — иначе harvest
    доливает больше, чем прогон успевает зондировать, и очередь растёт быстрее,
    чем взрослеет."""
    import re as _re
    cfg = {**EXPLORATION_DEFAULTS, **(cfg or {})}
    top_n = int(cfg["harvest_top_n"] if top_n is None else top_n)
    room = candidate_room(registry, cfg)
    if room <= 0:
        return []
    top_n = min(top_n, room)
    stop = {t.lower() for t in (STOP_TAGS if stop_tags is None else stop_tags)}
    known = {str(r.get("query", "")).lower() for r in registry}
    views = sorted(v if isinstance(v, (int, float)) else 0
                   for v in (row.get("views") or 0 for row in rows))
    p75 = views[min(len(views) - 1, int(len(views) * 0.75))] if views else 0
    counts = {}
    for row in rows:
        er = row.get("engagement_rate") or 0
        v = row.get("views") or 0
        if not (er >= min_er or (views and v >= p75)):
            continue
        for tag in _re.findall(r"#([^\s#]+)", str(row.get("caption") or "")):
            key = tag.lower()
            if not key or key in known or key in stop:
                continue
            counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    niche = next((r.get("niche") for r in registry
                  if r.get("status") == "active" and r.get("niche")), "other")
    # M28: query храним в каноническом lowercase — акторы обеих платформ отдают
    # source_query в lowercase, mixed-case кандидат не получал бы атрибуции.
    new = [{"query": key, "kind": "hashtag", "niche": niche,
            "status": "candidate", "origin": origin, "added_at": today}
           for key, _ in top]
    registry.extend(new)
    return new


def source_marker(record):
    """Запись реестра -> маркер грамматики source_query ('hashtag:#x' / 'query:q')."""
    if record.get("kind") == "hashtag":
        return "hashtag:#" + str(record["query"])
    return "query:" + str(record["query"])


def exploration_facts(explored=(), retired=(), promoted=()):
    """Факты exploration одного прогона для сводки владельца (тикет 06).

    Единственная точка формы {explored, retired, promoted}: потребитель
    (cf.messages.collect_sources_block) при пустом ключе просто молчит, поэтому
    две независимые копии этого словаря в коллекторах дрейфовали бы беззвучно
    (ревью 2026-08-10). promoted приходит элементами promote-плана — маркер
    берётся из их поля record.
    """
    return {"explored": [source_marker(r) for r in explored],
            "retired": [source_marker(r) for r in retired],
            "promoted": [source_marker(p["record"]) for p in promoted]}


def chunk(items, size):
    """JS chunk(): последовательные срезы по size, последний короче."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def make_batches(sources, batch_size=BATCH_SIZE, limit=None):
    """Шардинг источников по <=batch_size (эталон Build-TikTok-Actor-Input.js).

    Порядок JS tagged: сначала хэштеги, потом запросы; пустые значения
    отброшены (filter(Boolean)). limit усекает общий список ДО шардинга
    (эталон slice(0, 30) в Prepare-Instagram-Reel-Transcript-Input.js).
    Пустой список -> 0 батчей: пустой батч дарил бы актору битый платный запрос.
    source_query — ПОЛНЫЙ список на каждом батче (downstream читает .first(),
    резать по батчу нельзя — сломается атрибуция); batch_sources — маркеры
    'hashtag:#<tag>' / 'query:<q>' (грамматика source_query из CLAUDE.md).

    Батч однороден по kind (тикет 02, 2026-08-10): hashtag-батчи уходят
    clockworks~tiktok-hashtag-scraper, search-батчи — основному актору, и
    смешанный батч не мог бы уйти ни одному из них целиком. Граница kind'а
    даёт неполный батч (как хвост списка), покрытие и порядок не меняются.
    """
    tagged = ([("hashtag", v) for v in sources.get("hashtags", []) if v]
              + [("query", v) for v in sources.get("search_queries", []) if v])
    if limit is not None:
        tagged = tagged[:limit]
    groups = (chunk([t for t in tagged if t[0] == "hashtag"], batch_size)
              + chunk([t for t in tagged if t[0] == "query"], batch_size))
    full_source_query = ",".join(v for _, v in tagged)
    return [
        {
            "batch_index": index,
            "batch_total": len(groups),
            "batch_size": len(group),
            "batch_sources": [("hashtag:#" if kind == "hashtag" else "query:") + v
                              for kind, v in group],
            "hashtags": [v for kind, v in group if kind == "hashtag"],
            "search_queries": [v for kind, v in group if kind == "query"],
            "source_query": full_source_query,
        }
        for index, group in enumerate(groups)
    ]
