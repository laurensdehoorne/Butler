from unittest import mock

from tests.conftest import H, put
from tests.test_attachments import JPG, PDF, imap_for, mail_with_attachments


def test_export_and_receive_a_shared_thread(app, client, acc, tmp_path, monkeypatch):
    data = mail_with_attachments()
    tid, _ = put(acc, "INBOX", 5, data)
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap_for(data, 5))):
        out = client.get(f"/api/threads/{tid}/export", headers=H).json()
    m = out["messages"][0]
    assert out["subject"] == "E-ticket" and "e-ticket" in m["html"] and m["attachments"][0]["data"]

    monkeypatch.setattr(app, "DATA_DIR", str(tmp_path))
    with mock.patch.object(app.notifier, "send") as send:
        sid = client.post("/api/shared", headers=H, json={"sender": "Laurens", "note": "Onze vlucht", "thread": out}).json()["id"]
    assert send.call_args.args[0] == "Laurens deelde een mail"
    assert [x["subject"] for x in client.get("/api/shared", headers=H).json()["shared"]] == ["E-ticket"]
    d = client.get(f"/api/shared/{sid}", headers=H).json()
    atts = d["messages"][0]["attachments"]
    assert d["note"] == "Onze vlucht" and "data" not in atts[0]
    assert client.get(atts[0]["url"], headers=H).content == PDF and client.get(atts[1]["url"], headers=H).content == JPG
    assert client.get(atts[2]["url"], headers=H).headers["content-disposition"].startswith("attachment")  # html: downloaden
    client.post(f"/api/shared/{sid}/done", headers=H)
    assert client.get("/api/shared", headers=H).json()["shared"][0]["done"] == 1
    client.delete(f"/api/shared/{sid}", headers=H)
    assert client.get("/api/shared", headers=H).json()["shared"] == [] and not (tmp_path / "shared" / str(sid)).exists()
