from pathlib import Path
from unittest import mock

import pytest
import yaml

from app import config as C
from tests.conftest import H


@pytest.fixture
def saved(app):
    """Het bestand dat de app bewaart (in tests hetzelfde als het meegegeven config.yaml); nadien terugzetten."""
    path = C.data_path(app.DATA_DIR)
    before = path.read_text(encoding="utf-8")
    yield lambda: yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(before, encoding="utf-8")
    app.SAVED["cfg"] = app.CFG


def test_secrets_never_reach_the_browser(client):
    d = client.get("/api/config", headers=H).json()
    assert d["values"]["ai.api_key"] == "" and d["secrets_set"]["ai.api_key"] is True
    assert d["values"]["app.token"] == "" and d["secrets_set"]["app.token"] is True
    assert d["secrets_set"]["notify.ntfy_token"] is False
    acc = d["accounts"][0]
    assert (acc["id"], acc["email"], acc["password"], acc["password_set"]) == ("icloud", "me@icloud.com", "", True)
    assert "sk-ant-test" not in str(d) and "t0k" not in str(d)
    assert {s["id"] for s in d["sections"]} == {"sync", "followup", "ai", "notify", "general"}


def test_save_section_keeps_secrets_and_does_not_restart_in_tests(app, client, saved):
    r = client.put("/api/config", headers=H, json={"values": {"app.followup_days": 5, "ai.api_key": "",
                                                               "notify.digest_days": [5, 1, 9], "ai.signature": "Groet,\nJan\n"}})
    assert r.json() == {"ok": True, "restarting": False}
    cfg = saved()
    assert cfg["app"]["followup_days"] == 5
    assert cfg["ai"]["api_key"] == "sk-ant-test"  # leeg = behouden
    assert cfg["notify"]["digest_days"] == [1, 5]
    assert cfg["ai"]["signature"] == "Groet,\nJan\n"
    assert cfg["accounts"][0]["password"] == "x"  # accounts niet meegestuurd: onaangeroerd
    assert oct(Path(C.data_path(app.DATA_DIR)).stat().st_mode & 0o777) == "0o600"


def test_save_restarts_when_enabled(app, client, saved):
    with mock.patch.object(app, "RESTART", True), mock.patch.object(app, "restart_soon") as restart:
        assert client.put("/api/config", headers=H, json={"values": {"app.poll_seconds": 120}}).json()["restarting"]
    restart.assert_called_once()


def test_invalid_values_are_refused_per_field(client, saved):
    r = client.put("/api/config", headers=H, json={"values": {"app.timezone": "Europe/Gent", "notify.quiet_hours": "22-7",
                                                               "notify.mode": "soms", "app.sync_days": -1}})
    assert r.status_code == 400
    errors = r.json()["detail"]["errors"]
    assert set(errors) == {"app.timezone", "notify.quiet_hours", "notify.mode", "app.sync_days"}
    assert saved()["app"].get("timezone") is None  # niets bewaard


def test_accounts_edit_add_and_validate(client, saved):
    d = client.get("/api/config", headers=H).json()
    existing = {**d["accounts"][0], "name": "Mijn iCloud"}  # wachtwoord leeg = behouden
    new = {"name": "Gmail", "provider": "gmail", "email": "jan@gmail.com", "password": "abcd efgh", "aliases": "jan@werk.be, ",
           "imap_port": 993, "append_sent": True, "ai": False}
    for a in (existing, new):
        a.pop("password_set", None)
    r = client.put("/api/config", headers=H, json={"accounts": [existing, new]})
    assert r.status_code == 200, r.text
    accs = saved()["accounts"]
    assert accs[0]["name"] == "Mijn iCloud" and accs[0]["password"] == "x"
    assert accs[1]["id"] == "gmail" and accs[1]["aliases"] == ["jan@werk.be"] and accs[1]["ai"] is False
    assert "append_sent" not in accs[1] and "imap_port" not in accs[1]  # enkel voor andere providers: Gmail-standaard blijft

    r = client.put("/api/config", headers=H, json={"accounts": [{"name": "Werk", "provider": "custom", "email": "info@werk.be"}]})
    assert set(r.json()["detail"]["errors"]) == {"accounts.0.password", "accounts.0.imap_host", "accounts.0.smtp_host"}


def test_new_ids_are_unique():
    out = C.apply({"accounts": [{"id": "gmail", "email": "a@gmail.com", "password": "p", "provider": "gmail"}]},
                  accounts=[{"id": "gmail", "provider": "gmail", "email": "a@gmail.com"},
                            {"name": "Gmail", "provider": "gmail", "email": "b@gmail.com", "password": "q"}])
    assert [a["id"] for a in out["accounts"]] == ["gmail", "gmail-2"]
    assert out["accounts"][0]["password"] == "p"


def test_connection_test_uses_stored_password(client):
    d = client.get("/api/config", headers=H).json()
    acc = {k: v for k, v in d["accounts"][0].items() if k != "password_set"}
    with mock.patch("imapclient.IMAPClient") as imap:
        imap.return_value.list_folders.return_value = [1, 2, 3]
        r = client.post("/api/config/test-account", headers=H, json={"account": acc})
    assert r.json() == {"ok": True, "folders": 3}
    assert imap.call_args[0][0] == "imap.mail.me.com"
    imap.return_value.login.assert_called_once_with("me@icloud.com", "x")
    with mock.patch("imapclient.IMAPClient", side_effect=OSError("geen netwerk")):
        r = client.post("/api/config/test-account", headers=H, json={"account": acc})
    assert r.status_code == 400 and "geen netwerk" in r.json()["detail"]


def test_load_prefers_app_config_and_starts_empty_without_any(tmp_path):
    fallback = tmp_path / "mounted.yaml"
    assert C.load(tmp_path, fallback) == ({}, "")
    fallback.write_text("app: {followup_days: 4}\n")
    assert C.load(tmp_path, fallback)[0]["app"]["followup_days"] == 4
    C.save({"app": {"followup_days": 9}}, tmp_path)
    cfg, src = C.load(tmp_path, fallback)
    assert cfg["app"]["followup_days"] == 9 and src.endswith("config.yaml") and "mounted" not in src
    (tmp_path / "dir.yaml").mkdir()  # Docker maakt een map als het bestand ontbreekt
    assert C.load(tmp_path / "leeg", tmp_path / "dir.yaml") == ({}, "")


def test_bare_off_in_yaml_means_off():
    from app.notify import Notifier

    cfg = yaml.safe_load("notify: {mode: off}")
    assert cfg["notify"]["mode"] is False
    assert C.public(cfg)["values"]["notify.mode"] == "off"
    assert Notifier(cfg["notify"], {}).mode == "off"


def test_two_saves_without_restart_keep_both(app, client, saved):
    client.put("/api/config", headers=H, json={"values": {"app.followup_days": 6}})
    client.put("/api/config", headers=H, json={"values": {"notify.quiet_hours": "23:00-06:30"}})
    cfg = saved()
    assert cfg["app"]["followup_days"] == 6 and cfg["notify"]["quiet_hours"] == "23:00-06:30"
    assert client.get("/api/config", headers=H).json()["values"]["app.followup_days"] == 6
