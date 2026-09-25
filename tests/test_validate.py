import json

from cf.validate import validate_json_file

VALID_PATTERNS = {
    "meta": {"niche": "pets", "generated_at": "2026-07-09T00:00:00+00:00",
             "source_profile": "agent-runtime/profiles/x.json"},
    "patterns": [{
        "pattern_id": "pets-hook-question-01",
        "description": "Хук-вопрос в первые 2 секунды даёт выше ER",
        "evidence": {
            "source_urls": ["https://tiktok.com/@a/video/1",
                            "https://tiktok.com/@b/video/2",
                            "https://tiktok.com/@c/video/3"],
            "avg_views": 120000,
            "avg_er": 0.081
        },
        "confidence": "medium"
    }]
}


def write(tmp_path, data):
    p = tmp_path / "artifact.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_valid_patterns_pass(tmp_path):
    assert validate_json_file("patterns", write(tmp_path, VALID_PATTERNS)) == []


def test_pattern_without_evidence_urls_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["evidence"]["source_urls"] = []
    errors = validate_json_file("patterns", write(tmp_path, bad))
    assert errors and "source_urls" in errors[0]


def test_bad_confidence_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["confidence"] = "great"
    assert validate_json_file("patterns", write(tmp_path, bad)) != []


def test_empty_patterns_array_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"] = []
    assert validate_json_file("patterns", write(tmp_path, bad)) != []


def test_duplicate_source_urls_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["evidence"]["source_urls"] = ["https://tiktok.com/@a/video/1"] * 3
    assert validate_json_file("patterns", write(tmp_path, bad)) != []


def test_pattern_with_two_source_urls_rejected(tmp_path):
    # Железное правило (pattern-analyzer.md): < 3 URL → паттерн не пишется.
    # Схема обязана отклонять паттерн с 2 источниками (minItems=3).
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["evidence"]["source_urls"] = [
        "https://tiktok.com/@a/video/1", "https://tiktok.com/@b/video/2"]
    errors = validate_json_file("patterns", write(tmp_path, bad))
    assert errors and "source_urls" in errors[0]


def test_pattern_with_three_source_urls_valid(tmp_path):
    ok = json.loads(json.dumps(VALID_PATTERNS))
    ok["patterns"][0]["evidence"]["source_urls"] = [
        "https://tiktok.com/@a/video/1", "https://tiktok.com/@b/video/2",
        "https://tiktok.com/@c/video/3"]
    assert validate_json_file("patterns", write(tmp_path, ok)) == []


def test_meta_source_tab_accepted(tmp_path):
    # P2.16: meta.source_tab — часть контракта; схема его описывает и принимает.
    ok = json.loads(json.dumps(VALID_PATTERNS))
    ok["meta"]["source_tab"] = "raw_tiktok"
    assert validate_json_file("patterns", write(tmp_path, ok)) == []


# --- source-proposal: грамматика поля source (аудит 2026-07-26) -------------
# Маркер обязан нести префикс kind ('hashtag:#x' / 'query:q') — ровно то, что
# строит cf.collect.sources.source_marker. Голое имя тега молча ломает применение:
# _strip режет строку по первому ':' и берёт ХВОСТ, так что 'mensoutfits' даёт
# пустой query, а несколько таких записей схлопываются дедупликацией в одну.

VALID_SOURCE_PROPOSAL = {
    "platform": "instagram",
    "generated_at": "2026-07-26T07:09:05+00:00",
    "status": "pending",
    "remove": [{"source": "hashtag:#outfitideas",
                "reason": "rows 46, target_yield 0.04 за две недели", "stats": {}}],
    "add": [{"source": "hashtag:#mensoutfits", "kind": "hashtag",
             "evidence": "15x в капшенах целевых ниш, rows 3/3 целевых"}],
}


def source_proposal(**over):
    prop = json.loads(json.dumps(VALID_SOURCE_PROPOSAL))
    prop.update(over)
    return prop


def test_valid_source_proposal_passes(tmp_path):
    assert validate_json_file(
        "source-proposal", write(tmp_path, VALID_SOURCE_PROPOSAL)) == []


def test_add_source_without_kind_prefix_rejected(tmp_path):
    # Класс ошибки из proposals/2026-07-26-sources-instagram.json: агент скопировал
    # голое имя из candidate_hashtags[].hashtag, схема без pattern сказала OK.
    bad = source_proposal(add=[{"source": "mensoutfits", "kind": "hashtag",
                                "evidence": "15x в капшенах целевых ниш"}])
    errors = validate_json_file("source-proposal", write(tmp_path, bad))
    assert errors and "add/0/source" in errors[0]


def test_remove_source_without_kind_prefix_rejected(tmp_path):
    # Решётка без префикса kind — тот же no-op на применении, только в remove.
    bad = source_proposal(remove=[{"source": "#outfitideas",
                                   "reason": "rows 46, target_yield 0.04", "stats": {}}])
    errors = validate_json_file("source-proposal", write(tmp_path, bad))
    assert errors and "remove/0/source" in errors[0]


def test_hashtag_marker_without_hash_rejected(tmp_path):
    # 'hashtag:mensoutfits' пережил бы _strip (lstrip('#') терпит отсутствие
    # решётки), но грамматика source_query одна — маркер обязан быть с решёткой.
    bad = source_proposal(add=[{"source": "hashtag:mensoutfits", "kind": "hashtag",
                                "evidence": "15x в капшенах целевых ниш"}])
    assert validate_json_file("source-proposal", write(tmp_path, bad)) != []


def test_query_marker_with_spaces_stays_valid(tmp_path):
    # Пробелы в поисковом запросе законны ('query:pov парень' в закоммиченном
    # proposal 2026-07-26-retire-low-yield-tiktok-sources.json) — pattern не смеет
    # их резать.
    ok = source_proposal(remove=[{"source": "query:pov парень",
                                  "reason": "rows 214, target_yield 0.02", "stats": {}}])
    assert validate_json_file("source-proposal", write(tmp_path, ok)) == []


def test_empty_marker_value_rejected(tmp_path):
    # 'query:' — ровно тот пустой query, на котором споткнулась схема реестра.
    bad = source_proposal(add=[{"source": "query:", "kind": "query",
                                "evidence": "пустой маркер, evidence-заглушка"}])
    assert validate_json_file("source-proposal", write(tmp_path, bad)) != []


# --- Тикет 08 визуального контура: опциональный visual_refs у паттернов ---


def test_pattern_visual_refs_valid(tmp_path):
    ok = json.loads(json.dumps(VALID_PATTERNS))
    ok["patterns"][0]["evidence"]["visual_refs"] = [
        {"source_url": "https://tiktok.com/@a/video/1", "media": "cover"},
        {"source_url": "https://tiktok.com/@b/video/2", "media": "frame",
         "frame_index": 7},
    ]
    assert validate_json_file("patterns", write(tmp_path, ok)) == []


def test_pattern_visual_ref_garbage_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_PATTERNS))
    bad["patterns"][0]["evidence"]["visual_refs"] = [
        {"source_url": "https://t.tk/1", "media": "frame"}]  # кадр без номера
    assert validate_json_file("patterns", write(tmp_path, bad)) != []


# ── ревью 14.09.2026: схема проверяется сама, прежде чем проверять данные ──

def test_all_shipped_schemas_are_valid_json_schema():
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from cf.validate import SCHEMAS
    for kind, rel in SCHEMAS.items():
        Draft202012Validator.check_schema(json.loads(Path(rel).read_text(encoding="utf-8")))


def test_broken_schema_returns_error_list_instead_of_crashing(tmp_path):
    import json
    from cf.validate import SCHEMAS, validate_json_data
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (tmp_path / SCHEMAS["brief"]).write_text(json.dumps({
        "type": "object", "properties": {"_comment_x": ["это не схема"]}}), encoding="utf-8")
    errors = validate_json_data("brief", {"_comment_x": ["y"]}, schema_root=tmp_path)
    assert errors and "схема" in errors[0]
