"""Машинный чек-лист авто-решений: ворота, которые закрываются без человека.

Зачем модуль появился (решение оператора 2026-07-26, вечер). Ворота конвейера
сделали производство предсказуемым, но переложили на человека 33 решения за шесть
дней — из них 25 по рецептам. Продюсер, для которого строится завод, не инженер:
он эксперт по контенту и компетентен судить СЦЕНАРИЙ, а не рецепт, промпт и
покрытие. Значит человеческие ворота должны остаться ровно там, где человек
добавляет суждение, а остальные — стать машинными.

Чем обоснованы пороги. Чек-лист прогнан по ВСЕМ решённым рецептам завода на
2026-07-26 (16 approved, 8 rejected) — это и есть калибровка вместо догадок.
Замер воспроизводится командой ``cf gate-calibration`` и на 27.07 даёт
«совпало 22 из 24 · машина строже 1 · пропусков 1»; цифры ниже — оттуда же:

* ``evidence_resolves`` и ``evidence_niche`` не отсеяли ни одного рецепта: у всех
  24 все source_urls нашлись в raw и все — своей ниши. Это не повод их убрать:
  они защищают правило №1 от выдуманного evidence, просто сегодня чисто.
* ``niche`` отсеивает 7 отказов из 8 — оператор отклонял рецепты нецелевых тем
  (женская-мода, обувь, спорт-фитнес, стритвир, уход-грумминг).
* ``authors`` (>= 3 разных автора) отсеивает 1 отказ и НИ ОДНОГО одобренного:
  минимум у одобренных — ровно 3 (reference-recreation-fit).
* ``confidence`` в чек-лист НЕ входит, хотя просился: 8 из 16 одобренных имеют
  ``medium``. Порог «только high» отсёк бы половину живого производства.
* ``transcript`` мягкий (>= 1 расшифровка): у одобренного numbered-outfit-looks
  расшифровок нет вовсе, а откат 14.07 случился как раз из-за паттерна,
  выведенного из капшена. Нулевая расшифровка — не отказ, а эскалация к человеку.
* Медиана просмотров evidence против медианы ниши проверена и ОТВЕРГНУТА как
  порог: она пропускает все 24 рецепта, включая отказ, и значит ничего не решает.

Честная граница чек-листа. Единственный отказ оператора в ЦЕЛЕВОЙ теме —
``shopper-pov-store-find`` (бренды-магазины): evidence у него чистый по всем
машинным признакам, а отклонён он за то, что снимается от лица покупателя в чужом
магазине, то есть неисполним брендом-магазином. Ни один детерминированный признак
этого не видит — это суждение о бизнес-контексте. Поэтому чек-лист не
единственный слой: над ним стоит агент-судья (``/cf-review-formula``), а под ним —
ворота сценария и авто-пауза рецепта по трём отказам ревьюера. Цена ошибки
ограничена одним сценарием, который до съёмки не доходит.

Контракт проверки — как в queues.py: чистые функции, никаких часов и побочных
эффектов, ``None`` в поле ``ok`` означает «проверить не смогли» и ведёт к человеку,
а не к молчаливому «да» (правило №2).
"""
import json
import re
from pathlib import Path

from cf.abtest import ACTIVE_TOKENS
from cf.dashboard.queues import (approved_formulas_by_niche, brief_prompt_id,
                                 brief_prompt_path, fanout_exclude_niches,
                                 has_active_version, prompt_covers_formula)

# ── Политика ворот ───────────────────────────────────────────────────────────
# Рычаг оператора: любые ворота возвращаются в ручной режим одной строкой
# cf.config.json → gates.policy.<ключ> = "manual". Значение по умолчанию — auto:
# завод автономен, человек держит вето и откат (правило №3 в редакции 26.07).
GATE_AUTO = "auto"
GATE_MANUAL = "manual"
GATE_KEYS = ("recipes", "prompt")
DEFAULT_POLICY = GATE_AUTO


def gate_policy(config, gate):
    """Политика ворот из конфига: "auto" | "manual". Мусор/нет ключа → auto."""
    try:
        value = str((config or {}).get("gates", {}).get("policy", {})
                    .get(gate, DEFAULT_POLICY)).strip().lower()
    except AttributeError:
        return DEFAULT_POLICY
    return value if value in (GATE_AUTO, GATE_MANUAL) else DEFAULT_POLICY


def is_auto(config, gate):
    return gate_policy(config, gate) == GATE_AUTO


# ── Пороги чек-листа рецепта (обоснование — в docstring модуля) ──────────────
MIN_SOURCE_URLS = 3        # столько же требует schemas/formula.schema.json
MIN_RESOLVED = 3           # меньше — судить о рецепте не по чему
MIN_RESOLVED_SHARE = 0.6   # запас на ротацию raw (`cf archive --older-than 45d`)
MIN_NICHE_SHARE = 2 / 3    # «загрязнение чужой нишей» — реальный класс отказов
MIN_AUTHORS = 3            # минимум у одобренных ровно 3, у отказа — 2
MIN_TRANSCRIPTS = 1        # 0 расшифровок → к человеку, а не отказ


def make_check(key, ok, label, detail=""):
    return {"key": key, "ok": ok, "label": label, "detail": detail}


def raw_index(rows):
    """{url: строка} по вкладкам raw — вход проверок evidence.

    Индексируем и source_url, и url: у части строк Instagram канонический адрес
    лежит только во втором поле, и рецепт по такой строке иначе выглядел бы
    выдуманным."""
    index = {}
    for row in rows or ():
        for key in ("source_url", "url"):
            url = str(row.get(key) or "").strip()
            if url:
                index.setdefault(url, row)
    return index


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _num(value):
    try:
        return float(str(value).replace(",", ".").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def taxonomy_niches(root):
    """Список тем из единственного источника истины. None — файл не прочитан."""
    data = _load_json(Path(root) / "prompts" / "agents" / "niche-taxonomy.json")
    if not isinstance(data, dict):
        return None
    niches = data.get("niches")
    return [str(n).strip() for n in niches] if isinstance(niches, list) else None


# ── Журнал решений: человеческое «нет» не должно забываться ──────────────────
# Отказ оператора живёт в статусе файла рецепта, а следующий прогон фан-аута
# перезаписывает тот же файл черновиком v+1 со статусом proposed — и отказ
# исчезает. Пока решения принимал человек, это ловилось памятью человека. Для
# авто-режима нужен след: append-only журнал рядом с индексом одобренных.
DECISIONS_LEDGER = ("formulas", "_decisions.jsonl")


def ledger_path(root):
    return Path(root).joinpath(*DECISIONS_LEDGER)


def read_ledger(root):
    """Строки журнала решений (список словарей). Нет файла → []."""
    path = ledger_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    # split("\n"), не splitlines(): тот режет запись по U+2028 из причины отказа,
    # и обе половины молча уходили в «битые» — вето человека терялось.
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue          # битая строка журнала не должна ронять ворота
        if isinstance(entry, dict):
            out.append(entry)
    return out


def append_ledger(root, entry):
    """Дописать решение в журнал. Best-effort: сбой записи решение не отменяет."""
    path = ledger_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def human_rejections(root):
    """{(тема, рецепт)} — пары, по которым ЧЕЛОВЕК сказал «нет» или «пауза».

    Последнее решение по паре побеждает: ре-одобрение оператором снимает запрет,
    иначе однажды отклонённый рецепт никогда не вернулся бы в авто-режим."""
    last = {}
    for entry in read_ledger(root):
        key = (str(entry.get("niche") or ""), str(entry.get("name") or ""))
        if key == ("", ""):
            continue
        last[key] = entry
    return {key for key, entry in last.items()
            if entry.get("actor") == "human"
            and str(entry.get("decision")) in ("rejected", "paused")}


# ── Чек-лист рецепта ─────────────────────────────────────────────────────────

def _manifest_frame_count(row):
    """Число кадров из media_manifest строки; нечитаемый манифест → 0."""
    try:
        manifest = json.loads(str(row.get("media_manifest") or ""))
    except ValueError:
        return 0
    try:
        return int(manifest.get("frame_count") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _visual_refs_check(evidence, raw_by_url):
    """Проверка visual_refs evidence: каждая ссылка резолвится по данным строк.

    Семантика — как у остальных evidence-проверок: raw не прочитаны → ok=None,
    рецепт уходит к человеку («непрочитанный Raw — не да», правило №2)."""
    refs = [r for r in (evidence.get("visual_refs") or []) if isinstance(r, dict)]
    if not refs:
        return make_check("visual_refs_resolve", True, "визуальные ссылки",
                          "нет — не применяется")
    if raw_by_url is None:
        return make_check("visual_refs_resolve", None, "визуальные ссылки",
                          "raw-вкладки не прочитаны")
    problems = []
    for ref in refs:
        url = str(ref.get("source_url") or "").strip()
        row = raw_by_url.get(url)
        if row is None:
            problems.append(f"{url or '<пустой url>'}: нет в raw")
            continue
        if str(row.get("visual_status") or "").strip() != "done":
            problems.append(f"{url}: медиа не разобрано (visual_status ≠ done)")
            continue
        if str(ref.get("media")) == "frame":
            count = _manifest_frame_count(row)
            idx = ref.get("frame_index")
            if not isinstance(idx, int) or idx < 1 or idx > count:
                problems.append(
                    f"{url}: кадр {idx!r} вне манифеста ({count} кадров)")
    return make_check("visual_refs_resolve", not problems, "визуальные ссылки",
                      "; ".join(problems[:2]) if problems
                      else f"{len(refs)} резолвятся")


def formula_checks(root, formula, raw_by_url=None, schema_root=None):
    """Машинные проверки черновика рецепта — список словарей check.

    ``raw_by_url`` — индекс raw-строк (см. raw_index). ``None`` означает «строки
    не прочитаны»: проверки evidence отдают ok=None, и рецепт уходит к человеку.
    """
    root = Path(root)
    checks = []
    name = str(formula.get("name") or "").strip()
    niche = str(formula.get("niche") or "").strip()

    # 1. Схема. Битый рецепт не должен доходить ни до авто-, ни до ручных ворот.
    from cf.validate import validate_json_data
    try:
        errors = validate_json_data("formula", formula,
                                    schema_root=str(schema_root or root))
    except (OSError, ValueError) as exc:
        checks.append(make_check("schema", None, "схема", f"не проверена: {exc}"))
    else:
        checks.append(make_check("schema", not errors, "схема",
                             "; ".join(errors[:2]) if errors else "валиден"))

    # 2. Статус: применяется только к нерешённому черновику.
    status = str(formula.get("status") or "proposed")
    checks.append(make_check("status", status == "proposed", "статус черновика",
                         f"status={status!r}, нужен proposed"
                         if status != "proposed" else "proposed"))

    # 3. Тема: в таксономии и вне списка нецелевых. Самый результативный признак —
    #    7 отказов оператора из 8 были именно про это.
    taxonomy = taxonomy_niches(root)
    exclude = fanout_exclude_niches(root)
    if niche in exclude:
        checks.append(make_check("niche", False, "тема",
                             f"«{niche}» вне производства (exclude_niches)"))
    elif taxonomy is None:
        checks.append(make_check("niche", None, "тема", "таксономия не прочитана"))
    elif niche not in taxonomy:
        checks.append(make_check("niche", False, "тема",
                             f"«{niche}» нет в niche-taxonomy.json"))
    else:
        checks.append(make_check("niche", True, "тема", niche))

    # 4-8. Evidence: рецепт обязан быть выведен из РЕАЛЬНЫХ роликов (правило №1).
    evidence = formula.get("evidence") or {}
    urls = [str(u).strip() for u in (evidence.get("source_urls") or []) if str(u).strip()]
    urls = list(dict.fromkeys(urls))
    checks.append(make_check("evidence_urls", len(urls) >= MIN_SOURCE_URLS,
                         "ссылок на референсы",
                         f"{len(urls)} (нужно ≥ {MIN_SOURCE_URLS})"))
    if raw_by_url is None:
        for key, label in (("evidence_resolves", "референсы найдены в raw"),
                           ("evidence_niche", "референсы своей темы"),
                           ("authors", "разных авторов"),
                           ("transcript", "есть расшифровка")):
            checks.append(make_check(key, None, label, "raw-вкладки не прочитаны"))
    else:
        found = [raw_by_url[u] for u in urls if u in raw_by_url]
        share = (len(found) / len(urls)) if urls else 0.0
        checks.append(make_check(
            "evidence_resolves",
            len(found) >= MIN_RESOLVED and share >= MIN_RESOLVED_SHARE,
            "референсы найдены в raw",
            f"{len(found)} из {len(urls)}"))
        same = [r for r in found
                if str(r.get("niche") or "").strip() == niche]
        niche_share = (len(same) / len(found)) if found else 0.0
        checks.append(make_check("evidence_niche", niche_share >= MIN_NICHE_SHARE,
                             "референсы своей темы",
                             f"{len(same)} из {len(found)}"))
        authors = {str(r.get("author") or r.get("account") or "").strip()
                   for r in found}
        authors.discard("")
        checks.append(make_check("authors", len(authors) >= MIN_AUTHORS,
                             "разных авторов",
                             f"{len(authors)} (нужно ≥ {MIN_AUTHORS})"))
        transcripts = sum(1 for r in found
                          if str(r.get("transcript_text") or "").strip())
        checks.append(make_check("transcript", transcripts >= MIN_TRANSCRIPTS,
                             "есть расшифровка",
                             f"{transcripts} из {len(found)}"))

    # 8б. Визуальные ссылки (тикет 08 визуального контура): «кадр N видео X»
    #     резолвится детерминированно — выдуманный визуальный evidence в
    #     производство не проходит (правило №1). Применяется только при
    #     наличии visual_refs: старый чисто текстовый рецепт не блокируется.
    checks.append(_visual_refs_check(evidence, raw_by_url))

    # 9. Паттерны: цепочка pattern_id → formula_id не должна рваться.
    patterns = [p for p in (formula.get("source_pattern_ids") or []) if str(p).strip()]
    checks.append(make_check("patterns", bool(patterns), "паттерны-источники",
                         f"{len(patterns)}"))

    # 10. Дубль: та же пара (рецепт, версия) уже в индексе одобренных.
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    same_version = any(
        str(e.get("name")) == name and str(e.get("niche")) == niche
        and str(e.get("version")) == str(formula.get("version"))
        for e in (index.get("approved") or []))
    checks.append(make_check("duplicate", not same_version, "не дубль решения",
                         f"{name} v{formula.get('version')} уже в индексе"
                         if same_version else "новая версия"))

    # 11. Человеческое «нет» помнится: рецепт, отклонённый оператором, не
    #     возвращается в производство следующей версией без его же решения.
    rejected = (niche, name) in human_rejections(root)
    checks.append(make_check("human_veto", not rejected, "нет отказа оператора",
                         "оператор отклонял этот рецепт — решение за ним"
                         if rejected else "отказов нет"))
    return checks


def proposed_formula_drafts(root, exclude=None):
    """Черновики рецептов, ждущие решения: [(тема, путь)] в порядке ленты.

    Тот же обход, что у queues.proposed_by_niche (там считаются штуки, здесь нужны
    пути), с тем же пропуском каталога снапшотов и нецелевых тем."""
    root = Path(root)
    exclude = fanout_exclude_niches(root) if exclude is None else exclude
    base = root / "formulas"
    out = []
    if not base.is_dir():
        return out
    for niche_dir in sorted(base.iterdir()):
        if not niche_dir.is_dir() or niche_dir.name == "_approved":
            continue
        for path in sorted(niche_dir.glob("*.json")):
            data = _load_json(path)
            if not isinstance(data, dict) or data.get("status") != "proposed":
                continue
            niche = str(data.get("niche") or niche_dir.name).strip()
            if niche and niche not in exclude:
                out.append((niche, path))
    return out


def verdict(checks):
    """Свод чек-листа: green — можно решать машинно, иначе список причин.

    ``ok is None`` (не смогли проверить) НЕ зелёный: правило №2 — недостаток
    данных не выдаётся за положительный ответ."""
    red = [c for c in checks if not c["ok"]]
    return {
        "green": not red,
        "checks": checks,
        "red": red,
        "reasons": [f"{c['label']}: {c['detail']}" if c["detail"] else c["label"]
                    for c in red],
    }


def formula_verdict(root, formula, raw_by_url=None, schema_root=None):
    return verdict(formula_checks(root, formula, raw_by_url, schema_root))


# ── Чек-лист черновика промпта темы ──────────────────────────────────────────
# Стоит НА проверках prompt_apply (их семь, они внутри apply_prompt и отказывают
# исключением). Здесь — то, что было доверено человеку и один раз уже подвело:
# 26.07 черновик «мужской-стиль» протух за час и ссылался на вытесненные снапшоты,
# а промпт «мужские-образы» называл 1 рецепт из 4 и тема давала ноль сценариев.
_SNAPSHOT_RE = re.compile(
    r"formulas/_approved/(?P<niche>[^/\s]+)/(?P<name>[^/\s]+?)-v(?P<version>\d+)\.json")


# ── Целостность текста промпта ───────────────────────────────────────────────
# Прогон 27.07 01:15: кандидат reel-v3.md применился зелёным чек-листом и оказался
# обрублен посреди «Задачи» — блок в `## Proposed changes` закрылся на середине,
# и промпт потерял «Запреты» и «Выход». Покрытие рецептов, свежесть снапшотов и
# ссылки были в порядке, поэтому ворота его пропустили: ЦЕЛОСТНОСТЬ текста не
# проверялась вовсе. Генератор отказался писать по обрубку и назвал причину — но
# тема сутки не производила. Проверки ниже переносят этот отказ на ворота.
PROMPT_REQUIRED_SECTIONS = ("Вход", "Задача", "Запреты", "Выход")
_HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


def prompt_sections(text):
    """{заголовок: тело} секций второго уровня — в порядке появления.

    Заголовок нормализуем до первого слова («Запреты (нарушение = reject…)» →
    «Запреты»): хвост формулировки у тем разный, а секция та же."""
    out = {}
    matches = list(_HEADING_RE.finditer(str(text or "")))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        title = m.group("title").split("(")[0].strip()
        out[title] = text[m.end():end].strip()
    return out


def prompt_integrity_checks(text, active_text=None):
    """Текст промпта не обрублен и не потерял секций действующей версии."""
    checks = []
    sections = prompt_sections(text)
    empty = [t for t, body in sections.items() if len(body.splitlines()) < 2]
    checks.append(make_check("sections_filled", not empty, "секции не пустые",
                             ("пусты или в одну строку: " + ", ".join(empty[:3]))
                             if empty else f"{len(sections)} секций"))

    lines = [l for l in str(text or "").splitlines() if l.strip()]
    tail = lines[-1].strip() if lines else ""
    dangling = bool(tail) and (tail.endswith((":", "—", ",")) or tail.startswith("#"))
    checks.append(make_check("not_truncated", not dangling, "текст не оборван",
                             f"последняя строка: «{tail[:60]}»" if dangling else "цел"))

    if active_text is None:
        missing = [s for s in PROMPT_REQUIRED_SECTIONS if s not in sections]
        label, detail = "обязательные секции", (
            "нет: " + ", ".join(missing) if missing else
            ", ".join(PROMPT_REQUIRED_SECTIONS))
    else:
        # У кандидата эталон — действующая версия: она уже работает, и потерянная
        # секция это регрессия, а не стилистика.
        missing = [s for s in prompt_sections(active_text) if s not in sections]
        label, detail = "секции действующей версии на месте", (
            "потеряны: " + ", ".join(missing[:3]) if missing else "все на месте")
    checks.append(make_check("sections_present", not missing, label, detail))
    return checks


def prompt_draft_checks(root, niche, prompt_text, versions=None):
    """Проверки, при которых промпт темы можно включать без чтения человеком."""
    root = Path(root)
    checks = []
    by_niche = approved_formulas_by_niche(root)
    names = [n for n in by_niche.get(niche, []) if n]

    # 1. Есть что описывать: тема без утверждённых рецептов промпта не получает.
    checks.append(make_check("approved_formulas", bool(names), "утверждённые рецепты",
                         f"{len(names)}"))

    # 2. Покрытие: промпт называет ВСЕ утверждённые рецепты темы. Именно этот
    #    разрыв держал 3 рецепта «мужских-образов» без единого сценария.
    covered = [n for n in names if prompt_covers_formula(prompt_text, n)]
    uncovered = [n for n in names if n not in covered]
    checks.append(make_check("coverage", not uncovered and bool(names),
                         "названы все рецепты",
                         f"{len(covered)} из {len(names)}"
                         + (f" — без {', '.join(uncovered[:3])}" if uncovered else "")))

    # 3. Свежесть: каждая ссылка на снапшот указывает на ДЕЙСТВУЮЩУЮ версию
    #    рецепта. Черновик протухает вместе с индексом — аппликатор этого не видит.
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    current = {(str(e.get("niche")), str(e.get("name"))): str(e.get("version"))
               for e in (index.get("approved") or [])}
    stale = []
    for match in _SNAPSHOT_RE.finditer(prompt_text or ""):
        key = (match.group("niche"), match.group("name"))
        version = current.get(key)
        if version is not None and version != match.group("version"):
            stale.append(f"{match.group('name')} v{match.group('version')}"
                         f" → сейчас v{version}")
    checks.append(make_check("snapshots_fresh", not stale, "ссылки на актуальные версии",
                         "; ".join(stale[:2]) if stale else "актуальны"))

    # 4. Версия ещё не включена: авто-режим не переписывает работающий промпт —
    #    правка действующего идёт через A/B, а не через это звено. Признак берём
    #    у queues.has_active_version, а не своей копией: три копии предиката
    #    «готова ли тема» уже однажды остановили завод молча (CLAUDE.md).
    active = has_active_version(versions, niche)
    checks.append(make_check("not_active", None if active is None else not active,
                         "промпт темы ещё не включён",
                         "prompt_versions не прочитаны" if active is None else
                         ("уже включён — правка идёт через /cf-propose-update"
                          if active else "первое включение")))
    return checks


def prompt_draft_verdict(root, niche, prompt_text, versions=None):
    return verdict(prompt_draft_checks(root, niche, prompt_text, versions))


def prompt_candidate_checks(root, niche, prompt_text, version, versions=None):
    """Проверки ПРАВКИ действующего промпта темы — она идёт кандидатом в A/B.

    Разница с первым включением ровно одна и она принципиальная: здесь промпт темы
    обязан УЖЕ работать. Правка не заменяет активную версию (по ней уже произведены
    сценарии, и подмена текста порвала бы связку «версия → результат»), а встаёт
    рядом кандидатом: обе версии пишут сценарии параллельно, выбирает eval.
    """
    root = Path(root)
    checks = []
    by_niche = approved_formulas_by_niche(root)
    names = [n for n in by_niche.get(niche, []) if n]
    checks.append(make_check("approved_formulas", bool(names), "утверждённые рецепты",
                             f"{len(names)}"))

    covered = [n for n in names if prompt_covers_formula(prompt_text, n)]
    uncovered = [n for n in names if n not in covered]
    checks.append(make_check("coverage", not uncovered and bool(names),
                             "названы все рецепты",
                             f"{len(covered)} из {len(names)}"
                             + (f" — без {', '.join(uncovered[:3])}" if uncovered else "")))

    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    current = {(str(e.get("niche")), str(e.get("name"))): str(e.get("version"))
               for e in (index.get("approved") or [])}
    stale = [f"{m.group('name')} v{m.group('version')} → сейчас v{current[key]}"
             for m in _SNAPSHOT_RE.finditer(prompt_text or "")
             if (key := (m.group("niche"), m.group("name"))) in current
             and current[key] != m.group("version")]
    checks.append(make_check("snapshots_fresh", not stale, "ссылки на актуальные версии",
                             "; ".join(stale[:2]) if stale else "актуальны"))

    active = has_active_version(versions, niche)
    checks.append(make_check(
        "is_active", active, "промпт темы работает",
        "prompt_versions не прочитаны" if active is None else
        ("активная версия есть" if active else
         "активной версии нет — это первое включение темы, идёт другим путём")))

    newer = None
    if versions is not None:
        from cf.abtest import select_versions
        active_row, _cand = select_versions(versions, brief_prompt_id(niche))
        current_version = _version_number(active_row.get("version") if active_row else None)
        newer = current_version is not None and int(version) > current_version
        detail = (f"v{version} против активной v{current_version}"
                  if current_version is not None else
                  "у активной версии не читается номер")
    else:
        detail = "prompt_versions не прочитаны"
    checks.append(make_check("version_newer", newer, "версия новее активной", detail))

    # Эталон целостности для кандидата — ДЕЙСТВУЮЩИЙ текст: он уже производит
    # сценарии, и потерянная им секция это регрессия (27.07: кандидат v3 потерял
    # «Запреты» и «Выход», и тема встала на сутки).
    try:
        active_text = brief_prompt_path(root, niche).read_text(encoding="utf-8")
    except OSError:
        active_text = None
    checks += prompt_integrity_checks(prompt_text, active_text)
    return checks


def _version_number(value):
    """«v3»/«3» → 3; мусор/None → None (правило №2: не выдумываем номер)."""
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def prompt_candidate_verdict(root, niche, prompt_text, version, versions=None):
    return verdict(prompt_candidate_checks(root, niche, prompt_text, version, versions))


def _prompt_paths_in_production(versions):
    """github_path строк prompt_versions, которые сейчас производят: active + candidate.

    None (лист не прочитан) → None: тогда вызывающий смотрит все файлы на диске.
    Погашенные версии сюда не попадают — их файл лежит на диске, но сценариев не
    даёт, и предупреждать о нём значит учить оператора игнорировать предупреждения.
    """
    if versions is None:
        return None
    from cf.abtest import CANDIDATE_TOKEN
    live = set()
    for row in versions:
        status = str(row.get("active", "")).strip().upper()
        path = str(row.get("github_path") or "").strip()
        if path and (status in ACTIVE_TOKENS or status == CANDIDATE_TOKEN):
            live.add(path.replace("\\", "/"))
    return live


def stale_prompt_snapshots(root, niches=None, versions=None):
    """Уже ВКЛЮЧЁННЫЕ промпты, ссылающиеся на вытесненные версии снапшотов.

    Ворота проверяют свежесть в момент применения, но индекс живёт дальше: машина
    одобряет v+1 рецепта — и текст работающего промпта молча начинает указывать на
    прежний снапшот. Файл никуда не делся (снапшоты иммутабельны), поэтому ничего
    не падает: сценарии просто пишутся по старым правилам под новым номером версии
    в индексе. Ровно это случилось 27.07 через час после применения кандидата v3
    (`reference-recreation-fit` v1 → v2), и заметил это человек, а не завод.

    Возвращает [{niche, path, stale: ["имя v1 → сейчас v2", …]}] — вход строки ворот.
    Ничего не блокирует: промпт остаётся рабочим, это предупреждение.
    """
    root = Path(root)
    index = _load_json(root / "formulas" / "_approved" / "index.json") or {}
    current = {(str(e.get("niche")), str(e.get("name"))): str(e.get("version"))
               for e in (index.get("approved") or [])}
    live = _prompt_paths_in_production(versions)
    out = []
    base = root / "prompts" / "briefs"
    for path in sorted(base.rglob("*.md")) if base.is_dir() else ():
        niche = path.parent.name
        rel = path.relative_to(root).as_posix()
        if niches is not None and niche not in niches:
            continue
        if live is not None and rel not in live:
            continue          # версия погашена: файл есть, производства нет
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        stale = []
        for match in _SNAPSHOT_RE.finditer(text):
            key = (match.group("niche"), match.group("name"))
            version = current.get(key)
            if version is not None and version != match.group("version"):
                stale.append(f"{match.group('name')} v{match.group('version')}"
                             f" → сейчас v{version}")
        if stale:
            out.append({"niche": niche, "path": rel, "stale": stale})
    return out
