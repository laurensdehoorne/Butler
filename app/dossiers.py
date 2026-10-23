"""Dossiers: mails die over hetzelfde gaan, bij elkaar (een reis, een verbouwing, een aankoop, een project).

Er komt geen aparte AI-vraag bij: telkens Butler een mail samenvat of momenten zoekt, kiest de AI ook een dossier.
Ze krijgt daarbij de bestaande dossiers als lijst, zodat de bevestiging van de vlucht, het hotel en de parking in
hetzelfde dossier belanden in plaats van in drie dossiers met net een andere naam.

threads.dossier_ai: 0 = nog niet bepaald, 1 = bepaald door de AI, -1 = door jou uit het dossier gehaald (de AI zet de
thread er dan niet meer in).
"""

import json
import re
from datetime import datetime, timedelta, timezone

PROMPT = """- "dossier": hoort deze mail bij een groter onderwerp waar meerdere mails over gaan (een reis, een verbouwing, een aankoop, een project, een evenement, een zaak met een instantie)? Geef dan een korte naam van hoogstens 5 woorden, bv. "Reis Parijs en Málaga". Past een bestaand dossier, gebruik dan exact die naam: {names}. Een losse mail (een vraag, een nieuwsbrief, een melding die op zichzelf staat): null."""

MAX_NAME = 60


def names(db, days=365, limit=40):
    """Recente dossiers, meest recent eerst: die krijgt de AI te zien."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r["name"] for r in db.q("SELECT name FROM dossiers WHERE last_at >= ? ORDER BY last_at DESC LIMIT ?", (since, limit))]


def prompt(db):
    existing = names(db)
    return PROMPT.format(names=json.dumps(existing, ensure_ascii=False) if existing else "(nog geen)")


def extract(raw):
    """De dossiernaam uit een AI-antwoord (JSON), of None."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        v = (json.loads(m.group(0)) if m else {}).get("dossier")
    except (ValueError, AttributeError):
        return None
    v = str(v).strip().strip('"') if v else ""
    return v[:MAX_NAME] if v and v.lower() not in ("null", "none", "geen") else None


def assign(db, tid, name):
    """Thread in een dossier zetten (bestaand bij dezelfde naam, hoofdletters maken niet uit) of eruit (None)."""
    t = db.one("SELECT dossier_ai FROM threads WHERE tid=?", (tid,))
    if not t or t["dossier_ai"] == -1:
        return None
    now = datetime.now(timezone.utc).isoformat()
    did = None
    if name:
        row = db.one("SELECT id FROM dossiers WHERE name=? COLLATE NOCASE", (name,))
        did = row["id"] if row else db.x("INSERT INTO dossiers(name, created_at, last_at) VALUES(?,?,?)", (name, now, now))
        db.x("UPDATE dossiers SET last_at=? WHERE id=?", (now, did))
    db.x("UPDATE threads SET dossier_id=?, dossier_ai=1 WHERE tid=?", (did, tid))
    return did


def remove(db, tid):
    db.x("UPDATE threads SET dossier_id=NULL, dossier_ai=-1 WHERE tid=?", (tid,))
    db.x("DELETE FROM dossiers WHERE id NOT IN (SELECT dossier_id FROM threads WHERE dossier_id IS NOT NULL)")


def get(db, did):
    r = db.one("SELECT id, name FROM dossiers WHERE id=?", (did,))
    return dict(r) if r else None
