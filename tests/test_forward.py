from unittest import mock

from tests.conftest import H, put, raw
from tests.test_attachments import PDF, imap_for, mail_with_attachments

BILLIT = "praktijk-abc-nosplit@my.billit.be"


def test_forward_with_attachments(app, client, acc):
    data = mail_with_attachments()
    tid, _ = put(acc, "INBOX", 5, data)
    mid = client.get(f"/api/threads/{tid}", headers=H).json()["messages"][0]["id"]
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap_for(data, 5))), \
         mock.patch("app.actions.smtp_send") as smtp:
        r = client.post(f"/api/threads/{tid}/forward", headers=H,
                        json={"message_id": mid, "to": ["ann@y.be"], "note": "Ter info"}).json()
    em = smtp.call_args.args[1]
    assert r["attachments"] == 3 and em["To"] == "ann@y.be" and em["Subject"] == "Fwd: E-ticket"
    assert [p.get_filename() for p in em.iter_attachments()] == ["E-ticket AF1068.pdf", "boarding.jpg", "factuur.html"]
    assert next(em.iter_attachments()).get_content() == PDF
    body = em.get_body(("plain",)).get_content()
    assert body.startswith("Ter info") and "Doorgestuurd bericht" in body and "Van: Air France <af@airfrance.fr>" in body
    assert client.post(f"/api/threads/{tid}/forward", headers=H, json={"message_id": mid, "to": ["geen-adres"]}).status_code == 400


def test_invoice_goes_to_bookkeeping_in_one_tap(app, client, acc):
    for i in range(3):  # zo stuur je je facturen al door: Secretaresse leert het adres
        put(acc, "Sent Messages", 40 + i, raw("Me <me@icloud.com>", "", f"<s{i}@me>", to=f"Billit <{BILLIT}>", days=30))
    data = mail_with_attachments()
    tid, _ = put(acc, "INBOX", 5, data)
    assert client.get(f"/api/threads/{tid}", headers=H).json()["invoice"] is None  # een e-ticket is geen factuur
    app.db.x("UPDATE threads SET category='factuur' WHERE tid=?", (tid,))
    inv = client.get(f"/api/threads/{tid}", headers=H).json()["invoice"]
    assert inv["to"] == BILLIT and not inv["done"]
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap_for(data, 5))), mock.patch("app.actions.smtp_send"):
        client.post(f"/api/threads/{tid}/forward", headers=H, json={"message_id": inv["message_id"], "to": [BILLIT]})
    assert client.get(f"/api/threads/{tid}", headers=H).json()["invoice"]["done"]  # meteen, nog voor de sync

    plain, _ = put(acc, "INBOX", 6, raw("Ann <ann@y.be>", "Etentje", "<e@y>"))
    assert client.get(f"/api/threads/{plain}", headers=H).json()["invoice"] is None


def test_older_invoice_without_attachment_list(app, client, acc):
    put(acc, "Sent Messages", 40, raw("Me <me@icloud.com>", "", "<s@me>", to=BILLIT, days=30))
    tid, _ = put(acc, "INBOX", 7, raw("Acerta <f@acerta.be>", "Jouw factuur van Acerta", "<a@x>"))
    app.db.x("UPDATE messages SET attachments=NULL")  # van voor de bijlagen
    inv = client.get(f"/api/threads/{tid}", headers=H).json()["invoice"]
    assert inv["to"] == BILLIT and not inv["done"]
    put(acc, "Sent Messages", 41, raw("Me <me@icloud.com>", "Fwd: Jouw factuur van Acerta", "<s2@me>", to=BILLIT))
    assert client.get(f"/api/threads/{tid}", headers=H).json()["invoice"]["done"]
