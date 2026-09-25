import argparse
import math

import pytest

from cf.cli import cmd_cluster_other
from cf.cluster import _Vector, _cosine, _vector, cluster_rows, tokenize
from cf.io import read_json

from tests.fakes import FakeSheets


def _row(url, caption, transcript=""):
    return {"source_url": url, "caption": caption, "transcript_text": transcript,
            "niche": "other"}


def ns(**kw):
    return argparse.Namespace(**kw)


def test_tokenize_strips_urls_hashtags_stopwords():
    tokens = tokenize("Подборка для высоких мужчин #tall https://a.b и самых стильных")
    assert "подборка" in tokens and "высоких" in tokens
    assert "#tall" not in tokens and "https://a.b" not in tokens and "и" not in tokens


def test_clusters_group_similar_captions():
    tall = [_row(f"t{i}", f"образы для высоких мужчин вариант {i}") for i in range(6)]
    perfume = [_row(f"p{i}", f"мужской парфюм на лето обзор {i}") for i in range(6)]
    noise = [_row("n1", "случайное видео про кота")]
    clusters = cluster_rows(tall + perfume + noise, min_size=5)
    assert len(clusters) == 2
    sizes = sorted(c["size"] for c in clusters)
    assert sizes == [6, 6]
    top = clusters[0]
    assert top["sample_rows"] and top["top_terms"]


def test_cmd_cluster_other_dedupes_by_source_url(tmp_path):
    # P1.13.4: дубли одного source_url не должны раздувать total_other/кластеры
    # (как analyze_rows/_eligible_niches, которые дедупят перед подсчётом).
    dupes = [_row("dup", "образы для высоких мужчин вариант") for _ in range(4)]
    uniques = [_row(f"u{i}", "образы для высоких мужчин вариант {i}") for i in range(2)]
    sheets = FakeSheets({"raw_tiktok": dupes + uniques})
    rc = cmd_cluster_other(sheets, ns(tab="raw_tiktok", min_size=2, out_dir=str(tmp_path)))
    assert rc == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-clusters.json")))
    assert report["total_other"] == 3   # 4 дубля -> 1, плюс 2 уникальных


def test_cmd_cluster_other_filters_niche_and_writes_json(tmp_path):
    other_rows = [_row(f"t{i}", f"образы для высоких мужчин вариант {i}") for i in range(3)]
    kept_rows = [{"source_url": "k1", "caption": "не трогать", "transcript_text": "",
                  "niche": "стритвир"}]
    sheets = FakeSheets({"raw_tiktok": other_rows + kept_rows})
    rc = cmd_cluster_other(sheets, ns(tab="raw_tiktok", min_size=2, out_dir=str(tmp_path)))
    assert rc == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-clusters.json")))
    assert set(report.keys()) >= {"tab", "generated_at", "clusters"}
    assert report["tab"] == "raw_tiktok"
    assert report["total_other"] == 3


# --- P3.8: косинус с предвычисленными нормами --------------------------------

def test_vector_carries_precomputed_norm():
    # P3.8: норма лежит НА векторе (предвычислена один раз при построении),
    # а не пересчитывается заново на каждой паре в O(n²)-цикле.
    idf = {"кот": 2.0, "пёс": 3.0}
    vec = _vector({"caption": "кот пёс", "transcript_text": ""}, idf)
    assert set(vec.weights) == {"кот", "пёс"}
    assert vec.weights == {"кот": 2.0, "пёс": 3.0}
    # |v| = sqrt(2² + 3²) = sqrt(13)
    assert vec.norm == pytest.approx(math.sqrt(13.0))


def test_cosine_matches_hand_computed_value():
    # a=(1,0), b=(1,1): dot=1, |a|=1, |b|=sqrt(2) -> cos = 1/sqrt(2)
    a = _Vector({"x": 1.0, "y": 0.0})
    b = _Vector({"x": 1.0, "y": 1.0})
    assert _cosine(a, b) == pytest.approx(1.0 / math.sqrt(2.0))
    assert _cosine(a, b) == _cosine(b, a)
    # a=(3,4): |a|=5, cos(a,a)=1
    aa = _Vector({"x": 3.0, "y": 4.0})
    assert aa.norm == pytest.approx(5.0)
    assert _cosine(aa, aa) == pytest.approx(1.0)


def test_cosine_with_zero_vector_is_zero():
    # Нулевой/пустой вектор -> косинус 0.0, без деления на ноль (как раньше).
    zero = _Vector({})
    empty = _Vector({"x": 0.0})
    other = _Vector({"x": 1.0, "y": 2.0})
    assert zero.norm == 0.0 and empty.norm == 0.0
    assert _cosine(zero, other) == 0.0
    assert _cosine(other, zero) == 0.0
    assert _cosine(empty, other) == 0.0
    assert _cosine(zero, zero) == 0.0


def test_norm_computed_once_per_vector_not_per_pair(monkeypatch):
    # P3.8: sqrt зовётся O(n) раз (по разу на вектор при построении),
    # а не O(n²) раз (по 2 на каждую пару), как в старой реализации.
    import cf.cluster as cluster_mod
    calls = {"n": 0}
    real_sqrt = math.sqrt

    def counting_sqrt(x):
        calls["n"] += 1
        return real_sqrt(x)

    monkeypatch.setattr(cluster_mod.math, "sqrt", counting_sqrt)
    rows = [_row(f"t{i}", f"образы для высоких мужчин вариант {i}") for i in range(30)]
    cluster_rows(rows, min_size=5)
    # 30 векторов -> ровно 30 sqrt. Старый код на этих ~30 попарно-похожих
    # строках дал бы десятки sqrt (2 на пару). O(n), не O(n²).
    assert calls["n"] <= len(rows)


def test_exact_cluster_membership_preserved():
    # Эквивалентность результата: точный состав кластеров (регресс-гард на то,
    # что предвычисление норм НЕ меняет кластеризацию).
    tall = [_row(f"t{i}", "образы для высоких мужчин") for i in range(6)]
    perfume = [_row(f"p{i}", "мужской парфюм на лето обзор") for i in range(6)]
    noise = [_row("n1", "случайное видео про кота")]
    clusters = cluster_rows(tall + perfume + noise, min_size=5)
    memberships = {frozenset(c["source_urls"]) for c in clusters}
    assert memberships == {
        frozenset(f"t{i}" for i in range(6)),
        frozenset(f"p{i}" for i in range(6)),
    }
