import json
import os
import logging
import queue
import re
import threading
from datetime import date, datetime

from . import dossiers, fatigue, moments, tone

from .db import sender_rule, thread_messages
from .mailparse import strip_quotes

log = logging.getLogger("butler.ai")

PRIORITIES = {"hoog", "normaal", "laag"}
ACTIONS = {"antwoorden", "actie", "lezen", "wachten", "geen"}

TRIAGE_SYSTEM = """Je bent de e-mailassistent van {owner}. Vandaag is {today}.
Je krijgt een e-mailthread. Antwoord met uitsluitend één JSON-object, zonder uitleg of codeblok, met deze sleutels:
- "samenvatting": 1 à 3 korte zinnen in het Nederlands: waarover gaat het en wat wordt er (eventueel) van {name} verwacht.
- "prioriteit": "hoog" (dringend of belangrijk, vraagt snel een reactie), "normaal", of "laag" (informatief, reclame, automatische meldingen).
- "categorie": één kort woord, bv. praktijk, patiënt, verwijzer, leverancier, factuur, reis, familie, bestelling, nieuwsbrief, systeem, overig.
- "actie": "antwoorden" (er wordt een antwoord van {name} verwacht), "actie" (iets doen, maar niet per se antwoorden), "lezen", "wachten" ({name} wacht op de ander) of "geen".
- "deadline": een datum "JJJJ-MM-DD" als er een duidelijke deadline of afspraakdatum is, met het uur erbij ("JJJJ-MM-DDTUU:MM", lokale tijd) als dat in de mail staat (bv. een afspraak); anders null.
- "wacht_op_antwoord": true als het laatste bericht van {name} zelf is en {name} daarop nog een antwoord of reactie van de ander verwacht (een vraag, een verzoek, een offerteaanvraag, iets dat nog bevestigd of geregeld moet worden). false als dat laatste bericht een bedanking, bevestiging, afzegging, test of louter een mededeling is, of als de zaak rond is; ook false als het laatste bericht niet van {name} is.
- "gebeurtenis": true als de mail (of een bijlage) een boeking, reservatie, ticket of afspraak met een datum bevestigt: een hotel, vlucht, trein, huurwagen, concert, restaurant, afspraak bij iemand. Anders false.
Berichten gemarkeerd met [JIJ] zijn door {name} zelf verstuurd."""

DRAFT_SYSTEM = """Je schrijft een e-mailantwoord namens {owner}. Vandaag is {today}.
Schrijf in de taal van het laatste ontvangen bericht (meestal Nederlands) en kies een toon die past bij de afzender: zakelijk en beknopt voor professionele contacten, losser voor familie en vrienden.
Verzin geen feiten, afspraken, bedragen of data. Waar informatie ontbreekt, zet je [aanvullen].
Geef enkel de tekst van de mail (aanhef, inhoud, afsluiting), zonder onderwerpregel en zonder het geciteerde origineel.
Sluit af met:
{signature}"""


def _bool(v):
    """true/false uit een AI-antwoord als 1/0; ontbreekt het, dan None (onbekend)."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "ja", "nee"):
        return int(v.strip().lower() in ("true", "ja"))
    return None


def _fmt_people(lst):
    return ", ".join((f"{n} <{a}>" if n and n != a else a) for n, a in lst)


def thread_text(msgs, max_msgs=8, per_msg=3500):
    parts = []
    for m in msgs[-max_msgs:]:
        to = _fmt_people(json.loads(m["to_addrs"] or "[]"))
        who = "[JIJ] " if m["outgoing"] else ""
        body = strip_quotes(m["body"] or "")[:per_msg]
        # wat in de pdf-bijlagen staat (een hotelbevestiging zonder tekst in de mail zelf)
        att = (m.get("att_text") or "")[:per_msg]
        parts.append(
            f"--- {who}Van: {m['from_name']} <{m['from_addr']}> | Aan: {to} | Datum: {m['date']}\n"
            f"Onderwerp: {m['subject']}\n\n{body}" + (f"\n\nTekst uit de bijlagen:\n{att}" if att else "")
        )
    return "\n\n".join(parts)


PROVIDERS = {"claude": "Claude", "openai": "ChatGPT"}
DEFAULT_MODELS = {
    "claude": {"triage": "claude-haiku-5-5", "draft": "claude-sonnet-5-5"},
    "openai": {"triage": "gpt-6-luna", "draft": "gpt-6-luna"},  # zoals CellarPeaks; gpt-6.1-sol is het sterkere model
}


class AIError(RuntimeError):
    """Fout van de AI-dienst, in gewone taal voor de interface."""


def _openai_error(e):
    import openai

    if isinstance(e, openai.AuthenticationError):
        return AIError("De OpenAI-sleutel is ongeldig of ingetrokken. Pas hem aan in config.yaml.")
    if isinstance(e, openai.RateLimitError):
        if getattr(e, "code", None) == "insufficient_quota":
            return AIError("Je OpenAI-krediet is op. Vul het aan op platform.openai.com (Billing).")
        return AIError("Te veel verzoeken tegelijk bij OpenAI. Wacht even en probeer opnieuw.")
    if isinstance(e, openai.NotFoundError):
        return AIError("Dit OpenAI-model bestaat niet of is niet beschikbaar met je sleutel. Kies een ander in config.yaml.")
    if isinstance(e, openai.APIStatusError):
        if e.status_code >= 500:
            return AIError("OpenAI is tijdelijk overbelast. Probeer zo dadelijk opnieuw.")
        msg = e.body.get("message") if isinstance(e.body, dict) else None
        return AIError(f"Fout {e.status_code} van OpenAI: {msg}" if msg else f"Fout {e.status_code} van OpenAI.")
    if isinstance(e, openai.APITimeoutError):
        return AIError("OpenAI antwoordde niet op tijd. Probeer opnieuw.")
    if isinstance(e, openai.APIConnectionError):
        return AIError("De server kan OpenAI niet bereiken. Controleer de internetverbinding.")
    return AIError(f"Onverwachte fout bij OpenAI: {e}")


def _claude_error(e):
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return AIError("De Claude-sleutel is ongeldig of ingetrokken. Pas hem aan in config.yaml.")
    if isinstance(e, anthropic.RateLimitError):
        return AIError("Te veel verzoeken tegelijk bij Claude. Wacht even en probeer opnieuw.")
    if isinstance(e, anthropic.NotFoundError):
        return AIError("Dit Claude-model bestaat niet of is niet beschikbaar met je sleutel. Kies een ander in config.yaml.")
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code >= 500 or e.status_code == 529:
            return AIError("Claude is tijdelijk overbelast. Probeer zo dadelijk opnieuw.")
        return AIError(f"Fout {e.status_code} van Claude: {getattr(e, 'message', e)}")
    if isinstance(e, anthropic.APITimeoutError):
        return AIError("Claude antwoordde niet op tijd. Probeer opnieuw.")
    if isinstance(e, anthropic.APIConnectionError):
        return AIError("De server kan Claude niet bereiken. Controleer de internetverbinding.")
    return AIError(f"Onverwachte fout bij Claude: {e}")


class AIWorker:
    def __init__(self, db, cfg, accounts, notifier, meter=None):
        self.db = db
        self.meter = meter
        self.cfg = cfg
        self.accounts = accounts
        self.notifier = notifier
        self.keys = {
            "claude": cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "",
            "openai": cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "",
        }
        self.keys = {k: v for k, v in self.keys.items() if v and not v.endswith("...")}
        self.switched_on = bool(cfg.get("enabled", True))
        self._clients = {}
        self._no_reasoning = set()  # OpenAI-modellen die de reasoning-instelling weigeren
        self.q = queue.Queue()
        self.pending = set()
        self.after_triage = []  # haken na een samenvatting (autopilot.py): fn(tid)
        self.lock = threading.Lock()

    # --- dienst kiezen --------------------------------------------------
    def available(self):
        return [p for p in PROVIDERS if p in self.keys]

    @property
    def provider(self):
        row = self.db.one("SELECT value FROM settings WHERE key='ai_provider'")
        chosen = (row["value"] if row else None) or self.cfg.get("provider") or "claude"
        avail = self.available()
        return chosen if chosen in avail else (avail[0] if avail else chosen)

    def set_provider(self, provider):
        if provider not in self.available():
            raise AIError(f"Geen sleutel voor {PROVIDERS.get(provider, provider)} in config.yaml.")
        self.db.x("INSERT OR REPLACE INTO settings(key, value) VALUES('ai_provider', ?)", (provider,))

    def model(self, kind, provider=None):
        provider = provider or self.provider
        prefix = "" if provider == "claude" else "openai_"
        return self.cfg.get(f"{prefix}{kind}_model") or DEFAULT_MODELS[provider][kind]

    @property
    def enabled(self):
        return self.switched_on and self.provider in self.keys

    def active_for(self, account_id):
        acc = self.accounts.get(account_id)
        return self.enabled and acc is not None and acc.ai

    def _fmt(self, tpl):
        return tpl.format(
            owner=self.cfg.get("owner", "de gebruiker"),
            name=self.cfg.get("name", "de gebruiker"),
            today=date.today().isoformat(),
            signature=self.cfg.get("signature", "").strip(),
        )

    # --- aanroepen ------------------------------------------------------
    def _client(self, provider):
        if provider not in self._clients:
            if provider == "claude":
                import anthropic

                self._clients[provider] = anthropic.Anthropic(api_key=self.keys["claude"], timeout=120.0, max_retries=1)
            else:
                import openai

                self._clients[provider] = openai.OpenAI(api_key=self.keys["openai"], timeout=120.0, max_retries=1)
        return self._clients[provider]

    def _call(self, kind, system, user, max_tokens):
        provider = self.provider
        name = self.model(kind, provider)
        if provider == "claude":
            try:
                r = self._client("claude").messages.create(
                    model=name, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                )
            except Exception as e:
                raise _claude_error(e) from e
            if self.meter:
                self.meter.track_anthropic(name, r, kind)
            return "".join(getattr(b, "text", "") for b in r.content).strip()
        return self._call_openai(name, system, user, max_tokens, kind)

    def _call_openai(self, name, system, user, max_tokens, kind=""):
        """Responses API, zoals CellarPeaks. store=False: OpenAI bewaart het antwoord niet als 'response' aan zijn kant."""
        import openai

        kwargs = {"model": name, "instructions": system, "input": user, "store": False,
                  # redeneertokens tellen mee in max_output_tokens: ruimte laten
                  "max_output_tokens": max_tokens * 4, "reasoning": {"effort": "low"},
                  "prompt_cache_key": "butler"}
        while True:
            if name in self._no_reasoning:
                kwargs.pop("reasoning", None)
            try:
                r = self._client("openai").responses.create(**kwargs)
                if self.meter:
                    self.meter.track_openai(name, r, kind)
                break
            except openai.BadRequestError as e:
                if "reasoning" in str(e.message).lower() and "reasoning" in kwargs:
                    self._no_reasoning.add(name)
                    continue
                if "prompt_cache_key" in str(e.message).lower() and "prompt_cache_key" in kwargs:
                    kwargs.pop("prompt_cache_key")
                    continue
                raise _openai_error(e) from e
            except Exception as e:
                raise _openai_error(e) from e
        for item in getattr(r, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise AIError("ChatGPT weigerde dit verzoek.")
        text = (getattr(r, "output_text", "") or "").strip()
        if not text:
            details = getattr(r, "incomplete_details", None)
            if getattr(details, "reason", None) == "max_output_tokens":
                raise AIError("Het antwoord van ChatGPT werd afgebroken (te lang). Probeer opnieuw.")
            raise AIError("Geen antwoord ontvangen van ChatGPT. Probeer opnieuw.")
        return text

    # --- wachtrij -------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="ai").start()

    def enqueue(self, tid, notify=False):
        self._put(("triage", tid, notify))

    def enqueue_moments(self, tid):
        self._put(("moments", tid, False))

    def enqueue_tone(self):
        self._put(("tone", 0, False))

    def _put(self, job):
        with self.lock:
            if job in self.pending:
                return
            self.pending.add(job)
        self.q.put(job)

    def _run(self):
        while True:
            job = self.q.get()
            kind, tid, notify = job
            with self.lock:
                self.pending.discard(job)
            try:
                if kind == "moments":
                    self.moments(tid)
                    continue
                if kind == "tone":
                    tone.build(self, self.db)
                    continue
                res = self.triage(tid)
                if notify:
                    self._maybe_notify(tid, res)
                for hook in self.after_triage:
                    try:
                        hook(tid)
                    except Exception:
                        log.exception("automatische taak mislukt voor thread %s", tid)
            except Exception:
                log.exception("%s mislukt voor thread %s", kind, tid)

    def _maybe_notify(self, tid, res):
        t, msgs = thread_messages(self.db, tid)
        if not t or not msgs:
            return
        prio = (res or {}).get("priority") or "normaal"
        last = [m for m in msgs if not m["outgoing"]][-1:] or msgs[-1:]
        last = last[0]
        vip = fatigue.priority(self.db, t["account"], last["from_addr"])  # prioritaire afzender: altijd
        if not vip and (not self.notifier.wants(prio) or fatigue.muted(self.db, t["account"], last["from_addr"])):
            return
        if vip and self.notifier.mode == "off":
            return
        acc = self.accounts.get(t["account"])
        # wie en waarover: de afzender als titel, het account enkel als er meer dan één is
        who = last["from_name"] or last["from_addr"]
        title = f"{who} ({acc.name})" if acc and len(self.accounts) > 1 else who
        text = last["subject"] or "(geen onderwerp)"
        if res and res.get("summary"):
            text += "\n" + res["summary"]
        fatigue.record(self.db, t["account"], last["from_addr"], tid)
        self.notifier.send(title, text, tid=tid, high=(prio == "hoog" or vip), force=vip,
                           extra={"account": t["account"], "from": last["from_addr"], "subject": last["subject"],
                                  "priority": prio, "summary": (res or {}).get("summary")})

    # --- taken ----------------------------------------------------------
    def triage(self, tid):
        t, msgs = thread_messages(self.db, tid)
        if not t or not msgs:
            return None
        if all(m["bulk"] for m in msgs) and not (t.get("context") or "").strip():
            res = {"summary": msgs[-1]["snippet"][:200], "priority": "laag", "category": msgs[-1].get("kind") or "reclame",
                   "action": "geen", "deadline": None, "awaits": 0, "event": 0}
        elif not self.active_for(t["account"]):
            return None
        else:
            raw = self._call(
                "triage",
                self._fmt(TRIAGE_SYSTEM) + "\n" + dossiers.prompt(self.db),
                thread_text(msgs) + self.context(t, msgs),
                600,
            )
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
            prio = str(data.get("prioriteit", "normaal")).lower()
            act = str(data.get("actie", "lezen")).lower()
            dl = data.get("deadline")
            res = {
                "summary": str(data.get("samenvatting", "")).strip()[:600],
                "priority": prio if prio in PRIORITIES else "normaal",
                "category": str(data.get("categorie", "overig")).lower()[:30],
                "action": act if act in ACTIONS else "lezen",
                "deadline": dl if isinstance(dl, str) and re.match(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$", dl) else None,
                "awaits": _bool(data.get("wacht_op_antwoord")),
                "event": _bool(data.get("gebeurtenis")) or 0,
            }
            dossiers.assign(self.db, tid, dossiers.extract(raw))
        self.db.x(
            "UPDATE threads SET summary=?, priority=?, category=?, action=?, deadline=?, awaits=?, has_event=?, ai_count=? WHERE tid=?",
            (res["summary"], res["priority"], res["category"], res["action"], res["deadline"], res["awaits"], res["event"], len(msgs), tid),
        )
        if res["event"] and not t.get("moments_ai"):  # een boeking of afspraak: meteen de momenten zoeken (agenda)
            self.enqueue_moments(tid)
        return res

    def context(self, t, msgs):
        """Wat je Butler zelf vertelde over deze mail en over de afzender; gaat mee met elke AI-vraag."""
        name = self.cfg.get("name", "de gebruiker")
        parts = []
        if (t.get("context") or "").strip():
            parts.append(f"--- Context van {name} bij deze mail (gaat voor op wat de mail lijkt te zeggen):\n{t['context'].strip()}")
        incoming = [m for m in msgs if not m["outgoing"]]
        if incoming:
            rule = sender_rule(self.db, t["account"], incoming[-1]["from_addr"]) or {}
            if (rule.get("context") or "").strip():
                parts.append(f"--- Wat {name} weet over {incoming[-1]['from_addr']}:\n{rule['context'].strip()}")
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def moments(self, tid):
        """Momenten waarop een thread nodig is: gemarkeerde mail, en mail die een boeking of afspraak bevestigt."""
        t, msgs = thread_messages(self.db, tid)
        if not t or not msgs or not self.active_for(t["account"]):
            return []
        tz = self.notifier.tz
        system = moments.SYSTEM.format(owner=self.cfg.get("owner", "de gebruiker"), name=self.cfg.get("name", "de gebruiker"),
                                       now=datetime.now(tz).strftime("%Y-%m-%d %H:%M"), tz=tz.key)
        system += '\nVoeg aan het JSON-object ook deze sleutel toe:\n' + dossiers.prompt(self.db)
        raw = self._call("triage", system, thread_text(msgs, per_msg=6000) + self.context(t, msgs), 900)
        items = moments.parse(raw, tz)
        dossiers.assign(self.db, tid, dossiers.extract(raw))
        moments.save(self.db, tid, items)
        self.db.x("UPDATE threads SET moments_ai=? WHERE tid=?", (len(msgs), tid))
        return items

    def draft(self, tid, instruction=""):
        if not self.enabled:
            raise AIError("AI staat uit, of er is geen sleutel voor de gekozen dienst in config.yaml")
        t, msgs = thread_messages(self.db, tid)
        if not t:
            raise RuntimeError("thread niet gevonden")
        if not self.active_for(t["account"]):
            raise RuntimeError("AI staat uit voor deze mailbox")
        name = self.cfg.get("name", "de gebruiker")
        system = self._fmt(DRAFT_SYSTEM)
        if not self.cfg.get("signature", "").strip():
            system = system.replace("Sluit af met:\n", "Sluit af zoals in het schrijfprofiel.").rstrip()
        profile = tone.get(self.db)
        if profile and profile.get("text"):  # zo schrijft de gebruiker zelf, geleerd uit verstuurde mail
            system += f"\n\nSchrijfstijl van {name} (volg die: taal, u of je, aanhef, opbouw, afsluiting):\n{profile['text']}"
        user = thread_text(msgs) + self.context(t, msgs)
        incoming = [m for m in msgs if not m["outgoing"]]
        examples = tone.examples_for(self.db, t["account"], incoming[-1]["from_addr"] if incoming else "")
        if examples:
            user += (f"\n\n--- Eerdere mails van {name} aan deze persoon (neem dezelfde aanspreking en toon over, niet de inhoud):\n"
                     + "\n---\n".join(examples))
        if instruction.strip():
            user += f"\n\n--- Instructie van {name} voor het antwoord:\n{instruction.strip()}"
        else:
            user += "\n\n--- Schrijf een gepast antwoord op het laatste bericht."
        return self._call("draft", system, user, 1500)
