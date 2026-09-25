"""Расчётный лист продюсера и UTM-ссылки для bio (UTM-контур, тикет 04).

Чистые функции по контракту queues.py: время и данные — аргументами, часов и
побочных эффектов нет. Правила — из оффера (спека «Расчётный лист»):
уники месяца по аккаунтам × ставка + процент с подтверждённой выручки, холд по
дате заказа, опциональный месячный фикс. Ставки живут ТОЛЬКО в
cf.config.json → payout — в коде числовых констант оплаты нет: отсутствие
ставок даёт ноль с предупреждением, а не тихий дефолт.

Деньги не округляются до рубля молча: суммы канонизируются num() (199.0 → 199),
дробные копейки остаются как есть и показываются двумя знаками (fmt_money).
"""
from datetime import timedelta
from urllib.parse import urlencode

from cf.attribution import date_of as _date_of
from cf.collect.metrika import UTM_MEDIUM
from cf.collect.util import num

# Сноска обязательна во всех отчётах (решение гриля №7): атрибуция органики
# дырявая по природе, и без неё цифры читаются как «канал слабее, чем есть».
LOWER_BOUND_NOTE = ("Контур меряет нижнюю границу вклада роликов: часть "
                    "зрителей запоминает бренд и покупает без метки — эти "
                    "продажи здесь не видны.")

# Ставки, без которых лист не считается. Ключи конфига, не значения: сами
# числа (пилотные ставки) живут в cf.config.json → payout.
RATE_KEYS = ("per_transition_rub", "sales_percent", "hold_days")


def _resolve_slug(row, slugs):
    """Слаг аккаунта строки трафика по ТЕКУЩЕМУ реестру.

    Сохранённый account первичен, но сверяется с реестром: после переименования
    слага старая колонка не должна ни терять переход (campaign всё ещё наш),
    ни платить за метку, которой в реестре больше нет."""
    for candidate in (str(row.get("account") or "").strip(),
                      str(row.get("utm_campaign") or "").strip()):
        if candidate and candidate in slugs:
            return candidate
    return ""


def build_payout_sheet(utm_rows, orders, accounts, payout, month, now):
    """Расчётный лист месяца: разбивка по аккаунтам, продажи, холд, фикс, итог.

    utm_rows — строки CF UTM Traffic (деньги считаются ТОЛЬКО по месячным
    строкам row_kind=monthly: уники месяца — биллинговая цифра, суммой дневных
    она не является); orders — строки CF Orders; accounts — реестр
    cf.config.json → accounts; payout — блок ставок; month — 'YYYY-MM';
    now — момент формирования (datetime, для холда и штампа листа).
    """
    payout = payout or {}
    warnings = []
    missing = [k for k in RATE_KEYS if k not in payout]
    if missing:
        warnings.append("ставки payout не заданы в cf.config.json ("
                        + ", ".join(missing) + ") — эти деньги посчитаны нулём")
    per_transition = num(payout.get("per_transition_rub"))
    percent = num(payout.get("sales_percent"))
    hold_days = int(num(payout.get("hold_days")))

    # Переходы: месячные уники по аккаунтам реестра. Строки с меткой вне
    # реестра в деньги не входят — дисциплина «ссылки только из генератора»,
    # но и не молчат: предупреждение с числом потерянных уников.
    slugs = {a["slug"] for a in accounts}
    by_slug, unknown_users, unknown_campaigns = {}, 0, set()
    for row in utm_rows:
        if str(row.get("row_kind") or "").strip() != "monthly":
            continue
        if str(row.get("month") or "").strip() != month:
            continue
        users = num(row.get("users"))
        slug = _resolve_slug(row, slugs)
        if not slug:
            unknown_users = num(unknown_users + users)
            campaign = str(row.get("utm_campaign") or "").strip()
            if campaign:
                unknown_campaigns.add(campaign)
            continue
        by_slug[slug] = num(by_slug.get(slug, 0) + users)
    if unknown_users or unknown_campaigns:
        names = ", ".join(sorted(unknown_campaigns)) or "метка пустая"
        warnings.append(f"переходы с неизвестной меткой: {unknown_users} "
                        f"уников ({names}) — в деньги не входят, ссылки "
                        "должны идти из генератора")
    account_rows = []
    for entry in accounts:                       # порядок реестра, не словаря
        slug = entry["slug"]
        if slug not in by_slug:
            continue
        users = by_slug[slug]
        account_rows.append({"slug": slug,
                             "platform": entry.get("platform", ""),
                             "users": users,
                             "amount": num(users * per_transition)})
    transitions_users = num(sum(a["users"] for a in account_rows))
    transitions_amount = num(sum(a["amount"] for a in account_rows))

    # Продажи: только confirmed внутри месяца. Холд — по дате заказа
    # относительно момента формирования: созрел, когда прошло >= hold_days.
    # returned/rejected не входят никогда; candidates — строкой ожидания.
    today = now.date()
    sales, held = [], []
    cand_count, cand_revenue = 0, 0
    for order in orders:
        if str(order.get("order_date") or "")[:7] != month:
            continue
        status = str(order.get("status") or "").strip().lower()
        order_id = str(order.get("order_id") or "")
        revenue = num(order.get("revenue"))
        if status == "candidate":
            cand_count += 1
            cand_revenue = num(cand_revenue + revenue)
            continue
        if status != "confirmed":
            continue
        order_date = _date_of(order.get("order_date"))
        if order_date is None:
            warnings.append(f"заказ {order_id}: дата "
                            f"«{order.get('order_date')}» нечитаема — "
                            "в лист не вошёл")
            continue
        item = {"order_id": order_id,
                "order_date": order_date.isoformat(),
                "revenue": revenue,
                "account": str(order.get("account") or "")}
        if (today - order_date).days >= hold_days:
            sales.append(dict(item, amount=num(revenue * percent / 100)))
        else:
            held.append(dict(item, matures_at=(
                order_date + timedelta(days=hold_days)).isoformat()))
    sales_revenue = num(sum(s["revenue"] for s in sales))
    sales_amount = num(sum(s["amount"] for s in sales))

    fix_map = payout.get("monthly_fix_rub") or {}
    fix = num(fix_map[month]) if month in fix_map else None

    return {
        "month": month,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "rates": {"per_transition_rub": per_transition,
                  "sales_percent": percent, "hold_days": hold_days},
        "accounts": account_rows,
        "transitions_users": transitions_users,
        "transitions_amount": transitions_amount,
        "sales": sales,
        "sales_revenue": sales_revenue,
        "sales_amount": sales_amount,
        "held": held,
        "candidates": {"count": cand_count, "revenue": cand_revenue},
        "fix": fix,
        "total": num(transitions_amount + sales_amount + (fix or 0)),
        "warnings": warnings,
    }


def fmt_money(value):
    """Сумма в рублях: целая — без знаков, дробная — копейки как есть (два знака).

    Округления до рубля нет намеренно (решение спеки): 199.5 → «199.50»."""
    n = num(value)
    if isinstance(n, int):
        return str(n)
    return f"{n:.2f}"


def sheet_to_md(sheet):
    """Расчётный лист markdown'ом — те же числа, что на странице «Деньги».

    Пригоден для пересылки продюсеру как есть (образец жанра —
    markdown-выгрузка очереди съёмки). Сноска о нижней границе — безусловна."""
    rates = sheet["rates"]
    lines = [f"# Расчётный лист — {sheet['month']}", "",
             f"сформирован {sheet['generated_at']}", "",
             f"## Переходы ({fmt_money(rates['per_transition_rub'])} ₽ за уник)",
             ""]
    if sheet["accounts"]:
        lines += ["| Аккаунт | Уники месяца | Сумма |", "|---|---|---|"]
        for acc in sheet["accounts"]:
            lines.append(f"| {acc['slug']} | {acc['users']} | "
                         f"{fmt_money(acc['amount'])} ₽ |")
    else:
        lines.append("Переходов по меткам реестра в этом месяце нет.")
    lines += ["", f"## Продажи ({fmt_money(rates['sales_percent'])}% "
                  "с подтверждённых)", ""]
    if sheet["sales"]:
        lines += ["| Заказ | Дата | Выручка | Комиссия |", "|---|---|---|---|"]
        for s in sheet["sales"]:
            lines.append(f"| {s['order_id']} | {s['order_date']} | "
                         f"{fmt_money(s['revenue'])} ₽ | "
                         f"{fmt_money(s['amount'])} ₽ |")
    else:
        lines.append("Подтверждённых продаж, созревших к выплате, нет.")
    for h in sheet["held"]:
        lines.append(f"- {h['order_id']} ({fmt_money(h['revenue'])} ₽, заказ "
                     f"{h['order_date']}) — в холде, войдёт в следующий лист "
                     f"(созреет {h['matures_at']})")
    if sheet["fix"] is not None:
        lines += ["", "## Фикс", "",
                  f"- фикс месяца: {fmt_money(sheet['fix'])} ₽"]
    lines += ["", "## Итого", "",
              f"- переходы: {fmt_money(sheet['transitions_amount'])} ₽",
              f"- продажи: {fmt_money(sheet['sales_amount'])} ₽"]
    if sheet["fix"] is not None:
        lines.append(f"- фикс: {fmt_money(sheet['fix'])} ₽")
    lines.append(f"- **итого: {fmt_money(sheet['total'])} ₽**")
    notes = list(sheet["warnings"])
    if sheet["candidates"]["count"]:
        notes.append(f"ждут решения: {sheet['candidates']['count']} на "
                     f"{fmt_money(sheet['candidates']['revenue'])} ₽ — "
                     "подтвердите или отклоните заказы")
    if notes:
        lines += ["", "## Предупреждения", ""]
        lines += [f"- {n}" for n in notes]
    lines += ["", f"> {LOWER_BOUND_NOTE}", ""]
    return "\n".join(lines)


def bio_links(accounts, base_url):
    """Готовые ссылки UTM-конвенции по каждому АКТИВНОМУ аккаунту реестра.

    utm_source = платформа, utm_medium = константа завода, utm_campaign = слаг;
    utm_content (метка ролика) добавляет конструктор per-reel на странице.
    Пустой base_url или реестр — пустой список: страница покажет причину."""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return []
    links = []
    for entry in accounts:
        if not entry.get("active"):
            continue
        query = urlencode([("utm_source", entry.get("platform", "")),
                           ("utm_medium", UTM_MEDIUM),
                           ("utm_campaign", entry["slug"])])
        links.append({"slug": entry["slug"],
                      "platform": entry.get("platform", ""),
                      "handle": entry.get("handle", ""),
                      "url": f"{base}/?{query}"})
    return links
