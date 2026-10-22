from types import SimpleNamespace as NS

from app import costs
from tests.conftest import H


def test_anthropic_cache_tokens_priced_separately():
    usage = NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=2000, cache_read_input_tokens=5000)
    call = costs.from_anthropic("claude-sonnet-5-5", usage)
    assert call.input_tokens == 8000 and call.status == "estimated"
    want = (1000 * 2.0 + 2000 * 2.5 + 5000 * 0.10 + 200 * 10.0) / 1e6
    assert abs(call.cost_usd - want) < 1e-12


def test_openai_dated_model_and_cached():
    usage = NS(input_tokens=10_000, output_tokens=500, input_tokens_details=NS(cached_tokens=4000, cache_write_tokens=0))
    call = costs.from_openai("gpt-6-luna-2026-09-01", usage)
    want = (6000 * 0.10 + 4000 * 0.01 + 500 * 0.50) / 1e6
    assert abs(call.cost_usd - want) < 1e-12


def test_unknown_model_is_unknown():
    call = costs.from_openai("gpt-9-mystery", NS(input_tokens=1, output_tokens=1, input_tokens_details=None))
    assert call.status == "unknown" and call.cost_usd == 0


def test_summary_endpoint(app, client):
    app.meter.record(costs.Call("claude", "claude-haiku-5-5", "triage", 1000, 0, 0, 100, cost_usd=0.00015))
    app.meter.record(costs.Call("openai", "gpt-6-luna", "draft", 2000, 0, 0, 300, cost_usd=0.00035))
    m = client.get("/api/costs", headers=H).json()["months"][0]
    assert m["calls"] == 2 and abs(m["cost_eur"] - 0.0005 * costs.USD_TO_EUR) < 1e-6
    assert {(l["provider"], l["kind"]) for l in m["lines"]} == {("claude", "triage"), ("openai", "draft")}
