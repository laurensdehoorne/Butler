import json
from unittest import mock

from app import autopilot as A
from tests.conftest import H, put, raw
from tests.test_ai import claude_reply
from tests.test_attachments import imap_for, mail_with_attachments

BILLIT = "praktijk-abc-nosplit@my.billit.be"
TRIAGE = ('{"samenvatting":"x","prioriteit":"normaal","categorie":"%s","actie":"%s","deadline":null,'
          '"wacht_op_antwoord":false,"dossiers":[]}')


def setup(app, monkeypatch, **on):
    monkeypatch.setitem(app.APP, "auto", on)
    app.db.x("DELETE FROM settings WHERE key LIKE 'auto_since_%'")
    app.db.x("INSERT INTO settings(key, value) VALUES('auto_since_invoices', '2000-01-01'), ('auto_since_drafts', '2000-01-01')")
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    return fake


def test_new_invoice_goes_to_billit_by_itself(app, client, acc, monkeypatch):
    fake = setup(app, monkeypatch, invoices=True)
    put(acc, "Sent Messages", 40, raw("Me <me@icloud.com>", "", "<s@me>", to=BILLIT, days=30))
    data = mail_with_attachments()
    tid, _ = put(acc, "INBOX", 5, data)
    fake.messages.create.return_value = claude_reply(TRIAGE % ("factuur", "lezen"))
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap_for(data, 5))), \
         mock.patch("app.actions.smtp_send") as smtp:
        app.ai.triage(tid)
        for hook in app.ai.after_triage:
            hook(tid)
            hook(tid)  # nog eens bekeken: niet twee keer versturen
    assert smtp.call_count == 1 and smtp.call_args.args[1]["To"] == BILLIT
    e = client.get("/api/activity", headers=H).json()["entries"]
    assert [(x["kind"], x["can_undo"]) for x in e] == [("factuur", 0)] and BILLIT in e[0]["detail"]
    assert client.get(f"/api/threads/{tid}", headers=H).json()["auto"][0]["kind"] == "factuur"


def test_old_mail_is_left_alone(app, acc, monkeypatch):
    setup(app, monkeypatch, invoices=True, drafts=True)
    app.db.x("UPDATE settings SET value='2999-01-01' WHERE key LIKE 'auto_since_%'")  # aangezet na deze mail
    tid, _ = put(acc, "INBOX", 1, raw("Ann <ann@y.be>", "Vraag", "<q@y>"))
    app.db.x("UPDATE threads SET action='antwoorden'")
    with mock.patch.object(app.ai, "draft") as draft:
        for hook in app.ai.after_triage:
            hook(tid)
    draft.assert_not_called()


def test_draft_is_prepared_and_can_be_undone(app, client, acc, monkeypatch):
    setup(app, monkeypatch, drafts=True)
    tid, _ = put(acc, "INBOX", 1, raw("Ann <ann@y.be>", "Etentje zaterdag?", "<e@y>", body="Past zaterdag?"))
    app.db.x("UPDATE threads SET action='antwoorden'")
    imap = mock.MagicMock()
    imap.search.return_value = [77]
    with mock.patch.object(app.ai, "draft", return_value="Dag Ann,\n\nZaterdag past!\n\nLaurens"), \
         mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)):
        for hook in app.ai.after_triage:
            hook(tid)
            hook(tid)
        assert imap.append.call_count == 1 and imap.append.call_args.args[0] == "Drafts"
        e = client.get("/api/activity", headers=H).json()["entries"][0]
        assert e["kind"] == "concept" and e["can_undo"]
        assert client.post(f"/api/activity/{e['id']}/undo", headers=H).json()["ok"]
    imap.delete_messages.assert_called_once_with([77])
    assert client.post(f"/api/activity/{e['id']}/undo", headers=H).status_code == 400  # al teruggedraaid


def test_ads_after_a_day_logged_and_restorable(app, client, acc, monkeypatch):
    put(acc, "INBOX", 10, raw("Shop <news@shop.be>", "Promo", "<p1@x>", unsub="<https://shop.be/u>", days=2))
    put(acc, "INBOX", 11, raw("Shop <news@shop.be>", "Nieuwe promo", "<p2@x>", unsub="<https://shop.be/u>"))
    monkeypatch.setitem(app.syncer.cleanup, "reclame", A.ADS_AFTER_DAYS)
    imap = mock.MagicMock()
    imap.has_capability.return_value = True
    imap.search.return_value = [5]
    app.syncer.apply_rules(imap, acc)
    imap.move.assert_called_once_with([10], "Archive")  # die van vandaag blijft nog
    e = client.get("/api/activity", headers=H).json()["entries"][0]
    assert e["kind"] == "opruimen" and "Promo" in e["detail"]
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)):
        client.post(f"/api/activity/{e['id']}/undo", headers=H)
    assert imap.search.call_args.args[0] == ["HEADER", "Message-ID", "<p1@x>"]
    assert imap.move.call_args.args == ([5], "INBOX")
    assert "<p1@x>" in A.restored(app.db)  # en die ruimt Butler niet opnieuw op
