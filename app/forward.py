"""Mail doorsturen, met de bijlagen, en facturen met één tik naar je boekhouding (Billit).

Het boekhoudadres staat in de instellingen (app.invoice_forward) of Butler leert het uit je verstuurde mail: het
adres waar je het vaakst naartoe stuurt bij een boekhoudpakket (Billit, ...). Een factuur herkent Butler aan de
categorie die de AI gaf of aan een pdf met een factuurachtige naam. Is diezelfde bijlage al naar dat adres gegaan
(door Butler, of vanuit Spark), dan stelt Butler het niet meer voor.
"""

import json
import re
import time
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, make_msgid

from .mailparse import attachment_parts, make_snippet

BOOKKEEPING = re.compile(r"billit|accountable|yuki|exact|octopus|clearfacts|winbooks|silverfin|basecone|dext", re.I)
INVOICE_RE = re.compile(r"factu|invoice|rekening|nota\b|receipt|kwitantie|creditnota|facture|rechnung|bestelbon", re.I)
MAX_FORWARD = 30 * 1024 * 1024


def invoice_address(db, cfg):
    """Het adres van je boekhouding: uit de instellingen, anders het meest gebruikte boekhoudadres in je verstuurde mail."""
    if (cfg.get("invoice_forward") or "").strip():
        return cfg["invoice_forward"].strip().lower()
    counts = {}
    for r in db.q("SELECT to_addrs FROM messages WHERE outgoing=1 AND to_addrs LIKE ? ORDER BY date DESC LIMIT 2000",
                  ("%@%",)):
        for _, a in json.loads(r["to_addrs"] or "[]"):
            if BOOKKEEPING.search(a.split("@")[-1]):
                counts[a.lower()] = counts.get(a.lower(), 0) + 1
    return max(counts, key=counts.get) if counts else None


def _names(m):
    return [a["name"] for a in json.loads(m["attachments"] or "[]")] if m.get("attachments") else []


def invoice_message(t, msgs):
    """Het ontvangen bericht met de factuur in deze thread, of None. Van oudere mail is de bijlagenlijst er pas na
    het openen; tot dan volstaan de categorie of het onderwerp, voor mail die nog in je inbox staat."""
    for m in reversed([m for m in msgs if not m["outgoing"]]):
        if m.get("attachments") is None:
            if m["in_inbox"] and (t.get("category") == "factuur" or INVOICE_RE.search(m["subject"] or "")):
                return m
            continue
        pdfs = [n for n in _names(m) if n.lower().endswith(".pdf")]
        if pdfs and (t.get("category") in ("factuur", "bestelling") or INVOICE_RE.search(" ".join(pdfs) + " " + (m["subject"] or ""))):
            return m
    return None


def already_sent(db, addr, m):
    """Ging dit bericht al naar dit adres: doorgestuurd met hetzelfde onderwerp, of met dezelfde bijlage?"""
    names = set(_names(m))
    fwd = {"fwd: " + (m["subject"] or "").lower(), (m["subject"] or "").lower()}
    for r in db.q("SELECT subject, attachments FROM messages WHERE outgoing=1 AND to_addrs LIKE ? AND date >= ?",
                  (f'%"{addr}"%', (m["date"] or "")[:10])):
        if m["subject"] and (r["subject"] or "").lower() in fwd:
            return True
        if names and names & {a["name"] for a in json.loads(r["attachments"] or "[]")}:
            return True
    return False


def suggestion(db, cfg, t, msgs):
    """{"to", "message_id", "done"} als deze thread een factuur bevat en je een boekhoudadres hebt; anders None."""
    m = invoice_message(t, msgs)
    if not m:
        return None
    addr = invoice_address(db, cfg)
    if not addr:
        return None
    return {"to": addr, "message_id": m["id"], "done": already_sent(db, addr, m)}


def recipients(db, limit=300):
    """Adressen waar je naar schreef, het vaakst eerst: suggesties bij het doorsturen."""
    counts, names = {}, {}
    for r in db.q("SELECT to_addrs, cc_addrs FROM messages WHERE outgoing=1 ORDER BY date DESC LIMIT 5000"):
        for n, a in json.loads(r["to_addrs"] or "[]") + json.loads(r["cc_addrs"] or "[]"):
            a = a.lower()
            counts[a] = counts.get(a, 0) + 1
            if n and a not in names:
                names[a] = n
    top = sorted(counts, key=lambda a: -counts[a])[:limit]
    return [{"addr": a, "name": names.get(a, "")} for a in top]


def _header(m):
    try:
        d = datetime.fromisoformat(m["date"]).astimezone().strftime("%d-%m-%Y %H:%M")
    except Exception:
        d = m["date"] or ""
    to = ", ".join(a for _, a in json.loads(m["to_addrs"] or "[]"))
    who = f"{m['from_name']} <{m['from_addr']}>" if m["from_name"] else m["from_addr"]
    return (f"---------- Doorgestuurd bericht ----------\nVan: {who}\nDatum: {d}\nOnderwerp: {m['subject'] or ''}\n"
            f"Aan: {to}\n\n")


def build(acc, m, raw, to, note=""):
    """Een nieuwe mail aan `to` met je notitie, het origineel als tekst en al zijn bijlagen."""
    em = EmailMessage()
    em["From"] = formataddr((acc.display_name, acc.email)) if acc.display_name else acc.email
    em["To"] = ", ".join(to)
    subject = m["subject"] or ""
    em["Subject"] = subject if re.match(r"^\s*(fwd?|fw|doorst|tr|wg)\s*:", subject, re.I) else "Fwd: " + subject
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=acc.email.split("@", 1)[1])
    text = (note.strip() + "\n\n" if note.strip() else "") + _header(m) + (m["body"] or "")
    em.set_content(text)
    names = []
    if raw:
        for i, p in enumerate(attachment_parts(BytesParser(policy=policy.default).parsebytes(raw))):
            data = p.get_payload(decode=True) or b""
            maintype, _, subtype = p.get_content_type().partition("/")
            name = p.get_filename() or f"bijlage-{i + 1}"
            em.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
            names.append({"name": name, "type": p.get_content_type(), "size": len(data)})
    return em, names


def send(db, acc, m, rows, to, note, raw_fn, smtp_send, append):
    """Doorsturen en meteen bijhouden, zodat de suggestie verdwijnt nog voor de verstuurde kopie gesynchroniseerd is."""
    raw = raw_fn(acc, rows, MAX_FORWARD)
    if _names(m) and not raw:
        raise RuntimeError("Kon de bijlagen niet ophalen van de server.")
    em, names = build(acc, m, raw, to, note)
    smtp_send(acc, em)
    append(em)
    db.x(
        """INSERT INTO messages(account, folder, uid, message_id, thread_key, refs, from_addr, from_name, to_addrs,
           cc_addrs, reply_to, subject, date, snippet, body, seen, outgoing, in_inbox, bulk, attachments)
           VALUES(?, '_pending', ?, ?, ?, '', ?, ?, ?, '[]', '', ?, ?, ?, ?, 1, 1, 0, 0, ?)""",
        (acc.id, -int(time.time() * 1000), em["Message-ID"], "fwd:" + em["Message-ID"], acc.email,
         acc.display_name or acc.email, json.dumps([["", a] for a in to]), em["Subject"],
         datetime.now(timezone.utc).isoformat(), make_snippet(note or ""), note or "", json.dumps(names)),
    )
    return {"to": to, "subject": em["Subject"], "attachments": len(names)}
