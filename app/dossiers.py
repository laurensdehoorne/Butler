"""Dossiers: mails die over dezelfde zaak gaan, bij elkaar (een reis, een verbouwing, een schadegeval, een aankoop).

Er komt geen aparte AI-vraag bij: telkens Butler een mail samenvat of momenten zoekt, noemt de AI ook de zaken waar
de mail over gaat. Ze krijgt daarbij de bestaande dossiers als lijst, zodat de bevestiging van de vlucht, het hotel en
de parking in hetzelfde dossier belanden. Een mail kan in meerdere dossiers zitten: een overzicht van drie lopende
schadedossiers hoort bij elk van die drie.

Daarnaast koppelt Butler mails met hetzelfde referentienummer (S006326085, 2026-00285, ...) zonder AI aan elkaar.
Een nummer dat in veel mails staat (een ondernemingsnummer in een handtekening) telt niet mee.

thread_dossiers.source: 'ai' (gekozen door de AI, wordt vervangen bij een nieuwe samenvatting) of 'ref' (gekoppeld via
een referentienummer). threads.dossier_ai: 0 = nog niet bepaald, 1 = bepaald, -1 = door jou uit de dossiers gehaald
(de AI en de nummers zetten de thread er dan niet meer in).
"""

import json
import re
from datetime import datetime, timedelta, timezone

PROMPT = """- "dossiers": over welke lopende zaken gaat deze mail? Een zaak is iets waar meerdere mails over gaan: een reis, een verbouwing, een aankoop, een schadegeval, een project, een evenement, een zaak met een instantie. Geef een lijst met per zaak een korte, specifieke naam van hoogstens 5 woorden, bv. ["Aanrijding brievenbus", "Schade aan ramen"]. Gaat de mail over meerdere zaken (zoals een overzicht van lopende dossiers), noem ze dan allemaal; gebruik nooit een verzamelnaam. Past een bestaand dossier, gebruik dan exact die naam: {names}. Een losse mail (een vraag, een nieuwsbrief, een melding die op zichzelf staat): []."""

MAX_NAME = 60
MAX_PER_MAIL = 4
MAX_REF_THREADS = 8  # staat een nummer in meer threads, dan is het geen referentie maar bv. een ondernemingsnummer
REF_RE = re.compile(r"(?<![\w-])[A-Z]{0,4}\d[\d/-]{5,}\d[A-Z0-9]{0,4}(?![\w-])")
DATE_RE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$|^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")


def names(db, days=365, limit=50):
    """Recente dossiers, meest recent eerst: die krijgt de AI te zien."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r["name"] for r in db.q("SELECT name FROM dossiers WHERE last_at >= ? ORDER BY last_at DESC LIMIT ?", (since, limit))]


def prompt(db):
    existing = names(db)
    return PROMPT.format(names=json.dumps(existing, ensure_ascii=False) if existing else "(nog geen)")


def extract(raw):
    """De dossiernamen uit een AI-antwoord (JSON). Aanvaardt ook de oude vorm "dossier": "naam"."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        return []
    v = data.get("dossiers", data.get("dossier"))
    items = v if isinstance(v, list) else [v]
    out = []
    for x in items:
        x = str(x).strip().strip('"') if x else ""
        if x and x.lower() not in ("null", "none", "geen") and x[:MAX_NAME].lower() not in (o.lower() for o in out):
            out.append(x[:MAX_NAME])
    return out[:MAX_PER_MAIL]


def _id(db, name, now):
    row = db.one("SELECT id FROM dossiers WHERE name=? COLLATE NOCASE", (name,))
    did = row["id"] if row else db.x("INSERT INTO dossiers(name, created_at, last_at) VALUES(?,?,?)", (name, now, now))
    db.x("UPDATE dossiers SET last_at=? WHERE id=?", (now, did))
    return did


def _locked(db, tid):
    t = db.one("SELECT dossier_ai FROM threads WHERE tid=?", (tid,))
    return not t or t["dossier_ai"] == -1


def assign(db, tid, names_):
    """De dossiers die de AI voor deze thread koos (vervangt haar vorige keuze), plus koppeling via referentienummers."""
    if _locked(db, tid):
        return []
    now = datetime.now(timezone.utc).isoformat()
    db.x("DELETE FROM thread_dossiers WHERE tid=? AND source='ai'", (tid,))
    ids = [_id(db, n, now) for n in names_ or []]
    for did in ids:
        db.x("INSERT OR IGNORE INTO thread_dossiers(tid, dossier_id, source) VALUES(?,?,'ai')", (tid, did))
    db.x("UPDATE threads SET dossier_ai=1 WHERE tid=?", (tid,))
    if ids:
        link_by_reference(db, tid, ids)
    _prune(db)
    return ids


def references(db, tid, limit=6):
    """Referentienummers uit onderwerp en tekst van een thread: dossier-, polis-, boekings- en klantnummers."""
    t = db.one("SELECT account, thread_key FROM threads WHERE tid=?", (tid,))
    if not t:
        return []
    found = []
    for m in db.q("SELECT subject, substr(body, 1, 20000) AS body FROM messages WHERE account=? AND thread_key=?",
                  (t["account"], t["thread_key"])):
        for ref in REF_RE.findall(f"{m['subject'] or ''}\n{m['body'] or ''}"):
            digits = sum(c.isdigit() for c in ref)
            if digits < 7 or DATE_RE.match(ref) or (ref.isdigit() and ref.startswith("0")):  # datums en telefoonnummers niet
                continue
            if ref not in found:
                found.append(ref)
    # de meest herkenbare eerst: met letters of een streepje, dan de langste
    found.sort(key=lambda r: (not re.search(r"[A-Z/-]", r), -len(r)))
    return found[:limit]


def link_by_reference(db, tid, ids):
    """Andere threads met hetzelfde referentienummer in dezelfde dossiers zetten."""
    for ref in references(db, tid):
        rows = db.q("""SELECT DISTINCT t.tid FROM threads t JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                       WHERE t.tid != ? AND (m.subject LIKE ? OR m.body LIKE ?) LIMIT ?""",
                    (tid, f"%{ref}%", f"%{ref}%", MAX_REF_THREADS + 1))
        if len(rows) > MAX_REF_THREADS:
            continue
        for r in rows:
            if not _locked(db, r["tid"]):
                for did in ids:
                    db.x("INSERT OR IGNORE INTO thread_dossiers(tid, dossier_id, source) VALUES(?,?,'ref')", (r["tid"], did))


def remove(db, tid):
    db.x("DELETE FROM thread_dossiers WHERE tid=?", (tid,))
    db.x("UPDATE threads SET dossier_ai=-1 WHERE tid=?", (tid,))
    _prune(db)


def _prune(db):
    db.x("DELETE FROM dossiers WHERE id NOT IN (SELECT dossier_id FROM thread_dossiers)")


def of_thread(db, tid):
    return [dict(r) for r in db.q("""SELECT d.id, d.name FROM thread_dossiers td JOIN dossiers d ON d.id=td.dossier_id
                                     WHERE td.tid=? ORDER BY d.last_at DESC""", (tid,))]


def get(db, did):
    r = db.one("SELECT id, name FROM dossiers WHERE id=?", (did,))
    return dict(r) if r else None
