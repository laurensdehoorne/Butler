from datetime import datetime, timezone
from unittest import mock

from imapclient import SEEN

from tests.conftest import raw


class FakeIMAP:
    """Genoeg van IMAPClient voor Syncer: select, search, fetch."""

    def __init__(self):
        self.box = {"INBOX": {10: raw("P <p10@x.be>", "een", "<m10@x>"), 11: raw("P <p11@x.be>", "twee", "<m11@x>")},
                    "Sent Messages": {}}
        self.cur = None

    def select_folder(self, folder, readonly=False):
        self.cur = folder
        return {b"UIDVALIDITY": 7}

    def search(self, criteria):
        return list(self.box[self.cur])

    def fetch(self, uids, items):
        out = {}
        for u in uids:
            data, d = self.box[self.cur][u], {}
            for it in items:
                if it == "RFC822.SIZE":
                    d[b"RFC822.SIZE"] = len(data)
                elif it == "FLAGS":
                    d[b"FLAGS"] = (SEEN,) if u == 10 else ()
                elif it == "INTERNALDATE":
                    d[b"INTERNALDATE"] = datetime.now(timezone.utc)
                elif it == "BODY.PEEK[]":
                    d[b"BODY[]"] = data
            out[u] = d
        return out


def test_first_sync_is_silent_then_new_mail_notifies(app, acc):
    sent = []
    with mock.patch.object(app.notifier, "send", side_effect=lambda *a, **k: sent.append(a[0])), \
         mock.patch.object(app.ai, "enqueue") as enqueue:
        imap = FakeIMAP()
        app.syncer.sync_account(imap, acc)
        assert app.db.one("SELECT COUNT(*) n FROM messages WHERE folder='INBOX'")["n"] == 2
        assert sent == []  # eerste sync: geen meldingen
        imap.box["INBOX"][12] = raw("P <p12@x.be>", "drie", "<m12@x>")
        del imap.box["INBOX"][10]  # elders gearchiveerd
        app.syncer.sync_account(imap, acc)
    inbox = [r["subject"] for r in app.db.q("SELECT subject FROM messages WHERE folder='INBOX' ORDER BY uid")]
    assert inbox == ["twee", "drie"]
    assert app.db.one("SELECT COUNT(*) n FROM messages WHERE folder='_weg'")["n"] == 1
    # AI staat aan voor dit account: de melding volgt na de samenvatting (enqueue), niet rechtstreeks
    assert enqueue.call_count >= 1
