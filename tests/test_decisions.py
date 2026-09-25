import json

import pytest
from fastapi.testclient import TestClient

from cf.dashboard import decisions
from cf.dashboard.app import create_app
from cf.io import read_json
from cf.lock import LockTimeout
from tests.fakes import FakeSheets


class RecordingGit:
    """Фейковый git: копит (message, paths), реальный git не трогает."""

    def __init__(self):
        self.calls = []

    def __call__(self, message, paths):
        self.calls.append((message, list(paths)))


def _write_formula(root, niche, name, status="proposed", version=1, evidence=None):
    path = root / "formulas" / niche / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "niche": niche,
        "version": version,
        "status": status,
        "evidence": evidence if evidence is not None
        else {"source_urls": ["https://a/1", "https://a/2"],
              "avg_views": 1000.0, "avg_er": 0.12},
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _empty_index(root):
    idx = root / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": []}), encoding="utf-8")
    return idx


def _write_proposal(root, filename, name, status="proposed"):
    path = root / "agent-runtime" / "niche-proposals" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "title": name, "status": status,
                                "cluster_size": 5, "examples": []},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _write_taxonomy(root, niches):
    path = root / "prompts" / "agents" / "niche-taxonomy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"niches": list(niches), "updated_at": "2026-01-01"},
                               ensure_ascii=False), encoding="utf-8")
    return path


# --- decide_formula ---------------------------------------------------------

def test_decide_formula_approve(tmp_path):
    fp = _write_formula(tmp_path, "стритвир", "grid", version=1)
    _empty_index(tmp_path)
    git = RecordingGit()

    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "approved", git=git) is True

    assert read_json(fp)["status"] == "approved"
    idx = read_json(tmp_path / "formulas" / "_approved" / "index.json")
    assert any(e["name"] == "grid" for e in idx["approved"])

    dec_files = list((tmp_path / ".claude" / "memory" / "decisions").glob("*-approve-grid.md"))
    assert len(dec_files) == 1
    text = dec_files[0].read_text(encoding="utf-8")
    assert "grid" in text and "v1" in text

    assert len(git.calls) == 1
    msg, paths = git.calls[0]
    assert "approve grid" in msg
    assert ".claude/memory/decisions/" in paths


def test_decide_formula_pause_with_reason(tmp_path):
    fp = _write_formula(tmp_path, "стритвир", "grid")
    idx = _empty_index(tmp_path)
    git = RecordingGit()

    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "paused", reason="слабый evidence", git=git) is True

    data = read_json(fp)
    assert data["status"] == "paused"
    assert data["status_reason"] == "слабый evidence"
    assert read_json(idx)["approved"] == []
    assert len(git.calls) == 1
    assert "слабый evidence" in git.calls[0][0]


def test_decide_formula_path_traversal_and_absolute_and_approved(tmp_path):
    _write_formula(tmp_path, "стритвир", "grid")
    _empty_index(tmp_path)
    git = RecordingGit()

    assert decisions.decide_formula(tmp_path, "../secrets/x.json", "approved", git=git) is False
    assert decisions.decide_formula(tmp_path, "C:/Windows/x.json", "approved", git=git) is False
    assert decisions.decide_formula(tmp_path, "formulas/_approved/index.json",
                                    "approved", git=git) is False
    assert git.calls == []


def test_decide_formula_approve_hostile_name_refused_entirely(tmp_path):
    # имя формулы пишут агенты — traversal в имени раньше слугировался только
    # в имени decision-файла; со снапшотами (C1/H1 + ревью аудита) approve
    # с таким name/niche отклоняется ЦЕЛИКОМ: ни снапшота, ни decision-файла,
    # ни коммита, ни файлов за пределами репо.
    fp = _write_formula(tmp_path, "стритвир", "grid")
    data = json.loads(fp.read_text(encoding="utf-8"))
    data["name"] = "../../../evil"
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _empty_index(tmp_path)
    git = RecordingGit()

    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "approved", git=git) is False
    dec_dir = (tmp_path / ".claude" / "memory" / "decisions").resolve()
    assert not list(dec_dir.glob("*.md"))       # решение не записано
    assert git.calls == []                       # и не закоммичено
    for stray in (tmp_path / "evil.md", tmp_path / "evil-v1.json",
                  tmp_path.parent / "evil-v1.json",
                  tmp_path / ".claude" / "evil.md"):
        assert not stray.exists()
    index = json.loads((tmp_path / "formulas" / "_approved" / "index.json")
                       .read_text(encoding="utf-8"))
    assert index["approved"] == []               # индекс не тронут


def test_decide_formula_approve_twice_is_noop(tmp_path):
    # Gap#11: двойной клик по «Одобрить» дал два коммита подряд (278b6a1 19:01:26 и
    # 4250977 19:01:28), второй менял ровно approved_at — момент решения оператора
    # переезжал на момент лишнего клика, плюс лишняя строка dashboard-formula в
    # Run Log. Повтор по уже одобренной паре (рецепт, версия) — но-оп.
    _write_formula(tmp_path, "стритвир", "grid", version=1)
    idx = _empty_index(tmp_path)
    git = RecordingGit()
    sheets = FakeSheets({"run_log": []})

    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "approved", git=git, sheets=sheets) is True
    entry = read_json(idx)["approved"][0]
    snapshot = tmp_path / entry["path"]
    snapshot_before = snapshot.read_bytes()

    with pytest.raises(decisions.FormulaAlreadyApproved) as exc:
        decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                 "approved", git=git, sheets=sheets)
    assert "уже одобрен" in str(exc.value)
    assert "grid" in str(exc.value)

    assert len(git.calls) == 1                       # второго коммита нет
    assert len(sheets.tables["run_log"]) == 1        # второй строки в журнале нет
    index = read_json(idx)
    assert len(index["approved"]) == 1
    assert index["approved"][0]["approved_at"] == entry["approved_at"]
    assert snapshot.read_bytes() == snapshot_before  # снапшот не переписан


def test_decide_formula_approve_next_version_is_not_duplicate(tmp_path):
    # Страховка к но-опу: v2 после v1 — новое решение оператора, а не дубль клика.
    fp = _write_formula(tmp_path, "стритвир", "grid", version=1)
    idx = _empty_index(tmp_path)
    git = RecordingGit()
    sheets = FakeSheets({"run_log": []})

    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "approved", git=git, sheets=sheets) is True
    v1_snapshot = tmp_path / read_json(idx)["approved"][0]["path"]
    v1_before = v1_snapshot.read_bytes()

    data = read_json(fp)
    data["version"] = 2
    data["status"] = "proposed"
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "approved", git=git, sheets=sheets) is True
    assert len(git.calls) == 2
    assert "approve grid v2" in git.calls[1][0]
    assert len(sheets.tables["run_log"]) == 2
    assert [str(e["version"]) for e in read_json(idx)["approved"]] == ["2"]
    assert (tmp_path / "formulas" / "_approved" / "стритвир" / "grid-v2.json").is_file()
    assert v1_snapshot.read_bytes() == v1_before     # снапшот v1 иммутабелен


def test_decide_formula_invalid_decision_raises(tmp_path):
    _write_formula(tmp_path, "стритвир", "grid")
    with pytest.raises(ValueError):
        decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json", "maybe")


def test_decide_formula_missing_file_returns_false(tmp_path):
    (tmp_path / "formulas").mkdir()
    git = RecordingGit()
    assert decisions.decide_formula(tmp_path, "formulas/стритвир/нет.json",
                                    "approved", git=git) is False
    assert git.calls == []


def test_decide_formula_lock_timeout_is_graceful(tmp_path, monkeypatch):
    # set_formula_status берёт лок мутаций репозитория и может кинуть LockTimeout
    # (фан-аут пишет индекс). Решение должно мягко деградировать в False, а не
    # пробросить трасбек — и до git не дойти.
    _write_formula(tmp_path, "стритвир", "grid")
    _empty_index(tmp_path)
    git = RecordingGit()

    def boom(*a, **k):
        raise LockTimeout("лок репозитория занят")

    monkeypatch.setattr(decisions, "set_formula_status", boom)
    assert decisions.decide_formula(tmp_path, "formulas/стритвир/grid.json",
                                    "approved", git=git) is False
    assert git.calls == []


# --- decide_niche -----------------------------------------------------------

def test_decide_niche_accepted_dedupe(tmp_path):
    _write_proposal(tmp_path, "барбершоп.json", "барбершоп")
    _write_taxonomy(tmp_path, ["стритвир"])
    git = RecordingGit()

    assert decisions.decide_niche(tmp_path, "барбершоп.json", "accepted", git=git) is True
    prop = read_json(tmp_path / "agent-runtime" / "niche-proposals" / "барбершоп.json")
    assert prop["status"] == "accepted"
    tax = read_json(tmp_path / "prompts" / "agents" / "niche-taxonomy.json")
    assert tax["niches"].count("барбершоп") == 1
    assert len(git.calls) == 1
    assert "prompts/agents/niche-taxonomy.json" in git.calls[0][1]

    # второй вызов не дублирует нишу
    assert decisions.decide_niche(tmp_path, "барбершоп.json", "accepted", git=git) is True
    tax2 = read_json(tmp_path / "prompts" / "agents" / "niche-taxonomy.json")
    assert tax2["niches"].count("барбершоп") == 1


def test_decide_niche_rejected_no_git(tmp_path):
    _write_proposal(tmp_path, "шляпы.json", "шляпы")
    git = RecordingGit()
    assert decisions.decide_niche(tmp_path, "шляпы.json", "rejected", git=git) is True
    prop = read_json(tmp_path / "agent-runtime" / "niche-proposals" / "шляпы.json")
    assert prop["status"] == "rejected"
    assert git.calls == []


def test_decide_niche_path_safety(tmp_path):
    _write_proposal(tmp_path, "ok.json", "ok")
    git = RecordingGit()
    assert decisions.decide_niche(tmp_path, "..\\..\\x.json", "accepted", git=git) is False
    assert decisions.decide_niche(tmp_path, "sub/x.json", "accepted", git=git) is False
    assert decisions.decide_niche(tmp_path, "нет.json", "accepted", git=git) is False
    assert git.calls == []


def test_decide_niche_invalid_decision_raises(tmp_path):
    _write_proposal(tmp_path, "ok.json", "ok")
    with pytest.raises(ValueError):
        decisions.decide_niche(tmp_path, "ok.json", "maybe")


# --- routes -----------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    git = RecordingGit()
    monkeypatch.setattr(decisions, "_default_git", lambda root: git)
    sheets = FakeSheets({"run_log": [], "briefs": []})
    app = create_app(sheets=sheets, lab_root=tmp_path)
    # решения оператора требуют Origin (этап 3б): форма дашборда его шлёт
    return TestClient(app, headers={"origin": "http://127.0.0.1:8787"}), git


def test_route_formula_decision_approves(tmp_path, monkeypatch):
    fp = _write_formula(tmp_path, "стритвир", "grid")
    _empty_index(tmp_path)
    client, git = _client(tmp_path, monkeypatch)

    resp = client.post("/formulas/decision",
                       data={"path": "formulas/стритвир/grid.json", "decision": "approved"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/lab"
    assert read_json(fp)["status"] == "approved"
    assert len(git.calls) == 1


def test_route_formula_decision_second_click_is_noop_422(tmp_path, monkeypatch):
    # Gap#11 на маршруте: повторный сабмит формы не коммитит и не пишет в журнал,
    # а отдаёт оператору честную причину (ValueError -> 422 уже разобран роутером).
    _write_formula(tmp_path, "стритвир", "grid")
    _empty_index(tmp_path)
    client, git = _client(tmp_path, monkeypatch)
    form = {"path": "formulas/стритвир/grid.json", "decision": "approved"}

    assert client.post("/formulas/decision", data=form,
                       follow_redirects=False).status_code == 303
    resp = client.post("/formulas/decision", data=form, follow_redirects=False)

    assert resp.status_code == 422
    assert "уже одобрен" in resp.json()["detail"]
    assert len(git.calls) == 1


def test_route_formula_decision_bad_decision_422(tmp_path, monkeypatch):
    _write_formula(tmp_path, "стритвир", "grid")
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/formulas/decision",
                       data={"path": "formulas/стритвир/grid.json", "decision": "maybe"})
    assert resp.status_code == 422


def test_route_formula_decision_missing_file_404(tmp_path, monkeypatch):
    (tmp_path / "formulas").mkdir()
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/formulas/decision",
                       data={"path": "formulas/стритвир/нет.json", "decision": "approved"})
    assert resp.status_code == 404


def test_route_formula_decision_lock_timeout_no_500(tmp_path, monkeypatch):
    # Лок занят → LockTimeout не должен всплыть необработанным 500. TestClient с
    # raise_server_exceptions=True перебросил бы необработанный LockTimeout из
    # post(); мягкий путь decide_formula→False даёт штатный 404, не траслог.
    _write_formula(tmp_path, "стритвир", "grid")
    _empty_index(tmp_path)

    def boom(*a, **k):
        raise LockTimeout("лок репозитория занят")

    monkeypatch.setattr(decisions, "set_formula_status", boom)
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/formulas/decision",
                       data={"path": "formulas/стритвир/grid.json", "decision": "approved"})
    assert resp.status_code == 404
    assert resp.status_code != 500


def test_route_niche_decision_accepts(tmp_path, monkeypatch):
    _write_proposal(tmp_path, "барбершоп.json", "барбершоп")
    _write_taxonomy(tmp_path, ["стритвир"])
    client, git = _client(tmp_path, monkeypatch)
    resp = client.post("/niches/decision",
                       data={"filename": "барбершоп.json", "decision": "accepted"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/lab"
    tax = read_json(tmp_path / "prompts" / "agents" / "niche-taxonomy.json")
    assert "барбершоп" in tax["niches"]
    assert len(git.calls) == 1


def test_route_niche_decision_bad_decision_422(tmp_path, monkeypatch):
    _write_proposal(tmp_path, "ok.json", "ok")
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/niches/decision",
                       data={"filename": "ok.json", "decision": "maybe"})
    assert resp.status_code == 422


def test_route_niche_decision_missing_404(tmp_path, monkeypatch):
    (tmp_path / "agent-runtime" / "niche-proposals").mkdir(parents=True)
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/niches/decision",
                       data={"filename": "нет.json", "decision": "accepted"})
    assert resp.status_code == 404


def test_decide_niche_corrupt_taxonomy_raises_and_preserves_all(tmp_path):
    # Аудит M25: битая таксономия НЕ трактуется как пустая — accept обязан упасть
    # ДО каких-либо записей: и taxonomy, и proposal остаются нетронутыми.
    _write_proposal(tmp_path, "барбершоп.json", "барбершоп")
    tax_path = tmp_path / "prompts" / "agents" / "niche-taxonomy.json"
    tax_path.parent.mkdir(parents=True, exist_ok=True)
    tax_path.write_text('{"niches": ["стритвир", ', encoding="utf-8")  # обрыв JSON
    git = RecordingGit()

    with pytest.raises(json.JSONDecodeError):
        decisions.decide_niche(tmp_path, "барбершоп.json", "accepted", git=git)

    assert tax_path.read_text(encoding="utf-8") == '{"niches": ["стритвир", '
    prop = read_json(tmp_path / "agent-runtime" / "niche-proposals" / "барбершоп.json")
    assert prop["status"] == "proposed"          # решение не применено
    assert git.calls == []


def test_decide_niche_rejected_ignores_corrupt_taxonomy(tmp_path):
    # reject таксономию не читает — битый файл не мешает отклонить предложение
    _write_proposal(tmp_path, "шляпы.json", "шляпы")
    tax_path = tmp_path / "prompts" / "agents" / "niche-taxonomy.json"
    tax_path.parent.mkdir(parents=True, exist_ok=True)
    tax_path.write_text("{битый", encoding="utf-8")
    git = RecordingGit()
    assert decisions.decide_niche(tmp_path, "шляпы.json", "rejected", git=git) is True
