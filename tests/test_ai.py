from types import SimpleNamespace as NS
from unittest import mock

import httpx
import openai

from tests.conftest import H, put, raw

TRIAGE = '{"samenvatting":"Piet vraagt dinsdag.","prioriteit":"hoog","categorie":"praktijk","actie":"antwoorden","deadline":"2026-10-27"}'


def thread(acc):
    tid, _ = put(acc, "INBOX", 1, raw("Piet <piet@x.be>", "Afspraak", "<a@x>", body="Kan dinsdag?"))
    return tid


def claude_reply(text):
    return NS(content=[NS(type="text", text=text)], model="claude-haiku-5-5",
              usage=NS(input_tokens=1200, output_tokens=80, cache_creation_input_tokens=0, cache_read_input_tokens=0))


def bad_request(msg):
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.BadRequestError(msg, response=httpx.Response(400, request=req), body={"message": msg})


def test_providers_listed(client):
    d = client.get("/api/accounts", headers=H).json()
    assert d["ai_provider"] == "claude"
    assert [(p["id"], p["triage_model"], p["draft_model"]) for p in d["ai_providers"]] == [
        ("claude", "claude-haiku-5-5", "claude-sonnet-5-5"), ("openai", "gpt-6-luna", "gpt-6-luna")]


def test_claude_triage_and_cost(app, client, acc):
    tid = thread(acc)
    fake = mock.MagicMock()
    fake.messages.create.return_value = claude_reply(TRIAGE)
    app.ai._clients["claude"] = fake
    t = client.post(f"/api/threads/{tid}/summarize", headers=H).json()
    assert (t["summary"], t["priority"], t["action"], t["deadline"]) == ("Piet vraagt dinsdag.", "hoog", "antwoorden", "2026-10-27")
    row = app.db.one("SELECT * FROM ai_usage")
    assert row["provider"] == "claude" and row["kind"] == "triage"
    assert abs(row["cost_usd"] - (1200 * 0.10 + 80 * 0.50) / 1e6) < 1e-12


def test_openai_draft_with_reasoning_fallback(app, client, acc):
    tid = thread(acc)
    assert client.post("/api/settings/ai", headers=H, json={"provider": "openai"}).json()["ai_provider"] == "openai"
    calls = []

    def create(**kw):
        calls.append(kw)
        if "reasoning" in kw:
            raise bad_request("Unsupported parameter: 'reasoning'")
        return NS(output=[], output_text="Dag Piet,\n\nDinsdag past.", incomplete_details=None, model="gpt-6-luna",
                  usage=NS(input_tokens=1000, output_tokens=100, input_tokens_details=NS(cached_tokens=0, cache_write_tokens=0)))

    fake = mock.MagicMock()
    fake.responses.create.side_effect = create
    app.ai._clients["openai"] = fake
    r = client.post(f"/api/threads/{tid}/draft", headers=H, json={"instruction": "ja"}).json()
    assert r["draft"].startswith("Dag Piet")
    assert len(calls) == 2 and calls[-1]["store"] is False and "reasoning" not in calls[-1]
    assert app.db.one("SELECT provider, kind FROM ai_usage")[:] == ("openai", "draft")


def test_openai_quota_error_is_readable(app, client, acc):
    tid = thread(acc)
    client.post("/api/settings/ai", headers=H, json={"provider": "openai"})
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    fake = mock.MagicMock()
    fake.responses.create.side_effect = openai.RateLimitError("quota", response=httpx.Response(429, request=req),
                                                              body={"code": "insufficient_quota"})
    app.ai._clients["openai"] = fake
    r = client.post(f"/api/threads/{tid}/draft", headers=H, json={})
    assert r.status_code == 502 and "krediet is op" in r.json()["detail"]


def test_unknown_provider_refused(client):
    assert client.post("/api/settings/ai", headers=H, json={"provider": "gemini"}).status_code == 400


def test_newsletters_never_sent_to_ai(app, acc):
    tid, _ = put(acc, "INBOX", 9, raw("News <newsletter@shop.be>", "Promo", "<n@s>", unsub="<https://s/u>"))
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    res = app.ai.triage(tid)
    assert res["priority"] == "laag" and not fake.messages.create.called


def test_context_reaches_every_prompt(app, client, acc):
    tid, _ = put(acc, "INBOX", 1, raw("Billit <factuur@billit.be>", "Factuur 2026-118", "<f@x>", unsub="<https://b.be/u>"))
    fake = mock.MagicMock()
    fake.messages.create.return_value = claude_reply(TRIAGE)
    app.ai._clients["claude"] = fake
    t = client.post(f"/api/threads/{tid}/context", headers=H, json={"text": "Al betaald via domiciliëring."}).json()
    assert t["context"] == "Al betaald via domiciliëring." and t["summary"] == "Piet vraagt dinsdag."  # meteen opnieuw samengevat
    prompt = fake.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Al betaald via domiciliëring." in prompt  # ook een notificatie gaat mét context naar de AI

    t = client.post(f"/api/threads/{tid}/context", headers=H, json={"text": "Mijn boekhoudpakket", "sender": True}).json()
    assert t["sender"]["context"] == "Mijn boekhoudpakket" and t["context"] == "Al betaald via domiciliëring."
    prompt = fake.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Wat Jan weet over factuur@billit.be:\nMijn boekhoudpakket" in prompt

    client.post(f"/api/threads/{tid}/context", headers=H, json={"text": ""})
    assert client.get(f"/api/threads/{tid}", headers=H).json()["context"] == ""
