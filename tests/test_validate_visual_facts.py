# Тикет 02 плана 2026-08-14-visual-contour: схема визуальных фактов — новый
# валидируемый kind. Контракт честности вшит в СХЕМУ, а не только в промпт
# движка: факт без ссылки на конкретное медиа невалиден, гипотезы структурно
# не смешиваются с фактами, observed_media=none запрещает визуальные выводы.
# Этой схемой cf vision (тикет 05) режет мусорные ответы модели до Sheets.
import json

from cf.validate import validate_json_file

VALID_FACTS = {
    "observed_media": "cover",
    "visual_evidence": [
        {"fact": "Крупный план лица с текстовой плашкой сверху",
         "media_ref": "cover"},
    ],
    "visual_hook": "Лицо крупным планом + плашка с вопросом",
    "screen_text": ["А ВЫ ТАК НОСИТЕ?"],
    "shooting_format": "вертикаль, съёмка с рук",
    "main_emotion": "удивление",
    "formatting_pattern": "текст-плашка в верхней трети кадра",
    "visual_hypotheses": ["судя по капшену, дальше примерка нескольких образов"],
    "unsupported_visual_claims": [
        "монтаж, динамику и переходы по одной обложке утверждать нельзя"],
    "engine": "headless-subscription",
    "model": "claude-haiku-4-5-20251001",
    "analyzed_at": "2026-08-14T12:00:00+00:00",
}


def write(tmp_path, data):
    p = tmp_path / "facts.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def variant(**over):
    data = json.loads(json.dumps(VALID_FACTS))
    data.update(over)
    return data


def test_valid_facts_pass(tmp_path):
    assert validate_json_file("visual-facts", write(tmp_path, VALID_FACTS)) == []


def test_frames_media_ref_valid_when_frames_observed(tmp_path):
    # Этап 2 (кадры) не требует переделки контракта: media_ref уже умеет frame:N.
    data = variant(observed_media="frames",
                   visual_evidence=[{"fact": "смена сцены: улица -> примерочная",
                                     "media_ref": "frame:7"}])
    assert validate_json_file("visual-facts", write(tmp_path, data)) == []


def test_evidence_without_media_ref_rejected(tmp_path):
    # Железное правило №1: факт без ссылки на конкретное медиа не существует.
    data = variant(visual_evidence=[{"fact": "динамичный монтаж"}])
    errors = validate_json_file("visual-facts", write(tmp_path, data))
    assert errors and "media_ref" in errors[0]


def test_evidence_with_garbage_media_ref_rejected(tmp_path):
    data = variant(visual_evidence=[{"fact": "x", "media_ref": "видео целиком"}])
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []


def test_unknown_observed_media_rejected(tmp_path):
    assert validate_json_file(
        "visual-facts", write(tmp_path, variant(observed_media="video"))) != []


def test_none_observed_forbids_visual_evidence(tmp_path):
    # observed_media=none -> визуальные выводы не делаются (n8n-референс):
    # непустой visual_evidence при none — структурное нарушение честности.
    data = variant(observed_media="none")
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []
    empty = variant(observed_media="none", visual_evidence=[])
    assert validate_json_file("visual-facts", write(tmp_path, empty)) == []


def test_cover_only_forbids_frame_refs(tmp_path):
    # По одной обложке нельзя цитировать кадры, которых движок не видел.
    data = variant(visual_evidence=[{"fact": "x", "media_ref": "frame:3"}])
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []


def test_hypotheses_do_not_mix_into_evidence(tmp_path):
    # Гипотеза строкой в visual_evidence (без fact/media_ref) — невалидна:
    # гипотезы живут только в visual_hypotheses.
    data = variant(visual_evidence=["наверное, дальше динамичный монтаж"])
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []


def test_arbitrary_garbage_rejected(tmp_path):
    assert validate_json_file(
        "visual-facts", write(tmp_path, {"hello": "world"})) != []


def test_unknown_extra_key_rejected(tmp_path):
    # additionalProperties=false: выдуманное моделью поле — сигнал мусорного
    # ответа, а не расширение контракта.
    assert validate_json_file(
        "visual-facts", write(tmp_path, variant(vibe="динамично"))) != []


def test_missing_engine_attribution_rejected(tmp_path):
    # engine/model/analyzed_at — атрибуция разбора (правило №1: откуда факт).
    data = variant()
    del data["engine"]
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []


# --- Тикет 03 этапа 2 (кадры): опциональные поля кадрового режима ---

FRAMES_FIELDS = {
    "narrative_structure": "хук-вопрос в первых кадрах, примерка, финал-CTA",
    "cta_visual": "в финальных кадрах — плашка «подпишись» поверх лица",
    "frames_analyzed": 15,
}


def test_frames_mode_fields_valid_with_mixed_refs(tmp_path):
    # Все три поля + смесь cover- и frame-ссылок: «видел кадры» не запрещает
    # обложку (она могла подаваться тоже).
    data = variant(observed_media="frames",
                   visual_evidence=[{"fact": "лицо крупно", "media_ref": "cover"},
                                    {"fact": "смена сцены", "media_ref": "frame:7"}],
                   **FRAMES_FIELDS)
    assert validate_json_file("visual-facts", write(tmp_path, data)) == []


def test_frames_fields_forbidden_for_cover_and_none(tmp_path):
    # Зеркало запрета frame-ссылок по одной обложке: поля кадрового режима
    # легальны только при observed_media=frames.
    for observed, extra in (("cover", {}),
                            ("none", {"visual_evidence": []})):
        for field, value in FRAMES_FIELDS.items():
            data = variant(observed_media=observed, **extra, **{field: value})
            errors = validate_json_file("visual-facts", write(tmp_path, data))
            assert errors != [], (observed, field)


def test_frames_analyzed_must_be_integer(tmp_path):
    data = variant(observed_media="frames", frames_analyzed="пятнадцать")
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []
    data = variant(observed_media="frames", frames_analyzed=14.5)
    assert validate_json_file("visual-facts", write(tmp_path, data)) != []


def test_v1_facts_without_frames_fields_stay_valid(tmp_path):
    # Расширение, не ломка: выход v1 (и cover, и frames без новых полей) легален.
    assert validate_json_file("visual-facts", write(tmp_path, VALID_FACTS)) == []
    data = variant(observed_media="frames",
                   visual_evidence=[{"fact": "x", "media_ref": "frame:1"}])
    assert validate_json_file("visual-facts", write(tmp_path, data)) == []
