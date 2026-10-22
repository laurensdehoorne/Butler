import json
import logging
import queue
import re
import threading
from datetime import date

from .db import thread_messages
from .mailparse import strip_quotes

log = logging.getLogger("postvak.ai")

PRIORITIES = {"hoog", "normaal", "laag"}
ACTIONS = {"antwoorden", "actie", "lezen", "wachten", "geen"}

TRIAGE_SYSTEM = """Je bent de e-mailassistent van {owner}. Vandaag is {today}.
Je krijgt een e-mailthread. Antwoord met uitsluitend één JSON-object, zonder uitleg of codeblok, met deze sleutels:
- "samenvatting": 1 à 3 korte zinnen in het Nederlands: waarover gaat het en wat wordt er (eventueel) van {name} verwacht.
- "prioriteit": "hoog" (dringend of belangrijk, vraagt snel een reactie), "normaal", of "laag" (informatief, reclame, automatische meldingen).
- "categorie": één kort woord, bv. praktijk, patiënt, verwijzer, leverancier, factuur, reis, familie, bestelling, nieuwsbrief, systeem, overig.
- "actie": "antwoorden" (er wordt een antwoord van {name} verwacht), "actie" (iets doen, maar niet per se antwoorden), "lezen", "wachten" ({name} wacht op de ander) of "geen".
- "deadline": een datum "JJJJ-MM-DD" als er een duidelijke deadline of afspraakdatum is, anders null.
Berichten gemarkeerd met [JIJ] zijn door {name} zelf verstuurd."""

DRAFT_SYSTEM = """Je schrijft een e-mailantwoord namens {owner}. Vandaag is {today}.
Schrijf in de taal van het laatste ontvangen bericht (meestal Nederlands) en kies een toon die past bij de afzender: zakelijk en beknopt voor professionele contacten, losser voor familie en vrienden.
Verzin geen feiten, afspraken, bedragen of data. Waar informatie ontbreekt, zet je [aanvullen].
Geef enkel de tekst van de mail (aanhef, inhoud, afsluiting), zonder onderwerpregel en zonder het geciteerde origineel.
Sluit af met:
{signature}"""


def _fmt_people(lst):
    return ", ".join((f"{n} <{a}>" if n and n != a else a) for n, a in lst)


def thread_text(msgs, max_msgs=8, per_msg=3500):
    parts = []
    for m in msgs[-max_msgs:]:
        to = _fmt_people(json.loads(m["to_addrs"] or "[]"))
        who = "[JIJ] " if m["outgoing"] else ""
        body = strip_quotes(m["body"] or "")[:per_msg]
        parts.append(
            f"--- {who}Van: {m['from_name']} <{m['from_addr']}> | Aan: {to} | Datum: {m['date']}\n"
            f"Onderwerp: {m['subject']}\n\n{body}"
        )
    return "\n\n".join(parts)


class AIWorker:
    def __init__(self, db, cfg, accounts, notifier):
        self.db = db
        self.cfg = cfg
        self.accounts = accounts
        self.notifier = notifier
        self.enabled = bool(cfg.get("enabled")) and bool(cfg.get("api_key"))
        self.client = None
        if self.enabled:
            import anthropic

            self.client = anthropic.Anthropic(api_key=cfg["api_key"])
        self.q = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()

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

    def _call(self, model, system, user, max_tokens):
        r = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in r.content).strip()

    # --- wachtrij -------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="ai").start()

    def enqueue(self, tid, notify=False):
        with self.lock:
            if (tid, notify) in self.pending:
                return
            self.pending.add((tid, notify))
        self.q.put((tid, notify))

    def _run(self):
        while True:
            tid, notify = self.q.get()
            with self.lock:
                self.pending.discard((tid, notify))
            try:
                res = self.triage(tid)
                if notify:
                    self._maybe_notify(tid, res)
            except Exception:
                log.exception("triage mislukt voor thread %s", tid)

    def _maybe_notify(self, tid, res):
        t, msgs = thread_messages(self.db, tid)
        if not t or not msgs:
            return
        prio = (res or {}).get("priority") or "normaal"
        if not self.notifier.wants(prio):
            return
        last = [m for m in msgs if not m["outgoing"]][-1:] or msgs[-1:]
        last = last[0]
        acc = self.accounts.get(t["account"])
        title = f"{acc.name if acc else t['account']}: {last['from_name']}"
        text = last["subject"]
        if res and res.get("summary"):
            text += "\n" + res["summary"]
        self.notifier.send(title, text, tid=tid, high=(prio == "hoog"),
                           extra={"account": t["account"], "from": last["from_addr"], "subject": last["subject"],
                                  "priority": prio, "summary": (res or {}).get("summary")})

    # --- taken ----------------------------------------------------------
    def triage(self, tid):
        t, msgs = thread_messages(self.db, tid)
        if not t or not msgs:
            return None
        if all(m["bulk"] for m in msgs):
            res = {"summary": msgs[-1]["snippet"][:200], "priority": "laag", "category": msgs[-1].get("kind") or "reclame",
                   "action": "geen", "deadline": None}
        elif not self.active_for(t["account"]):
            return None
        else:
            raw = self._call(
                self.cfg.get("triage_model", "claude-haiku-5-5"),
                self._fmt(TRIAGE_SYSTEM),
                thread_text(msgs),
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
                "deadline": dl if isinstance(dl, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", dl) else None,
            }
        self.db.x(
            "UPDATE threads SET summary=?, priority=?, category=?, action=?, deadline=?, ai_count=? WHERE tid=?",
            (res["summary"], res["priority"], res["category"], res["action"], res["deadline"], len(msgs), tid),
        )
        return res

    def draft(self, tid, instruction=""):
        if not self.enabled:
            raise RuntimeError("AI staat uit in de configuratie")
        t, msgs = thread_messages(self.db, tid)
        if not t:
            raise RuntimeError("thread niet gevonden")
        if not self.active_for(t["account"]):
            raise RuntimeError("AI staat uit voor deze mailbox")
        user = thread_text(msgs)
        if instruction.strip():
            user += f"\n\n--- Instructie van {self.cfg.get('name', 'de gebruiker')} voor het antwoord:\n{instruction.strip()}"
        else:
            user += "\n\n--- Schrijf een gepast antwoord op het laatste bericht."
        return self._call(self.cfg.get("draft_model", "claude-sonnet-5-5"), self._fmt(DRAFT_SYSTEM), user, 1500)
