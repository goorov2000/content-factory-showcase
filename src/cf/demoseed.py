"""Демо-сеялка eval-петли: фиктивные ролики и замеры с заранее известным ответом.

Отдела съёмки и выкладки ещё нет: CF Published Reels и CF Performance пусты, и вся
обратная связь завода (eval-агент, атрибуция провалов, own_performance,
performance-гвардия P5.10, вердикт A/B между версиями промпта) ни разу не
выполнялась на данных. Сеялка подставляет данные, чьи правильные выводы известны
заранее (ключ ответов пишется ДО прогона), чтобы проверить выводы системы, пока
цена ошибки нулевая.

Помеченность — не украшение, а предохранитель. `DEMO_PREFIX` в ключах делает
стирание точным фильтром, а не угадыванием; `DEMO_STATUS` в строке ролика держит
ночной `cf collect performance` подальше от демо-строк: build_metric_requests
берёт ролик только при status == "published" (ПУСТОЙ статус тоже трактуется как
published), и без явного "demo" фиктивные URL ушли бы в Apify за деньги.

Строки пишутся каноническими именами колонок (reel_id -> published_id,
er -> engagement_rate — перевод делает слой Sheets по column_aliases).
"""
from datetime import datetime, timedelta, timezone

from cf.runlog import log_run

DEMO_PREFIX = "DEMO-"
DEMO_STATUS = "demo"
# Маркер, по которому eval отличает состоявшийся замер от упавшего Apify-рана
# (FAILED_MEASUREMENT_MARKER в evalprep вытесняет строки с metrics_empty_or_unavailable).
DEMO_EVAL_NOTES = "collection_status=metrics_collected; DEMO"
# Зона .invalid зарезервирована RFC 2606 и не резолвится никогда — второй
# предохранитель на случай, если status кто-то поправит на published.
DEMO_URL_HOST = "https://demo.invalid"


class DemoSeedError(ValueError):
    """План инъекции не годится: нет такого сценария, разошёлся рецепт, битая дата.

    Наследник ValueError — CLI ловит его и печатает причину. Отказ происходит ДО
    единой записи в боевую таблицу: половина инъекции хуже честного отказа, потому
    что молча ломает когорты (замер без брифа теряет formula_id, и ни гвардия, ни
    A/B на нём не работают — а выглядит это как «система не справилась»)."""


def _parse_iso(value, what):
    text = str(value or "").strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise DemoSeedError(f"{what}: не ISO8601-дата {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _int(value, what):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DemoSeedError(f"{what}: ожидалось целое число, получено {value!r}")


def build_rows(briefs, plan):
    """План -> (строки роликов, строки замеров). Ничего не пишет, только проверяет.

    Проверки, без которых демо проваливается молча:
      - brief_id существует в CF Creative Briefs (иначе brief_found=False и пустой
        formula_id вниз по цепочке);
      - formula_id плана совпадает с формулой настоящего брифа (иначе арифметика
        ключа ответов посчитана не по тому рецепту);
      - slug уникален (иначе два ролика делят reel_id и дедуп замеров схлопнет их
        в один);
      - есть хотя бы один замер и просмотры положительны.

    er и views_per_hour СЧИТАЮТСЯ, а не берутся из плана: витрины показывают их
    рядом с просмотрами, и разъехавшиеся числа читаются как дефект системы.
    """
    reels_plan = plan.get("reels") or []
    if not reels_plan:
        raise DemoSeedError("в плане нет ни одного ролика (ключ 'reels')")
    briefs_by_id = {str(b.get("brief_id", "")): b for b in briefs}
    note = str(plan.get("note") or f"DEMO eval-loop {plan.get('created_at', '')}").strip()

    reel_rows, perf_rows, seen_slugs = [], [], set()
    for item in reels_plan:
        slug = str(item.get("slug", "")).strip()
        if not slug:
            raise DemoSeedError(f"ролик без slug: {item!r}")
        if slug in seen_slugs:
            raise DemoSeedError(f"slug {slug!r} встречается дважды — reel_id обязан быть уникальным")
        seen_slugs.add(slug)

        brief_id = str(item.get("brief_id", "")).strip()
        brief = briefs_by_id.get(brief_id)
        if brief is None:
            raise DemoSeedError(
                f"{slug}: сценарий {brief_id!r} не найден в CF Creative Briefs — "
                f"без связки замер теряет formula_id и демо провалится молча")
        plan_formula = str(item.get("formula_id", "")).strip()
        brief_formula = str(brief.get("formula_id", "")).strip()
        if plan_formula and plan_formula != brief_formula:
            raise DemoSeedError(
                f"{slug}: в плане рецепт {plan_formula!r}, а у сценария {brief_id} — "
                f"{brief_formula!r}")

        reel_id = f"{DEMO_PREFIX}{slug}"
        platform = str(item.get("platform") or brief.get("platform") or "tiktok").strip()
        published = _parse_iso(item.get("published_at"), f"{slug}: published_at")
        version = str(item.get("prompt_version") or brief.get("prompt_version") or "").strip()
        if not version:
            raise DemoSeedError(f"{slug}: нет prompt_version ни в плане, ни у сценария — "
                                f"замер попадёт в ведро 'unknown' и вердикта не будет")
        reel_rows.append({
            "reel_id": reel_id,
            "brief_id": brief_id,
            "platform": platform,
            "post_url": f"{DEMO_URL_HOST}/{platform}/{reel_id}",
            "published_at": published.isoformat(timespec="seconds"),
            "creator": "",
            "content_owner": "",
            "prompt_version": version,
            "status": DEMO_STATUS,
            "production_notes": note,
        })

        measurements = item.get("measurements") or []
        if not measurements:
            raise DemoSeedError(f"{slug}: нет ни одного замера")
        for m in measurements:
            days_after = _int(m.get("days_after", 7), f"{slug}: days_after")
            views = _int(m.get("views"), f"{slug}: views")
            if views <= 0:
                raise DemoSeedError(f"{slug}: просмотры обязаны быть больше нуля")
            likes = _int(m.get("likes", 0), f"{slug}: likes")
            comments = _int(m.get("comments", 0), f"{slug}: comments")
            shares = _int(m.get("shares", 0), f"{slug}: shares")
            saves = _int(m.get("saves", 0), f"{slug}: saves")
            measured = published + timedelta(days=days_after)
            hours = max(days_after * 24, 1)
            perf_rows.append({
                "performance_id": f"{reel_id}-{measured.strftime('%Y%m%d%H%M')}",
                "reel_id": reel_id,
                "brief_id": brief_id,
                "prompt_version": version,
                "platform": platform,
                "measured_at": measured.isoformat(timespec="seconds"),
                "hours_since_publish": hours,
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "saves": saves,
                "er": round((likes + comments + shares + saves) / views, 4),
                "views_per_hour": round(views / hours, 2),
                "result_label": "unknown",
                "result_reason": "demo_seed",
                "eval_notes": DEMO_EVAL_NOTES,
            })

    _assert_marked(reel_rows, "reel_id")
    _assert_marked(perf_rows, "performance_id")
    return reel_rows, perf_rows


def _assert_marked(rows, key):
    """Ни одна строка не уходит в боевую таблицу без метки DEMO_PREFIX.

    Последний рубеж: помеченность — единственное, что делает стирание точным, и
    единственное, что удерживает ночной сбор статистики от платных запросов."""
    unmarked = [r for r in rows if not str(r.get(key, "")).startswith(DEMO_PREFIX)]
    if unmarked:
        raise DemoSeedError(f"строки без метки {DEMO_PREFIX} в {key}: {unmarked[:3]}")


def _is_demo(row, *keys):
    return any(str(row.get(k, "")).startswith(DEMO_PREFIX) for k in keys)


def seed(sheets, plan, started_at=None, trigger_type="manual"):
    """Влить план в CF Published Reels и CF Performance двумя батчами.

    Повторный запуск не плодит дублей (сверка по reel_id / performance_id): демо
    ставится в один заход, но заход может прерваться на середине, и второй прогон
    обязан быть безопасным. Возвращает сводку записанного/пропущенного.
    """
    briefs = sheets.read_rows("briefs")
    reel_rows, perf_rows = build_rows(briefs, plan)

    known_reels = {str(r.get("reel_id", "")) for r in sheets.read_rows("reels")}
    known_perf = {str(r.get("performance_id", "")) for r in sheets.read_rows("performance")}
    new_reels = [r for r in reel_rows if r["reel_id"] not in known_reels]
    new_perf = [p for p in perf_rows if p["performance_id"] not in known_perf]

    sheets.append_rows("reels", new_reels)
    sheets.append_rows("performance", new_perf)

    summary = {
        "reels": len(new_reels),
        "performance": len(new_perf),
        "reels_skipped": len(reel_rows) - len(new_reels),
        "performance_skipped": len(perf_rows) - len(new_perf),
    }
    log_run(sheets, agent="demo-seed", status="success",
            input_summary=(f"DEMO: залито {summary['reels']} роликов / "
                           f"{summary['performance']} замеров "
                           f"(пропущено как уже существующие: {summary['reels_skipped']}/"
                           f"{summary['performance_skipped']})"),
            trigger_type=trigger_type, started_at=started_at)
    return summary


def wipe(sheets, started_at=None, trigger_type="manual"):
    """Стереть демо-строки: вкладка переписывается СПИСКОМ СОХРАНЁННЫХ строк.

    Не «очистить вкладку»: чужая строка, появившаяся между инъекцией и стиранием
    (оператор отметил настоящую публикацию), обязана пережить уборку. Поэтому
    фильтр по метке, а не replace_rows(tab, []) вслепую; при нуле демо-строк
    запись не делается вовсе — незачем трогать боевой лист.
    """
    removed = {}
    for tab, keys in (("reels", ("reel_id",)),
                      ("performance", ("performance_id", "reel_id"))):
        rows = sheets.read_rows(tab)
        kept = [r for r in rows if not _is_demo(r, *keys)]
        removed[tab] = len(rows) - len(kept)
        if removed[tab]:
            sheets.replace_rows(tab, kept)

    log_run(sheets, agent="demo-wipe", status="success",
            input_summary=(f"DEMO: стёрто {removed['reels']} роликов / "
                           f"{removed['performance']} замеров"),
            trigger_type=trigger_type, started_at=started_at)
    return removed
