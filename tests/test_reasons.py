import pytest

from cf.reasons import (REASON_CODES, format_reason, is_reason_code,
                        parse_reason_code)


def test_enum_contains_plan_codes():
    # Дословный enum из плана P5.12 — контракт с CLI/дашбордом/промптом,
    # плюс три кода аудита 2026-08-04 (дефекты ремесла копились в «Другое» и не
    # группировались, а Prompt Optimizer видит только повторы ПО КОДУ).
    assert set(REASON_CODES) == {
        "reference_mismatch", "female_reference", "prohibition_violation",
        "weak_hook", "not_producible", "other",
        "derivative_script", "self_contradiction", "unfilled_placeholder"}


def test_other_stays_last_in_the_dropdown():
    # Порядок словаря = порядок в дропдауне дашборда: «Другое» обязано быть
    # последним пунктом, иначе оператор выбирает его вместо конкретного кода.
    assert list(REASON_CODES)[-1] == "other"


def test_is_reason_code():
    assert is_reason_code("weak_hook")
    assert not is_reason_code("bogus")
    assert not is_reason_code("")


def test_format_reason_bracket_prefix():
    assert format_reason("reference_mismatch") == "[reference_mismatch]"
    assert format_reason("weak_hook", "хук на 4-й секунде") == \
        "[weak_hook] хук на 4-й секунде"
    # Пустая деталь не оставляет висячего пробела.
    assert format_reason("other", "  ") == "[other]"


def test_format_reason_rejects_unknown_code():
    # Невалидный код не должен уйти в лист как '[bogus]', который parse не узнает.
    with pytest.raises(ValueError):
        format_reason("bogus")


def test_parse_reason_code_case_insensitive():
    # Ручная правка ячейки в верхнем регистре ('[WEAK_HOOK]') всё равно матчится
    # и нормализуется в канонический код (класс багов P1.10 — грязный ввод в Sheets).
    assert parse_reason_code("[WEAK_HOOK] x") == "weak_hook"
    assert parse_reason_code("[Reference_Mismatch]") == "reference_mismatch"


def test_parse_reason_code_roundtrip():
    assert parse_reason_code(format_reason("female_reference")) == "female_reference"
    assert parse_reason_code("[weak_hook] деталь") == "weak_hook"


def test_parse_reason_code_legacy_and_unknown():
    # Старый свободный текст без скобок -> None (группируется по тексту).
    assert parse_reason_code("слабый референс") is None
    assert parse_reason_code("") is None
    assert parse_reason_code(None) is None
    # Скобки есть, но код не из enum -> None (не выдаём чужой код за свой).
    assert parse_reason_code("[unknown_code] x") is None
