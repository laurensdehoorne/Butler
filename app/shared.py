"""Mail delen tussen gebruikers van dezelfde Butler (zie gateway.py).

Elke gebruiker heeft een eigen database, dus delen is een kopie: de Butler van wie deelt geeft de thread mee (tekst,
HTML en bijlagen, die hij van zijn mailserver haalt), en de Butler van wie ontvangt bewaart die als "gedeeld met jou",
los van zijn eigen mail. De bijlagen staan als bestanden in <data>/shared/<id>/; de ontvanger heeft geen toegang tot
de mailbox van wie deelde, en dat hoeft ook niet.
"""

import base64
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .db import thread_messages

MAX_SHARE_BYTES = 25 * 1024 * 1024  # bijlagen samen; wat erover gaat, staat in de lijst zonder bestand


def export(db, tid, fetch_html, fetch_attachment):
    """De thread als JSON voor een andere gebruiker: de berichten met tekst, HTML en bijlagen (base64)."""
    t, msgs = thread_messages(db, tid)
    if not t or not msgs:
        return None
    total, out = 0, []
    for m in msgs:
        html, atts = m.get("html"), json.loads(m["attachments"]) if m.get("attachments") is not None else None
        if html is None or atts is None:
            html, atts = fetch_html(m["id"])
        files = []
        for i, a in enumerate(atts or []):
            data = None
            if total + (a.get("size") or 0) <= MAX_SHARE_BYTES:
                got = fetch_attachment(m["id"], i)
                if got and total + len(got[0]) <= MAX_SHARE_BYTES:
                    data = got[0]
                    total += len(data)
            files.append({"name": a["name"], "type": a["type"], "size": a.get("size") or 0,
                          "data": base64.b64encode(data).decode() if data is not None else None})
        out.append({"from_name": m["from_name"], "from_addr": m["from_addr"], "to": json.loads(m["to_addrs"] or "[]"),
                    "cc": json.loads(m["cc_addrs"] or "[]"), "date": m["date"], "subject": m["subject"],
                    "body": m["body"] or "", "html": html or "", "attachments": files})
    return {"subject": t.get("subject") or msgs[-1]["subject"] or "", "summary": t.get("summary") or "", "messages": out}


def save(db, data_dir, sender, note, thread):
    """Een gedeelde thread bewaren; de bijlagen als bestanden. Geeft het id terug."""
    msgs = []
    for m in thread["messages"]:
        msgs.append({**m, "attachments": [{k: v for k, v in a.items() if k != "data"} | {"stored": a.get("data") is not None}
                                          for a in m.get("attachments") or []]})
    sid = db.x("""INSERT INTO shared(from_name, note, subject, summary, messages, created_at, done)
                  VALUES(?,?,?,?,?,?,0)""",
               (sender[:80], (note or "")[:2000], thread.get("subject", "")[:300], thread.get("summary", "")[:1000],
                json.dumps(msgs, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
    folder = Path(data_dir) / "shared" / str(sid)
    for mi, m in enumerate(thread["messages"]):
        for ai, a in enumerate(m.get("attachments") or []):
            if a.get("data") is not None:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{mi}-{ai}").write_bytes(base64.b64decode(a["data"]))
    return sid


def listing(db):
    return [dict(r) for r in db.q("""SELECT id, from_name, note, subject, summary, created_at, done FROM shared
                                     ORDER BY done, created_at DESC LIMIT 100""")]


def get(db, sid):
    r = db.one("SELECT * FROM shared WHERE id=?", (sid,))
    if not r:
        return None
    d = dict(r)
    d["messages"] = json.loads(d["messages"] or "[]")
    return d


def attachment(db, data_dir, sid, mi, ai):
    """(bytes, type, naam) of None."""
    d = get(db, sid)
    try:
        a = d["messages"][mi]["attachments"][ai]
    except (TypeError, IndexError, KeyError):
        return None
    f = Path(data_dir) / "shared" / str(sid) / f"{mi}-{ai}"
    if not a.get("stored") or not f.is_file():
        return None
    return f.read_bytes(), a["type"], a["name"]


def remove(db, data_dir, sid):
    db.x("DELETE FROM shared WHERE id=?", (sid,))
    shutil.rmtree(Path(data_dir) / "shared" / str(sid), ignore_errors=True)
