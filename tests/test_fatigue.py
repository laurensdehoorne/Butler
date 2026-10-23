from datetime import datetime, timedelta, timezone

from app import fatigue as F
from tests.conftest import H, put, raw


def test_ignored_sender_goes_quiet_and_can_be_unmuted(app, client, acc):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for i in range(F.IGNORED_MAX):
        tid, _ = put(acc, "INBOX", i + 1, raw("Shop <deals@shop.be>", f"Aanbieding {i}", f"<a{i}@x>", days=3))
        app.db.x("INSERT INTO notify_log(at, account, addr, tid) VALUES(?,?,?,?)", (old, acc.id, "deals@shop.be", tid))
    put(acc, "INBOX", 50, raw("Ann <ann@y.be>", "Vraag", "<q@y>", days=3))
    assert F.check(app.db) == ["deals@shop.be"]
    assert F.muted(app.db, acc.id, "deals@shop.be") and not F.muted(app.db, acc.id, "ann@y.be")
    assert client.get("/api/notify/muted", headers=H).json()["muted"][0]["notify_auto"] == 1
    t = client.get(f"/api/threads/{tid}", headers=H).json()
    assert t["sender"]["muted"] and t["sender"]["muted_auto"]
    client.post("/api/senders/rule", headers=H, json={"account": acc.id, "addr": "deals@shop.be", "notify": 1})
    assert not F.muted(app.db, acc.id, "deals@shop.be") and F.check(app.db) == []  # jouw keuze blijft


def test_read_mail_is_not_ignored(app, acc):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for i in range(F.IGNORED_MAX):
        tid, _ = put(acc, "INBOX", i + 1, raw("Ann <ann@y.be>", f"Vraag {i}", f"<q{i}@y>", days=3))
        app.db.x("INSERT INTO notify_log(at, account, addr, tid) VALUES(?,?,?,?)", (old, acc.id, "ann@y.be", tid))
    app.db.x("UPDATE messages SET seen=1 WHERE subject='Vraag 2'")
    assert F.check(app.db) == []


def test_topics_can_be_switched_off(app, monkeypatch):
    monkeypatch.setitem(app.notifier.topics, "shared", False)
    sent = []
    monkeypatch.setattr(app.notifier, "push", type("P", (), {"send": lambda self, m: sent.append(m)})())
    app.notifier.send("Laurens deelde een mail", "x", extra={"type": "shared"}, force=True)
    app.notifier.send("Ann", "Vraag", tid=1, force=True)
    assert [m["title"] for m in sent] == ["Ann"]


def test_priority_sender_always_notifies(app, client, acc, monkeypatch):
    tid, _ = put(acc, "INBOX", 1, raw("Mama <mama@thuis.be>", "Zondag eten?", "<z@t>"))
    other, _ = put(acc, "INBOX", 2, raw("Shop <s@shop.be>", "Nieuws", "<n@s>"))
    monkeypatch.setattr(app.notifier, "mode", "important")
    monkeypatch.setattr(app.notifier, "quiet", (__import__("datetime").time(0, 0), __import__("datetime").time(23, 59)))
    sent = []
    monkeypatch.setattr(app.notifier, "push", type("P", (), {"send": lambda self, m: sent.append(m)})())
    client.post("/api/senders/rule", headers=H, json={"account": acc.id, "addr": "mama@thuis.be", "notify": 1})
    app.ai._maybe_notify(tid, {"priority": "normaal"})  # gewone mail, tijdens de stille uren
    app.ai._maybe_notify(other, {"priority": "normaal"})
    assert [m["title"] for m in sent] == ["Mama"]
    assert client.get("/api/notify/muted", headers=H).json()["priority"][0]["addr"] == "mama@thuis.be"
    assert client.get(f"/api/threads/{tid}", headers=H).json()["sender"]["priority"]
