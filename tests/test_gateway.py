import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import gateway as G


class FakeBackends:
    secret = "geheim"

    def __init__(self):
        self.started = []

    def start(self, u):
        self.started.append(u["id"])


@pytest.fixture
def gw(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "DATA", tmp_path)
    cfg = tmp_path / "start.yaml"
    cfg.write_text(yaml.safe_dump({"app": {"token": "oud-token-123"}, "ai": {"name": "Laurens", "api_key": "sk-1",
                                                                            "signature": "Laurens"}}))
    monkeypatch.setattr(G, "MAIN_CONFIG", str(cfg))
    seen = []

    def upstream(request):  # de Butler van de gebruiker
        seen.append(request)
        body = json.dumps({"port": request.url.port, "path": request.url.path}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    users = G.Users(tmp_path / "users.json")
    G.migrate(users)  # de bestaande Butler wordt de beheerder, met het oude token als wachtwoord
    backends = FakeBackends()
    app = G.create_app(users, key=b"k" * 32, backends=backends, transport=httpx.MockTransport(upstream))
    return TestClient(app), users, backends, seen


def login(c, name, pw):
    return c.post("/login", json={"name": name, "password": pw})


def test_login_required_and_routes_to_own_butler(gw):
    c, users, _, seen = gw
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    r = c.get("/api/followup")
    assert r.status_code == 401 and r.headers["x-butler-login"] == "/login"
    assert c.get("/manifest.webmanifest").status_code == 200  # de app-schil mag zonder aanmelden
    assert login(c, "laurens", "fout").status_code == 401
    assert login(c, "Laurens", "oud-token-123").status_code == 200
    r = c.get("/api/followup?account=gmail").json()
    assert r == {"port": 9101, "path": "/api/followup"}
    assert seen[-1].headers["x-butler-gateway"] == "geheim" and "cookie" not in seen[-1].headers
    assert seen[-1].url.query == b"account=gmail"


def test_admin_adds_user_with_own_database(gw, tmp_path):
    c, users, backends, _ = gw
    login(c, "laurens", "oud-token-123")
    r = c.post("/api/users", json={"name": "Marie-Elisabeth", "password": "kort"})
    assert r.status_code == 400
    assert c.post("/api/users", json={"name": "Marie-Elisabeth", "password": "geheim-wachtwoord"}).json()["id"] == "marie-elisabeth"
    m = users.get("marie-elisabeth")
    assert m["port"] == 9102 and m["data"] == str(tmp_path / "users" / "marie-elisabeth") and not m["admin"]
    seeded = yaml.safe_load((tmp_path / "users" / "marie-elisabeth" / "config.yaml").read_text())
    assert seeded["ai"] == {"api_key": "sk-1", "name": "Marie"} and seeded["accounts"] == []  # sleutel wel, handtekening niet
    assert backends.started == ["marie-elisabeth"]
    assert [u["name"] for u in c.get("/api/me").json()["users"]] == ["Laurens", "Marie-Elisabeth"]

    m = TestClient(c.app)
    assert login(m, "marie-elisabeth", "geheim-wachtwoord").status_code == 200
    assert m.get("/api/threads/1").json()["port"] == 9102  # haar eigen Butler
    assert m.post("/api/users", json={"name": "X", "password": "12345678"}).status_code == 403
    assert m.put("/api/users/laurens/password", json={"password": "12345678"}).status_code == 403
    assert m.put("/api/users/marie-elisabeth/password", json={"password": "nieuw-wachtwoord"}).json()["ok"]
    assert login(m, "Marie-Elisabeth", "nieuw-wachtwoord").status_code == 200


def test_tampered_session_is_refused(gw):
    c, *_ = gw
    login(c, "laurens", "oud-token-123")
    uid, exp, sig = c.cookies[G.COOKIE].split("|")
    c.cookies.clear()
    c.cookies.set(G.COOKIE, f"marie-elisabeth|{exp}|{sig}")
    assert c.get("/api/me").status_code == 401


def test_first_visit_creates_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "DATA", tmp_path)
    monkeypatch.setattr(G, "MAIN_CONFIG", str(tmp_path / "geen.yaml"))
    users = G.Users(tmp_path / "users.json")
    G.migrate(users)  # geen token: niemand aangemaakt
    c = TestClient(G.create_app(users, key=b"k" * 32, backends=FakeBackends()))
    assert 'const setup = "1"' in c.get("/login").text
    assert login(c, "Laurens", "een-goed-wachtwoord").status_code == 200
    assert users.get("laurens")["admin"] and c.get("/api/me").json()["admin"]


def test_share_copies_straight_to_the_other_butler(gw):
    c, users, backends, seen = gw
    login(c, "laurens", "oud-token-123")
    c.post("/api/users", json={"name": "Marie-Elisabeth", "password": "geheim-wachtwoord"})
    r = c.post("/api/share", json={"tid": 7, "to": "marie-elisabeth", "note": "Kijk eens"})
    assert r.json() == {"ok": True, "to": "Marie-Elisabeth"}
    export, receive = seen[-2], seen[-1]
    assert (export.url.port, export.url.path) == (9101, "/api/threads/7/export")
    assert (receive.url.port, receive.url.path) == (9102, "/api/shared")
    body = json.loads(receive.content)
    assert body["sender"] == "Laurens" and body["note"] == "Kijk eens" and receive.headers["x-butler-gateway"] == "geheim"
    assert c.post("/api/share", json={"tid": 7, "to": "laurens"}).status_code == 400  # niet met jezelf


def test_calendar_link_works_without_session(gw):
    c, users, _, seen = gw
    tok = "a" * 32
    assert c.get(f"/cal/laurens/{tok}.ics").json() == {"port": 9101, "path": f"/cal/{tok}.ics"}
    assert c.get(f"/cal/niemand/{tok}.ics").status_code == 404
    assert c.get("/cal/laurens/kort.ics", follow_redirects=False).status_code == 303  # geen geheim: aanmelden
