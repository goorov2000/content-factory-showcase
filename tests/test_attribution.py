from datetime import date

import pytest

from cf.attribution import (WINDOW_DAYS, attribute_clicks, last_known_views,
                            month_bounds, source_query_from_raw_json)
from cf.collect.performance import FAILED_MEASUREMENT_MARKER


def test_hashtag_attribution():
    raw = '{"searchHashtag": {"name": "мужскаяодежда", "views": 1}, "id": "1"}'
    assert source_query_from_raw_json(raw) == "hashtag:#мужскаяодежда"


def test_query_attribution():
    raw = '{"searchQuery": "outfit ideas men", "id": "1"}'
    assert source_query_from_raw_json(raw) == "query:outfit ideas men"


def test_hashtag_wins_over_query():
    raw = '{"searchHashtag": {"name": "menswear"}, "searchQuery": "menswear"}'
    assert source_query_from_raw_json(raw) == "hashtag:#menswear"


def test_unknown_on_missing_fields():
    assert source_query_from_raw_json('{"id": "1"}') == "unknown"


def test_unknown_on_broken_json():
    assert source_query_from_raw_json('{"id": tru') == "unknown"
    assert source_query_from_raw_json("") == "unknown"


# ── По-роликовая атрибуция переходов (UTM-контур, тикет 05) ───────────────────
# Оценка — модель, поэтому её свойства зафиксированы железно: сумма
# распределённого за день равна остатку дня, ролик вне 14-дневного окна не
# получает ничего, день без роликов уходит в unattributed, результат
# детерминирован (не зависит от порядка строк на входе).

JUNE = (date(2026, 6, 1), date(2026, 6, 30))


def _daily(day, account, content="", visits=0):
    """Дневная строка CF UTM Traffic — схема cf.collect.metrika (row_kind=daily)."""
    return {"utm_id": f"d-{day}-{account}-{content or '-'}", "row_kind": "daily",
            "date": day, "month": day[:7], "account": account,
            "utm_campaign": account, "utm_content": content, "reel_id": content,
            "visits": visits, "users": visits, "collected_at": ""}


def _reel(rid, account, published_at):
    return {"reel_id": rid, "brief_id": f"b-{rid}", "account": account,
            "published_at": published_at}


def _perf(rid, views, measured_at="2026-06-20", notes=""):
    return {"reel_id": rid, "views": views, "er": "0.05",
            "measured_at": measured_at, "eval_notes": notes}


def _order(order_id, rid, revenue, status="confirmed", order_date="2026-06-10"):
    return {"order_id": order_id, "status": status, "order_date": order_date,
            "revenue": revenue, "account": "acc-1", "reel_id": rid,
            "utm_content": rid}


# ── точные клики ──────────────────────────────────────────────────────────────

def test_exact_clicks_sum_tagged_rows_per_reel():
    utm = [_daily("2026-06-10", "acc-1", content="r1", visits=3),
           _daily("2026-06-11", "acc-1", content="r1", visits=4),
           _daily("2026-06-11", "acc-1", content="r2", visits=5)]
    out = attribute_clicks(utm, [], [], [], *JUNE)
    assert out["reels"]["r1"]["clicks_exact"] == 7
    assert out["reels"]["r2"]["clicks_exact"] == 5
    # точные строки оценкой не являются
    assert out["reels"]["r1"]["clicks_estimated"] == 0
    assert out["reels"]["r1"]["estimated"] is False
    assert out["unattributed_total"] == 0


def test_monthly_rows_do_not_participate():
    # Месячная строка — биллинговая цифра, в по-роликовую атрибуцию не входит.
    utm = [{"utm_id": "m-2026-06-acc-1", "row_kind": "monthly", "date": "",
            "month": "2026-06", "account": "acc-1", "utm_campaign": "acc-1",
            "utm_content": "", "reel_id": "", "visits": 100, "users": 90,
            "collected_at": ""}]
    out = attribute_clicks(utm, [], [], [], *JUNE)
    assert out["reels"] == {}
    assert out["unattributed_total"] == 0


# ── инварианты распределения остатка ──────────────────────────────────────────

def test_remainder_split_proportional_to_last_known_views():
    utm = [_daily("2026-06-15", "acc-1", visits=90)]
    reels = [_reel("r1", "acc-1", "2026-06-14"), _reel("r2", "acc-1", "2026-06-10")]
    perf = [_perf("r1", "3000"), _perf("r2", "1000")]
    out = attribute_clicks(utm, reels, perf, [], *JUNE)
    assert out["reels"]["r1"]["clicks_estimated"] == pytest.approx(67.5)
    assert out["reels"]["r2"]["clicks_estimated"] == pytest.approx(22.5)
    assert out["reels"]["r1"]["estimated"] is True
    # сумма распределённого за день == остатку дня
    total = sum(r["clicks_estimated"] for r in out["reels"].values())
    assert total == pytest.approx(90)
    assert out["unattributed_total"] == 0


def test_no_views_at_all_split_equally():
    utm = [_daily("2026-06-15", "acc-1", visits=9)]
    reels = [_reel("r1", "acc-1", "2026-06-14"), _reel("r2", "acc-1", "2026-06-13"),
             _reel("r3", "acc-1", "2026-06-12")]
    out = attribute_clicks(utm, reels, [], [], *JUNE)
    assert [out["reels"][r]["clicks_estimated"] for r in ("r1", "r2", "r3")] \
        == [pytest.approx(3)] * 3


def test_zero_views_reel_next_to_measured_one_gets_zero():
    # Просмотры есть хотя бы у одного — деление пропорциональное, нулевой вес
    # честно даёт ноль (не «поровну»); инвариант суммы сохраняется.
    utm = [_daily("2026-06-15", "acc-1", visits=50)]
    reels = [_reel("r1", "acc-1", "2026-06-14"), _reel("r2", "acc-1", "2026-06-13")]
    perf = [_perf("r1", "1000"), _perf("r2", "0")]
    out = attribute_clicks(utm, reels, perf, [], *JUNE)
    assert out["reels"]["r1"]["clicks_estimated"] == pytest.approx(50)
    assert out["reels"]["r2"]["clicks_estimated"] == 0
    total = sum(r["clicks_estimated"] for r in out["reels"].values())
    assert total == pytest.approx(50)


def test_reel_outside_14_day_window_gets_zero():
    # Окно — 14 дней жизни начиная со дня публикации: D-13 ещё внутри,
    # D-14 уже снаружи, будущая публикация не участвует.
    utm = [_daily("2026-06-20", "acc-1", visits=30)]
    reels = [_reel("in-edge", "acc-1", "2026-06-07"),     # 13 дней назад — внутри
             _reel("out-edge", "acc-1", "2026-06-06"),    # ровно 14 — снаружи
             _reel("future", "acc-1", "2026-06-21"),      # ещё не вышел
             _reel("same-day", "acc-1", "2026-06-20")]    # день публикации — внутри
    out = attribute_clicks(utm, reels, [], [], *JUNE)
    assert set(out["reels"]) == {"in-edge", "same-day"}
    assert sum(r["clicks_estimated"] for r in out["reels"].values()) \
        == pytest.approx(30)


def test_day_without_window_reels_goes_unattributed():
    utm = [_daily("2026-06-25", "acc-1", visits=40),  # роликов в окне нет
           _daily("2026-06-25", "acc-2", visits=15)]  # у аккаунта роликов нет вовсе
    reels = [_reel("old", "acc-1", "2026-05-01")]
    out = attribute_clicks(utm, reels, [], [], *JUNE)
    assert out["reels"] == {}
    assert out["unattributed_total"] == 55


def test_sum_invariant_over_mixed_days_and_accounts():
    # Несколько дней и аккаунтов: распределённое + unattributed == все остатки.
    utm = [_daily("2026-06-10", "acc-1", visits=10),
           _daily("2026-06-11", "acc-1", visits=20),
           _daily("2026-06-11", "acc-2", visits=7),    # без роликов -> unattributed
           _daily("2026-06-11", "acc-1", content="r1", visits=99)]  # точная, не остаток
    reels = [_reel("r1", "acc-1", "2026-06-09"), _reel("r2", "acc-1", "2026-06-10")]
    perf = [_perf("r1", "300"), _perf("r2", "100")]
    out = attribute_clicks(utm, reels, perf, [], *JUNE)
    distributed = sum(r["clicks_estimated"] for r in out["reels"].values())
    assert distributed + out["unattributed_total"] == pytest.approx(10 + 20 + 7)
    assert out["unattributed_total"] == 7
    assert out["reels"]["r1"]["clicks_exact"] == 99


def test_duplicate_published_rows_do_not_double_count():
    # Ролик, отмеченный опубликованным дважды, — один участник распределения.
    utm = [_daily("2026-06-15", "acc-1", visits=10)]
    reels = [_reel("r1", "acc-1", "2026-06-14"), _reel("r1", "acc-1", "2026-06-13"),
             _reel("r2", "acc-1", "2026-06-13")]
    out = attribute_clicks(utm, reels, [], [], *JUNE)
    assert out["reels"]["r1"]["clicks_estimated"] == pytest.approx(5)
    assert out["reels"]["r2"]["clicks_estimated"] == pytest.approx(5)


def test_deterministic_regardless_of_input_order():
    utm = [_daily("2026-06-15", "acc-1", visits=90),
           _daily("2026-06-16", "acc-1", visits=10),
           _daily("2026-06-15", "acc-1", content="r2", visits=3)]
    reels = [_reel("r1", "acc-1", "2026-06-14"), _reel("r2", "acc-1", "2026-06-10")]
    perf = [_perf("r1", "3000"), _perf("r2", "1000")]
    orders = [_order("o1", "r1", 500)]
    straight = attribute_clicks(utm, reels, perf, orders, *JUNE)
    reordered = attribute_clicks(list(reversed(utm)), list(reversed(reels)),
                                 list(reversed(perf)), orders, *JUNE)
    assert straight == reordered
    assert straight == attribute_clicks(utm, reels, perf, orders, *JUNE)


# ── границы периода ───────────────────────────────────────────────────────────

def test_period_bounds_filter_daily_rows_and_orders():
    utm = [_daily("2026-05-31", "acc-1", content="r1", visits=5),   # до периода
           _daily("2026-06-01", "acc-1", content="r1", visits=2),
           _daily("2026-07-01", "acc-1", content="r1", visits=8)]   # после
    orders = [_order("in", "r1", 100, order_date="2026-06-05"),
              _order("out", "r1", 900, order_date="2026-07-02")]
    out = attribute_clicks(utm, [], [], orders, *JUNE)
    assert out["reels"]["r1"]["clicks_exact"] == 2
    assert out["reels"]["r1"]["revenue"] == 100
    assert out["reels"]["r1"]["orders"] == 1


def test_none_bounds_take_everything():
    utm = [_daily("2026-05-31", "acc-1", content="r1", visits=5),
           _daily("2026-07-01", "acc-1", content="r1", visits=8)]
    out = attribute_clicks(utm, [], [], [])
    assert out["reels"]["r1"]["clicks_exact"] == 13


# ── заказы ролика ─────────────────────────────────────────────────────────────

def test_orders_only_confirmed_by_reel_id_revenue_summed():
    orders = [_order("o1", "r1", 1000),
              _order("o2", "r1", 990),
              _order("o3", "r1", 500, status="candidate"),
              _order("o4", "r1", 500, status="returned"),
              _order("o5", "r1", 500, status="rejected"),
              _order("o6", "", 500)]          # без метки ролика — не по-роликовый
    out = attribute_clicks([], [], [], orders, *JUNE)
    assert out["reels"]["r1"]["orders"] == 2
    assert out["reels"]["r1"]["revenue"] == 1990
    assert set(out["reels"]) == {"r1"}


def test_order_reel_id_falls_back_to_utm_content():
    order = _order("o1", "", 700)
    order["utm_content"] = "r9"                # reel_id пуст, метка на месте
    out = attribute_clicks([], [], [], [order], *JUNE)
    assert out["reels"]["r9"]["orders"] == 1
    assert out["reels"]["r9"]["revenue"] == 700


# ── последние известные просмотры ─────────────────────────────────────────────

def test_last_known_views_latest_measurement_wins():
    perf = [_perf("r1", "100", measured_at="2026-06-10"),
            _perf("r1", "900", measured_at="2026-06-19"),
            _perf("r1", "500", measured_at="2026-06-12")]
    assert last_known_views(perf) == {"r1": 900}


def test_last_known_views_skips_failed_measurements():
    # Сбойный замер (маркер упавшего Apify-рана) не считается «известными
    # просмотрами» — как H16 в evalprep: нулевой сбой не затирает честный замер.
    perf = [_perf("r1", "800", measured_at="2026-06-10"),
            _perf("r1", "0", measured_at="2026-06-19",
                  notes=f"x; {FAILED_MEASUREMENT_MARKER}; y")]
    assert last_known_views(perf) == {"r1": 800}


# ── month_bounds ──────────────────────────────────────────────────────────────

def test_month_bounds_regular_and_leap():
    assert month_bounds("2026-06") == (date(2026, 6, 1), date(2026, 6, 30))
    assert month_bounds("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
    assert month_bounds("мусор") is None
    assert month_bounds("") is None


def test_window_days_constant_is_14():
    # Число из спеки (решение гриля №5): «опубликованные в последние 14 дней».
    assert WINDOW_DAYS == 14
