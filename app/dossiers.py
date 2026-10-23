"""Dossiers: mails die over dezelfde zaak gaan, bij elkaar (een reis, een verbouwing, een schadegeval, een aankoop).

Er komt geen aparte AI-vraag bij: telkens Secretaresse een mail samenvat of momenten zoekt, noemt de AI ook de zaken waar
de mail over gaat. Ze krijgt daarbij de bestaande dossiers als lijst, zodat de bevestiging van de vlucht, het hotel en
de parking in hetzelfde dossier belanden. Een mail kan in meerdere dossiers zitten: een overzicht van drie lopende
schadedossiers hoort bij elk van die drie.

Daarnaast koppelt Secretaresse mails met hetzelfde referentienummer (S006326085, 2026-00285, ...) zonder AI aan elkaar.
Een nummer dat in veel mails staat (een ondernemingsnummer in een handtekening) telt niet mee.

thread_dossiers.source: 'ai' (gekozen door de AI, wordt vervangen bij een nieuwe samenvatting) of 'ref' (gekoppeld via
een referentienummer). threads.dossier_ai: 0 = nog niet bepaald, 1 = bepaald, -1 = door jou uit de dossiers gehaald
(de AI en de nummers zetten de thread er dan niet meer in).

Een dossier kan afgehandeld zijn (dossiers.closed_at): de mails erin staan dan op klaar. Komt er nieuwe mail over
dezelfde zaak, dan gaat het dossier vanzelf weer open.
"""

import json
import re
from datetime import datetime, timedelta, timezone

PROMPT = """- "dossiers": over welke lopende zaken gaat deze mail? Een zaak is iets waar meerdere mails over gaan: een reis, een verbouwing, een aankoop, een schadegeval, een project, een evenement, een zaak met een instantie. Geef een lijst met per zaak een korte naam van hoogstens 5 woorden die zegt waarover het gaat, bv. ["Reis Borneo juli 2027", "Huurwagen Málaga november", "Aanrijding brievenbus"]: bij een reis de bestemming en de maand, nooit enkel een boekings- of dossiernummer. Gaat de mail over meerdere zaken (zoals een overzicht van lopende dossiers), noem ze dan allemaal; gebruik nooit een verzamelnaam. Kies een bestaand dossier enkel als de mail over exact dezelfde zaak gaat (bij een reis: dezelfde bestemming en dezelfde periode); gebruik dan exact die naam. Bestaande dossiers:
{names}
Een losse mail (een vraag, een nieuwsbrief, een melding die op zichzelf staat): []."""

MAX_NAME = 60
MAX_PER_MAIL = 4
MAX_REF_THREADS = 8  # staat een nummer in meer threads, dan is het geen referentie maar bv. een ondernemingsnummer
REF_RE = re.compile(r"(?<![\w-])[A-Z]{0,4}\d[\d/-]{5,}\d[A-Z0-9]{0,4}(?![\w-])")
DATE_RE = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$|^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
# in de tekst telt een nummer enkel op een regel die over een referentie gaat; zo vallen wetteksten in een
# voettekst (Verordening (EU) 2016/679) en telefoonnummers weg
REF_WORDS = re.compile(r"\b(ref|referentie|reference|kenmerk|dossier|polis|policy|boeking|booking|reserv|klant|schade|claim|"
                       r"order|bestel|factuur|invoice|case|ticket|nr|nummer|number|no\.|n°)", re.I)
LEGAL_RE = re.compile(r"^(19|20)\d{2}/\d{1,4}$")  # EU-verordeningen en -richtlijnen: 2016/679, 2019/1937


def names(db, days=365, limit=50):
    """Recente dossiers, meest recent eerst: die krijgt de AI te zien."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r["name"] for r in db.q("SELECT name FROM dossiers WHERE last_at >= ? ORDER BY last_at DESC LIMIT ?", (since, limit))]


def prompt(db):
    """De bestaande dossiers met een voorbeeldonderwerp en de periode, zodat de AI ziet waarover elk dossier gaat."""
    lines = []
    for name in names(db):
        r = db.one("""SELECT MIN(m.subject) AS subj, MIN(m.date) AS first, MAX(m.date) AS last
                      FROM dossiers d JOIN thread_dossiers td ON td.dossier_id=d.id JOIN threads t ON t.tid=td.tid
                      JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key WHERE d.name=?""", (name,))
        hint = f' (bv. "{r["subj"][:70]}", mails van {r["first"][:7]} tot {r["last"][:7]})' if r and r["subj"] else ""
        lines.append(f"- {json.dumps(name, ensure_ascii=False)}{hint}")
    return PROMPT.format(names="\n".join(lines) if lines else "(nog geen)")


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
    """De dossiers die de AI voor deze thread koos, plus koppeling via referentienummers. De AI heeft het laatste
    woord over de thread zelf: haar keuze vervangt ook wat eerder via een nummer gekoppeld werd."""
    if _locked(db, tid):
        return []
    now = datetime.now(timezone.utc).isoformat()
    db.x("DELETE FROM thread_dossiers WHERE tid=?", (tid,))
    ids = [_id(db, n, now) for n in names_ or []]
    _reopen_on_news(db, tid, ids)
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
        lines = [m["subject"] or ""] + [ln for ln in (m["body"] or "").splitlines() if REF_WORDS.search(ln)]
        for ref in REF_RE.findall("\n".join(lines)):
            digits = sum(c.isdigit() for c in ref)
            if digits < 7 or DATE_RE.match(ref) or LEGAL_RE.match(ref) or (ref.isdigit() and ref.startswith("0")):
                continue
            if ref not in found:
                found.append(ref)
    # de meest herkenbare eerst: met letters of een streepje, dan de langste
    found.sort(key=lambda r: (not re.search(r"[A-Z/-]", r), -len(r)))
    return found[:limit]


def link_by_reference(db, tid, ids):
    """Andere threads met hetzelfde referentienummer in dezelfde dossiers zetten, zolang de AI ze nog niet zelf
    bekeek (dossier_ai=0): daarna geldt haar oordeel."""
    for ref in references(db, tid):
        # S00632608501 en S006326085 zijn hetzelfde dossier: verzekeraars hangen soms een volgnummer aan de referentie
        core = ref[:-2] if re.match(r"^[A-Z]{1,4}\d{10,}$", ref) else ref
        rows = db.q("""SELECT DISTINCT t.tid FROM threads t JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                       WHERE t.tid != ? AND (m.subject LIKE ? OR m.body LIKE ?) LIMIT ?""",
                    (tid, f"%{core}%", f"%{core}%", MAX_REF_THREADS + 1))
        if len(rows) > MAX_REF_THREADS:
            continue
        for r in rows:
            if db.one("SELECT dossier_ai FROM threads WHERE tid=?", (r["tid"],))["dossier_ai"] == 0:
                for did in ids:
                    db.x("INSERT OR IGNORE INTO thread_dossiers(tid, dossier_id, source) VALUES(?,?,'ref')", (r["tid"], did))


def choose(db, tid, name):
    """Door jou gekozen: de thread zit enkel in dit dossier, en de AI en de nummers veranderen dat niet meer."""
    now = datetime.now(timezone.utc).isoformat()
    db.x("DELETE FROM thread_dossiers WHERE tid=?", (tid,))
    did = _id(db, name.strip()[:MAX_NAME], now)
    db.x("INSERT OR IGNORE INTO thread_dossiers(tid, dossier_id, source) VALUES(?,?,'manual')", (tid, did))
    db.x("UPDATE threads SET dossier_ai=-1 WHERE tid=?", (tid,))
    _prune(db)
    return did


def close(db, did):
    """Afgehandeld: het dossier zakt naar onderen en alle mail erin staat op klaar."""
    db.x("UPDATE dossiers SET closed_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), did))
    db.x("UPDATE threads SET done=1 WHERE tid IN (SELECT tid FROM thread_dossiers WHERE dossier_id=?)", (did,))


def reopen(db, did):
    db.x("UPDATE dossiers SET closed_at=NULL WHERE id=?", (did,))


def _reopen_on_news(db, tid, ids):
    """Nieuwe mail in een afgehandeld dossier (later dan het afronden): dan loopt de zaak weer."""
    last = db.one("""SELECT MAX(m.date) AS d FROM threads t JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                     WHERE t.tid=?""", (tid,))
    for did in ids:
        d = db.one("SELECT closed_at FROM dossiers WHERE id=?", (did,))
        if d and d["closed_at"] and last and last["d"] and last["d"] > d["closed_at"]:
            reopen(db, did)


def remove(db, tid):
    db.x("DELETE FROM thread_dossiers WHERE tid=?", (tid,))
    db.x("UPDATE threads SET dossier_ai=-1 WHERE tid=?", (tid,))
    _prune(db)


def _prune(db):
    db.x("DELETE FROM dossiers WHERE id NOT IN (SELECT dossier_id FROM thread_dossiers)")


def of_thread(db, tid):
    return [dict(r) for r in db.q("""SELECT d.id, d.name, d.closed_at FROM thread_dossiers td JOIN dossiers d ON d.id=td.dossier_id
                                     WHERE td.tid=? ORDER BY d.last_at DESC""", (tid,))]


def get(db, did):
    r = db.one("SELECT id, name, closed_at FROM dossiers WHERE id=?", (did,))
    return dict(r) if r else None
