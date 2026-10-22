import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

from imapclient import SEEN

from .mailparse import parse

log = logging.getLogger("postvak.sync")

MAX_FULL_SIZE = 5 * 1024 * 1024  # groter → enkel headers
CHUNK = 40


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _internaldate(v):
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


class Syncer:
    def __init__(self, db, app_cfg, ai_cfg, accounts, ai_worker, notifier):
        self.db = db
        self.accounts = accounts
        self.ai = ai_worker
        self.notifier = notifier
        self.sync_days = int(app_cfg.get("sync_days", 60))
        self.poll = int(app_cfg.get("poll_seconds", 300))
        self.ai_initial_days = int(ai_cfg.get("initial_days", 7))

    def start(self):
        for acc in self.accounts.values():
            threading.Thread(target=self.run, args=(acc,), daemon=True, name=f"sync-{acc.id}").start()

    # --- lus per account ------------------------------------------------
    def run(self, acc):
        backoff = 10
        while True:
            c = None
            try:
                c = acc.connect()
                acc.status, acc.error, backoff = "verbonden", None, 10
                while True:
                    self.sync_account(c, acc)
                    self.wait_for_mail(c, acc)
            except Exception as e:
                acc.status, acc.error = "fout", str(e)[:300]
                log.warning("%s: %s — opnieuw over %ss", acc.id, e, backoff)
                try:
                    if c:
                        c.logout()
                except Exception:
                    pass
                acc.wake.wait(backoff)
                acc.wake.clear()
                backoff = min(backoff * 2, 300)

    def wait_for_mail(self, c, acc):
        c.select_folder("INBOX", readonly=True)
        if c.has_capability("IDLE"):
            c.idle()
            try:
                deadline = time.time() + self.poll
                while time.time() < deadline and not acc.wake.is_set():
                    if c.idle_check(timeout=15):
                        break
            finally:
                c.idle_done()
        else:
            acc.wake.wait(self.poll)
        acc.wake.clear()

    def sync_account(self, c, acc):
        self.sync_folder(c, acc, "inbox")
        if acc.folders.get("sent"):
            self.sync_folder(c, acc, "sent")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.sync_days * 2)).isoformat()
        self.db.x("DELETE FROM messages WHERE account=? AND folder IN ('_weg','_pending') AND date < ?", (acc.id, cutoff))
        acc.last_sync = datetime.now(timezone.utc).isoformat()

    # --- map synchroniseren ---------------------------------------------
    def sync_folder(self, c, acc, role):
        folder = acc.folders[role]
        info = c.select_folder(folder, readonly=True)
        uv = int(info.get(b"UIDVALIDITY", 0))
        st = self.db.one("SELECT * FROM folder_state WHERE account=? AND folder=?", (acc.id, folder))
        if not st or st["uidvalidity"] != uv:
            self.db.x("DELETE FROM messages WHERE account=? AND folder=?", (acc.id, folder))
            self.db.x(
                "INSERT OR REPLACE INTO folder_state(account, folder, uidvalidity, last_uid, initialized) VALUES(?,?,?,0,0)",
                (acc.id, folder, uv),
            )
            last_uid, initialized = 0, False
        else:
            last_uid, initialized = st["last_uid"], bool(st["initialized"])

        since = date.today() - timedelta(days=self.sync_days)
        current = set(c.search(["SINCE", since]))
        known = {r["uid"] for r in self.db.q("SELECT uid FROM messages WHERE account=? AND folder=?", (acc.id, folder))}

        gone = known - current
        if gone:
            # weg uit deze map (gearchiveerd/verwijderd elders): geschiedenis bewaren, uit inbox halen
            self.db.xm(
                "UPDATE messages SET in_inbox=0, folder='_weg', uid=-id WHERE account=? AND folder=? AND uid=?",
                [(acc.id, folder, u) for u in gone],
            )

        new = sorted(current - known)
        triage = {}
        for chunk in _chunks(new, CHUNK):
            self.fetch_store(c, acc, folder, role, chunk, initialized, last_uid, triage)

        if role == "inbox":
            still = sorted(known & current)[-500:]
            if still:
                flags = c.fetch(still, ["FLAGS"])
                self.db.xm(
                    "UPDATE messages SET seen=? WHERE account=? AND folder=? AND uid=?",
                    [(1 if SEEN in d.get(b"FLAGS", ()) else 0, acc.id, folder, u) for u, d in flags.items()],
                )

        for tid, notify in triage.items():
            if self.ai.active_for(acc.id):
                self.ai.enqueue(tid, notify)

        top = max(current) if current else 0
        self.db.x(
            "UPDATE folder_state SET last_uid=?, initialized=1 WHERE account=? AND folder=?",
            (max(top, last_uid), acc.id, folder),
        )
        if new:
            log.info("%s/%s: %d nieuwe berichten", acc.id, folder, len(new))

    def fetch_store(self, c, acc, folder, role, uids, initialized, last_uid, triage):
        items = ["RFC822.SIZE", "FLAGS", "INTERNALDATE"] + (["X-GM-THRID"] if acc.gmail else [])
        meta = c.fetch(uids, items)
        small = [u for u, d in meta.items() if d.get(b"RFC822.SIZE", 0) <= MAX_FULL_SIZE]
        big = [u for u in meta if u not in small]
        raw = {}
        if small:
            for u, d in c.fetch(small, ["BODY.PEEK[]"]).items():
                raw[u] = (d.get(b"BODY[]"), False)
        if big:
            for u, d in c.fetch(big, ["BODY.PEEK[HEADER]"]).items():
                raw[u] = (d.get(b"BODY[HEADER]"), True)

        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=self.ai_initial_days)).isoformat()
        for uid in uids:
            data, header_only = raw.get(uid, (None, False))
            if not data:
                continue
            try:
                p = parse(data, header_only)
            except Exception:
                log.exception("%s: kon bericht %s niet lezen", acc.id, uid)
                continue
            m = meta.get(uid, {})
            if not p["date"]:
                p["date"] = _internaldate(m.get(b"INTERNALDATE"))
            seen = SEEN in m.get(b"FLAGS", ())
            thrid = m.get(b"X-GM-THRID")
            tid, outgoing = self.store(acc, folder, role, uid, p, seen, thrid)

            arrival = initialized and role == "inbox" and uid > last_uid and not outgoing
            if arrival:
                # nieuwe activiteit: thread terug openzetten
                self.db.x("UPDATE threads SET done=0, snooze_until=NULL WHERE tid=?", (tid,))
            if role != "inbox" or outgoing or p["bulk"]:
                continue
            if arrival and not seen:
                if self.ai.active_for(acc.id):
                    triage[tid] = True
                elif self.notifier.mode != "off":  # geen AI voor deze mailbox → elke persoonlijke mail melden
                    self.notifier.send(f"{acc.name}: {p['from_name']}", p["subject"] + "\n" + p["snippet"][:200],
                                       tid=tid, extra={"account": acc.id, "from": p["from_addr"], "subject": p["subject"]})
            elif arrival or (not initialized and p["date"] >= recent_cutoff):
                triage.setdefault(tid, False)

    def thread_key_for(self, acc, folder, uid, p, thrid):
        if thrid:
            return f"gm:{thrid}"
        cands = list(p["refs"]) + ([p["in_reply_to"]] if p["in_reply_to"] else [])
        if cands:
            ph = ",".join("?" * len(cands))
            r = self.db.one(
                f"SELECT thread_key FROM messages WHERE account=? AND message_id IN ({ph}) LIMIT 1",
                (acc.id, *cands),
            )
            if r and r["thread_key"]:
                return r["thread_key"]
        if p["message_id"]:
            r = self.db.one(
                "SELECT thread_key FROM messages WHERE account=? AND (refs LIKE ? OR message_id=?) LIMIT 1",
                (acc.id, f"%{p['message_id']}%", p["message_id"]),
            )
            if r and r["thread_key"]:
                return r["thread_key"]
        return (p["refs"][0] if p["refs"] else p["in_reply_to"]) or p["message_id"] or f"uid:{acc.id}:{folder}:{uid}"

    def store(self, acc, folder, role, uid, p, seen, thrid=None):
        outgoing = acc.is_me(p["from_addr"])
        key = self.thread_key_for(acc, folder, uid, p, thrid)
        if p["message_id"]:
            self.db.x("DELETE FROM messages WHERE account=? AND folder='_pending' AND message_id=?",
                      (acc.id, p["message_id"]))
        self.db.x(
            """INSERT OR REPLACE INTO messages(account, folder, uid, message_id, thread_key, refs, from_addr, from_name,
               to_addrs, cc_addrs, reply_to, subject, date, snippet, body, seen, outgoing, in_inbox, bulk)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc.id, folder, uid, p["message_id"], key, " ".join(p["refs"]), p["from_addr"], p["from_name"],
             json.dumps(p["to"]), json.dumps(p["cc"]), p["reply_to"], p["subject"], p["date"], p["snippet"],
             p["body"], 1 if (seen or outgoing) else 0, 1 if outgoing else 0, 1 if role == "inbox" else 0,
             1 if (p["bulk"] and not outgoing) else 0),
        )
        self.db.x("INSERT OR IGNORE INTO threads(account, thread_key) VALUES(?,?)", (acc.id, key))
        tid = self.db.one("SELECT tid FROM threads WHERE account=? AND thread_key=?", (acc.id, key))["tid"]
        return tid, outgoing
