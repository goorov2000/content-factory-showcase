import argparse
import json
import logging

from cf.cli import cmd_add_brief

from tests.fakes import FakeSheets

# Заголовки живого листа briefs БЕЗ колонки cta — воспроизводит текущее состояние
# боевой таблицы, где оператор ещё не добавил cta (P1.14).
BRIEFS_HEADERS_NO_CTA = ["brief_id", "generated_at", "platform", "hook", "script",
                         "visual_direction", "caption", "references", "prompt_version",
                         "formula_id", "source_pattern_ids", "payload_json", "review_status"]

VALID_BRIEF = {
    "brief_id": "b-short-styling-idea-reel-20260710-x1",
    "source_pattern_ids": ["muzhskie-obrazy-short-outfit-format-01"],
    "formula_id": "short-styling-idea-reel",
    "prompt_version": "v2",
    "hook": "Один цвет — три оттенка: монохром, который выглядит дорого",
    "script": "0-3с: хук словами + первый образ. 3-21с: три монохромных образа по 6с. "
              "21-27с: резюме приёма + подписка.",
    "visual_direction": "Телефон, 9:16, дневной свет. Обязательно: текст-плашка с идеей "
                        "на каждом образе; публикация в окно 9:00-21:00.",
    "cta": "Подпишись — новые идеи мужских образов каждую неделю",
    "references": ["https://www.tiktok.com/@aypricot/video/7652315929052810516"],
}


def ns(**kw):
    base = {"platform": "tiktok", "caption": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def write(tmp_path, data):
    p = tmp_path / "brief.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_valid_brief_appended_as_pending(tmp_path, capsys):
    sheets = FakeSheets()
    assert cmd_add_brief(sheets, ns(file=write(tmp_path, VALID_BRIEF))) == 0
    (tab, row), = sheets.appended
    assert tab == "briefs"
    assert row["brief_id"] == VALID_BRIEF["brief_id"]
    assert row["review_status"] == "pending"
    assert json.loads(row["payload_json"]) == VALID_BRIEF
    assert VALID_BRIEF["references"][0] in row["references"]


def test_add_brief_writes_cta_column(tmp_path, capsys):
    # P1.14(а): cta обязателен в brief.schema (minLength 3); ревьюер собирает бриф
    # из полей строки, поэтому add-brief обязан писать cta своей колонкой.
    sheets = FakeSheets()
    assert cmd_add_brief(sheets, ns(file=write(tmp_path, VALID_BRIEF))) == 0
    (tab, row), = sheets.appended
    assert row["cta"] == VALID_BRIEF["cta"]


def test_add_brief_row_reassembles_into_valid_brief(tmp_path, capsys):
    # Приёмка P1.14(а): собранный из колонок строки JSON (как в cf-review-brief.md,
    # шаг 2) проходит `cf validate brief` — cta присутствует, references распарсились
    # по разделителю "; " в НЕСКОЛЬКО URL (не один склеенный элемент).
    from cf.validate import validate_json_file

    brief = dict(VALID_BRIEF)
    brief["references"] = ["https://tiktok.com/@a/video/1",
                           "https://tiktok.com/@b/video/2"]
    brief["source_pattern_ids"] = ["muzhskie-obrazy-01", "muzhskie-obrazy-02"]
    sheets = FakeSheets()
    assert cmd_add_brief(sheets, ns(file=write(tmp_path, brief))) == 0
    (tab, row), = sheets.appended

    references = row["references"].split("; ")
    assert len(references) == 2  # разделитель "; " дал два URL, не один склеенный
    assembled = {
        "brief_id": row["brief_id"],
        "source_pattern_ids": row["source_pattern_ids"].split(", "),
        "formula_id": row["formula_id"],
        "prompt_version": row["prompt_version"],
        "hook": row["hook"],
        "script": row["script"],
        "visual_direction": row["visual_direction"],
        "cta": row["cta"],
        "references": references,
    }
    p = tmp_path / "assembled.json"
    p.write_text(json.dumps(assembled, ensure_ascii=False), encoding="utf-8")
    assert validate_json_file("brief", str(p)) == []


def test_add_brief_drops_and_warns_when_no_cta_column(tmp_path, caplog):
    # Блайнд-спот ревью: пока в живом листе нет колонки cta, значение выбрасывается
    # append_row'ом (пишутся только колонки из заголовков). Запись НЕ падает (лишние
    # ключи легитимны у части вызывающих), но обязана громко предупредить — иначе
    # потеря всплывёт лишь на ревью, а не сразу при add-brief.
    sheets = FakeSheets(headers={"briefs": BRIEFS_HEADERS_NO_CTA})
    with caplog.at_level(logging.WARNING):
        assert cmd_add_brief(sheets, ns(file=write(tmp_path, VALID_BRIEF))) == 0
    (tab, row), = sheets.appended
    assert "cta" not in row  # значение cta выброшено — колонки нет в заголовках
    assert any("cta" in r.getMessage() and "briefs" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)
    # payload_json всё равно несёт полный валидный бриф с cta — ревьюер читает его,
    # поэтому бриф валиден даже без колонки cta в листе.
    assert json.loads(row["payload_json"])["cta"] == VALID_BRIEF["cta"]


def test_invalid_brief_rejected_without_write(tmp_path, capsys):
    bad = dict(VALID_BRIEF)
    del bad["hook"]
    sheets = FakeSheets()
    assert cmd_add_brief(sheets, ns(file=write(tmp_path, bad))) == 1
    assert sheets.appended == []


def test_duplicate_brief_id_is_noop_success(tmp_path, capsys):
    # P1.5: brief_id уже есть -> идемпотентный no-op УСПЕХ (не второй ряд и не ошибка).
    # Добавление существующего брифа — достижение желаемого состояния, значит успех.
    sheets = FakeSheets({"briefs": [{"brief_id": VALID_BRIEF["brief_id"]}]})
    assert cmd_add_brief(sheets, ns(file=write(tmp_path, VALID_BRIEF))) == 0
    assert sheets.appended == []  # второй ряд не записан


def test_concurrent_double_add_brief_yields_one_row(tmp_path, capsys):
    # P1.5 приёмка: конкурентный двойной add-brief одного brief_id -> один ряд.
    # Реалистичный сериализованный порядок, который допускает фейк: первый вызов
    # пишет ряд, pre-check второго видит его и второй ряд НЕ пишет.
    sheets = FakeSheets()
    f = write(tmp_path, VALID_BRIEF)
    assert cmd_add_brief(sheets, ns(file=f)) == 0
    assert cmd_add_brief(sheets, ns(file=f)) == 0
    rows = [b for b in sheets.read_rows("briefs")
            if str(b["brief_id"]) == VALID_BRIEF["brief_id"]]
    assert len(rows) == 1


def test_add_brief_concurrent_landing_after_precheck_reported_as_success(tmp_path, capsys):
    # P1.5 хвост-путь: жёсткая одновременность — параллельный процесс вписывает тот же
    # brief_id, пока мы пишем (оба прошли pre-check). После аппенда перечитываем по
    # brief_id, видим дубль и трактуем как успех первого; ТРЕТИЙ ряд не добавляем.
    # Жёсткая гарантия единственного ряда для двух одновременных писателей — лок P1.9.
    state = {"fired": False}

    def concurrent(sheets, tab_key, stored):
        if tab_key == "briefs" and not state["fired"]:
            state["fired"] = True
            sheets.tables[tab_key].insert(0, {"brief_id": stored["brief_id"]})

    sheets = FakeSheets(on_append=concurrent)
    assert cmd_add_brief(sheets, ns(file=write(tmp_path, VALID_BRIEF))) == 0
    rows = [b for b in sheets.read_rows("briefs")
            if str(b["brief_id"]) == VALID_BRIEF["brief_id"]]
    assert len(rows) == 2  # два процесса -> два ряда; мы третий не добавили
    assert len(sheets.appended) == 1  # этот процесс записал ровно один ряд
    assert "параллель" in capsys.readouterr().out  # пост-аппенд re-read засёк гонку
