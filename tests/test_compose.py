import base64
from unittest import mock

from tests.conftest import H


def body(acc, **kw):
    return {"account": acc.id, "to": ["ann@y.be"], "subject": "Offerte", "body": "Dag Ann,\n\nHierbij.", **kw}


def test_compose_sends_new_mail_with_attachment(app, client, acc):
    pdf = base64.b64encode(b"%PDF-1.4 test").decode()
    with mock.patch("app.actions.smtp_send") as smtp, mock.patch("app.actions.append_sent") as sent:
        r = client.post("/api/compose/send", headers=H, json=body(
            acc, cc=["bob@y.be"], files=[{"name": "offerte.pdf", "type": "application/pdf", "data": pdf}]))
    assert r.status_code == 200 and r.json()["to"] == ["ann@y.be"]
    em = smtp.call_args.args[1]
    assert em["To"] == "ann@y.be" and em["Cc"] == "bob@y.be" and em["Subject"] == "Offerte"
    assert not em["In-Reply-To"] and em["Message-ID"]
    assert [p.get_filename() for p in em.iter_attachments()] == ["offerte.pdf"]
    assert em.get_body(("plain",)).get_content().startswith("Dag Ann,")
    sent.assert_called_once()


def test_compose_draft_goes_to_drafts_folder(app, client, acc):
    acc.folders["drafts"] = "Drafts"
    with mock.patch("app.actions.smtp_send") as smtp, mock.patch.object(acc, "run_imap") as imap:
        r = client.post("/api/compose/draft", headers=H, json=body(acc))
    assert r.status_code == 200 and r.json()["folder"] == "Drafts"
    smtp.assert_not_called()
    imap.assert_called_once()


def test_compose_validates_input(app, client, acc):
    with mock.patch("app.actions.smtp_send") as smtp:
        assert client.post("/api/compose/send", headers=H, json=body(acc, to=["geen-adres"])).status_code == 400
        assert client.post("/api/compose/send", headers=H, json=body(acc, to=[])).status_code == 400
        assert client.post("/api/compose/send", headers=H, json=body(acc, body="  ")).status_code == 400
        assert client.post("/api/compose/send", headers=H, json=body(acc, account="onbekend")).status_code == 404
    smtp.assert_not_called()
