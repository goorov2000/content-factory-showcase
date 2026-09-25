import json
import os

import pytest

from cf.config import accounts_from_config, load_config, resolve_path


def test_loads_config_from_explicit_path(tmp_path):
    p = tmp_path / "cf.config.json"
    p.write_text(json.dumps({"spreadsheet_id": "abc", "tabs": {}}), encoding="utf-8")
    cfg = load_config(p)
    assert cfg["spreadsheet_id"] == "abc"


def test_missing_config_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError) as exc_info:
        load_config(tmp_path / "nope.json")
    assert "cf.config.json" in str(exc_info.value)


def test_resolve_path_expands_tilde():
    resolved = resolve_path("~/.cf/secrets/service-account.json")
    assert resolved.startswith(os.path.expanduser("~"))
    assert resolved.endswith(".cf/secrets/service-account.json")


def test_resolve_path_expands_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("CF_TEST_DIR", str(tmp_path))
    var = "%CF_TEST_DIR%" if os.name == "nt" else "$CF_TEST_DIR"
    assert resolve_path(f"{var}/key.txt") == f"{tmp_path}/key.txt"


def test_resolve_path_keeps_plain_relative_path():
    assert resolve_path("secrets/service-account.json") == "secrets/service-account.json"


# --- accounts_from_config: реестр аккаунтов завода (UTM-контур, тикет 01) -----
# По образцу creator_slots_from_config: мусор/отсутствие ключа -> пустой список,
# страница публикации от этого не падает, а говорит причину.


def test_accounts_from_config_normalizes_entries():
    cfg = {"accounts": [
        {"slug": " tiktok-1 ", "platform": "tiktok", "handle": " @x ",
         "active": True},
        {"slug": "instagram-1", "platform": "instagram", "handle": "PLACEHOLDER",
         "active": False},
    ]}
    assert accounts_from_config(cfg) == [
        {"slug": "tiktok-1", "platform": "tiktok", "handle": "@x", "active": True},
        {"slug": "instagram-1", "platform": "instagram", "handle": "PLACEHOLDER",
         "active": False},
    ]


def test_accounts_from_config_garbage_and_absent_key_give_empty_list():
    assert accounts_from_config({}) == []
    assert accounts_from_config(None) == []
    assert accounts_from_config({"accounts": "мусор"}) == []
    assert accounts_from_config({"accounts": {"slug": "не-список"}}) == []
    assert accounts_from_config("совсем не конфиг") == []


def test_accounts_from_config_drops_entries_without_slug():
    # Слаг — ключ реестра (utm_campaign, валидация публикации): запись без него
    # бесполезна и отбрасывается, не роняя остальные.
    cfg = {"accounts": [{"platform": "tiktok", "handle": "@x", "active": True},
                        {"slug": "  ", "active": True},
                        "строка-вместо-словаря",
                        {"slug": "tiktok-1"}]}
    assert accounts_from_config(cfg) == [
        {"slug": "tiktok-1", "platform": "", "handle": "", "active": False}]
