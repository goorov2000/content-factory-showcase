import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

SCHEMAS = {
    "patterns": "schemas/patterns.schema.json",
    "formula": "schemas/formula.schema.json",
    "brief": "schemas/brief.schema.json",
    "source-proposal": "schemas/source-proposal.schema.json",
    "sources": "schemas/sources.schema.json",
    "visual-facts": "schemas/visual-facts.schema.json",
}


def validate_json_data(kind, data, schema_root="."):
    """Валидация уже разобранных данных — для проверки ДО записи на диск (M38)."""
    schema_path = Path(schema_root) / SCHEMAS[kind]
    validator, schema_error = _validator(str(schema_path), schema_path.stat().st_mtime_ns)
    if schema_error:
        return [schema_error]
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(data)
    ]


@lru_cache(maxsize=32)
def _validator(schema_path, mtime_ns):
    """(валидатор, None) или (None, текст ошибки схемы); кэш по пути и mtime.

    Сломанная схема не отвергается валидатором, а ведёт себя непредсказуемо:
    опечатка в ключевом слове молча даёт «ошибок нет», массив в properties роняет
    iter_errors AttributeError-ом (brief.schema.json до 14.09.2026). Контракт
    validate_json_data — список ошибок, поэтому поломку схемы отдаём тем же списком.
    Кэш нужен горячим путям (vision валидирует каждую строку): схема читается и
    проверяется один раз, пока файл не изменился.
    """
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        where = "/".join(str(p) for p in exc.path) or "<root>"
        return None, f"<schema>: схема {schema_path} невалидна в {where}: {exc.message}"
    return Draft202012Validator(schema), None


def validate_json_file(kind, path, schema_root="."):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_json_data(kind, data, schema_root=schema_root)
