from unittest import mock

from app import dossiers as D
from tests.conftest import H, put, raw
from tests.test_ai import claude_reply


def triage_reply(dossier):
    d = "null" if dossier is None else f'"{dossier}"'
    return ('{"samenvatting":"x","prioriteit":"normaal","categorie":"reis","actie":"lezen","deadline":null,'
            f'"dossier":{d}}}')


def test_triage_puts_related_mails_in_one_dossier(app, client, acc):
    hotel, _ = put(acc, "INBOX", 1, raw("Hotel <info@innside.com>", "Reservatie Parijs", "<h1@x>"))
    flight, _ = put(acc, "INBOX", 2, raw("Air France <af@airfrance.fr>", "Boeking Parijs-Málaga", "<a1@x>"))
    loose, _ = put(acc, "INBOX", 3, raw("Ann <ann@y.be>", "Vraagje", "<q1@x>"))
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    fake.messages.create.return_value = claude_reply(triage_reply("Reis Parijs en Málaga"))
    app.ai.triage(hotel)
    # tweede mail: de AI ziet het bestaande dossier in de opdracht en kiest het (hoofdletters maken niet uit)
    fake.messages.create.return_value = claude_reply(triage_reply("reis parijs en málaga"))
    app.ai.triage(flight)
    assert '"Reis Parijs en Málaga"' in fake.messages.create.call_args.kwargs["system"]
    fake.messages.create.return_value = claude_reply(triage_reply(None))
    app.ai.triage(loose)

    t = client.get(f"/api/threads/{flight}", headers=H).json()
    assert t["dossier"]["name"] == "Reis Parijs en Málaga"
    assert [r["tid"] for r in t["dossier_threads"]] == [hotel]
    assert client.get(f"/api/threads/{loose}", headers=H).json()["dossier"] is None
    d = client.get(f"/api/dossiers/{t['dossier']['id']}", headers=H).json()
    assert {c["tid"] for c in d["threads"]} == {hotel, flight}

    # eruit gehaald: de AI zet de mail er niet opnieuw in
    client.post(f"/api/threads/{hotel}/dossier/remove", headers=H)
    fake.messages.create.return_value = claude_reply(triage_reply("Reis Parijs en Málaga"))
    app.ai.triage(hotel)
    assert client.get(f"/api/threads/{hotel}", headers=H).json()["dossier"] is None


def test_extract_and_catch_up(app, acc):
    assert D.extract('{"dossier": "Verbouwing keuken"}') == "Verbouwing keuken"
    assert D.extract('{"dossier": null}') is None and D.extract("geen json") is None
    tid, _ = put(acc, "INBOX", 1, raw("Ann <ann@y.be>", "Offerte", "<o1@x>"))
    app.db.x("UPDATE threads SET ai_count=1 WHERE tid=?", (tid,))  # al samengevat, maar nog zonder dossier
    app.syncer._triage_tried.clear()
    with mock.patch.object(app.ai, "enqueue") as enqueue:
        app.syncer.queue_triage(acc)
    enqueue.assert_called_once_with(tid)
