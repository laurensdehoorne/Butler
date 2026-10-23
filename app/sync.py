import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from imapclient import FLAGGED, SEEN

from .db import sender_rule
from .mailparse import parse

log = logging.getLogger("butler.sync")

MAX_FULL_SIZE = 5 * 1024 * 1024  # groter → enkel headers
CHUNK = 40
MOMENT_DAYS = 365  # gemarkeerde mails ouder dan dit gaan niet meer naar de AI voor momenten
HEADER_COST = 20_000  # een bericht boven MAX_FULL_SIZE kost enkel zijn headers


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
        self.sync_days = int(app_cfg.get("sync_days", 0))  # 0 = alle mail
        self.archive = bool(app_cfg.get("sync_archive", True))  # ook Alle e-mail (Gmail) / Archief (iCloud)
        # oude mail ophalen (archief, verzonden) spreiden over dagen: Gmail laat ~2,5 GB IMAP-download per dag toe
        self.daily_bytes = int(app_cfg.get("sync_daily_mb", 1000) or 0) * 1024 * 1024
        self.tz = ZoneInfo(app_cfg.get("timezone", "Europe/Brussels"))
        self._triage_tried = set()  # inhaal-triage: één poging per thread per opstart
        self.poll = int(app_cfg.get("poll_seconds", 300))
        self.ai_initial_days = int(ai_cfg.get("initial_days", 7))
        cl = app_cfg.get("cleanup") or {}
        # standaard voor afzenders zonder eigen regel: na X dagen archiveren (0 = uit)
        self.cleanup = {"notificatie": int(cl.get("notificatie_days", 0) or 0),
                        "reclame": int(cl.get("reclame_days", 0) or 0)}

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
        self.apply_rules(c, acc)
        self.queue_triage(acc)
        if acc.folders.get("sent"):
            self.sync_folder(c, acc, "sent")
        if self.archive and acc.folders.get("archive"):
            self.sync_folder(c, acc, "archive")
        self.queue_moments(acc)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(self.sync_days, 60) * 2)).isoformat()
        self.db.x("DELETE FROM messages WHERE account=? AND folder IN ('_weg','_pending') AND date < ?", (acc.id, cutoff))
        acc.last_sync = datetime.now(timezone.utc).isoformat()

    # --- opruimregels --------------------------------------------------
    def plan_cleanup(self, acc):
        """Welke inbox-berichten moeten volgens de regels weg? → {'archive': [(id, uid)], 'trash': [...]}"""
        rules = {r["addr"]: r for r in self.db.q("SELECT * FROM sender_rules WHERE account=?", (acc.id,))}
        now = datetime.now(timezone.utc)
        plan = {"archive": [], "trash": []}
        rows = self.db.q(
            "SELECT id, uid, from_addr, kind, date FROM messages WHERE account=? AND folder='INBOX' AND uid>0 AND outgoing=0",
            (acc.id,),
        )
        for m in rows:
            try:
                age = (now - datetime.fromisoformat(m["date"])).total_seconds() / 86400
            except Exception:
                age = 0
            r = rules.get(m["from_addr"])
            act = r["action"] if r else "none"
            if act == "keep":
                continue
            if act in ("archive", "trash"):
                plan[act].append((m["id"], m["uid"]))
            elif act == "archive_after" and age >= (r["after_days"] or 7):
                plan["archive"].append((m["id"], m["uid"]))
            elif act == "none":
                days = self.cleanup.get(m["kind"] or "", 0)
                if days and age >= days:
                    plan["archive"].append((m["id"], m["uid"]))
        return plan

    def apply_rules(self, c, acc):
        plan = self.plan_cleanup(acc)
        if not (plan["archive"] or plan["trash"]):
            return
        c.select_folder("INBOX", readonly=False)
        for role, items in plan.items():
            dest = acc.folders.get(role)
            if not items or not dest:
                continue
            uids = [u for _, u in items]
            if c.has_capability("MOVE"):
                c.move(uids, dest)
            else:
                c.copy(uids, dest)
                c.delete_messages(uids)
                c.expunge(uids)
            self.db.xm("UPDATE messages SET in_inbox=0, folder='_weg', uid=-id WHERE id=?", [(i,) for i, _ in items])
            log.info("%s: %d berichten automatisch naar %s (opruimregels)", acc.id, len(items), dest)

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

        if self.sync_days:  # venster, maar gemarkeerde mail blijft altijd (vlucht van maanden geleden geboekt)
            since = date.today() - timedelta(days=self.sync_days)
            current = set(c.search(["OR", "FLAGGED", ["SINCE", since]]))
        else:
            current = set(c.search(["ALL"]))
        known = {r["uid"] for r in self.db.q("SELECT uid FROM messages WHERE account=? AND folder=?", (acc.id, folder))}

        gone = known - current
        if gone:
            # weg uit deze map (gearchiveerd/verwijderd elders): geschiedenis bewaren, uit inbox halen
            self.db.xm(
                "UPDATE messages SET in_inbox=0, folder='_weg', uid=-id WHERE account=? AND folder=? AND uid=?",
                [(acc.id, folder, u) for u in gone],
            )

        # nieuwste eerst: bij de eerste sync van een groot archief verschijnt het recente meteen
        new = sorted(current - known, reverse=True)
        flagged = set(c.search(["FLAGGED"])) & current
        if role != "inbox":
            # nieuw binnengekomen mail altijd; oude mail binnen het dagbudget, gemarkeerde eerst
            fresh = [u for u in new if initialized and u > last_uid]
            old, left = self.take_budget(c, acc, [u for u in new if u not in set(fresh)], flagged)
            new = fresh + old
            acc.backlog[folder] = left
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

        # vlaggen (Spark, Gmail-ster): de server zoekt ze op, zodat niet elk bericht opgehaald hoeft te worden
        was = {r["uid"] for r in self.db.q("SELECT uid FROM messages WHERE account=? AND folder=? AND flagged=1",
                                           (acc.id, folder))}
        if flagged != was:
            self.db.xm("UPDATE messages SET flagged=? WHERE account=? AND folder=? AND uid=?",
                       [(1, acc.id, folder, u) for u in flagged - was] + [(0, acc.id, folder, u) for u in was - flagged])

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
            tid, outgoing = self.store(acc, folder, role, uid, p, seen, thrid, flagged=FLAGGED in m.get(b"FLAGS", ()))

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

    def store(self, acc, folder, role, uid, p, seen, thrid=None, flagged=False):
        # van jou, ook vanuit een ander account (Gmail naar je eigen iCloud)
        outgoing = any(a.is_me(p["from_addr"]) for a in self.accounts.values())
        rule = sender_rule(self.db, acc.id, p["from_addr"])
        if rule and rule["kind"] is not None:  # handmatige correctie per afzender
            p["kind"] = rule["kind"]
            p["bulk"] = bool(rule["kind"])
        if outgoing:
            p["kind"], p["bulk"] = "", False
        key = self.thread_key_for(acc, folder, uid, p, thrid)
        if p["message_id"]:
            self.db.x("DELETE FROM messages WHERE account=? AND folder='_pending' AND message_id=?",
                      (acc.id, p["message_id"]))
        self.db.x(
            """INSERT OR REPLACE INTO messages(account, folder, uid, message_id, thread_key, refs, from_addr, from_name,
               to_addrs, cc_addrs, reply_to, subject, date, snippet, body, seen, outgoing, in_inbox, bulk,
               kind, unsub, unsub_post, flagged, html)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc.id, folder, uid, p["message_id"], key, " ".join(p["refs"]), p["from_addr"], p["from_name"],
             json.dumps(p["to"]), json.dumps(p["cc"]), p["reply_to"], p["subject"], p["date"], p["snippet"],
             p["body"], 1 if (seen or outgoing) else 0, 1 if outgoing else 0, 1 if role == "inbox" else 0,
             1 if p["bulk"] else 0, p["kind"], p.get("unsub", ""), 1 if p.get("unsub_post") else 0, 1 if flagged else 0,
             # HTML enkel voor de inbox bewaren (opent meteen); de rest wordt opgehaald wanneer je de mail opent
             p.get("html") if role == "inbox" else None),
        )
        self.db.x("INSERT OR IGNORE INTO threads(account, thread_key) VALUES(?,?)", (acc.id, key))
        tid = self.db.one("SELECT tid FROM threads WHERE account=? AND thread_key=?", (acc.id, key))["tid"]
        return tid, outgoing

    # --- dagbudget voor oude mail -----------------------------------------
    def _budget(self, acc):
        today = datetime.now(self.tz).date().isoformat()
        row = self.db.one("SELECT value FROM settings WHERE key=?", (f"sync_budget:{acc.id}",))
        d = json.loads(row["value"]) if row else {}
        return today, (d.get("bytes", 0) if d.get("day") == today else 0)

    def take_budget(self, c, acc, uids, priority=()):
        """Welke oude berichten passen vandaag nog in het budget? → (uids, aantal dat moet wachten)."""
        if not self.daily_bytes or not uids:
            return uids, 0
        today, used = self._budget(acc)
        left = self.daily_bytes - used
        order = [u for u in uids if u in priority] + [u for u in uids if u not in priority]
        out, spent = [], 0
        for chunk in _chunks(order, 500):
            sizes = c.fetch(chunk, ["RFC822.SIZE"])
            for u in chunk:
                size = sizes.get(u, {}).get(b"RFC822.SIZE", 0)
                cost = size if size <= MAX_FULL_SIZE else HEADER_COST
                if spent + cost > left:
                    break
                out.append(u)
                spent += cost
            else:
                continue
            break
        if spent:
            self.db.x("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                      (f"sync_budget:{acc.id}", json.dumps({"day": today, "bytes": used + spent})))
        if len(out) < len(uids):
            log.info("%s: dagbudget op, %d oude berichten volgen later", acc.id, len(uids) - len(out))
        return out, len(uids) - len(out)

    # --- inhaal-triage --------------------------------------------------------
    def queue_triage(self, acc):
        """Inbox-threads die nog nooit door de AI gingen, bv. omdat de AI pas na de eerste sync aangezet werd.

        Zonder triage telt een thread als "wacht op jou"; de inbox is klein, dus dit kost weinig."""
        if not self.ai.active_for(acc.id):
            return
        rows = self.db.q(
            """SELECT t.tid FROM threads t JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
               WHERE t.account=? AND (t.ai_count=0 OR t.dossier_ai=0) GROUP BY t.tid
               HAVING SUM(m.in_inbox) > 0 AND MIN(m.bulk) = 0 ORDER BY MAX(m.date) DESC LIMIT 300""",
            (acc.id,),
        )
        for r in rows:
            if r["tid"] not in self._triage_tried:
                self._triage_tried.add(r["tid"])
                self.ai.enqueue(r["tid"])

    # --- momenten uit gemarkeerde mail -------------------------------------
    def queue_moments(self, acc):
        """Gemarkeerde threads met nieuwe berichten naar de AI; vlag weg = momenten weg."""
        since = (datetime.now(timezone.utc) - timedelta(days=MOMENT_DAYS)).isoformat()
        rows = self.db.q(
            """SELECT t.tid, t.moments_ai, t.dossier_ai, COUNT(DISTINCT COALESCE(m.message_id, 'row' || m.id)) AS n,
                      MAX(m.flagged) AS flagged, MAX(m.date) AS last
               FROM threads t JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
               WHERE t.account=? GROUP BY t.tid HAVING MAX(m.flagged)=1 OR t.moments_ai>0""",
            (acc.id,),
        )
        for r in rows:
            if not r["flagged"]:
                self.db.x("DELETE FROM moments WHERE tid=?", (r["tid"],))
                self.db.x("UPDATE threads SET moments_ai=0 WHERE tid=?", (r["tid"],))
            elif (r["moments_ai"] != r["n"] or r["dossier_ai"] == 0) and r["last"] >= since and self.ai.active_for(acc.id):
                self.ai.enqueue_moments(r["tid"])
