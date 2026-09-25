# Этап 3 — дашборд за Caddy: POST с Origin публичного домена должен проходить
# CSRF-гард (без public_origin ловил бы 403 на каждой кнопке).
from fastapi.testclient import TestClient

from cf.dashboard.app import create_app

from tests.fakes import FakeSheets


def make_client(**kwargs):
    sheets = FakeSheets(tables={"run_log": [], "briefs": []})
    return TestClient(create_app(sheets=sheets, **kwargs))


def test_public_origin_allowed():
    client = make_client(public_origin="https://cf.example.com")
    resp = client.post("/refresh", headers={"Origin": "https://cf.example.com"},
                       follow_redirects=False)
    assert resp.status_code != 403


def test_foreign_origin_still_rejected():
    client = make_client(public_origin="https://cf.example.com")
    resp = client.post("/refresh", headers={"Origin": "https://evil.example"},
                       follow_redirects=False)
    assert resp.status_code == 403


def test_without_public_origin_domain_rejected():
    client = make_client()
    resp = client.post("/refresh", headers={"Origin": "https://cf.example.com"},
                       follow_redirects=False)
    assert resp.status_code == 403


def test_trailing_slash_normalized():
    client = make_client(public_origin="https://cf.example.com/")
    resp = client.post("/refresh", headers={"Origin": "https://cf.example.com"},
                       follow_redirects=False)
    assert resp.status_code != 403
