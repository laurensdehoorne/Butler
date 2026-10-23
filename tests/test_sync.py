from datetime import datetime, timezone
from unittest import mock

from imapclient import FLAGGED, SEEN

from tests.conftest import raw


class FakeIMAP:
    """Genoeg van IMAPClient voor Syncer: select, search, fetch. Zoeken kent enkel FLAGGED; de rest geeft alles."""

    def __init__(self):
        self.box = {"INBOX": {10: raw("P <p10@x.be>", "een", "<m10@x>"), 11: raw("P <p11@x.be>", "twee", "<m11@x>")},
                    "Sent Messages": {}, "Archive": {}}
        self.flagged = set()  # (map, uid)
        self.cur = None

    def select_folder(self, folder, readonly=False):
        self.cur = folder
        return {b"UIDVALIDITY": 7}

    def search(self, criteria):
        if criteria == ["FLAGGED"]:
            return [u for u in self.box[self.cur] if (self.cur, u) in self.flagged]
        return list(self.box[self.cur])

    def fetch(self, uids, items):
        out = {}
        for u in uids:
            data, d = self.box[self.cur][u], {}
            for it in items:
                if it == "RFC822.SIZE":
                    d[b"RFC822.SIZE"] = len(data)
                elif it == "FLAGS":
                    d[b"FLAGS"] = ((SEEN,) if u == 10 else ()) + ((FLAGGED,) if (self.cur, u) in self.flagged else ())
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


def test_old_mail_spread_over_days(app, acc):
    acc.backlog.clear()
    imap = FakeIMAP()
    imap.box["Archive"] = {u: raw("A <a@x.be>", f"oud {u}", f"<o{u}@x>", days=100 + u) for u in range(1, 7)}
    imap.flagged = {("Archive", 1)}
    size = len(imap.box["Archive"][1])
    archived = lambda: {r["subject"] for r in app.db.q("SELECT subject FROM messages WHERE folder='Archive'")}  # noqa: E731
    with mock.patch.object(app.syncer, "daily_bytes", size * 3 + 10), \
         mock.patch.object(app.ai, "enqueue"), mock.patch.object(app.ai, "enqueue_moments"):
        app.syncer.sync_account(imap, acc)
        assert archived() == {"oud 1", "oud 6", "oud 5"}  # gemarkeerd eerst, dan van nieuw naar oud
        assert acc.public()["backlog"] == 3
        assert app.db.one("SELECT COUNT(*) n FROM messages WHERE folder='INBOX'")["n"] == 2  # inbox: nooit beperkt

        imap.box["Archive"][20] = raw("B <b@x.be>", "nieuw", "<n20@x>")
        app.syncer.sync_account(imap, acc)  # budget op, maar nieuwe mail komt toch binnen
        assert "nieuw" in archived() and acc.public()["backlog"] == 3

        with mock.patch.object(app.syncer, "_budget", return_value=("morgen", 0)):
            app.syncer.sync_account(imap, acc)
    assert archived() == {"oud 1", "oud 2", "oud 3", "oud 4", "oud 5", "oud 6", "nieuw"}
    assert acc.public()["backlog"] == 0
