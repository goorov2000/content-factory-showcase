import json

from cf.validate import validate_json_file

VALID_BRIEF = {
    "brief_id": "b-001",
    "source_pattern_ids": ["pets-hook-question-01"],
    "formula_id": "pets-question-hook",
    "prompt_version": "v1",
    "hook": "А вы знали, что кошки различают ваш голос из тысячи?",
    "script": "0-3с: хук-вопрос с крупным планом кошки. 3-15с: домашний эксперимент. "
              "15-25с: реакция кошки и вывод. Финал: CTA подписаться.",
    "visual_direction": "Съёмка на телефон, дневной свет, кошка в кадре с первой секунды",
    "cta": "Подпишись, чтобы узнать больше о своём питомце",
    "references": ["https://tiktok.com/@a/video/1", "https://tiktok.com/@b/video/2"]
}


def write(tmp_path, data):
    p = tmp_path / "brief.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_valid_brief_passes(tmp_path):
    assert validate_json_file("brief", write(tmp_path, VALID_BRIEF)) == []


def test_brief_without_references_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_BRIEF))
    bad["references"] = []
    errors = validate_json_file("brief", write(tmp_path, bad))
    assert errors and "references" in errors[0]


def test_brief_without_formula_fails(tmp_path):
    bad = json.loads(json.dumps(VALID_BRIEF))
    del bad["formula_id"]
    assert validate_json_file("brief", write(tmp_path, bad)) != []


def test_empty_string_in_source_pattern_ids_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_BRIEF))
    bad["source_pattern_ids"] = [""]
    assert validate_json_file("brief", write(tmp_path, bad)) != []


def test_duplicate_references_rejected(tmp_path):
    bad = json.loads(json.dumps(VALID_BRIEF))
    bad["references"] = ["https://tiktok.com/@a/video/1"] * 2
    assert validate_json_file("brief", write(tmp_path, bad)) != []
