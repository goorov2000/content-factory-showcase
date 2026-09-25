# Инварианты боевого cf.config.json (дефект Д3, передача 2026-07-26).
#
# «стритвир» полгода стоял ОДНОВРЕМЕННО в target_niches и в
# dashboard.fanout.exclude_niches: скоринг источников считал тему целевой,
# производство — нет. Одновременно «бренды-магазины» и «маркетплейс-подборки»
# производили, но в target_niches их не было, поэтому target_yield в
# cf source-stats оценивал источники по двум темам из четырёх плюс одной
# мёртвой — крупнейший производитель считался бесполезным.
#
# Ошибка молчаливая: ни схема, ни код расхождения не ловили. Эти тесты читают
# ИМЕННО боевой конфиг, а не фикстуру, — иначе класс дефекта снова станет
# невидимым.
import json
from pathlib import Path

from conftest import repo_config_path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config():
    # витринная копия: без боевого файла читается cf.config.example.json (conftest)
    return json.loads(repo_config_path().read_text(encoding="utf-8"))


def _taxonomy():
    path = REPO_ROOT / "prompts" / "agents" / "niche-taxonomy.json"
    return json.loads(path.read_text(encoding="utf-8"))["niches"]


def _target_niches(cfg):
    return cfg.get("target_niches") or []


def _exclude_niches(cfg):
    return cfg.get("dashboard", {}).get("fanout", {}).get("exclude_niches") or []


def test_target_and_exclude_niches_do_not_overlap():
    cfg = _config()
    overlap = sorted(set(_target_niches(cfg)) & set(_exclude_niches(cfg)))
    assert overlap == [], (
        "ниша одновременно целевая для скоринга источников и снятая с "
        f"производства: {overlap}. Скоринг будет кормить тему, которая не "
        "производит (дефект Д3, «стритвир» 2026-07-26)")


def test_niche_lists_use_only_taxonomy_names():
    # опечатка в имени ниши не падает нигде — она просто перестаёт совпадать
    # со строками raw, и тема тихо выпадает из скоринга.
    cfg = _config()
    known = set(_taxonomy())
    unknown = sorted((set(_target_niches(cfg)) | set(_exclude_niches(cfg))) - known)
    assert unknown == [], (
        f"имена вне prompts/agents/niche-taxonomy.json: {unknown}. "
        "Таксономия — единственный источник истины по нишам (CLAUDE.md)")


def test_target_niches_is_taxonomy_minus_excluded():
    # Решение владельца от 2026-07-26 (коммит bdfe270): в производстве 5 тем
    # из 12. Значит target_niches не самостоятельный список, а дополнение
    # exclude_niches — если они разъедутся снова, разъедется и тюнинг источников.
    cfg = _config()
    expected = sorted(set(_taxonomy()) - set(_exclude_niches(cfg)))
    assert sorted(_target_niches(cfg)) == expected, (
        "target_niches разошёлся с «таксономия минус exclude_niches»: "
        f"ожидалось {expected}, в конфиге {sorted(_target_niches(cfg))}")


def test_published_reels_row_columns_have_aliases():
    # mark_published строит строку с production_notes, а в живой вкладке
    # колонка называется notes; Sheets.append_row поля вне заголовков
    # выбрасывает МОЛЧА. Без алиаса первая же публикация теряет
    # производственные заметки — то самое поле, по которому eval отличает
    # провал производства от провала формулы.
    cfg = _config()
    aliases = cfg.get("column_aliases", {}).get("reels", {})
    assert aliases.get("production_notes") == "notes", (
        "нет алиаса production_notes -> notes в column_aliases.reels: "
        "заметки о публикации потеряются молча")


def test_accounts_registry_shape():
    # Реестр аккаунтов (UTM-контур, тикет 01): utm_campaign = слаг, значит слаги
    # обязаны быть уникальными и непустыми, а платформа — известной, иначе
    # сборщик Метрики молча не сматчит переходы на аккаунт.
    accounts = _config().get("accounts")
    assert isinstance(accounts, list) and accounts, (
        "верхнеуровневый ключ accounts отсутствует или пуст — формам публикации "
        "нечего показывать, кроме причины")
    slugs = []
    for entry in accounts:
        assert isinstance(entry, dict), f"запись реестра не словарь: {entry!r}"
        assert set(entry) == {"slug", "platform", "handle", "active"}, (
            f"структура записи разошлась со спекой: {sorted(entry)}")
        assert str(entry["slug"]).strip(), "пустой slug — запись без ключа реестра"
        assert entry["platform"] in ("tiktok", "instagram"), (
            f"неизвестная платформа {entry['platform']!r}")
        assert isinstance(entry["active"], bool), (
            f"active обязан быть булевым, не {entry['active']!r}")
        slugs.append(entry["slug"])
    assert len(slugs) == len(set(slugs)), f"слаги аккаунтов не уникальны: {slugs}"


def test_active_accounts_carry_real_handles():
    # active: true при хэндле-заглушке — ложь конфигу: публикация привяжется к
    # аккаунту, которого нет. Заготовки слотов живут только с active: false.
    for entry in _config().get("accounts") or []:
        if entry.get("active"):
            handle = str(entry.get("handle") or "").strip()
            assert handle and "PLACEHOLDER" not in handle, (
                f"аккаунт {entry.get('slug')!r} активен с хэндлом-заглушкой")


def test_metrika_block_and_utm_tab_declared():
    # UTM-контур (тикет 02): без блока metrika сборщику неоткуда взять токен и
    # счётчик, без вкладки в tabs ensure_tab не знает имени листа. counter_id
    # пустой — валидное «не заведён» (PLACEHOLDER владельца при приёмке):
    # сборщик отвечает на него честным insufficient_data, а не падением.
    cfg = _config()
    m = cfg.get("metrika") or {}
    assert m.get("token_file"), "нет metrika.token_file — неоткуда взять OAuth-токен"
    assert "counter_id" in m, "нет ключа metrika.counter_id"
    assert cfg.get("tabs", {}).get("utm_traffic") == "CF UTM Traffic", (
        "вкладка utm_traffic не объявлена в tabs — сборщику Метрики некуда писать")


def test_orders_tab_declared():
    # Реестр заказов (тикет 03): без вкладки в tabs сборщику покупок и пульту
    # некуда писать кандидатов, а backup/restore не увидят источник истины по
    # деньгам (они обходят tabs).
    assert _config().get("tabs", {}).get("orders") == "CF Orders", (
        "вкладка orders не объявлена в tabs — реестру заказов негде жить")


def test_payout_rates_and_site_declared():
    # Страница «Деньги» (тикет 04): ставки живут ТОЛЬКО здесь — в коде констант
    # оплаты нет, и без блока payout лист честно считает нулём с предупреждением.
    # Пилотные значения (30/10/14) тут не проверяются намеренно: их пересмотр
    # через 2 месяца — правка конфига, а не падение сьюта.
    cfg = _config()
    payout = cfg.get("payout")
    assert isinstance(payout, dict), (
        "нет блока payout в cf.config.json — расчётному листу неоткуда взять ставки")
    for key in ("per_transition_rub", "sales_percent", "hold_days"):
        assert isinstance(payout.get(key), (int, float)), (
            f"payout.{key} отсутствует или не число — лист посчитает эти деньги нулём")
    assert isinstance(payout.get("monthly_fix_rub"), dict), (
        "payout.monthly_fix_rub обязан быть словарём месяц -> сумма (пустой — валиден)")
    base_url = (cfg.get("site") or {}).get("base_url", "")
    assert str(base_url).startswith("https://"), (
        "site.base_url отсутствует или не https — генератору ссылок для bio "
        "не от чего собирать URL")


def test_fanout_thresholds_are_explicit():
    # Пороги конвейера работали дефолтами из dashboard/runner.py, и в конфиге
    # их не было вовсе. Обещание передачи «откат — одна строка в
    # cf.config.json» было невыполнимо: строки не существовало.
    fanout = _config().get("dashboard", {}).get("fanout", {})
    for key in ("briefs_per_formula", "approve_cap", "autocontinue"):
        assert key in fanout, (
            f"порог {key} не задан явно — оператор не найдёт строку, чтобы "
            "его изменить, и будет править код вместо конфига")
