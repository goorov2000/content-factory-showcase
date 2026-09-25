import argparse

from cf.cli import cmd_eval_prep
from cf.io import read_json

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def _tables():
    return {
        "briefs": [{"brief_id": "b1", "formula_id": "f1", "prompt_version": "v1",
                    "review_status": "approved"}],
        "reels": [{"reel_id": "R1", "brief_id": "b1", "published_at": "2026-07-01"}],
        "performance": [{"reel_id": "R1", "brief_id": "b1", "views": "1000",
                         "er": "0.05", "measured_at": "2026-07-08"}],
        "prompt_versions": [{"prompt_id": "brief-pets", "version": "v1",
                             "github_path": "x", "active": "TRUE"}],
    }


def test_cmd_eval_prep_writes_dataset_with_join_health(tmp_path, capsys):
    sheets = FakeSheets(_tables())
    assert cmd_eval_prep(sheets, ns(since=None, out_dir=str(tmp_path))) == 0

    ds = read_json(next(tmp_path.glob("*-eval-dataset.json")))
    assert ds["status"] == "ok"
    assert ds["join_health"] == {"unknown_version_share": 0.0, "briefs_not_found": 0}
    assert len(ds["rows"]) == 1
    assert ds["rows"][0]["days_after_publish"] == 7   # published_at из reels проведён
    assert ds["reviewer_pass_rate"] == 1.0

    out = capsys.readouterr().out
    assert "join_health" in out
    assert "status: ok" in out


def test_cmd_eval_prep_summary_reports_comparable_pairs(tmp_path, capsys):
    # P5.13: сводка печатает число сравнимых A/B-пар и сколько из них ok.
    briefs, reels, perf = [], [], []
    for label in ("v1", "v2"):
        for i in range(5):                                     # 5 reels у каждой версии
            bid, rid = f"{label}b{i}", f"{label}r{i}"
            pub = f"2026-07-0{(i % 3) + 1}"                     # окна 01-03 пересекаются
            briefs.append({"brief_id": bid, "formula_id": "f1", "prompt_version": label,
                           "review_status": "approved"})
            reels.append({"reel_id": rid, "brief_id": bid, "published_at": pub})
            perf.append({"reel_id": rid, "brief_id": bid, "prompt_version": label,
                         "views": "1000", "er": "0.05", "measured_at": pub})
    sheets = FakeSheets({"briefs": briefs, "reels": reels, "performance": perf,
                         "prompt_versions": [{"prompt_id": "brief-x", "version": "v1",
                                              "github_path": "x", "active": "TRUE"}]})
    assert cmd_eval_prep(sheets, ns(since=None, out_dir=str(tmp_path))) == 0
    assert "comparable A/B pairs: 1 (ok: 1)" in capsys.readouterr().out


class _NoReelsSheets(FakeSheets):
    """read_rows('reels') падает — вкладки CF Published Reels нет в таблице."""

    def read_rows(self, tab_key, include_heavy=False):
        if tab_key == "reels":
            raise KeyError("вкладка reels отсутствует")
        return super().read_rows(tab_key, include_heavy)


def test_cmd_eval_prep_best_effort_without_reels_tab(tmp_path, capsys):
    tables = {k: v for k, v in _tables().items() if k != "reels"}
    sheets = _NoReelsSheets(tables)
    # Не падает: reels недоступна -> fallback по measured_at, датасет всё равно собран.
    assert cmd_eval_prep(sheets, ns(since=None, out_dir=str(tmp_path))) == 0

    ds = read_json(next(tmp_path.glob("*-eval-dataset.json")))
    assert len(ds["rows"]) == 1
    assert ds["rows"][0]["days_after_publish"] is None   # published_at неизвестен

    out = capsys.readouterr().out
    assert "reels" in out.lower()                        # предупреждение напечатано


# --- Тикет 05 (UTM-контур): деньги в eval-датасете ----------------------------

def test_cmd_eval_prep_enriches_dataset_with_money_tabs(tmp_path):
    tables = _tables()
    tables["reels"][0]["account"] = "acc-1"              # окно оценки по аккаунту
    tables["utm_traffic"] = [
        {"utm_id": "d-2026-07-03-acc-1-R1", "row_kind": "daily",
         "date": "2026-07-03", "month": "2026-07", "account": "acc-1",
         "utm_campaign": "acc-1", "utm_content": "R1", "reel_id": "R1",
         "visits": 5, "users": 4, "collected_at": ""},
        {"utm_id": "d-2026-07-03-acc-1--", "row_kind": "daily",
         "date": "2026-07-03", "month": "2026-07", "account": "acc-1",
         "utm_campaign": "acc-1", "utm_content": "", "reel_id": "",
         "visits": 10, "users": 8, "collected_at": ""},
    ]
    tables["orders"] = [
        {"order_id": "mk-1", "status": "confirmed", "order_date": "2026-07-05",
         "reel_id": "R1", "utm_content": "R1", "account": "acc-1",
         "revenue": 1990},
        {"order_id": "mk-2", "status": "candidate", "order_date": "2026-07-05",
         "reel_id": "R1", "utm_content": "R1", "account": "acc-1",
         "revenue": 500},                                # кандидат — не считается
    ]
    sheets = FakeSheets(tables)
    assert cmd_eval_prep(sheets, ns(since=None, out_dir=str(tmp_path))) == 0

    ds = read_json(next(tmp_path.glob("*-eval-dataset.json")))
    row = ds["rows"][0]
    assert row["clicks_exact"] == 5
    assert row["clicks_estimated"] == 10                 # единственный ролик окна
    assert row["orders"] == 1 and row["revenue"] == 1990
    agg = ds["by_prompt_version"]["f1:v1"]
    assert agg["clicks_exact"] == 5 and agg["orders"] == 1
    assert not any("CF UTM Traffic" in w or "CF Orders" in w
                   for w in ds["warnings"])


class _NoMoneySheets(FakeSheets):
    """Вкладок CF UTM Traffic / CF Orders ещё нет (сбор Метрики не запускался)."""

    def read_rows(self, tab_key, include_heavy=False):
        if tab_key in ("utm_traffic", "orders"):
            raise KeyError(f"вкладка {tab_key} отсутствует")
        return super().read_rows(tab_key, include_heavy)


def test_cmd_eval_prep_without_money_tabs_prior_shape_plus_warning(tmp_path, capsys):
    sheets = _NoMoneySheets(_tables())
    assert cmd_eval_prep(sheets, ns(since=None, out_dir=str(tmp_path))) == 0

    ds = read_json(next(tmp_path.glob("*-eval-dataset.json")))
    assert ds["status"] == "ok"                          # статус и гейты не тронуты
    assert "clicks_exact" not in ds["rows"][0]           # датасет прежней формы
    assert "orders" not in ds["rows"][0]
    assert any("CF UTM Traffic" in w and "CF Orders" in w
               for w in ds["warnings"])                  # …плюс предупреждение

    out = capsys.readouterr().out
    assert "utm_traffic" in out and "orders" in out      # WARNING в stdout
