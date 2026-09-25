# UTM-контур, тикет 04 — расчётный лист продюсера и генератор ссылок для bio.
#
# Чистые функции (контракт queues.py: время и данные аргументами, без побочек):
# build_payout_sheet считает деньги месяца по правилам оффера, sheet_to_md
# отдаёт тот же лист markdown'ом, bio_links собирает ссылки UTM-конвенции.
# Ставки — ТОЛЬКО из payout-конфига: в коде числовых констант оплаты нет;
# значения PAYOUT ниже — условные тестовые.
from datetime import datetime, timezone

from cf.payout import (LOWER_BOUND_NOTE, bio_links, build_payout_sheet,
                       fmt_money, sheet_to_md)

ACCOUNTS = [
    {"slug": "tiktok-1", "platform": "tiktok", "handle": "@боевой", "active": True},
    {"slug": "instagram-1", "platform": "instagram", "handle": "@пусто",
     "active": False},
]

PAYOUT = {"per_transition_rub": 30, "sales_percent": 10, "hold_days": 14,
          "monthly_fix_rub": {}}

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def monthly_row(campaign, users, month="2026-07", account=None):
    return {"utm_id": f"m-{month}-{campaign}", "row_kind": "monthly",
            "date": "", "month": month, "utm_campaign": campaign,
            "account": campaign if account is None else account,
            "utm_content": "", "reel_id": "", "visits": users * 2,
            "users": users, "collected_at": "2026-07-30T08:20:00.000Z"}


def daily_row(day, campaign, users=5):
    return {"utm_id": f"d-{day}-{campaign}--", "row_kind": "daily",
            "date": day, "month": day[:7], "utm_campaign": campaign,
            "account": campaign, "utm_content": "", "reel_id": "",
            "visits": users, "users": users,
            "collected_at": "2026-07-30T08:20:00.000Z"}


def order(order_id, order_date, revenue, status="confirmed", account="tiktok-1"):
    return {"order_id": order_id, "source": "metrika_ecommerce",
            "order_date": order_date, "revenue": revenue, "utm_source": "tiktok",
            "utm_campaign": account, "utm_content": "", "account": account,
            "reel_id": "", "status": status, "status_changed_at": "",
            "decided_by": "", "notes": "", "collected_at": ""}


# ── Переходы ─────────────────────────────────────────────────────────────────

def test_transitions_take_monthly_uniques_not_daily_sums():
    # Дневных строк много и суммарно они больше месячных уников — биллинговая
    # цифра всё равно берётся из месячной строки (один человек ходит в разные дни).
    utm = [monthly_row("tiktok-1", 100),
           daily_row("2026-07-01", "tiktok-1", users=90),
           daily_row("2026-07-02", "tiktok-1", users=90)]
    sheet = build_payout_sheet(utm, [], ACCOUNTS, PAYOUT, "2026-07", NOW)
    (acc,) = sheet["accounts"]
    assert acc["slug"] == "tiktok-1"
    assert acc["users"] == 100
    assert acc["amount"] == 3000
    assert sheet["transitions_amount"] == 3000
    assert sheet["total"] == 3000


def test_transitions_other_months_and_unknown_campaigns_stay_out():
    utm = [monthly_row("tiktok-1", 100),
           monthly_row("tiktok-1", 999, month="2026-06"),   # чужой месяц
           monthly_row("чужая-метка", 50, account="")]      # вне реестра
    sheet = build_payout_sheet(utm, [], ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert [a["slug"] for a in sheet["accounts"]] == ["tiktok-1"]
    assert sheet["transitions_amount"] == 3000
    # незнакомая метка не в деньгах, но и не молчит — предупреждение с числом
    assert any("неизвестн" in w and "50" in w for w in sheet["warnings"])


def test_transitions_empty_registry_counts_nothing_and_warns():
    utm = [monthly_row("tiktok-1", 100)]
    sheet = build_payout_sheet(utm, [], [], PAYOUT, "2026-07", NOW)
    assert sheet["accounts"] == []
    assert sheet["transitions_amount"] == 0
    assert sheet["total"] == 0
    assert any("неизвестн" in w for w in sheet["warnings"])


def test_transitions_resolve_by_registry_not_by_stored_account():
    # Слаг переименовали после сбора: в строке account старый, но campaign
    # совпадает с реестром — переход остаётся в деньгах (реестр — истина).
    utm = [monthly_row("tiktok-1", 40, account="старый-слаг")]
    sheet = build_payout_sheet(utm, [], ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert [a["slug"] for a in sheet["accounts"]] == ["tiktok-1"]


# ── Продажи и холд ───────────────────────────────────────────────────────────

def test_sales_only_confirmed_inside_month():
    orders = [order("mk-1", "2026-07-10", 1990),
              order("mk-2", "2026-06-30", 5000),                 # чужой месяц
              order("mk-3", "2026-07-11", 700, status="returned"),
              order("mk-4", "2026-07-12", 800, status="rejected")]
    sheet = build_payout_sheet([], orders, ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert [o["order_id"] for o in sheet["sales"]] == ["mk-1"]
    assert sheet["sales"][0]["amount"] == 199
    assert sheet["sales_revenue"] == 1990
    assert sheet["sales_amount"] == 199
    assert sheet["total"] == 199


def test_hold_boundary_day_matures_and_younger_stays_held():
    # hold_days=14, момент формирования 30.07: заказ от 16.07 созрел РОВНО
    # сегодня (прошло 14 дней) — входит; заказ от 17.07 — ещё в холде.
    orders = [order("mk-old", "2026-07-16", 1000),
              order("mk-young", "2026-07-17", 2000)]
    sheet = build_payout_sheet([], orders, ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert [o["order_id"] for o in sheet["sales"]] == ["mk-old"]
    (held,) = sheet["held"]
    assert held["order_id"] == "mk-young"
    assert held["matures_at"] == "2026-07-31"     # дата созревания названа
    assert sheet["sales_amount"] == 100
    assert sheet["total"] == 100


def test_candidates_wait_line_and_are_not_money():
    orders = [order("mk-c1", "2026-07-01", 1000, status="candidate"),
              order("mk-c2", "2026-07-02", 500, status="candidate")]
    sheet = build_payout_sheet([], orders, ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert sheet["sales"] == [] and sheet["total"] == 0
    assert sheet["candidates"]["count"] == 2
    assert sheet["candidates"]["revenue"] == 1500


def test_kopecks_are_kept_not_silently_rounded():
    orders = [order("mk-1", "2026-07-10", 1995)]
    sheet = build_payout_sheet([], orders, ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert sheet["sales_amount"] == 199.5
    assert sheet["total"] == 199.5
    assert fmt_money(199.5) == "199.50"
    assert fmt_money(3000) == "3000"


# ── Фикс и пустой месяц ──────────────────────────────────────────────────────

def test_monthly_fix_only_when_month_key_present():
    payout = dict(PAYOUT, monthly_fix_rub={"2026-07": 15000})
    sheet = build_payout_sheet([], [], ACCOUNTS, payout, "2026-07", NOW)
    assert sheet["fix"] == 15000
    assert sheet["total"] == 15000
    other = build_payout_sheet([], [], ACCOUNTS, payout, "2026-08", NOW)
    assert other["fix"] is None
    assert other["total"] == 0


def test_empty_month_is_a_state_not_an_error():
    sheet = build_payout_sheet([], [], ACCOUNTS, PAYOUT, "2026-07", NOW)
    assert sheet["month"] == "2026-07"
    assert sheet["accounts"] == [] and sheet["sales"] == []
    assert sheet["held"] == [] and sheet["total"] == 0


def test_missing_payout_rates_warn_instead_of_inventing_constants():
    # Ставок в конфиге нет — код не подставляет свои 30/10/14, а честно
    # предупреждает и считает нулём.
    utm = [monthly_row("tiktok-1", 100)]
    sheet = build_payout_sheet(utm, [], ACCOUNTS, {}, "2026-07", NOW)
    assert sheet["transitions_amount"] == 0
    assert any("payout" in w for w in sheet["warnings"])


# ── markdown-экспорт ─────────────────────────────────────────────────────────

def test_sheet_to_md_carries_numbers_hold_footnote_and_date():
    payout = dict(PAYOUT, monthly_fix_rub={"2026-07": 15000})
    utm = [monthly_row("tiktok-1", 100),
           monthly_row("чужая-метка", 7, account="")]
    orders = [order("mk-1", "2026-07-10", 1990),
              order("mk-2", "2026-07-25", 500),
              order("mk-c", "2026-07-20", 300, status="candidate")]
    sheet = build_payout_sheet(utm, orders, ACCOUNTS, payout, "2026-07", NOW)
    md = sheet_to_md(sheet)
    assert md.startswith("# Расчётный лист")
    assert "2026-07" in md
    assert "tiktok-1" in md and "100" in md and "3000" in md   # уники × ставка
    assert "mk-1" in md and "199" in md                        # 10% с продажи
    assert "mk-2" in md and "в холде" in md and "2026-08-08" in md
    assert "15000" in md                                       # фикс
    assert "ждут решения" in md and "300" in md                # кандидаты
    assert "неизвестн" in md and "7" in md                     # чужая метка
    assert LOWER_BOUND_NOTE in md                              # сноска безусловна
    assert "сформирован 2026-07-30" in md
    assert str(sheet["total"]) != "" and fmt_money(sheet["total"]) in md


def test_sheet_to_md_empty_month_still_has_footnote():
    md = sheet_to_md(build_payout_sheet([], [], ACCOUNTS, PAYOUT, "2026-07", NOW))
    assert LOWER_BOUND_NOTE in md
    assert "0" in md


# ── Ссылки для bio ───────────────────────────────────────────────────────────

def test_bio_links_follow_convention_and_skip_inactive():
    links = bio_links(ACCOUNTS, "https://jelapeche.com")
    (link,) = links                                # неактивный не показывается
    assert link["slug"] == "tiktok-1"
    assert link["url"] == ("https://jelapeche.com/?utm_source=tiktok"
                           "&utm_medium=cf-organic&utm_campaign=tiktok-1")


def test_bio_links_trailing_slash_not_doubled():
    (link,) = bio_links([ACCOUNTS[0]], "https://jelapeche.com/")
    assert "com//" not in link["url"]
    assert link["url"].startswith("https://jelapeche.com/?utm_source=")


def test_bio_links_empty_base_url_or_registry_give_no_links():
    assert bio_links(ACCOUNTS, "") == []
    assert bio_links([], "https://jelapeche.com") == []
