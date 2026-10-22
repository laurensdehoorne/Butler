"""Wat de AI kost: het tokenverbruik dat elk API-antwoord meldt, maal de prijslijst hieronder (uit CellarPeaks).

Elke aanroep (samenvatten of antwoord voorstellen) wordt één rij in ai_usage. Het zijn schattingen: verbruik × lijstprijs.
Wat de dienst echt aanrekent, staat in zijn eigen console.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("butler.costs")

# USD per miljoen tokens, per soort: input (niet uit cache), cached (uit de cache gelezen), cache_write en output.
# Een model met een datum achteraan ("gpt-6-luna-2026-09-01") telt als het model zonder datum.
# Aanpassen zonder code: <data>/prices.json, bv. {"openai": {"gpt-6-luna": {"input": 0.1, "output": 0.5}}}
PRICES = {
    # Anthropic, lijstprijzen oktober 2026 (zoals in CellarPeaks)
    "claude": {
        "claude-fable-5-1": {"input": 10.0, "cached": 0.25, "cache_write": 12.5, "output": 50.0},
        "claude-opus-5-5": {"input": 4.0, "cached": 0.20, "cache_write": 5.0, "output": 20.0},
        "claude-sonnet-5-5": {"input": 2.0, "cached": 0.10, "cache_write": 2.50, "output": 10.0},
        "claude-sonnet-5": {"input": 2.0, "cached": 0.20, "cache_write": 2.50, "output": 10.0},
        "claude-haiku-5-5": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "output": 0.50,
                             "long_context": {"above": 100_000, "input": 5.0, "output": 5.0}},
        "claude-haiku-4-5": {"input": 1.0, "cached": 0.10, "cache_write": 1.25, "output": 5.0},
    },
    # OpenAI, Standard tier, oktober 2026
    "openai": {
        "gpt-6-luna": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "output": 0.50,
                       "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
        "gpt-6.1-sol": {"input": 2.0, "cached": 0.10, "cache_write": 2.50, "output": 10.0,
                        "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
        "gpt-6-sol": {"input": 2.0, "cached": 0.20, "cache_write": 2.50, "output": 10.0,
                      "long_context": {"above": 272_000, "input": 2.0, "output": 1.5}},
    },
}
USD_TO_EUR = float(os.environ.get("USD_EUR", "0.87"))
_DATED = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")


def load_overrides(data_dir):
    path = os.path.join(data_dir, "prices.json")
    try:
        with open(path, encoding="utf-8") as f:
            extra = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning("prices.json niet leesbaar: %s", e)
        return
    for provider, models in extra.items():
        if provider in PRICES and isinstance(models, dict):
            for model, p in models.items():
                PRICES[provider][model] = {**PRICES[provider].get(model, {}), **p}


def price(provider, model):
    return PRICES.get(provider, {}).get(_DATED.sub("", model or ""))


@dataclass
class Call:
    provider: str
    model: str
    kind: str = ""
    input_tokens: int = 0          # de héle prompt (gewoon + uit cache + naar cache)
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0         # redeneertokens inbegrepen
    cost_usd: float = 0.0
    status: str = "estimated"      # estimated | partial | unknown
    missing: list = field(default_factory=list)


def _get(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _count(obj, name):
    v = _get(obj, name) if obj is not None else None
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _price_tokens(call):
    p = price(call.provider, call.model)
    if p is None:
        call.status, call.missing = "unknown", [*call.missing, "prijs van het model"]
        return call
    f_in = f_out = 1.0
    if (long := p.get("long_context")) and call.input_tokens > long["above"]:
        f_in, f_out = long["input"], long["output"]
    plain = max(call.input_tokens - call.cached_input_tokens - call.cache_write_tokens, 0)
    cost = plain * p["input"] * f_in + call.output_tokens * p["output"] * f_out
    for tokens, key in ((call.cached_input_tokens, "cached"), (call.cache_write_tokens, "cache_write")):
        if tokens and key in p:
            cost += tokens * p[key] * f_in
        elif tokens:
            call.missing.append(f"prijs van {key}")
    call.cost_usd = cost / 1_000_000
    if call.missing:
        call.status = "partial"
    return call


def from_anthropic(model, usage, kind=""):
    """Anthropic: input_tokens is enkel het niet-gecachte deel; cache-tokens komen erbovenop."""
    call = Call("claude", model, kind)
    plain, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    if plain is None or out is None:
        call.status, call.missing = "unknown", ["tokengebruik"]
        return call
    call.cache_write_tokens = _count(usage, "cache_creation_input_tokens") or 0
    call.cached_input_tokens = _count(usage, "cache_read_input_tokens") or 0
    call.input_tokens = plain + call.cache_write_tokens + call.cached_input_tokens
    call.output_tokens = out
    return _price_tokens(call)


def from_openai(model, usage, kind="", service_tier=None):
    """OpenAI Responses API: input_tokens is de hele prompt; cached_tokens en cache_write_tokens zijn delen ervan."""
    call = Call("openai", model, kind)
    inp, out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    if inp is None or out is None:
        call.status, call.missing = "unknown", ["tokengebruik"]
        return call
    call.input_tokens, call.output_tokens = inp, out
    details = _get(usage, "input_tokens_details")
    call.cached_input_tokens = _count(details, "cached_tokens") or 0
    call.cache_write_tokens = _count(details, "cache_write_tokens") or 0
    if service_tier not in (None, "", "default", "auto", "standard"):
        call.missing.append(f"service tier {service_tier}: andere prijzen")
    return _price_tokens(call)


class Meter:
    def __init__(self, db):
        self.db = db

    def record(self, call):
        self.db.x(
            """INSERT INTO ai_usage(ts, provider, model, kind, input_tokens, cached_tokens, cache_write_tokens,
               output_tokens, cost_usd, status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), call.provider, call.model, call.kind, call.input_tokens,
             call.cached_input_tokens, call.cache_write_tokens, call.output_tokens, call.cost_usd, call.status),
        )

    # Nooit laten falen: wat een aanroep kost, mag de aanroep zelf niet breken.
    def track_anthropic(self, requested, response, kind):
        try:
            self.record(from_anthropic(getattr(response, "model", None) or requested, getattr(response, "usage", None), kind))
        except Exception as e:
            log.warning("kost van Claude-aanroep: %r", e)

    def track_openai(self, requested, response, kind):
        try:
            self.record(from_openai(getattr(response, "model", None) or requested, getattr(response, "usage", None),
                                    kind, getattr(response, "service_tier", None)))
        except Exception as e:
            log.warning("kost van OpenAI-aanroep: %r", e)

    def summary(self, months=6):
        """Per maand en per dienst/model: aantal aanroepen, tokens en kost in euro (nieuwste maand eerst)."""
        rows = self.db.q(
            """SELECT substr(ts, 1, 7) AS month, provider, model, kind, COUNT(*) AS calls,
                      SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                      SUM(cost_usd) AS cost_usd, SUM(status != 'estimated') AS uncertain
               FROM ai_usage GROUP BY month, provider, model, kind ORDER BY month DESC, cost_usd DESC"""
        )
        out = {}
        for r in rows:
            m = out.setdefault(r["month"], {"month": r["month"], "calls": 0, "cost_eur": 0.0, "uncertain": 0, "lines": []})
            eur = (r["cost_usd"] or 0) * USD_TO_EUR
            m["calls"] += r["calls"]
            m["cost_eur"] += eur
            m["uncertain"] += r["uncertain"] or 0
            m["lines"].append({"provider": r["provider"], "model": r["model"], "kind": r["kind"], "calls": r["calls"],
                               "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                               "cost_eur": round(eur, 6)})
        months_list = list(out.values())[:months]
        for m in months_list:
            m["cost_eur"] = round(m["cost_eur"], 6)
        return {"months": months_list, "usd_to_eur": USD_TO_EUR}
