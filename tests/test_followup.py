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
    app.db.x("UPDATE threads SET ai_count=1, dossier_ai=1 WHERE tid=?", (done,))  # samengevat en dossier bepaald
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


def test_waiting_on_me_most_important_first(app, client, acc):
    from datetime import datetime, timedelta, timezone

    from app import moments as MO

    plain, _ = put(acc, "INBOX", 1, raw("A <a@x.be>", "Gewoon", "<g1@x>", days=9))
    urgent, _ = put(acc, "INBOX", 2, raw("B <b@x.be>", "Dringend", "<d1@x>", days=1))
    concert, _ = put(acc, "INBOX", 3, raw("C <c@x.be>", "Concert", "<c1@x>", days=1), flagged=True)
    flight, _ = put(acc, "INBOX", 4, raw("D <d@x.be>", "Vlucht", "<v1@x>", days=2), flagged=True)
    flagged_only, _ = put(acc, "INBOX", 5, raw("E <e@x.be>", "Gemarkeerd", "<e1@x>", days=8), flagged=True)
    app.db.x("UPDATE threads SET priority='hoog' WHERE tid=?", (urgent,))
    now = datetime.now(timezone.utc)
    for tid, days in ((flight, 3), (concert, 200)):
        MO.save(app.db, tid, [{"kind": "x", "title": "t", "detail": "", "at": (now + timedelta(days=days)).isoformat(),
                               "until": (now + timedelta(days=days, hours=2)).isoformat()}])
    order = [c["tid"] for c in client.get("/api/followup", headers=H).json()["waiting_on_me"]]
    # vlucht en concert hebben een moment dat nog komt: die wachten tot het zover is (Binnenkort, Nu nodig, agenda)
    assert order == [flagged_only, urgent, plain]


def test_flag_and_unflag_reach_the_server(app, client, acc):
    from app import moments as MO

    tid, _ = put(acc, "INBOX", 7, raw("TAP <noreply@flytap.com>", "Boeking", "<f1@x>"))
    imap = mock.MagicMock()
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)), mock.patch.object(app.ai, "enqueue_moments") as em:
        assert client.post(f"/api/threads/{tid}/flag", headers=H, json={"flagged": True}).json()["flagged"]
        imap.add_flags.assert_called_once_with([7], [b"\\Flagged"])
        em.assert_called_once_with(tid)
        assert client.get(f"/api/threads/{tid}", headers=H).json()["flagged"]

        MO.save(app.db, tid, [{"kind": "vlucht", "title": "Inchecken", "detail": "", "at": "2099-01-01T08:00:00+00:00",
                               "until": "2099-01-01T10:00:00+00:00"}])
        client.post(f"/api/threads/{tid}/flag", headers=H, json={"flagged": False})
        imap.remove_flags.assert_called_once_with([7], [b"\\Flagged"])
    assert not client.get(f"/api/threads/{tid}", headers=H).json()["flagged"]
    assert app.db.one("SELECT COUNT(*) n FROM moments")["n"] == 0


def test_archive_all_advertising_in_one_move(app, client, acc):
    for i in range(3):
        put(acc, "INBOX", 10 + i, raw(f"Shop{i} <news@shop{i}.be>", "Promo", f"<p{i}@x>", unsub="<https://shop.be/u>"))
    put(acc, "INBOX", 20, raw("Ann <ann@y.be>", "Vraag", "<q@x>"))
    imap = mock.MagicMock()
    imap.has_capability.return_value = True
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)):
        r = client.post("/api/senders/archive-kind", headers=H, json={"kind": "reclame"}).json()
    assert r["moved"] == 3
    imap.move.assert_called_once_with([10, 11, 12], "Archive")
    left = [m["subject"] for m in app.db.q("SELECT subject FROM messages WHERE folder='INBOX'")]
    assert left == ["Vraag"]
    assert client.post("/api/senders/archive-kind", headers=H, json={"kind": "persoonlijk"}).status_code == 400


def test_mail_with_a_coming_moment_waits_until_it_matters(app, client, acc):
    from datetime import datetime, timedelta, timezone

    from app import moments as MO

    booking, _ = put(acc, "INBOX", 1, raw("Air France <af@airfrance.fr>", "Booking Paris < > Manchester", "<b1@af>"), flagged=True)
    todo, _ = put(acc, "INBOX", 2, raw("Ann <ann@y.be>", "Formulier invullen", "<t1@y>"), flagged=True)  # vlag zonder datum
    appt, _ = put(acc, "INBOX", 3, raw("Dr. V <dr@v.be>", "Afspraak", "<a1@v>"))
    reply, _ = put(acc, "INBOX", 4, raw("Piet <p@x.be>", "Past 10 nov?", "<r1@x>"))
    now = datetime.now(timezone.utc)
    MO.save(app.db, booking, [{"kind": "vlucht", "title": "Vertrek", "detail": "", "at": (now + timedelta(days=200)).isoformat(),
                               "until": (now + timedelta(days=200, hours=3)).isoformat()}])
    far = (now + timedelta(days=30)).strftime("%Y-%m-%dT10:00")
    app.db.x("UPDATE threads SET deadline=?, action='actie' WHERE tid=?", (far, appt))
    app.db.x("UPDATE threads SET deadline=?, action='antwoorden' WHERE tid=?", (far, reply))  # antwoord gevraagd: blijft
    mine = {c["tid"] for c in client.get("/api/followup", headers=H).json()["waiting_on_me"]}
    assert mine == {todo, reply}
