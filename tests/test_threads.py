from unittest import mock

from tests.conftest import H, put, raw


def seed(acc):
    tA, _ = put(acc, "INBOX", 1, raw("Piet <piet@x.be>", "Vraag over offerte", "<a1@x>", days=1, body="Kan je dit bevestigen?"))
    tB, out = put(acc, "Sent Messages", 1, raw("Me <me@icloud.com>", "Factuur", "<b1@me>", to="Ann <ann@y.be>", days=5))
    assert out
    # antwoord komt binnen vóór het origineel: moet toch in dezelfde thread
    tC, _ = put(acc, "INBOX", 2, raw("Bob <bob@z.be>", "Re: Plan", "<c2@z>", irt="<c1@me>", refs="<c1@me>", body="Ok\n> oud"))
    tC2, _ = put(acc, "Sent Messages", 2, raw("Me <me@icloud.com>", "Plan", "<c1@me>", to="bob@z.be", days=2))
    assert tC == tC2
    return tA, tB, tC


def test_auth_required(client):
    assert client.get("/api/inbox").status_code == 401
    assert client.get("/api/inbox", headers={"Authorization": "Bearer fout"}).status_code == 401


def test_inbox_and_followup(client, acc):
    tA, tB, tC = seed(acc)
    inbox = client.get("/api/inbox", headers=H).json()["threads"]
    assert {t["tid"] for t in inbox} == {tA, tC}
    fu = client.get("/api/followup", headers=H).json()
    assert {t["tid"] for t in fu["waiting_on_me"]} == {tA, tC}
    assert [t["tid"] for t in fu["waiting_on_them"]] == [tB]
    assert client.get("/api/inbox?q=factuur", headers=H).json()["threads"][0]["tid"] == tB


def test_reply_threads_correctly(client, acc):
    _, _, tC = seed(acc)
    with mock.patch("smtplib.SMTP") as smtp, mock.patch.object(acc, "run_imap"):
        r = client.post(f"/api/threads/{tC}/send", headers=H, json={"body": "Top.", "archive": True})
    assert r.status_code == 200 and r.json()["to"] == ["bob@z.be"]
    sent = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert sent["Subject"] == "Re: Plan" and sent["In-Reply-To"] == "<c2@z>" and "<c1@me>" in sent["References"]
    fu = client.get("/api/followup", headers=H).json()
    assert tC not in {t["tid"] for t in fu["waiting_on_me"]}


def test_save_draft_goes_to_drafts_folder(client, acc):
    tA, _, _ = seed(acc)
    calls = []
    fake = mock.MagicMock(append=lambda folder, data, flags: calls.append((folder, data, flags)))
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(fake)):
        r = client.post(f"/api/threads/{tA}/save-draft", headers=H, json={"body": "Ja, bevestigd."})
    assert r.json()["folder"] == "Drafts"
    folder, data, flags = calls[0]
    assert b"In-Reply-To: <a1@x>" in data and len(flags) == 2


def test_snooze_done_tags(client, acc):
    tA, tB, _ = seed(acc)
    client.post(f"/api/threads/{tA}/snooze", headers=H, json={"days": 2})
    assert tA not in {t["tid"] for t in client.get("/api/inbox", headers=H).json()["threads"]}
    client.post(f"/api/threads/{tB}/done", headers=H, json={"done": True})
    assert not client.get("/api/followup", headers=H).json()["waiting_on_them"]
    client.post(f"/api/threads/{tA}/tags", headers=H, json={"tags": ["offerte", " werk "]})
    assert client.get("/api/tags", headers=H).json()["tags"] == ["offerte", "werk"]


def test_digest(app, acc):
    seed(acc)
    text = app.digest_text()
    assert text.startswith("Wacht op jou: 2") and "Wacht op antwoord: 1" in text


def test_inbox_badge_counts_unread_personal_mail_only(app, client, acc):
    put(acc, "INBOX", 1, raw("Ann <ann@y.be>", "Vraag", "<q@y>"))
    put(acc, "INBOX", 2, raw("Shop <news@shop.be>", "Promo", "<p@x>", unsub="<https://shop.be/u>"))
    put(acc, "INBOX", 3, raw("Bob <bob@y.be>", "Gelezen", "<g@y>"))
    app.db.x("UPDATE messages SET seen=1 WHERE subject='Gelezen'")
    assert client.get("/api/inbox", headers=H).json()["unread"] == 1
    assert client.get("/api/followup", headers=H).json()["inbox_unread"] == 1
