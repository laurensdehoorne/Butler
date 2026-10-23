from unittest import mock

from app import dossiers as D
from app import tone as T
from tests.conftest import H, put, raw
from tests.test_ai import claude_reply

PROFILE = "Nederlands, formeel (u) tegen instanties:\n- aanhef: Geachte\n- afsluiting: Met vriendelijke groeten, Laurens"


def sent(acc, uid, to, subject, body, days=2):
    return put(acc, "Sent Messages", uid, raw("Me <me@icloud.com>", subject, f"<s{uid}@me>", to=to, days=days, body=body))


def test_samples_skip_forwards_quotes_and_busy_threads(app, acc):
    sent(acc, 1, "kbc@kbc.be", "Fwd: polis", "Zie onder, doorgestuurd zonder eigen woorden van betekenis hier.")
    sent(acc, 2, "sarah@y.be", "Accepted: Overleg", "Accepted this meeting, see you there then everyone.")
    sent(acc, 3, "ann@y.be", "Kort", "ok")
    for i in range(5):  # één druk gesprek telt maar PER_RECIPIENT keer mee
        sent(acc, 10 + i, "sofie@arch.be", f"Re: offerte {i}", f"Dag Sofie,\n\nDat is goed voor mij, dinsdag past.\n\n> geciteerd {i}\n\nGroetjes")
    sent(acc, 20, "baloise@bal.be", "Schade ramen", "Geachte,\n\nGraag een update over het dossier.\n\nMet vriendelijke groeten,\nLaurens")
    got = T.samples(app.db)
    assert len(got) == T.PER_RECIPIENT + 1
    assert not any("geciteerd" in s or "Fwd" in s or "Accepted" in s for s in got)


def test_profile_is_learned_and_used_in_drafts(app, client, acc):
    for i in range(6):
        sent(acc, 10 + i, f"p{i}@y.be", f"Onderwerp {i}", f"Dag iemand {i},\n\nDat lijkt me een goed plan, laten we dat zo doen.\n\nGroetjes,\nLaurens")
    sent(acc, 30, "sofie@arch.be", "Re: offerte", "Dag Sofie,\n\nAkkoord met de offerte, mag ingepland worden.\n\nGroetjes,\nLaurens", days=20)
    tid, _ = put(acc, "INBOX", 40, raw("Sofie <sofie@arch.be>", "Nieuwe offerte", "<o1@arch>", body="Past dinsdag?"))
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    fake.messages.create.return_value = claude_reply(PROFILE)
    r = client.post("/api/tone/build", headers=H).json()["tone"]
    assert r["text"] == PROFILE and r["samples"] == 7
    assert "schrijfprofiel" in fake.messages.create.call_args.kwargs["system"]

    fake.messages.create.return_value = claude_reply("Dag Sofie,\n\nDinsdag past.\n\nGroetjes,\nLaurens")
    client.post(f"/api/threads/{tid}/draft", headers=H, json={"instruction": "dinsdag past"})
    call = fake.messages.create.call_args.kwargs
    assert PROFILE in call["system"]
    assert "Akkoord met de offerte" in call["messages"][0]["content"]  # eerdere mail aan Sofie als voorbeeld


def test_edited_profile_stays_and_is_not_stale(app, client):
    assert T.stale(app.db)
    client.put("/api/tone", headers=H, json={"text": "Altijd kort en informeel."})
    t = client.get("/api/tone", headers=H).json()["tone"]
    assert t["edited"] and t["text"] == "Altijd kort en informeel."
    assert not T.stale(app.db)
    assert client.post("/api/tone/build", headers=H).status_code == 400  # te weinig verstuurde mail


def test_quick_draft_saves_a_draft(app, client, acc):
    tid, _ = put(acc, "INBOX", 1, raw("Ann <ann@y.be>", "Etentje?", "<e1@y>", body="Zaterdag?"))
    fake = mock.MagicMock()
    fake.messages.create.return_value = claude_reply("Dag Ann,\n\nZaterdag lukt!\n\nGroetjes")
    app.ai._clients["claude"] = fake
    imap = mock.MagicMock()
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)):
        r = client.post(f"/api/threads/{tid}/quick-draft", headers=H, json={"instruction": "ja graag"}).json()
    assert r["folder"] == "Drafts" and "Zaterdag lukt" in r["draft"]
    assert imap.append.call_args[0][0] == "Drafts" and b"Zaterdag lukt" in imap.append.call_args[0][1]
    assert "ja graag" in fake.messages.create.call_args.kwargs["messages"][0]["content"]
    assert client.post(f"/api/threads/{tid}/quick-draft", headers=H, json={"instruction": " "}).status_code == 400


def test_dossier_overview(app, client, acc):
    a, _ = put(acc, "INBOX", 1, raw("KBC <k@kbc.be>", "Brievenbus", "<b1@x>", days=3))
    b, _ = put(acc, "INBOX", 2, raw("Air <a@af.fr>", "Vlucht", "<f1@x>", days=1))
    D.assign(app.db, a, ["Aanrijding brievenbus"])
    D.assign(app.db, b, ["Reis Parijs"])
    rows = client.get("/api/dossiers", headers=H).json()["dossiers"]
    assert [(r["name"], r["n"]) for r in rows] == [("Reis Parijs", 1), ("Aanrijding brievenbus", 1)]
