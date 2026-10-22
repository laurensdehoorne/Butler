from unittest import mock

from tests.conftest import H, put, raw


def seed(acc):
    put(acc, "INBOX", 1, raw("Zalando <newsletter@zalando.be>", "Sale!", "<z1@z>", days=1, unsub="<https://z.be/u?x=1>", one_click=True))
    put(acc, "INBOX", 2, raw("Zalando <newsletter@zalando.be>", "Sale 2", "<z2@z>", days=3, unsub="<https://z.be/u?x=1>", one_click=True))
    put(acc, "INBOX", 3, raw("Shop <info@shop.be>", "Promo", "<s1@s>", days=2, unsub="<mailto:unsub@shop.be?subject=stop>, <https://shop.be/u>"))
    put(acc, "INBOX", 4, raw("Bol <noreply@bol.com>", "Je bestelling is verzonden", "<b1@b>", days=10))
    put(acc, "INBOX", 5, raw("Piet <piet@x.be>", "Vraagje", "<p1@p>", days=1))


def test_sender_overview(client, acc):
    seed(acc)
    rec = client.get("/api/senders?kind=reclame", headers=H).json()["senders"]
    assert [(s["addr"], s["in_inbox"], s["unsub_method"]) for s in rec] == [
        ("newsletter@zalando.be", 2, "one-click"), ("info@shop.be", 1, "mail")]
    notif = client.get("/api/senders?kind=notificatie", headers=H).json()["senders"]
    assert [s["addr"] for s in notif] == ["noreply@bol.com"]
    assert [t["subject"] for t in client.get("/api/inbox?filter=persoonlijk", headers=H).json()["threads"]] == ["Vraagje"]


def test_unsubscribe_one_click_and_mailto(client, acc):
    seed(acc)
    with mock.patch("httpx.post") as hp:
        hp.return_value.status_code = 200
        r = client.post("/api/senders/unsubscribe", headers=H, json={"account": "icloud", "addr": "newsletter@zalando.be"})
    assert r.json()["method"] == "one-click"
    assert hp.call_args[0][0] == "https://z.be/u?x=1" and hp.call_args[1]["data"] == {"List-Unsubscribe": "One-Click"}
    with mock.patch("smtplib.SMTP") as smtp:
        r = client.post("/api/senders/unsubscribe", headers=H, json={"account": "icloud", "addr": "info@shop.be"})
    m = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert r.json()["method"] == "mail" and m["To"] == "unsub@shop.be" and m["Subject"] == "stop"


def test_rules_are_applied(app, client, acc):
    seed(acc)
    with mock.patch("httpx.post") as hp:
        hp.return_value.status_code = 200
        client.post("/api/senders/unsubscribe", headers=H, json={"account": "icloud", "addr": "newsletter@zalando.be"})
    client.post("/api/senders/rule", headers=H, json={"account": "icloud", "addr": "noreply@bol.com",
                                                     "action": "archive_after", "after_days": 7})
    client.post("/api/senders/rule", headers=H, json={"account": "icloud", "addr": "info@shop.be", "action": "trash"})
    plan = app.syncer.plan_cleanup(acc)
    assert sorted(i for i, _ in plan["archive"]) == [1, 2, 4] and [i for i, _ in plan["trash"]] == [3]
    imap = mock.MagicMock()
    imap.has_capability.return_value = True
    app.syncer.apply_rules(imap, acc)
    moved = {c.args[1]: sorted(c.args[0]) for c in imap.move.call_args_list}
    assert moved == {"Archive": [1, 2, 4], "Deleted Messages": [3]}
    assert [t["subject"] for t in client.get("/api/inbox", headers=H).json()["threads"]] == ["Vraagje"]


def test_kind_correction(client, acc):
    seed(acc)
    client.post("/api/senders/rule", headers=H, json={"account": "icloud", "addr": "info@shop.be", "kind": "persoonlijk"})
    assert "info@shop.be" not in [s["addr"] for s in client.get("/api/senders?kind=reclame", headers=H).json()["senders"]]
    assert "Promo" in [t["subject"] for t in client.get("/api/inbox?filter=persoonlijk", headers=H).json()["threads"]]
