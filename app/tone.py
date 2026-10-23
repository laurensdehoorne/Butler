"""Je schrijfstijl, geleerd uit je eigen verstuurde mail, zodat antwoordvoorstellen klinken zoals jij.

Twee lagen:
- Een schrijfprofiel: de AI leest een steekproef van je verstuurde mails (zonder geciteerde tekst, zonder doorgestuurde
  mail of agenda-antwoorden) en beschrijft per taal en per toon (u of je) hoe je aanspreekt, opbouwt, weigert en
  afsluit. Bewaard in settings ('tone'), maandelijks opnieuw geleerd, en aan te passen in Instellingen.
- Voorbeelden per ontvanger: bij een antwoordvoorstel gaan je laatste mails aan diezelfde persoon mee, zodat de
  aanspreking en toon kloppen ("Dag Sofie" of "Geachte heer").
"""

import json
import re
from datetime import datetime, timedelta, timezone

from .mailparse import strip_quotes

SAMPLE_DAYS = 120
MAX_SAMPLES = 40
PER_RECIPIENT = 3
SAMPLE_CHARS = 1200
REBUILD_DAYS = 30
SKIP_SUBJECT = re.compile(r"^\s*(fwd?|fw|doorst|tr|wg)\s*:|^(accepted|declined|tentative|geaccepteerd|geweigerd|voorlopig)\b", re.I)

PROMPT = """Je leert de schrijfstijl van {owner} kennen uit e-mails die {name} zelf verstuurde.
Schrijf een beknopt, praktisch schrijfprofiel in het Nederlands dat een assistent kan volgen om mails te schrijven die klinken als {name}. Geen inleiding, geen opmaak met sterretjes of kopjes met #; korte regels met een streepje.
Per taal die {name} gebruikt (bv. Nederlands, Engels, Frans), en binnen een taal per toon:
- wanneer {name} formeel schrijft (u) en wanneer informeel (je/jij), en tegen wie (bv. instanties, klanten, collega's, familie)
- aanhef: de exacte vormen die {name} gebruikt
- opening en opbouw: hoe snel {name} ter zake komt, lengte van zinnen en alinea's
- typische woorden en uitdrukkingen
- hoe {name} weigert, uitstelt of iets vraagt
- afsluiting en ondertekening: exact
- opmaak: lijstjes, emoji's, hoofdletters
Haal voorbeelden uit de eigen woorden van {name}, maar neem geen namen, adressen, bedragen of andere gegevens van anderen over."""


def samples(db, since_days=SAMPLE_DAYS, limit=MAX_SAMPLES):
    """Recente verstuurde mails als tekst, nieuwste eerst, hoogstens PER_RECIPIENT per ontvanger (zodat één druk
    gesprek het profiel niet bepaalt)."""
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    seen, per, out = set(), {}, []
    for m in db.q("""SELECT message_id, subject, to_addrs, body FROM messages
                     WHERE outgoing=1 AND folder NOT IN ('_pending') AND date >= ? ORDER BY date DESC LIMIT 2000""", (since,)):
        if (m["message_id"] and m["message_id"] in seen) or SKIP_SUBJECT.search(m["subject"] or ""):
            continue
        body = strip_quotes(m["body"] or "").strip()
        if len(body) < 40:
            continue
        to = json.loads(m["to_addrs"] or "[]")
        key = (to[0][1] if to else "").lower()
        if per.get(key, 0) >= PER_RECIPIENT:
            continue
        per[key] = per.get(key, 0) + 1
        seen.add(m["message_id"])
        out.append(f"--- Onderwerp: {m['subject']}\n{body[:SAMPLE_CHARS]}")
        if len(out) >= limit:
            break
    return out


def get(db):
    row = db.one("SELECT value FROM settings WHERE key='tone'")
    try:
        return json.loads(row["value"]) if row else None
    except ValueError:
        return None


def put(db, text, samples_used=0, edited=False):
    value = {"text": text.strip()[:6000], "built_at": datetime.now(timezone.utc).isoformat(), "samples": samples_used,
             "edited": edited}
    db.x("INSERT OR REPLACE INTO settings(key, value) VALUES('tone', ?)", (json.dumps(value, ensure_ascii=False),))
    return value


def stale(db):
    """Nog geen profiel, of ouder dan REBUILD_DAYS (een profiel dat je zelf aanpaste, blijft staan)."""
    t = get(db)
    if not t:
        return True
    if t.get("edited"):
        return False
    return datetime.fromisoformat(t["built_at"]) < datetime.now(timezone.utc) - timedelta(days=REBUILD_DAYS)


def build(ai, db):
    """Het schrijfprofiel (opnieuw) leren uit je verstuurde mail. Geeft het bewaarde profiel terug, of None."""
    texts = samples(db)
    if len(texts) < 5:
        return None
    system = PROMPT.format(owner=ai.cfg.get("owner", "de gebruiker"), name=ai.cfg.get("name", "de gebruiker"))
    text = ai._call("draft", system, "\n\n".join(texts), 1500)
    return put(db, text, len(texts))


def examples_for(db, account, addr, limit=2, chars=800):
    """Je laatste mails aan deze persoon: de beste maatstaf voor aanspreking en toon."""
    if not addr:
        return []
    out = []
    for m in db.q("""SELECT body FROM messages WHERE account=? AND outgoing=1 AND to_addrs LIKE ? AND folder NOT IN ('_pending')
                     ORDER BY date DESC LIMIT 10""", (account, f'%"{addr.lower()}"%')):
        body = strip_quotes(m["body"] or "").strip()
        if len(body) >= 30:
            out.append(body[:chars])
        if len(out) >= limit:
            break
    return out
