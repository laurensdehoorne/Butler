import json
import re
import smtplib
import ssl
import time
from urllib.parse import parse_qs, unquote, urlsplit
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import httpx
from imapclient import DRAFT, FLAGGED, SEEN

from .db import thread_messages, upsert_rule
from .mailparse import make_snippet, parse


def _inbox_uids(db, t):
    rows = db.q(
        "SELECT id, uid FROM messages WHERE account=? AND thread_key=? AND folder='INBOX' AND uid>0",
        (t["account"], t["thread_key"]),
    )
    return [r["id"] for r in rows], [r["uid"] for r in rows]


def move_thread(db, acc, tid, role):
    """role: 'archive' of 'trash' — haalt alle inbox-berichten van de thread uit INBOX."""
    t = db.one("SELECT * FROM threads WHERE tid=?", (tid,))
    if not t:
        raise ValueError("thread niet gevonden")
    ids, uids = _inbox_uids(db, t)
    dest = acc.folders.get(role)
    if uids:
        if not dest:
            raise RuntimeError(f"geen {role}-map gevonden voor {acc.name}")

        def fn(c):
            c.select_folder("INBOX")
            if c.has_capability("MOVE"):
                c.move(uids, dest)
            else:
                c.copy(uids, dest)
                c.delete_messages(uids)
                c.expunge(uids)

        acc.run_imap(fn)
    if ids:
        db.xm("UPDATE messages SET in_inbox=0, folder='_weg', uid=-id WHERE id=?", [(i,) for i in ids])
    return len(uids)


MAX_HTML_FETCH = 15 * 1024 * 1024


def _copies(db, mid):
    """Het bericht en dezelfde mail in andere mappen (bv. Alle e-mail na archiveren)."""
    row = db.one("SELECT * FROM messages WHERE id=?", (mid,))
    if not row:
        return None, []
    return row, [row] + (db.q("SELECT * FROM messages WHERE account=? AND message_id=? AND id!=?",
                              (row["account"], row["message_id"], mid)) if row["message_id"] else [])


def _raw(acc, rows, limit=MAX_HTML_FETCH):
    """De volledige mail van de server, via de eerste kopie die er nog staat; None als dat niet lukt of te groot is."""
    for r in rows:
        if r["uid"] > 0 and not r["folder"].startswith("_"):
            def fn(c, r=r):
                c.select_folder(r["folder"], readonly=True)
                size = c.fetch([r["uid"]], ["RFC822.SIZE"]).get(r["uid"], {}).get(b"RFC822.SIZE", 0)
                if not size or size > limit:
                    return None
                return c.fetch([r["uid"]], ["BODY.PEEK[]"]).get(r["uid"], {}).get(b"BODY[]")
            return acc.run_imap(fn)
    return None


def fetch_html(db, acc, mid):
    """HTML-versie en bijlagenlijst van een bericht dat nog niet opgehaald was (alles buiten de inbox, en mail van
    voor de bijlagen): één keer van de server, daarna bewaard. → (html, bijlagen)"""
    row, rows = _copies(db, mid)
    if not row:
        return None, None
    html = next((r["html"] for r in rows if r["html"] is not None), None)
    atts = next((json.loads(r["attachments"]) for r in rows if r["attachments"] is not None), None)
    if html is not None and atts is not None:
        return html, atts
    data = _raw(acc, rows)
    if not data:  # ophalen mislukte (bericht verplaatst, te groot, server traag): niet als "geen HTML" bewaren
        return html or "", atts or []
    p = parse(data)
    html = html if html is not None else p["html"]
    atts = atts if atts is not None else p["attachments"]
    db.x("UPDATE messages SET html=?, attachments=?, att_text=COALESCE(att_text, ?) WHERE id=? OR (account=? AND message_id=? AND message_id!='')",
         (html, json.dumps(atts), p.get("att_text"), mid, row["account"], row["message_id"] or ""))
    if p.get("body") and not (row["body"] or "").strip():  # lege tekstversie: nu met de tekst uit de HTML
        db.x("UPDATE messages SET body=?, snippet=? WHERE id=? OR (account=? AND message_id=? AND message_id!='')",
             (p["body"][:50000], make_snippet(p["body"]), mid, row["account"], row["message_id"] or ""))
        db.x("UPDATE threads SET ai_count=0 WHERE account=? AND thread_key=?", (row["account"], row["thread_key"]))
    return html, atts


MAX_ATTACHMENT_FETCH = 40 * 1024 * 1024


def fetch_attachment(db, acc, mid, idx):
    """(bytes, type, naam) van bijlage nummer idx, rechtstreeks van de server; None als ze niet (meer) bestaat."""
    from email import policy
    from email.parser import BytesParser

    from .mailparse import att_type, attachment_parts

    _, rows = _copies(db, mid)
    data = _raw(acc, rows, MAX_ATTACHMENT_FETCH) if rows else None
    if not data:
        return None
    parts = attachment_parts(BytesParser(policy=policy.default).parsebytes(data))
    if not 0 <= idx < len(parts):
        return None
    p = parts[idx]
    name = p.get_filename() or f"bijlage-{idx + 1}"
    return p.get_payload(decode=True) or b"", att_type(p.get_content_type(), name), name


def set_flag(db, acc, tid, on):
    """Vlag zetten of weghalen, ook op de server, zodat Spark (en Gmail: een ster) hetzelfde toont.
    Aanzetten markeert het laatste ontvangen bericht; uitzetten haalt de vlag van elk bericht in de thread."""
    t = db.one("SELECT * FROM threads WHERE tid=?", (tid,))
    if not t:
        raise ValueError("thread niet gevonden")
    rows = db.q("""SELECT id, folder, uid, flagged, outgoing FROM messages WHERE account=? AND thread_key=?
                   AND uid>0 AND folder NOT IN ('_weg', '_pending') ORDER BY date DESC, id DESC""",
                (t["account"], t["thread_key"]))
    if on:
        targets = [r for r in rows if not r["outgoing"]][:1] or rows[:1]
    else:
        targets = [r for r in rows if r["flagged"]]
    if not targets:
        if on:
            raise RuntimeError("geen bericht op de server om te markeren")
        return 0
    by_folder = {}
    for r in targets:
        by_folder.setdefault(r["folder"], []).append(r["uid"])

    def fn(c):
        for folder, uids in by_folder.items():
            c.select_folder(folder)
            (c.add_flags if on else c.remove_flags)(uids, [FLAGGED])

    acc.run_imap(fn)
    if on:
        db.x("UPDATE messages SET flagged=1 WHERE id=?", (targets[0]["id"],))
    else:
        db.x("UPDATE messages SET flagged=0 WHERE account=? AND thread_key=?", (t["account"], t["thread_key"]))
    return len(targets)


def mark_read(db, acc, tid):
    t = db.one("SELECT * FROM threads WHERE tid=?", (tid,))
    if not t:
        raise ValueError("thread niet gevonden")
    ids, uids = _inbox_uids(db, t)
    if uids:
        def fn(c):
            c.select_folder("INBOX")
            c.add_flags(uids, [SEEN])

        acc.run_imap(fn)
    db.x("UPDATE messages SET seen=1 WHERE account=? AND thread_key=?", (t["account"], t["thread_key"]))


def _quote(m):
    who = m["from_name"] or m["from_addr"]
    try:
        d = datetime.fromisoformat(m["date"]).astimezone().strftime("%d-%m-%Y %H:%M")
    except Exception:
        d = m["date"] or ""
    body = (m["body"] or "")[:6000]
    quoted = "\n".join("> " + l for l in body.split("\n"))
    return f"\n\nOp {d} schreef {who} <{m['from_addr']}>:\n{quoted}\n"


def smtp_send(acc, em):
    ctx = ssl.create_default_context()
    if acc.smtp_port == 465:
        with smtplib.SMTP_SSL(acc.smtp_host, acc.smtp_port, timeout=60, context=ctx) as s:
            s.login(acc.smtp_user, acc.password)
            s.send_message(em)
    else:
        with smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(acc.smtp_user, acc.password)
            s.send_message(em)


MAX_UPLOAD = 18 * 1024 * 1024  # bijlagen samen: Gmail en iCloud weigeren mail boven ~25 MB (met base64-overhead)


def add_files(em, files):
    """Bijlagen die je zelf toevoegt: [(naam, type, bytes)]."""
    if sum(len(d) for _, _, d in files) > MAX_UPLOAD:
        raise ValueError(f"bijlagen samen groter dan {MAX_UPLOAD // 1024 // 1024} MB")
    for name, ctype, data in files:
        main, _, sub = (ctype or "application/octet-stream").partition("/")
        em.add_attachment(data, maintype=main, subtype=sub or "octet-stream", filename=name)


def build_reply(db, acc, tid, body, reply_all=False, files=()):
    t, msgs = thread_messages(db, tid)
    if not t or not msgs:
        raise ValueError("thread niet gevonden")
    incoming = [m for m in msgs if not m["outgoing"]]
    last = msgs[-1]
    base = incoming[-1] if incoming else last

    if incoming:
        to = [base["reply_to"] or base["from_addr"]]
    else:  # enkel eigen berichten: opnieuw naar dezelfde ontvangers (herinnering)
        to = [a for _, a in json.loads(base["to_addrs"] or "[]")]
    cc = []
    if reply_all:
        others = json.loads(base["to_addrs"] or "[]") + json.loads(base["cc_addrs"] or "[]")
        for _, a in others:
            if not acc.is_me(a) and a not in to and a not in cc:
                cc.append(a)
    to = [a for a in to if not acc.is_me(a)] or to
    if not to:
        raise ValueError("geen ontvanger gevonden")

    subject = base["subject"] or ""
    if not re.match(r"^\s*(re|antw|aw|sv)\s*:", subject, re.I):
        subject = "Re: " + subject

    em = EmailMessage()
    em["From"] = formataddr((acc.display_name, acc.email)) if acc.display_name else acc.email
    em["To"] = ", ".join(to)
    if cc:
        em["Cc"] = ", ".join(cc)
    em["Subject"] = subject
    em["Date"] = formatdate(localtime=True)
    domain = acc.email.split("@", 1)[1]
    mid = make_msgid(domain=domain)
    em["Message-ID"] = mid
    refs = (last["refs"] or "").split()
    if last["message_id"]:
        em["In-Reply-To"] = last["message_id"]
        refs = [r for r in refs if r != last["message_id"]] + [last["message_id"]]
    if refs:
        em["References"] = " ".join(refs[-20:])
    em.set_content(body.rstrip() + _quote(base))
    add_files(em, files)
    return t, em, mid, to, cc, subject, refs


def save_draft(db, acc, tid, body, reply_all=False, files=()):
    """Zet het antwoord als concept in de Concepten-map: verschijnt in Spark/Apple Mail/Gmail om daar te versturen."""
    _, em, _, to, cc, subject, _ = build_reply(db, acc, tid, body, reply_all, files)
    folder = acc.folders.get("drafts")
    if not folder:
        raise RuntimeError(f"geen Concepten-map gevonden voor {acc.name}")
    raw = em.as_bytes()
    acc.run_imap(lambda c: c.append(folder, raw, flags=(DRAFT, SEEN)))
    return {"to": to, "cc": cc, "subject": subject, "folder": folder, "message_id": em["Message-ID"]}


def append_sent(acc, em):
    if acc.append_sent and acc.folders.get("sent"):
        raw = em.as_bytes()
        try:
            acc.run_imap(lambda c: c.append(acc.folders["sent"], raw, flags=(SEEN,)))
        except Exception:
            pass  # verzonden is verzonden; enkel de kopie in 'Verzonden' ontbreekt dan


def forward(db, acc, mid, to, note=""):
    from . import forward as fw

    row, rows = _copies(db, mid)
    if not row:
        raise ValueError("bericht niet gevonden")
    return fw.send(db, acc, dict(row), rows, to, note, _raw, smtp_send, lambda em: append_sent(acc, em))


def send_reply(db, acc, tid, body, reply_all=False, files=()):
    t, em, mid, to, cc, subject, refs = build_reply(db, acc, tid, body, reply_all, files)

    smtp_send(acc, em)
    append_sent(acc, em)

    now = datetime.now(timezone.utc).isoformat()
    db.x(
        """INSERT INTO messages(account, folder, uid, message_id, thread_key, refs, from_addr, from_name, to_addrs,
           cc_addrs, reply_to, subject, date, snippet, body, seen, outgoing, in_inbox, bulk, attachments)
           VALUES(?, '_pending', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, 1, 1, 0, 0, ?)""",
        (acc.id, -int(time.time() * 1000), mid, t["thread_key"], " ".join(refs), acc.email,
         acc.display_name or acc.email, json.dumps([["", a] for a in to]), json.dumps([["", a] for a in cc]),
         subject, now, make_snippet(body), body,
         json.dumps([{"name": n, "type": c, "size": len(d)} for n, c, d in files])),
    )
    return {"to": to, "cc": cc, "subject": subject}


# --- afzenders: alles verplaatsen en uitschrijven ------------------------------
def move_sender(db, acc, addr, role):
    rows = db.q(
        "SELECT id, uid FROM messages WHERE account=? AND from_addr=? AND folder='INBOX' AND uid>0",
        (acc.id, addr.lower()),
    )
    return _move_rows(db, acc, rows, role)


def move_kind(db, acc, kind, role, archived=False):
    """Alle reclame (of notificaties) uit de inbox in één keer: één IMAP-verplaatsing per map. Met archived ook
    wat al in het archief staat (enkel naar de prullenbak: archiveren is het daar al)."""
    moved = _move_rows(db, acc, kind_rows(db, acc, kind, "INBOX"), role)
    if archived and role == "trash" and acc.folders.get("archive"):
        moved += _move_rows(db, acc, kind_rows(db, acc, kind, acc.folders["archive"]), role, acc.folders["archive"])
    return moved


def kind_rows(db, acc, kind, folder):
    return db.q("SELECT id, uid FROM messages WHERE account=? AND kind=? AND folder=? AND uid>0 AND outgoing=0",
                (acc.id, kind, folder))


def _move_rows(db, acc, rows, role, src="INBOX"):
    if not rows:
        return 0
    dest = acc.folders.get(role)
    if not dest:
        raise RuntimeError(f"geen {role}-map gevonden voor {acc.name}")
    uids = [r["uid"] for r in rows]

    def fn(c):
        c.select_folder(src)
        if c.has_capability("MOVE"):
            c.move(uids, dest)
        else:
            c.copy(uids, dest)
            c.delete_messages(uids)
            c.expunge(uids)

    acc.run_imap(fn)
    db.xm("UPDATE messages SET in_inbox=0, folder='_weg', uid=-id WHERE id=?", [(r["id"],) for r in rows])
    return len(rows)


def unsub_method(unsub, unsub_post):
    links = re.findall(r"<([^>]+)>", unsub or "")
    https = [l for l in links if l.lower().startswith("https://")]
    mailto = [l for l in links if l.lower().startswith("mailto:")]
    if https and unsub_post:
        return "one-click", https[0]
    if mailto:
        return "mail", mailto[0]
    if https:
        return "link", https[0]
    return None, None


def unsubscribe(db, acc, addr):
    """Uitschrijven volgens RFC 8058 (one-click) of RFC 2369 (mailto). Een gewone link geven we terug om zelf te openen."""
    r = db.one(
        "SELECT unsub, unsub_post FROM messages WHERE account=? AND from_addr=? AND unsub != '' ORDER BY date DESC LIMIT 1",
        (acc.id, addr.lower()),
    )
    if not r:
        raise ValueError("geen uitschrijflink gevonden in de mails van deze afzender")
    method, target = unsub_method(r["unsub"], r["unsub_post"])
    if method == "one-click":
        resp = httpx.post(target, data={"List-Unsubscribe": "One-Click"}, timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Butler (RFC 8058 one-click unsubscribe)"})
        if resp.status_code >= 400:
            raise RuntimeError(f"uitschrijven geweigerd door afzender (HTTP {resp.status_code})")
    elif method == "mail":
        u = urlsplit(target)
        qs = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        em = EmailMessage()
        em["From"] = acc.email
        em["To"] = unquote(u.path)
        em["Subject"] = qs.get("subject", "unsubscribe")
        em["Date"] = formatdate(localtime=True)
        em["Message-ID"] = make_msgid(domain=acc.email.split("@", 1)[1])
        em.set_content(qs.get("body", "unsubscribe"))
        smtp_send(acc, em)
    elif method == "link":
        return {"method": "link", "url": target}
    else:
        raise ValueError("geen bruikbare uitschrijfmethode")
    rule = upsert_rule(db, acc.id, addr, unsubscribed_at=datetime.now(timezone.utc).isoformat())
    if rule["action"] in (None, "none"):
        upsert_rule(db, acc.id, addr, action="archive")  # wat nog binnenkomt, gaat meteen naar het archief
    return {"method": method}
