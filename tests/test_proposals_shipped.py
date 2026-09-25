"""Гвард закоммиченных source-proposal'ов (аудит 2026-07-26).

Ни один тест раньше не читал реальный каталог proposals/. Опечатка в поле
`source` (например 'query:мужские-образы' через дефис вместо пробела) всплывала бы
только на проде, в момент `cf apply-sources`, — и молча: remove просто не нашёл бы
запись, summary['missing'] уехал бы в stdout, а оператор увидел бы «применено».

Проверяем ВСЕ proposals/*.json, а не только свежий: правило №4 (sources правится
только через proposal) держится на том, что каждый лежащий тут файл применим.

Markdown-proposals намеренно НЕ проверяются целиком: validate_proposal_text жёстко
требует `status: proposed`, а уже применённые файлы имеют approved/rejected —
такой тест был бы красным с первого запуска. Свежие proposal'ы прогоняются
оператором через `cf validate proposal` вручную.
"""
import json
from pathlib import Path

import pytest

from cf.io import read_json
from cf.sourcepatch import apply_proposal_to_registry
from cf.validate import validate_json_data, validate_json_file

REPO = Path(__file__).resolve().parents[1]
SOURCE_PROPOSALS = sorted((REPO / "proposals").glob("*.json"))


@pytest.mark.skipif(not SOURCE_PROPOSALS, reason="source-proposal'ов в репо нет")
@pytest.mark.parametrize("path", SOURCE_PROPOSALS, ids=lambda p: p.name)
def test_shipped_source_proposals_apply_cleanly(path):
    assert validate_json_file("source-proposal", str(path)) == []
    prop = read_json(str(path))
    registry = read_json(str(REPO / "sources" / f"{prop['platform']}.json"))
    records, summary = apply_proposal_to_registry(registry, prop, today="2026-07-26")
    # missing — источник из proposal, которого нет в реестре: опечатка либо
    # proposal, отставший от реестра. Применение такого — молчаливый no-op.
    assert summary["missing"] == [], \
        f"{path.name}: источники не найдены в реестре: {summary['missing']}"
    assert validate_json_data("sources", records) == [], \
        f"{path.name}: после применения реестр не проходит схему"


def test_retire_low_yield_proposal_retires_exactly_five():
    # Именной якорь: proposal 26.07 снимает ровно пять search-источников и НЕ
    # трогает query:мужские образы (у него winner в formulas/спорт-фитнес/
    # star-peak-moment.json — правило Source Tuner «источник с winner'ом не удаляем»).
    path = REPO / "proposals" / "2026-07-26-retire-low-yield-tiktok-sources.json"
    if not path.is_file():
        pytest.skip("proposal уже применён и убран из каталога")
    prop = read_json(str(path))
    registry = read_json(str(REPO / "sources" / "tiktok.json"))
    records, summary = apply_proposal_to_registry(registry, prop, today="2026-07-26")
    assert summary["retired"] == 5
    retired = {r["query"] for r in records if r.get("status") == "retired"}
    assert "мужские образы" not in retired
    # хэштеги-двойники ретайрящихся запросов обязаны остаться активными
    active = {(r["kind"], r["query"]) for r in records if r.get("status") == "active"}
    assert ("hashtag", "мужскаяодежда") in active
    assert ("hashtag", "мужскойстиль") in active


def test_shipped_source_proposals_have_rationale_doc():
    # Машиночитаемый JSON без человеческого .md рядом — evidence, которую никто
    # не ревьюил (железное правило №1: без evidence правок не бывает).
    for path in SOURCE_PROPOSALS:
        doc = json.loads(path.read_text(encoding="utf-8")).get("rationale_doc")
        assert doc, f"{path.name}: нет rationale_doc"
        assert (REPO / doc).is_file(), f"{path.name}: rationale_doc {doc} не найден"
