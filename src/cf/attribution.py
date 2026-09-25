"""Атрибуция: чей вклад стоит за строкой данных.

Два конца ключа связей завода (source_url → … → clicks/orders):
- source_query_from_raw_json — источник raw-строки сбора (hashtag/query Apify);
- attribute_clicks — по-роликовая атрибуция переходов и заказов (UTM-контур,
  тикет 05): какие ролики ведут на сайт.

Чистые функции по контракту queues.py: данные и границы периода — аргументами,
часов и побочных эффектов нет. Оценка НИГДЕ не хранится — пересчитывается на
лету (страница «Деньги», eval-датасет).

Числа переходов ролика двух сортов, и они не смешиваются:
- clicks_exact — визиты дневных строк CF UTM Traffic с меткой ролика
  (utm_content=reel_id): факт Метрики, не модель;
- clicks_estimated — ОЦЕНКА: остаток дневных визитов аккаунта (строки без
  utm_content — переходы из bio) раскладывается по роликам этого аккаунта,
  опубликованным в последние WINDOW_DAYS дней до этого дня, пропорционально их
  последним известным просмотрам из CF Performance; просмотров нет ни у
  одного — поровну; роликов в окне нет — остаток дня честно уходит в
  unattributed_total. В расчётный лист продюсера оценка не входит никогда
  (решение гриля №5) — её потребители только аналитика и eval.
"""
import calendar
import json
from datetime import date

from cf.collect.performance import FAILED_MEASUREMENT_MARKER
from cf.collect.util import num

# Окно жизни ролика в оценке — из спеки (решение гриля №5): остаток дня делят
# ролики, опубликованные в последние 14 дней; день публикации — первый из 14.
WINDOW_DAYS = 14


def source_query_from_raw_json(raw_json_text):
    """Источник строки из Apify-итема: hashtag:#x | query:x | unknown.

    raw_json обрезается при записи до 45000 символов, поэтому парс может падать —
    это не ошибка данных, а норма: возвращаем unknown.
    """
    try:
        raw = json.loads(raw_json_text or "")
    except (json.JSONDecodeError, TypeError):
        return "unknown"
    if not isinstance(raw, dict):
        return "unknown"
    hashtag = raw.get("searchHashtag")
    if isinstance(hashtag, dict) and str(hashtag.get("name", "")).strip():
        return f"hashtag:#{str(hashtag['name']).strip().lstrip('#')}"
    query = str(raw.get("searchQuery", "")).strip()
    if query:
        return f"query:{query}"
    return "unknown"


# ── По-роликовая атрибуция переходов (UTM-контур, тикет 05) ───────────────────

def date_of(value):
    """YYYY-MM-DD префикс строки -> date, мусор -> None. Канон разбора дат
    UTM-контура: расчётный лист (cf.payout) импортирует его отсюда."""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def month_bounds(month):
    """'YYYY-MM' -> (первый, последний день месяца), мусор -> None.

    Границы для страницы «Деньги»: по-роликовая секция считается за выбранный
    месяц, eval границ не передаёт (окно всего датасета)."""
    try:
        year, mon = str(month or "").split("-")
        first = date(int(year), int(mon), 1)
    except ValueError:
        return None
    return first, date(first.year, first.month,
                       calendar.monthrange(first.year, first.month)[1])


def last_known_views(performance):
    """reel_id -> последние известные просмотры (строка с максимальным measured_at).

    Сбойные замеры (маркер упавшего Apify-рана в eval_notes) «известными
    просмотрами» не считаются — как H16 в evalprep: нулевой сбой не должен
    затирать честный замер. measured_at сравнивается строкой (ISO сортируется
    лексикографически); при равенстве побеждает более поздняя строка ввода."""
    best = {}
    for row in performance or []:
        rid = str(row.get("reel_id") or "").strip()
        if not rid or FAILED_MEASUREMENT_MARKER in str(row.get("eval_notes", "")):
            continue
        stamp = str(row.get("measured_at") or "")
        prev = best.get(rid)
        if prev is None or stamp >= prev[0]:
            best[rid] = (stamp, num(row.get("views")))
    return {rid: views for rid, (_, views) in best.items()}


def _split(remainder, eligible, views_by_reel):
    """Доли остатка дня между роликами окна.

    Пропорционально последним известным просмотрам; просмотров нет ни у одного
    (сумма весов 0) — поровну. Инвариант (закреплён тестами): сумма долей ==
    остатку дня, поэтому здесь НЕТ округления — точность решает потребитель."""
    weights = {rid: max(num(views_by_reel.get(rid, 0)), 0) for rid in eligible}
    total = sum(weights.values())
    if total > 0:
        return {rid: remainder * w / total for rid, w in weights.items()}
    return {rid: remainder / len(eligible) for rid in eligible}


def attribute_clicks(utm_rows, reels, performance, orders, date_from=None,
                     date_to=None, window_days=WINDOW_DAYS):
    """Per-reel сводка периода: точные и оценочные переходы + заказы ролика.

    utm_rows — строки CF UTM Traffic (участвуют только row_kind=daily в границах
    [date_from, date_to]; None-граница = не ограничивать); reels — CF Published
    Reels (published_at + account дают окно распределения; строки без аккаунта
    или даты в оценке не участвуют); performance — CF Performance (веса — см.
    last_known_views); orders — CF Orders: в сводку входят ТОЛЬКО confirmed
    периода, по reel_id (utm_content ласт-клика), revenue суммой.

    Возврат: {"reels": {reel_id: {account, clicks_exact, clicks_estimated,
    estimated, orders, revenue}}, "unattributed_total": N}. estimated=True —
    в переходах ролика есть модельная часть (бейдж «оценка» на странице).
    clicks_estimated не округляется: инвариант «сумма долей == остатку дня»
    остаётся проверяемым, округляют потребители. Детерминизм: строки обходятся
    в отсортированном порядке, результат не зависит от порядка на входе."""
    views_by_reel = last_known_views(performance)
    published = {}                     # account -> {(pub_date, reel_id), ...}
    for r in reels or []:
        rid = str(r.get("reel_id") or "").strip()
        account = str(r.get("account") or "").strip()
        pub = date_of(r.get("published_at"))
        if rid and account and pub is not None:
            published.setdefault(account, set()).add((pub, rid))

    summary, unattributed = {}, 0.0

    def entry(rid, account):
        item = summary.setdefault(rid, {"account": "", "clicks_exact": 0,
                                        "clicks_estimated": 0.0, "orders": 0,
                                        "revenue": 0})
        if account and not item["account"]:
            item["account"] = account
        return item

    daily = [r for r in utm_rows or []
             if str(r.get("row_kind") or "").strip() == "daily"]
    daily.sort(key=lambda r: (str(r.get("date") or ""),
                              str(r.get("utm_campaign") or ""),
                              str(r.get("utm_content") or "")))
    for row in daily:
        day = date_of(row.get("date"))
        if day is None:
            continue
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        visits = num(row.get("visits"))
        if visits <= 0:
            continue
        account = (str(row.get("account") or "").strip()
                   or str(row.get("utm_campaign") or "").strip())
        content = str(row.get("utm_content") or "").strip()
        if content:                    # метка ролика: факт, не модель
            item = entry(content, account)
            item["clicks_exact"] = num(item["clicks_exact"] + visits)
            continue
        # Остаток дня (переходы аккаунта из bio) — по роликам окна. Дубли
        # публикации схлопнуты множеством reel_id: дважды отмеченный ролик —
        # один участник, иначе «поровну» ломало бы инвариант суммы.
        eligible = sorted({rid for pub, rid in published.get(account, ())
                           if 0 <= (day - pub).days < window_days})
        if not eligible:
            unattributed += visits
            continue
        for rid, share in _split(visits, eligible, views_by_reel).items():
            entry(rid, account)["clicks_estimated"] += share

    for order in orders or []:
        if str(order.get("status") or "").strip().lower() != "confirmed":
            continue
        day = date_of(order.get("order_date"))
        if (date_from or date_to) and day is None:
            continue                   # период задан, дату не прочитать — мимо
        if day is not None and ((date_from and day < date_from)
                                or (date_to and day > date_to)):
            continue
        rid = (str(order.get("reel_id") or "").strip()
               or str(order.get("utm_content") or "").strip())
        if not rid:
            continue                   # заказ без метки ролика — не по-роликовый
        item = entry(rid, str(order.get("account") or "").strip())
        item["orders"] += 1
        item["revenue"] = num(item["revenue"] + num(order.get("revenue")))

    for item in summary.values():
        item["estimated"] = item["clicks_estimated"] > 0
    return {"reels": summary, "unattributed_total": num(unattributed)}
