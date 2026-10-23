from unittest import mock

from app import dossiers as D
from tests.conftest import H, put, raw
from tests.test_ai import claude_reply


def triage_reply(names):
    import json
    return ('{"samenvatting":"x","prioriteit":"normaal","categorie":"verzekering","actie":"lezen","deadline":null,'
            f'"dossiers":{json.dumps(names)}}}')


def test_overview_mail_belongs_to_each_case(app, client, acc):
    box, _ = put(acc, "INBOX", 1, raw("KBC <s@kbc.be>", "Aanrijding brievenbus", "<b1@x>", body="Uw schade ref. S006326085"))
    windows, _ = put(acc, "INBOX", 2, raw("Baloise <b@baloise.be>", "Schade aan ramen", "<r1@x>", body="Dossier 263801522"))
    overview, _ = put(acc, "INBOX", 3, raw("Makelaar <m@mak.be>", "Lopende schadedossiers", "<o1@x>"))
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    for tid, names in ((box, ["Aanrijding brievenbus"]), (windows, ["Schade aan ramen"]),
                       (overview, ["aanrijding brievenbus", "Schade aan ramen"])):  # hoofdletters maken niet uit
        fake.messages.create.return_value = claude_reply(triage_reply(names))
        app.ai.triage(tid)
    assert "verzamelnaam" in fake.messages.create.call_args.kwargs["system"]
    t = client.get(f"/api/threads/{overview}", headers=H).json()
    assert {d["name"] for d in t["dossiers"]} == {"Schade aan ramen", "Aanrijding brievenbus"}
    assert {g["dossier"]["name"]: [r["tid"] for r in g["threads"]] for g in t["dossier_threads"]} == {
        "Schade aan ramen": [windows], "Aanrijding brievenbus": [box]}

    client.post(f"/api/threads/{overview}/dossier/remove", headers=H)  # eruit: blijft eruit
    fake.messages.create.return_value = claude_reply(triage_reply(["Schade aan ramen"]))
    app.ai.triage(overview)
    assert client.get(f"/api/threads/{overview}", headers=H).json()["dossiers"] == []


def test_same_reference_links_archived_mail(app, client, acc):
    old, _ = put(acc, "Archive", 5, raw("Me <me@icloud.com>", "Schade brievenbus", "<a1@me>", to="kbc@kbc.be",
                                        body="Mijn aangifte, ref S006326085", days=8))
    other, _ = put(acc, "Archive", 6, raw("X <x@y.be>", "Iets anders", "<a2@x>", body="Factuur 2026-00999"))
    new, _ = put(acc, "INBOX", 7, raw("Makelaar <m@mak.be>", "Re: Schade [S00632608501]", "<n1@x>", body="Betaald."))
    D.assign(app.db, new, ["Aanrijding brievenbus"])
    did = D.of_thread(app.db, new)[0]["id"]
    linked = {c["tid"] for c in client.get(f"/api/dossiers/{did}", headers=H).json()["threads"]}
    assert linked == {new, old} and other not in linked


def test_common_numbers_do_not_link(app, acc):
    # een nummer dat in veel mails staat (handtekening) is geen referentie
    for i in range(12):
        put(acc, "Archive", 10 + i, raw("Kantoor <k@kantoor.be>", f"Mail {i}", f"<c{i}@x>", body="BTW BE0123456789"))
    tid, _ = put(acc, "INBOX", 30, raw("Kantoor <k@kantoor.be>", "Vraag", "<c30@x>", body="BTW BE0123456789"))
    D.assign(app.db, tid, ["Iets"])
    did = D.of_thread(app.db, tid)[0]["id"]
    assert app.db.one("SELECT COUNT(*) n FROM thread_dossiers WHERE dossier_id=?", (did,))["n"] == 1


def test_references_skip_dates_and_phones():
    import re
    found = [r for r in D.REF_RE.findall("tel 0476123456, datum 15-10-2026, ref S006326085 en 2026-00285, polis 43343168")]
    kept = [r for r in found if sum(c.isdigit() for c in r) >= 7 and not D.DATE_RE.match(r) and not (r.isdigit() and r.startswith("0"))]
    assert kept == ["S006326085", "2026-00285", "43343168"]
    assert D.extract('{"dossiers": ["A", "a", null, "B"]}') == ["A", "B"]
    assert D.extract('{"dossier": "Oud formaat"}') == ["Oud formaat"]
    assert re  # gebruikt


def test_catch_up_includes_recent_archived_personal_mail(app, acc):
    recent, _ = put(acc, "Archive", 1, raw("Ann <ann@y.be>", "Offerte", "<o1@x>", days=30))
    put(acc, "Archive", 2, raw("Oud <o@y.be>", "Heel oud", "<o2@x>", days=400))
    put(acc, "Archive", 3, raw("Shop <news@shop.be>", "Promo", "<o3@x>", unsub="<https://s.be/u>", days=5))
    app.syncer._triage_tried.clear()
    with mock.patch.object(app.ai, "enqueue") as enqueue:
        app.syncer.queue_triage(acc)
    assert [c.args[0] for c in enqueue.call_args_list] == [recent]


def test_legal_footer_numbers_do_not_link_and_ai_has_final_say(app, acc):
    footer = "Uw gegevens worden verwerkt volgens Verordening (EU) 2016/679 (AVG)."
    malaga, _ = put(acc, "INBOX", 1, raw("Air France <af@airfrance.fr>", "Boeking Parijs < > Malaga", "<m1@af>",
                                         body=f"Uw boeking.\n{footer}"))
    manchester, _ = put(acc, "INBOX", 2, raw("Air France <af@airfrance.fr>", "Boeking Parijs < > Manchester", "<m2@af>",
                                             body=f"Uw boeking.\n{footer}"))
    assert D.references(app.db, malaga) == []  # 2016/679 is een wet, geen referentie
    D.assign(app.db, malaga, ["Reis Parijs-Malaga"])
    assert D.of_thread(app.db, manchester) == []

    # een echte referentie koppelt wel, tot de AI de mail zelf bekijkt en anders beslist
    shared, _ = put(acc, "INBOX", 3, raw("KBC <k@kbc.be>", "Brievenbus", "<k1@kbc>", body="Schadenummer: S006326085"))
    other, _ = put(acc, "INBOX", 4, raw("KBC <k@kbc.be>", "Iets", "<k2@kbc>", body="Uw schade S006326085 is betaald"))
    D.assign(app.db, shared, ["Aanrijding brievenbus"])
    assert [d["name"] for d in D.of_thread(app.db, other)] == ["Aanrijding brievenbus"]
    D.assign(app.db, other, ["Schade aan ramen"])
    assert [d["name"] for d in D.of_thread(app.db, other)] == ["Schade aan ramen"]
