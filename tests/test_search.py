from unittest import mock

from app import search as S
from tests.conftest import H, put, raw
from tests.test_ai import claude_reply


def subjects(client, q):
    return [t["subject"] for t in client.get("/api/inbox", headers=H, params={"q": q}).json()["threads"]]


def test_prefix_accents_and_any_order(app, client, acc):
    put(acc, "Archive", 1, raw("Goldcar <g@goldcar.es>", "Car hire Málaga confirmation", "<c@x>", days=40))
    put(acc, "INBOX", 2, raw("Proximus <f@proximus.be>", "Uw factuur oktober", "<f@x>", body="Bedrag 45 EUR"))
    put(acc, "INBOX", 3, raw("Ann <ann@y.be>", "Etentje", "<e@y>", body="Zaterdag in Malaga?"))
    assert subjects(client, "malaga car") == ["Car hire Málaga confirmation"]  # accent, volgorde, archief
    assert subjects(client, "factu proximus") == ["Uw factuur oktober"]  # begin van een woord
    assert subjects(client, "van:goldcar") == ["Car hire Málaga confirmation"]
    assert set(subjects(client, "malaga")) == {"Car hire Málaga confirmation", "Etentje"}
    # niets met alle woorden: wat het meest overeenkomt
    assert subjects(client, "malaga treinticket")[0] in ("Car hire Málaga confirmation", "Etentje")


def test_filters(app, acc):
    words, f = S.parse('vlucht van:"Air France" na:2026-03 voor:2027 pdf')
    assert words == ["vlucht"] and f == {"van": "Air France", "na": "2026-03", "voor": "2027", "pdf": True}
    assert S.is_question("Wanneer vertrekt mijn vlucht naar Lissabon?") and not S.is_question("vlucht lissabon")


def test_ask_butler_answers_with_sources(app, client, acc):
    put(acc, "INBOX", 1, raw("TAP <b@flytap.com>", "Boarding pass TP643", "<b@x>", body="Flight TP643 Brussels-Lisbon departs 07:10"))
    put(acc, "INBOX", 2, raw("Ann <ann@y.be>", "Etentje", "<e@y>"))
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    fake.messages.create.side_effect = [
        claude_reply('{"woorden": ["vlucht", "flight", "lisbon", "lissabon"], "van": "", "aan": "", "na": "", "voor": "", "bijlage": false}'),
        claude_reply("Je vlucht TP643 naar Lissabon vertrekt om 07:10 [1]."),
    ]
    r = client.get("/api/ask", headers=H, params={"q": "Wanneer vertrekt mijn vlucht naar Lissabon?"}).json()
    assert r["answer"].startswith("Je vlucht TP643") and r["threads"][0]["subject"] == "Boarding pass TP643"
    assert r["threads"][0]["ref"] == 1
    assert "07:10" in fake.messages.create.call_args.kwargs["messages"][0]["content"]
