"""Wat Butler zelf doet, als je het aanzet (Instellingen → Automatisch), en wat hij deed (auto_log).

Harde regel: Butler beantwoordt en deelt nooit zelf iets. Er is precies één uitzondering: een factuur (pdf) doorsturen
naar je boekhouding (Billit), en dat enkel als je het aanzet. Wat hij zelf mag:
- Facturen: een nieuwe mail met een factuur (pdf) gaat meteen naar je boekhouding (zie forward.py), nooit naar iemand anders.
- Reclame: reclame die een dag in je inbox staat, gaat naar het archief (nooit naar de prullenbak).
- Antwoorden: bij nieuwe mail waar de AI een antwoord van jou verwacht, zet Butler een concept klaar in je
  Concepten-map. Versturen doe jij, in Butler of in Spark, na het nalezen.

Enkel mail die binnenkomt nadat je iets aanzette, nooit je oude mail. Alles komt in het logboek "Wat Butler deed";
opruimen en concepten kun je daar terugdraaien, een doorgestuurde factuur niet (die is verstuurd).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from . import forward
from .db import thread_messages

log = logging.getLogger("butler.auto")

KINDS = ("invoices", "ads", "drafts")
ADS_AFTER_DAYS = 1


def enabled(app_cfg, kind):
    return bool((app_cfg.get("auto") or {}).get(kind))


def since(db, kind):
    """Vanaf wanneer dit aanstaat: oudere mail blijft onaangeroerd."""
    r = db.one("SELECT value FROM settings WHERE key=?", (f"auto_since_{kind}",))
    return r["value"] if r else None


def mark_enabled(db, app_cfg):
    now = datetime.now(timezone.utc).isoformat()
    for k in KINDS:
        if enabled(app_cfg, k):
            db.x("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (f"auto_since_{k}", now))
        else:
            db.x("DELETE FROM settings WHERE key=?", (f"auto_since_{k}",))


def record(db, kind, account, tid, title, detail, data=None, undo=False):
    return db.x("""INSERT INTO auto_log(at, kind, account, tid, title, detail, data, can_undo)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (datetime.now(timezone.utc).isoformat(), kind, account, tid, title[:200], detail[:500],
                 json.dumps(data or {}), int(undo)))


def done_before(db, kind, tid, key=""):
    return bool(db.one("SELECT 1 FROM auto_log WHERE kind=? AND tid=? AND data LIKE ?", (kind, tid, f'%{key}%')))


def entries(db, days=14, limit=200):
    since_ = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for r in db.q("SELECT * FROM auto_log WHERE at >= ? ORDER BY at DESC LIMIT ?", (since_, limit)):
        d = dict(r)
        d.pop("data", None)
        out.append(d)
    return out


def _last_incoming(msgs):
    inc = [m for m in msgs if not m["outgoing"]]
    return inc[-1] if inc else None


# --- na elke samenvatting: facturen en conceptantwoorden ----------------------------
def after_triage(db, app_cfg, ai, accounts, actions, tid, quiet=frozenset()):
    t, msgs = thread_messages(db, tid)
    if not t or not msgs:
        return
    acc = accounts.get(t["account"])
    last = _last_incoming(msgs)
    if not acc or not last:
        return
    if enabled(app_cfg, "invoices"):
        _invoice(db, app_cfg, acc, actions, t, msgs)
    if enabled(app_cfg, "drafts") and ai.active_for(acc.id):
        _draft(db, ai, acc, actions, t, msgs, last, quiet)


def _new(db, kind, m):
    s = since(db, kind)
    return bool(s and (m["date"] or "") >= s)


def _invoice(db, app_cfg, acc, actions, t, msgs):
    m = forward.invoice_message(t, msgs)
    # enkel met een gekende pdf: een onderwerp alleen ("uw factuur") volstaat niet om zelf iets te versturen
    if not m or m.get("attachments") is None or not _new(db, "invoices", m) or done_before(db, "factuur", t["tid"]):
        return
    if not any(a["name"].lower().endswith(".pdf") for a in json.loads(m["attachments"])):
        return
    addr = forward.invoice_address(db, app_cfg)
    if not addr or forward.already_sent(db, addr, m):
        return
    res = actions.forward(db, acc, m["id"], [addr])
    record(db, "factuur", acc.id, t["tid"], m["subject"] or "(geen onderwerp)",
           f"Doorgestuurd naar {addr} ({res['attachments']} bijlage{'n' if res['attachments'] != 1 else ''})",
           {"message_id": m["id"], "to": addr})
    log.info("%s: factuur %s doorgestuurd naar %s", acc.id, t["tid"], addr)


def _draft(db, ai, acc, actions, t, msgs, last, quiet):
    if msgs[-1]["outgoing"] or last["bulk"] or not last["in_inbox"] or t.get("action") != "antwoorden":
        return
    if last["from_addr"] in quiet or not _new(db, "drafts", last):
        return
    key = last["message_id"] or str(last["id"])
    if done_before(db, "concept", t["tid"], key):
        return
    text = ai.draft(t["tid"])
    res = actions.save_draft(db, acc, t["tid"], text)
    record(db, "concept", acc.id, t["tid"], t.get("subject") or last["subject"] or "(geen onderwerp)",
           f"Concept voor {last['from_name'] or last['from_addr']} in {res['folder']}",
           {"answers": key, "draft_id": res.get("message_id"), "folder": res["folder"]}, undo=True)
    log.info("%s: concept klaargezet voor thread %s", acc.id, t["tid"])


# --- opruimen: wat de opruimregels deden ------------------------------------------
def record_cleanup(db, acc, rows, role, dest):
    """Eén regel in het logboek per opruimbeurt, met de berichten om terug te zetten."""
    if not rows:
        return
    subjects = [r["subject"] or "(geen onderwerp)" for r in rows[:3]]
    more = f" en {len(rows) - 3} meer" if len(rows) > 3 else ""
    word = "gearchiveerd" if role == "archive" else "naar de prullenbak"
    record(db, "opruimen", acc.id, None, f"{len(rows)} reclame en meldingen {word}", ", ".join(subjects) + more,
           {"message_ids": [r["message_id"] for r in rows if r["message_id"]], "folder": dest}, undo=True)


# --- terugdraaien ---------------------------------------------------------------
def undo(db, accounts, entry_id):
    e = db.one("SELECT * FROM auto_log WHERE id=?", (entry_id,))
    if not e or not e["can_undo"] or e["undone_at"]:
        raise ValueError("Dit kan niet (meer) teruggedraaid worden.")
    acc = accounts.get(e["account"])
    if not acc:
        raise ValueError("Onbekend account.")
    data = json.loads(e["data"] or "{}")
    if e["kind"] == "opruimen":
        restore(acc, data.get("folder"), data.get("message_ids") or [])
    elif e["kind"] == "concept":
        delete_draft(acc, data.get("folder"), data.get("draft_id"))
    db.x("UPDATE auto_log SET undone_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), entry_id))
    acc.wake.set()


def restored(db):
    """Message-ID's die je na het opruimen terugzette: die ruimt Butler niet opnieuw op."""
    out = set()
    for r in db.q("SELECT data FROM auto_log WHERE kind='opruimen' AND undone_at IS NOT NULL"):
        out |= set(json.loads(r["data"] or "{}").get("message_ids") or [])
    return out


def _find(c, mids):
    uids = []
    for mid in mids:
        uids += c.search(["HEADER", "Message-ID", mid])
    return uids


def restore(acc, folder, mids):
    """Terug naar de inbox. Bij Gmail is het archief 'Alle e-mail': daar volstaat het label Inbox (kopiëren)."""
    if not folder or not mids:
        return

    def fn(c):
        c.select_folder(folder)
        uids = _find(c, mids)
        if not uids:
            return
        if folder.startswith("[Gmail]/All") or not c.has_capability("MOVE"):
            c.copy(uids, "INBOX")
        else:
            c.move(uids, "INBOX")
    acc.run_imap(fn)


def delete_draft(acc, folder, draft_id):
    """Het concept dat Butler klaarzette weer weghalen (enkel dat ene concept, op zijn Message-ID)."""
    if not folder or not draft_id:
        return

    def fn(c):
        c.select_folder(folder)
        uids = _find(c, [draft_id])
        if uids:
            c.delete_messages(uids)
            c.expunge(uids)
    acc.run_imap(fn)
