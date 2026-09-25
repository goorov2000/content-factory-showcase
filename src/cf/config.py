import json
import os
from pathlib import Path


def resolve_path(p):
    """Разворачивает переменные окружения и ~ в путях из конфига.

    Секреты лежат вне репо (~/.cf/secrets, P0.3), а конфиг коммитится —
    поэтому пути в нём машинно-независимые: ~/... или %USERPROFILE%/...
    """
    return os.path.expanduser(os.path.expandvars(p))


def load_config(path=None):
    p = Path(path or os.environ.get("CF_CONFIG", "cf.config.json"))
    if not p.exists():
        raise FileNotFoundError(
            f"cf.config.json не найден по пути {p}. Создай его в корне репо: "
            f"spreadsheet_id, tabs, service_account_file (см. образец в плане, Task 4)."
        )
    return json.loads(p.read_text(encoding="utf-8"))


# ── Реестр аккаунтов завода (UTM-контур) ─────────────────────────────────────
# Слоты — ЗНАЧЕНИЯ ПОЛЯ, как dashboard.creator_slots: при заведении реального
# аккаунта владелец правит handle и active в одной строке конфига. Слаг — ключ
# реестра: он же utm_campaign в ссылках и колонка account в CF Published Reels.
def accounts_from_config(config):
    """Аккаунты завода из cf.config.json → accounts (верхнеуровневый ключ).

    Возвращает нормализованные записи {slug, platform, handle, active};
    записи без слага и мусор отбрасываются, отсутствие ключа → пустой список:
    формы публикации от этого не падают, а показывают причину."""
    try:
        raw = (config or {}).get("accounts") or []
    except AttributeError:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            continue
        out.append({"slug": slug,
                    "platform": str(entry.get("platform") or "").strip(),
                    "handle": str(entry.get("handle") or "").strip(),
                    "active": bool(entry.get("active"))})
    return out
