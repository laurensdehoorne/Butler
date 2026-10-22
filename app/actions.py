import json
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from imapclient import SEEN

from .db import thread_messages
from .mailparse import make_snippet


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


def send_reply(db, acc, tid, body, reply_all=False):
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
