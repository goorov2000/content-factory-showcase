import json

from cf.validate import validate_json_file

VALID_FORMULA = {
    "name": "pets-question-hook",
    "niche": "pets",
    "hook_structure": {"timing": "0-3s", "elements": ["question"]},
    "problem_definition": "Владельцы питомцев не удерживают внимание в первые секунды",
    "solution_structure": {"beats": ["хук-вопрос", "демонстрация", "результат"]},
    "visual_requirements": ["крупный план питомца в первые 2 секунды"],
    "cta_type": "follow",
    "prohibitions": ["длинное интро"],
    "evidence": {
        "source_urls": ["https://tiktok.com/@a/video/1",
                        "https://tiktok.com/@b/video/2",
                        "https://tiktok.com/@c/video/3"],
        "avg_views": 120000,
        "avg_er": 0.081
    },
    "confidence": "medium",
    "conditions": "короткие ролики до 30с в нише pets",
    "source_pattern_ids": ["pets-hook-question-01"],
    "version": 1,
    "updated_at": "2026-07-09T00:00:00+00:00"
}


def write(tmp_path, data):
    p = tmp_path / "formula.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_valid_formula_passes(tmp_path):
    assert validate_json_file("formula", write(tmp_path, VALID_FORMULA)) == []


def test_evidence_gate_two_urls_fail(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["source_urls"] = bad["evidence"]["source_urls"][:2]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_low_confidence_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["confidence"] = "low"
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_missing_conditions_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    del bad["conditions"]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_evidence_gate_three_identical_urls_fail(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["source_urls"] = ["https://tiktok.com/@a/video/1"] * 3
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_empty_string_in_source_pattern_ids_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["source_pattern_ids"] = [""]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_empty_string_in_hook_elements_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["hook_structure"]["elements"] = [""]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_empty_string_in_visual_requirements_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["visual_requirements"] = [""]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_empty_string_in_prohibitions_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["prohibitions"] = [""]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


# --- Тикет 08 визуального контура: опциональный visual_refs в evidence ---


def test_visual_refs_valid_cover_and_frame(tmp_path):
    ok = json.loads(json.dumps(VALID_FORMULA))
    ok["evidence"]["visual_refs"] = [
        {"source_url": "https://tiktok.com/@a/video/1", "media": "cover"},
        {"source_url": "https://tiktok.com/@b/video/2", "media": "frame",
         "frame_index": 3},
    ]
    assert validate_json_file("formula", write(tmp_path, ok)) == []


def test_visual_refs_absent_is_legal(tmp_path):
    # Старые формулы и чисто текстовый evidence не ломаются.
    assert "visual_refs" not in VALID_FORMULA["evidence"]
    assert validate_json_file("formula", write(tmp_path, VALID_FORMULA)) == []


def test_visual_ref_without_source_url_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["visual_refs"] = [{"media": "cover"}]
    errors = validate_json_file("formula", write(tmp_path, bad))
    assert errors and "source_url" in errors[0]


def test_visual_ref_media_outside_enum_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["visual_refs"] = [
        {"source_url": "https://t.tk/1", "media": "видео"}]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_visual_ref_frame_index_not_integer_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["visual_refs"] = [
        {"source_url": "https://t.tk/1", "media": "frame", "frame_index": "три"}]
    assert validate_json_file("formula", write(tmp_path, bad)) != []


def test_visual_ref_frame_without_index_rejected(tmp_path):
    # «кадр видео X» без номера кадра — не проверяемое основание.
    bad = json.loads(json.dumps(VALID_FORMULA))
    bad["evidence"]["visual_refs"] = [
        {"source_url": "https://t.tk/1", "media": "frame"}]
    assert validate_json_file("formula", write(tmp_path, bad)) != []
