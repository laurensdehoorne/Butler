from unittest import mock

import pytest

from app.accounts import Account
from tests.conftest import H, put, raw


@pytest.fixture
def gmail(app):
    """Een tweede account van dezelfde persoon."""
    acc = Account({"id": "gmail", "name": "Gmail", "provider": "gmail", "email": "me@gmail.com", "password": "x"})
    app.accounts["gmail"] = acc
    yield acc
    del app.accounts["gmail"]


def test_catch_up_triage_once_for_untriaged_inbox(app, acc):
    old, _ = put(acc, "INBOX", 1, raw("Piet <piet@x.be>", "Oude vraag", "<o1@x>", days=120))
    put(acc, "INBOX", 2, raw("Shop <news@shop.be>", "Promo", "<p1@x>", unsub="<https://shop.be/u>"))  # nieuwsbrief: geen AI
    done, _ = put(acc, "INBOX", 3, raw("Ann <ann@y.be>", "Al gedaan", "<d1@x>"))
    app.db.x("UPDATE threads SET ai_count=1 WHERE tid=?", (done,))
    app.syncer._triage_tried.clear()
    with mock.patch.object(app.ai, "enqueue") as enqueue:
        app.syncer.queue_triage(acc)
        app.syncer.queue_triage(acc)  # volgende sync: niet opnieuw (ook niet als het mislukte)
    assert [c.args[0] for c in enqueue.call_args_list] == [old]


def test_mail_between_own_accounts_waits_on_nobody(app, client, acc, gmail):
    # van Gmail naar je eigen iCloud: in iCloud telt het als van jou, en in "wacht op antwoord" wacht het op niemand
    tid, outgoing = put(acc, "INBOX", 1, raw("Me <me@gmail.com>", "Notitie aan mezelf", "<n1@x>", to="me@icloud.com", days=5))
    assert outgoing
    put(acc, "Sent Messages", 2, raw("Me <me@icloud.com>", "Ook naar mezelf", "<n2@x>", to="me@gmail.com", days=5))
    put(acc, "Sent Messages", 3, raw("Me <me@icloud.com>", "Vraag aan Ann", "<n3@x>", to="ann@y.be", days=5))
    fu = client.get("/api/followup", headers=H).json()
    assert fu["waiting_on_me"] == []
    assert [c["subject"] for c in fu["waiting_on_them"]] == ["Vraag aan Ann"]


@pytest.mark.parametrize("subject", ["Accepted: Overleg dinsdag", "Geaccepteerd: Etentje", "Declined: Call",
                                     "Voorlopig: Vergadering", "tentative: sync"])
def test_calendar_replies_wait_on_nobody(client, acc, subject):
    put(acc, "Sent Messages", 1, raw("Me <me@icloud.com>", subject, "<c1@me>", to="sarah@y.be", days=10))
    assert client.get("/api/followup", headers=H).json()["waiting_on_them"] == []


def test_normal_question_still_waits(client, acc):
    put(acc, "Sent Messages", 1, raw("Me <me@icloud.com>", "Accepteer je de offerte?", "<q1@me>", to="sarah@y.be", days=10))
    assert [c["subject"] for c in client.get("/api/followup", headers=H).json()["waiting_on_them"]] == ["Accepteer je de offerte?"]


def test_learns_recipients_that_never_answer(app, client, acc):
    # facturen naar Billit: drie keer verstuurd, nooit antwoord → daar wacht je niet op
    for i in range(3):
        put(acc, "Sent Messages", 10 + i, raw("Me <me@icloud.com>", "", f"<b{i}@me>", to="facturen@billit.be", days=10 + i))
    # Ann antwoordde wel eens: dan blijft een nieuwe vraag aan haar wachten
    for i in range(3):
        put(acc, "Sent Messages", 20 + i, raw("Me <me@icloud.com>", f"Vraag {i}", f"<a{i}@me>", to="ann@y.be", days=20 + i))
    put(acc, "INBOX", 30, raw("Ann <ann@y.be>", "Re: Vraag 2", "<r2@y>", irt="<a2@me>", refs="<a2@me>", days=21))
    # twee keer naar een nieuw adres is nog te weinig om iets te leren
    for i in range(2):
        put(acc, "Sent Messages", 40 + i, raw("Me <me@icloud.com>", f"Offerte {i}", f"<n{i}@me>", to="nieuw@z.be", days=5 + i))
    assert app.quiet_recipients() == {"facturen@billit.be"}
    subjects = {c["subject"] for c in client.get("/api/followup", headers=H).json()["waiting_on_them"]}
    assert subjects == {"Vraag 0", "Vraag 1", "Offerte 0", "Offerte 1"}
