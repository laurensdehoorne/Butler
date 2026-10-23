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


def fetch_html(db, acc, mid):
    """HTML-versie van een bericht dat nog niet opgehaald was (alles buiten de inbox): één keer van de server, daarna
    bewaard. Zoekt ook bij dezelfde mail in een andere map (bv. Alle e-mail na archiveren)."""
    row = db.one("SELECT * FROM messages WHERE id=?", (mid,))
    if not row:
        return None
    rows = [row] + (db.q("SELECT * FROM messages WHERE account=? AND message_id=? AND id!=?",
                         (row["account"], row["message_id"], mid)) if row["message_id"] else [])
    for r in rows:
        if r["html"] is not None:
            return r["html"]
    html = ""
    for r in rows:
        if r["uid"] > 0 and not r["folder"].startswith("_"):
            def fn(c, r=r):
                c.select_folder(r["folder"], readonly=True)
                size = c.fetch([r["uid"]], ["RFC822.SIZE"]).get(r["uid"], {}).get(b"RFC822.SIZE", 0)
                if not size or size > MAX_HTML_FETCH:
                    return ""
                data = c.fetch([r["uid"]], ["BODY.PEEK[]"]).get(r["uid"], {}).get(b"BODY[]")
                return parse(data)["html"] if data else ""
            html = acc.run_imap(fn)
            break
    db.x("UPDATE messages SET html=? WHERE id=? OR (account=? AND message_id=? AND message_id!='')",
         (html, mid, row["account"], row["message_id"] or ""))
    return html


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


def build_reply(db, acc, tid, body, reply_all=False):
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
    return t, em, mid, to, cc, subject, refs


def save_draft(db, acc, tid, body, reply_all=False):
    """Zet het antwoord als concept in de Concepten-map: verschijnt in Spark/Apple Mail/Gmail om daar te versturen."""
    _, em, _, to, cc, subject, _ = build_reply(db, acc, tid, body, reply_all)
    folder = acc.folders.get("drafts")
    if not folder:
        raise RuntimeError(f"geen Concepten-map gevonden voor {acc.name}")
    raw = em.as_bytes()
    acc.run_imap(lambda c: c.append(folder, raw, flags=(DRAFT, SEEN)))
    return {"to": to, "cc": cc, "subject": subject, "folder": folder}


def send_reply(db, acc, tid, body, reply_all=False):
    t, em, mid, to, cc, subject, refs = build_reply(db, acc, tid, body, reply_all)

    smtp_send(acc, em)

    if acc.append_sent and acc.folders.get("sent"):
        raw = em.as_bytes()
        try:
            acc.run_imap(lambda c: c.append(acc.folders["sent"], raw, flags=(SEEN,)))
        except Exception:
            pass  # verzonden is verzonden; enkel de kopie in 'Verzonden' ontbreekt dan

    now = datetime.now(timezone.utc).isoformat()
    db.x(
        """INSERT INTO messages(account, folder, uid, message_id, thread_key, refs, from_addr, from_name, to_addrs,
           cc_addrs, reply_to, subject, date, snippet, body, seen, outgoing, in_inbox, bulk)
           VALUES(?, '_pending', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, 1, 1, 0, 0)""",
        (acc.id, -int(time.time() * 1000), mid, t["thread_key"], " ".join(refs), acc.email,
         acc.display_name or acc.email, json.dumps([["", a] for a in to]), json.dumps([["", a] for a in cc]),
         subject, now, make_snippet(body), body),
    )
    return {"to": to, "cc": cc, "subject": subject}


# --- afzenders: alles verplaatsen en uitschrijven ------------------------------
def move_sender(db, acc, addr, role):
    rows = db.q(
        "SELECT id, uid FROM messages WHERE account=? AND from_addr=? AND folder='INBOX' AND uid>0",
        (acc.id, addr.lower()),
    )
    return _move_rows(db, acc, rows, role)


def move_kind(db, acc, kind, role):
    """Alle reclame (of notificaties) uit de inbox in één keer: één IMAP-verplaatsing per account."""
    rows = db.q("SELECT id, uid FROM messages WHERE account=? AND kind=? AND folder='INBOX' AND uid>0 AND outgoing=0",
                (acc.id, kind))
    return _move_rows(db, acc, rows, role)


def _move_rows(db, acc, rows, role):
    if not rows:
        return 0
    dest = acc.folders.get(role)
    if not dest:
        raise RuntimeError(f"geen {role}-map gevonden voor {acc.name}")
    uids = [r["uid"] for r in rows]

    def fn(c):
        c.select_folder("INBOX")
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
