"""Slim zoeken: snel in alle mail (SQLite FTS5), en vragen aan Secretaresse ("wanneer vertrekt mijn vlucht naar Lissabon?").

Gewoon zoeken
- Elk woord moet ergens voorkomen (onderwerp, afzender, ontvangers, tekst, namen en tekst van pdf-bijlagen of de
  samenvatting),
  als begin van een woord en zonder te letten op accenten: "factu" vindt "Factuur", "malaga" vindt "Málaga".
- Filters: van:sofie, aan:billit, na:2026-03, voor:2025, bijlage (of heeft:bijlage), pdf.
- Niets met alle woorden? Dan wat met de meeste woorden overeenkomt.

Vragen
- De AI maakt van je vraag zoekwoorden (met synoniemen en vertalingen: factuur, invoice, rekening) en filters, Secretaresse
  zoekt, en de AI antwoordt in een paar zinnen op basis van de mails die hij vond, met verwijzingen ernaar.
"""

import json
import re
from datetime import datetime

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject, from_name, from_addr, to_addrs, body, attachments, att_text,
  content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, subject, from_name, from_addr, to_addrs, body, attachments, att_text)
  VALUES (new.id, new.subject, new.from_name, new.from_addr, new.to_addrs, new.body, new.attachments, new.att_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, from_name, from_addr, to_addrs, body, attachments, att_text)
  VALUES ('delete', old.id, old.subject, old.from_name, old.from_addr, old.to_addrs, old.body, old.attachments, old.att_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF subject, from_name, from_addr, to_addrs, body, attachments, att_text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, from_name, from_addr, to_addrs, body, attachments, att_text)
  VALUES ('delete', old.id, old.subject, old.from_name, old.from_addr, old.to_addrs, old.body, old.attachments, old.att_text);
  INSERT INTO messages_fts(rowid, subject, from_name, from_addr, to_addrs, body, attachments, att_text)
  VALUES (new.id, new.subject, new.from_name, new.from_addr, new.to_addrs, new.body, new.attachments, new.att_text);
END;
"""

QUESTION = re.compile(r"\?\s*$|^(wie|wat|wanneer|waar|waarom|hoeveel|hoe|welke|welk|heb|had|is|zijn|wordt|moet|kan|"
                      r"when|what|who|where|how|which|did|do|does|is)\b", re.I)
OPS = re.compile(r"\b(van|aan|na|voor|heeft|from|to|after|before):(\"[^\"]+\"|\S+)", re.I)
DATE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def setup(conn):
    """FTS-index en triggers; de eerste keer de bestaande mail indexeren. False als SQLite geen FTS5 heeft."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages_fts)")}
        if cols and "att_text" not in cols:  # van voor de tekst uit pdf-bijlagen: opnieuw opbouwen
            for name in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute("DROP TABLE messages_fts")
        new = not conn.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone()
        conn.executescript(FTS_SQL)
        if new:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        return True
    except Exception:
        return False


def is_question(q):
    return bool(QUESTION.search(q.strip())) and len(q.split()) >= 3


def _date(v):
    """2025, 2025-03 of 2025-03-14; als tekst vergelijkbaar met de datum van een mail (voor:2025 = voor 2025 begint)."""
    m = DATE.match(v)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{int(mo):02d}-{int(d):02d}" if d else f"{y}-{int(mo):02d}" if mo else y


def parse(q):
    """→ (woorden, filters). Filters: van, aan, na, voor, bijlage, pdf."""
    f = {}
    for key, val in OPS.findall(q):
        key, val = key.lower(), val.strip('"')
        key = {"from": "van", "to": "aan", "after": "na", "before": "voor"}.get(key, key)
        if key == "heeft":
            f["bijlage"] = True
        elif key in ("na", "voor"):
            d = _date(val)
            if d:
                f[key] = d
        else:
            f[key] = val
    rest = OPS.sub(" ", q)
    words = []
    for w in re.findall(r"[\w@.'-]+", rest, re.U):
        lw = w.lower().strip(".'-")
        if lw in ("bijlage", "bijlagen", "attachment"):
            f["bijlage"] = True
        elif lw == "pdf":
            f["pdf"] = True
        elif lw:
            words.append(lw)
    return words, f


def _match(words, op="AND"):
    terms = []
    for w in words:
        for part in re.findall(r"\w+", w, re.U):  # het@adres.be → het adres be (zoals de tokenizer)
            terms.append(f'"{part}"*')
    return f" {op} ".join(terms)


def find(db, q, account="", limit=150, any_word=False, words=None, filters=None, fts=True):
    """Thread-id's die passen, de beste eerst (alle woorden, nieuwste eerst; anders de meeste woorden)."""
    if words is None:
        words, filters = parse(q)
    filters = filters or {}
    where, args = ["(?='' OR m.account=?)"], [account, account]
    if filters.get("van"):
        where.append("(m.from_addr LIKE ? OR m.from_name LIKE ?)")
        args += [f"%{filters['van']}%"] * 2
    if filters.get("aan"):
        where.append("m.to_addrs LIKE ?")
        args.append(f"%{filters['aan']}%")
    if filters.get("na"):
        where.append("m.date >= ?")
        args.append(filters["na"])
    if filters.get("voor"):
        where.append("m.date < ?")
        args.append(filters["voor"])
    if filters.get("bijlage") or filters.get("pdf"):
        where.append("m.attachments IS NOT NULL AND m.attachments != '[]'")
    if filters.get("pdf"):
        where.append("m.attachments LIKE '%.pdf%'")

    def run(op):
        sql_words = _match(words, op)
        if sql_words and fts:
            # onderwerp en afzender wegen zwaarder dan de tekst
            sql = f"""SELECT t.tid, MAX(m.date) AS d, MIN(f.r) AS rank
                      FROM (SELECT rowid, bm25(messages_fts, 6, 4, 3, 1, 1, 3, 1) AS r FROM messages_fts
                            WHERE messages_fts MATCH ? ORDER BY r LIMIT 5000) f
                      JOIN messages m ON m.id = f.rowid  -- LIMIT: anders vouwt SQLite de subquery uit en mag bm25 niet
                      JOIN threads t ON t.account=m.account AND t.thread_key=m.thread_key
                      WHERE {' AND '.join(where)} GROUP BY t.tid"""
            rows = db.q(sql, [sql_words] + args)
        elif words:  # zonder FTS5
            like = " AND ".join("(m.subject LIKE ? OR m.from_name LIKE ? OR m.from_addr LIKE ? OR m.body LIKE ?)" for _ in words)
            sql = f"""SELECT t.tid, MAX(m.date) AS d, 0 AS rank FROM messages m
                      JOIN threads t ON t.account=m.account AND t.thread_key=m.thread_key
                      WHERE {like} AND {' AND '.join(where)} GROUP BY t.tid"""
            rows = db.q(sql, [a for w in words for a in [f"%{w}%"] * 4] + args)
        else:
            sql = f"""SELECT t.tid, MAX(m.date) AS d, 0 AS rank FROM messages m
                      JOIN threads t ON t.account=m.account AND t.thread_key=m.thread_key
                      WHERE {' AND '.join(where)} GROUP BY t.tid ORDER BY d DESC LIMIT 500"""
            rows = db.q(sql, args)
        return rows

    rows = list(run("OR" if any_word else "AND"))
    found = {r["tid"] for r in rows}
    # ook de samenvatting (de AI schrijft er vaak de kern in: "vlucht naar Lissabon")
    if words and not any_word:
        like = " AND ".join("t.summary LIKE ?" for _ in words)
        for r in db.q(f"""SELECT t.tid, MAX(m.date) AS d, 0 AS rank FROM threads t
                          JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                          WHERE {like} AND {' AND '.join(where)} GROUP BY t.tid""",
                      [f"%{w}%" for w in words] + args):
            if r["tid"] not in found:
                rows.append(r)
                found.add(r["tid"])
    if not rows and words and len(words) > 1 and not any_word:
        return find(db, q, account, limit, True, words, filters, fts)
    if any_word:
        rows.sort(key=lambda r: (r["rank"], -(datetime.fromisoformat(r["d"]).timestamp() if r["d"] else 0)))
    else:
        rows.sort(key=lambda r: r["d"] or "", reverse=True)
    return [r["tid"] for r in rows[:limit]]


# --- vragen aan Secretaresse -------------------------------------------------------------
PLAN = """Je helpt zoeken in de mailbox van {name}. Vandaag is {today}.
Zet de vraag om in een zoekopdracht. Antwoord met uitsluitend één JSON-object:
{{"woorden": [...], "van": "", "aan": "", "na": "", "voor": "", "bijlage": false}}
- "woorden": 2 tot 8 losse zoekwoorden die in de gezochte mails zouden staan, met synoniemen en vertalingen
  (Nederlands, Engels, Frans), bv. ["vlucht", "flight", "vol", "lissabon", "lisbon", "boarding"]. Geen stopwoorden.
- "van"/"aan": een naam, domein of adres als de vraag dat noemt, anders "".
- "na"/"voor": "JJJJ-MM-DD" als de vraag een periode noemt (vorige maand, in 2025), anders "".
- "bijlage": true als het om een bijlage gaat (factuur als pdf, ticket, contract)."""

ANSWER = """Je bent Secretaresse, de mailassistent van {name}. Vandaag is {today}.
Beantwoord de vraag in het Nederlands in hoogstens drie korte zinnen, enkel op basis van de mails hieronder. Verwijs
naar de mails met hun nummer tussen haken, bv. [2]. Staat het antwoord er niet in, zeg dat dan eerlijk in één zin.
Verzin niets: geen data, bedragen of namen die niet in de mails staan."""


def ask(ai, db, question, account="", limit=8):
    """→ {"answer", "tids", "cited"}. Twee korte AI-vragen: zoekwoorden maken, dan antwoorden."""
    from .db import thread_messages

    fmt = {"name": ai.cfg.get("name", "de gebruiker"), "today": datetime.now().strftime("%Y-%m-%d")}
    raw = ai._call("triage", PLAN.format(**fmt), question, 300)
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        plan = json.loads(m.group(0)) if m else {}
    except ValueError:
        plan = {}
    words = [str(w).lower() for w in plan.get("woorden") or [] if str(w).strip()][:10] or parse(question)[0]
    filters = {k: plan.get(k) for k in ("van", "aan", "na", "voor") if plan.get(k)}
    if plan.get("bijlage"):
        filters["bijlage"] = True
    tids = find(db, question, account, limit, any_word=True, words=words, filters=filters)
    if not tids and filters:  # te streng gefilterd: enkel op de woorden
        tids = find(db, question, account, limit, any_word=True, words=words, filters={})
    if not tids:
        return {"answer": "Ik vond geen mail die hierover gaat.", "tids": [], "cited": []}
    parts = []
    for n, tid in enumerate(tids, 1):
        t, msgs = thread_messages(db, tid)
        if not msgs:
            continue
        last = msgs[-1]
        body = re.sub(r"\s+", " ", last["body"] or "")[:700]
        parts.append(f"[{n}] {last['date'][:10]}, van {last['from_name'] or last['from_addr']}: {last['subject']}\n"
                     f"{('Samenvatting: ' + t['summary'] + chr(10)) if t.get('summary') else ''}{body}")
    answer = ai._call("triage", ANSWER.format(**fmt), f"Vraag: {question}\n\nMails:\n\n" + "\n\n".join(parts), 400).strip()
    cited = [int(x) for x in re.findall(r"\[(\d+)\]", answer) if 0 < int(x) <= len(tids)]
    return {"answer": answer, "tids": tids, "cited": list(dict.fromkeys(cited))}
