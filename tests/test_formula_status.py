import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from cf.cli import cmd_approve_formula, cmd_formula_status, set_formula_status

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


def _formula(tmp_path, name="test-formula", niche="стритвир", status="proposed"):
    p = tmp_path / "formulas" / niche / f"{name}.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"name": name, "niche": niche, "version": 1,
                             "status": status}), encoding="utf-8")
    return p


def test_approve_adds_to_index_and_sets_status(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    rc = cmd_formula_status(FakeSheets(), ns(path=str(p), status="approved",
                                             reason="", index=str(index)))
    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "approved"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"][0]["name"] == "test-formula"


def test_pause_removes_from_index_keeps_reason(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    cmd_formula_status(FakeSheets(), ns(path=str(p), status="approved", reason="", index=str(index)))
    rc = cmd_formula_status(FakeSheets(), ns(path=str(p), status="paused",
                                             reason="сезон закончился", index=str(index)))
    assert rc == 0
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "paused"
    assert data["status_reason"] == "сезон закончился"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []


def test_approve_twice_does_not_duplicate_index(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    cmd_formula_status(FakeSheets(), ns(path=str(p), status="approved", reason="", index=str(index)))
    cmd_formula_status(FakeSheets(), ns(path=str(p), status="approved", reason="", index=str(index)))
    data = json.loads(index.read_text(encoding="utf-8"))
    assert len(data["approved"]) == 1


def test_approve_with_empty_object_index_does_not_crash(tmp_path):
    # index.json существует, но без ключа approved (например, {}) — approve не падает
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text("{}", encoding="utf-8")
    rc = cmd_formula_status(FakeSheets(), ns(path=str(p), status="approved",
                                             reason="", index=str(index)))
    assert rc == 0
    assert json.loads(index.read_text(encoding="utf-8"))["approved"][0]["name"] == "test-formula"


def test_cmd_approve_formula_still_works_backward_compatible(tmp_path):
    p = _formula(tmp_path, status="proposed")
    index = tmp_path / "formulas" / "_approved" / "index.json"
    rc = cmd_approve_formula(FakeSheets(), ns(path=str(p), index=str(index)))
    assert rc == 0
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "approved"
    assert json.loads(index.read_text(encoding="utf-8"))["approved"][0]["name"] == "test-formula"


def test_canonical_path_collapses_absolute_backslash_relative(tmp_path, monkeypatch):
    # Файл формулы и индекс в каноническом расположении; CWD == корень репозитория,
    # как в бою (n8n гоняет CLI из корня).
    fdir = tmp_path / "formulas" / "men-style"
    fdir.mkdir(parents=True)
    fpath = fdir / "grid.json"
    fpath.write_text(json.dumps({"name": "grid", "niche": "men-style",
                                 "version": 1, "status": "proposed"}), encoding="utf-8")
    index = tmp_path / "formulas" / "_approved" / "index.json"
    index.parent.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    forms = [
        str(fpath),                          # абсолютный (как пишет дашборд)
        "formulas/men-style/grid.json",      # относительный, прямые слэши (как CLI)
        "formulas\\men-style\\grid.json",    # относительный, backslash (Windows)
    ]
    stored = []
    for form in forms:
        set_formula_status(form, "approved", index_path=str(index))
        data = json.loads(index.read_text(encoding="utf-8"))
        assert len(data["approved"]) == 1          # дедуп: разные написания не плодят записи
        stored.append(data["approved"][0]["path"])
    # путь в индексе — канонический путь СНАПШОТА (аудит C1/H1)
    assert stored[0] == stored[1] == stored[2] == "formulas/_approved/men-style/grid-v1.json"

    # апрув одним написанием, пауза другим -> запись уходит из индекса
    set_formula_status("formulas\\men-style\\grid.json", "paused",
                       reason="", index_path=str(index))
    assert json.loads(index.read_text(encoding="utf-8"))["approved"] == []


# --- Approved-снапшоты (аудит 2026-07-24, C1/H1) ---------------------------
# Инцидент: formula-writer перезаписал одобренную v2 черновиком v3 в том же
# файле, а запись индекса потерялась. Approve теперь пишет иммутабельный
# снапшот formulas/_approved/<ниша>/<имя>-v<N>.json, индекс указывает на него.

def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def test_approve_writes_snapshot_and_index_points_to_it(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    set_formula_status(str(p), "approved", index_path=str(index))

    snap = tmp_path / "formulas" / "_approved" / "стритвир" / "test-formula-v1.json"
    assert snap.is_file()
    data = _read(snap)
    assert data["status"] == "approved" and data["version"] == 1
    entry = _read(index)["approved"][0]
    assert entry["path"] == "formulas/_approved/стритвир/test-formula-v1.json"


def test_reapprove_new_version_replaces_entry_keeps_old_snapshot(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    set_formula_status(str(p), "approved", index_path=str(index))

    body = _read(p)
    body.update(version=2, status="proposed")
    p.write_text(json.dumps(body), encoding="utf-8")
    set_formula_status(str(p), "approved", index_path=str(index))

    entries = _read(index)["approved"]
    assert len(entries) == 1 and entries[0]["version"] == 2
    assert entries[0]["path"].endswith("test-formula-v2.json")
    # прежний снапшот остаётся на диске для аудита
    assert (tmp_path / "formulas" / "_approved" / "стритвир" / "test-formula-v1.json").is_file()


def test_pause_via_snapshot_path_does_not_mutate_snapshot(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    set_formula_status(str(p), "approved", index_path=str(index))
    snap = tmp_path / "formulas" / "_approved" / "стритвир" / "test-formula-v1.json"
    before = snap.read_text(encoding="utf-8")

    # гвардия зовёт set_formula_status по entry["path"] — теперь это снапшот
    set_formula_status(str(snap), "paused", reason="3 reject из 5",
                       index_path=str(index))

    assert snap.read_text(encoding="utf-8") == before      # снапшот иммутабелен
    assert _read(index)["approved"] == []                  # запись убрана
    working = _read(p)                                     # статус — в рабочий файл
    assert working["status"] == "paused"
    assert working["status_reason"] == "3 reject из 5"


def test_pause_via_snapshot_path_leaves_newer_draft_untouched(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    set_formula_status(str(p), "approved", index_path=str(index))
    # formula-writer уже переписал рабочий файл черновиком v3
    p.write_text(json.dumps({"name": "test-formula", "niche": "стритвир",
                             "version": 3, "status": "proposed"}), encoding="utf-8")
    snap = tmp_path / "formulas" / "_approved" / "стритвир" / "test-formula-v1.json"

    set_formula_status(str(snap), "paused", reason="x", index_path=str(index))

    assert _read(index)["approved"] == []
    draft = _read(p)                       # чужую (новую) версию не трогаем
    assert draft["version"] == 3 and draft["status"] == "proposed"


def test_approve_snapshot_path_raises_valueerror(tmp_path):
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    set_formula_status(str(p), "approved", index_path=str(index))
    snap = tmp_path / "formulas" / "_approved" / "стритвир" / "test-formula-v1.json"
    with pytest.raises(ValueError, match="рабочему файлу"):
        set_formula_status(str(snap), "approved", index_path=str(index))


def test_pause_working_path_removes_entry_by_identity(tmp_path):
    # Запись индекса указывает на снапшот, пауза приходит по РАБОЧЕМУ пути —
    # удаление обязано сработать по (name, niche), а не по совпадению строки пути.
    p = _formula(tmp_path)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    set_formula_status(str(p), "approved", index_path=str(index))
    set_formula_status(str(p), "paused", reason="", index_path=str(index))
    assert _read(index)["approved"] == []


def _index_with_legacy_absolute_entry(tmp_path, name="legacy", niche="men-style"):
    # Боевое состояние до фикса 7d7b303: запись индекса с абсолютным D:/-путём
    # (наследие Windows-машины оператора), см. 8156c43.
    p = _formula(tmp_path, name=name, niche=niche)
    index = tmp_path / "formulas" / "_approved" / "index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"approved": [{
        "name": name, "niche": niche,
        "path": f"D:/local-path/CF/formulas/{niche}/{name}.json",
        "version": 1, "approved_at": "2026-07-17T09:46:25+00:00"}]}), encoding="utf-8")
    return p, index


def test_legacy_absolute_entry_is_replaced_not_duplicated(tmp_path):
    # Регрессия 8156c43 (половина «дубли»): дедуп по строке пути не схлопнул бы
    # легаси-запись с абсолютным путём — ре-апрув по рабочему пути дал бы вторую
    # запись той же формулы. Идентичность (name, niche) обязана её заменить.
    p, index = _index_with_legacy_absolute_entry(tmp_path)
    set_formula_status(str(p), "approved", index_path=str(index))
    entries = _read(index)["approved"]
    assert len(entries) == 1
    assert entries[0]["path"] == "formulas/_approved/men-style/legacy-v1.json"


def test_legacy_absolute_entry_removed_on_pause(tmp_path):
    # Та же регрессия в сторону удаления: пауза по рабочему пути обязана убрать
    # запись с легаси-путём, иначе индекс держит «одобрение-призрак».
    p, index = _index_with_legacy_absolute_entry(tmp_path)
    set_formula_status(str(p), "paused", reason="гвардия", index_path=str(index))
    assert _read(index)["approved"] == []
    assert _read(p)["status"] == "paused"


def test_approve_incomplete_formula_raises_and_leaves_file_unchanged(tmp_path):
    # Неполная формула (нет name) -> запись индекса строится ДО мутации файла,
    # KeyError не должен оставить файл в полусостоянии (status=approved уже записан).
    fpath = tmp_path / "formulas" / "men-style" / "no-name.json"
    fpath.parent.mkdir(parents=True)
    fpath.write_text(json.dumps({"niche": "men-style", "version": 1,
                                 "status": "proposed"}), encoding="utf-8")
    index = tmp_path / "formulas" / "_approved" / "index.json"

    before = fpath.read_text(encoding="utf-8")
    with pytest.raises(KeyError):
        set_formula_status(str(fpath), "approved", index_path=str(index))
    assert fpath.read_text(encoding="utf-8") == before   # файл не мутирован
    assert not index.exists()                            # индекс не создан


def test_path_outside_repo_raises_valueerror(tmp_path):
    repo = tmp_path / "repo"
    index = repo / "formulas" / "_approved" / "index.json"
    index.parent.mkdir(parents=True)
    # Реальный файл вне корня репозитория: read_json пройдёт, канонизация — нет.
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "x", "niche": "n", "version": 1,
                                   "status": "proposed"}), encoding="utf-8")
    with pytest.raises(ValueError, match="вне репозитория"):
        set_formula_status(str(outside), "approved", index_path=str(index))


def test_real_approved_index_is_clean_and_in_sync():
    # Инвариант боевого индекса (после миграции C1/H1): каждая запись указывает
    # на относительный путь СНАПШОТА formulas/_approved/<ниша>/<имя>-v<N>.json,
    # снапшот существует, approved, совпадает с записью по имени/версии,
    # а рабочий черновик formulas/<ниша>/<имя>.json существует рядом.
    # Индекс НЕ обязан покрывать дерево снапшотов: осиротевший снапшот прошлой
    # версии (ре-апрув, пауза) — это аудит-след одобренного тела, а не рассинхрон.
    repo = Path(__file__).resolve().parent.parent
    data = json.loads((repo / "formulas" / "_approved" / "index.json")
                      .read_text(encoding="utf-8"))
    assert data["approved"], "боевой индекс пуст"
    for e in data["approved"]:
        p = e["path"]
        assert not os.path.isabs(p) and ":" not in p and not p.startswith("/"), \
            f"путь в индексе должен быть относительным: {p!r}"
        assert p == f"formulas/_approved/{e['niche']}/{e['name']}-v{e['version']}.json", \
            f"{e['name']}: путь не канонический снапшот: {p!r}"
        f = repo / p
        assert f.is_file(), f"снапшот из индекса не найден: {p!r}"
        formula = json.loads(f.read_text(encoding="utf-8"))
        assert formula.get("status") == "approved", \
            f"{e['name']}: запись approved, но снапшот {formula.get('status')!r}"
        assert formula.get("version") == e.get("version"), \
            f"{e['name']}: версия индекса {e.get('version')} != снапшота {formula.get('version')}"
        assert formula.get("name") == e.get("name")
        draft = repo / "formulas" / e["niche"] / f"{e['name']}.json"
        assert draft.is_file(), f"рабочий черновик не найден: {draft}"
    # --- Инвариант вместо пина на конкретную версию (инцидент 8156c43) -----
    # Что сломал инцидент: удаление/дедуп записей индекса шли по СТРОКЕ пути,
    # поэтому прогон фан-аута молча выбросил одобрение short-styling-idea-reel
    # (а легаси-запись с абсолютным D:/-путём, наоборот, не удалялась бы —
    # вторая половина того же бага). Фикс 7d7b303 перевёл идентичность записи
    # на (name, niche). Пин «v2 в индексе» ломался на любом законном ре-апруве
    # (d8829fe: v2 -> v4), поэтому сторожим сам инвариант, а не номер версии.
    ids = [(e["name"], e["niche"]) for e in data["approved"]]
    assert len(ids) == len(set(ids)), \
        f"формула дважды в индексе (дедуп по идентичности сломан): {ids}"

    # Одобрение не исчезает молча: approve и pause двигают статус рабочего файла
    # и запись индекса одной транзакцией под локом, значит «черновик approved, а
    # записи нет» = запись потеряна помимо lifecycle (ровно остаток 8156c43).
    # Проверяем по ЧЕРНОВИКУ, а не по снапшоту: пауза (в т.ч. автоматическая, от
    # formula-guard) снимает запись индекса и переводит черновик в paused, а
    # снапшот остаётся лежать аудит-следом — сверка «снапшот => запись» краснела
    # бы на каждой штатной паузе. Обратное направление тоже НЕ проверяем:
    # formula-writer вправе положить рядом более новый proposed-черновик.
    indexed = {(e["name"], e["niche"]): e for e in data["approved"]}
    for draft_path in sorted((repo / "formulas").glob("*/*.json")):
        if draft_path.parent.name == "_approved":
            continue                       # это index.json, а не черновик
        draft_body = json.loads(draft_path.read_text(encoding="utf-8"))
        if draft_body.get("status") != "approved":
            continue
        key = (draft_body.get("name"), draft_body.get("niche"))
        assert key in indexed, \
            f"{draft_path.name}: черновик approved, но записи в индексе нет ({key})"

    # Именной якорь инцидента: восстановленное в 7d7b303 одобрение
    # short-styling-idea-reel остаётся в индексе (версия намеренно любая).
    # Снимать этот assert допустимо только вместе с решением оператора о паузе
    # или реджекте формулы (decision-файл в .claude/memory/decisions/).
    # Витринная копия: индекс обрезан до трёх формул-примеров (боевой индекс и
    # рабочий конфиг не публикуются), поэтому именной якорь проверяется только
    # при наличии боевого cf.config.json рядом.
    if (repo / "cf.config.json").is_file():
        assert ("short-styling-idea-reel", "мужские-образы") in indexed, \
            "одобрение short-styling-idea-reel исчезло из индекса (регрессия 8156c43)"


def test_real_approved_snapshots_are_immutable_and_self_consistent():
    # Снапшоты formulas/_approved/<ниша>/<имя>-vN.json иммутабельны (CLAUDE.md:
    # писать в _approved вручную запрещено), значит КАЖДЫЙ файл дерева обязан
    # выглядеть так, как его записал approve: status=approved, niche совпадает
    # с каталогом, имя файла — с name/version тела. Ручная правка или обрыв
    # записи всплывут здесь. Осиротевшие снапшоты (версия ушла из индекса после
    # ре-апрува или паузы) проверяются наравне с активными: их наличие с
    # индексом НЕ сверяется, они и есть аудит-след одобренного тела (C1/H1).
    repo = Path(__file__).resolve().parent.parent
    snapshots = sorted((repo / "formulas" / "_approved").glob("*/*.json"))
    assert snapshots, "дерево снапшотов пусто"
    for snap in snapshots:
        body = json.loads(snap.read_text(encoding="utf-8"))
        assert body.get("status") == "approved", \
            f"{snap.name}: снапшот со статусом {body.get('status')!r} — правка _approved вручную"
        assert body.get("niche") == snap.parent.name, \
            f"{snap.name}: niche {body.get('niche')!r} не совпадает с каталогом {snap.parent.name!r}"
        assert snap.stem == f"{body.get('name')}-v{body.get('version')}", \
            f"{snap.name}: имя файла не совпадает с name/version тела"


def test_set_formula_status_no_lost_updates_under_threads(tmp_path):
    # N потоков параллельно апрувят разные формулы в ОДИН index.json. Без лока
    # read-modify-write индекса теряет обновления (final < N). Лок мутаций репозитория
    # сериализует потоки в процессе — в индексе должны оказаться ровно все N записей.
    n = 20
    index = tmp_path / "formulas" / "_approved" / "index.json"
    paths = []
    for i in range(n):
        p = tmp_path / "formulas" / "niche" / f"f{i}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"name": f"f{i}", "niche": "niche", "version": 1,
                                 "status": "proposed"}), encoding="utf-8")
        paths.append(p)

    barrier = threading.Barrier(n)   # максимизируем конкуренцию на общий индекс

    def worker(p):
        barrier.wait()
        set_formula_status(str(p), "approved", index_path=str(index))

    threads = [threading.Thread(target=worker, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads(index.read_text(encoding="utf-8"))
    names = sorted(e["name"] for e in data["approved"])
    assert names == sorted(f"f{i}" for i in range(n))   # ни одного потерянного апдейта


def test_schema_allows_status_field():
    # Дешевле, чем собирать полный валидный fixture (evidence с 3 URL и т.д.):
    # проверяем enum и то, что status не попал в required, прямо в JSON схемы.
    schema = json.loads(Path("schemas/formula.schema.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]["status"]["enum"]) == {
        "proposed", "approved", "paused", "rejected"}
    assert schema["properties"]["status_reason"]["type"] == "string"
    assert "status" not in schema["required"]


def test_approve_hostile_niche_raises_and_writes_nothing(tmp_path):
    # Ревью аудита: name/niche пишут агенты — traversal в них не должен вывести
    # снапшот за пределы formulas/_approved/ (и вообще ничего не должен записать).
    p = tmp_path / "formulas" / "стритвир" / "evil.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"name": "evil", "niche": "../../..", "version": 1,
                             "status": "proposed"}), encoding="utf-8")
    index = tmp_path / "formulas" / "_approved" / "index.json"
    before = p.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="недопустимым"):
        set_formula_status(str(p), "approved", index_path=str(index))

    assert p.read_text(encoding="utf-8") == before   # файл не мутирован
    assert not index.exists()                        # индекс не создан
    assert not (tmp_path.parent / "evil-v1.json").exists()
