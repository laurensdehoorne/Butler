"""Meldingen die je steeds negeert, stoppen vanzelf (naar de richtlijnen voor meldingen: stilte is beter dan dat je
alle meldingen uitzet).

Elke melding over nieuwe mail komt in notify_log. Lees je die mail niet binnen een dag (in Butler of in Spark: de
gelezen-vlag komt van de server) en staat ze nog in je inbox, dan telt ze als genegeerd. Na IGNORED_MAX genegeerde
meldingen op rij van dezelfde afzender krijg je van die afzender geen meldingen meer; de mail zelf blijft gewoon
binnenkomen. Weer aanzetten kan in de mail (Meer) of in Instellingen → Meldingen.

sender_rules.notify: NULL = gewoon, 0 = stil (door jou of door Butler), 1 = prioritair: altijd een melding, ook voor
mail die de AI niet belangrijk vindt en tijdens de stille uren (en Butler zet ze nooit stil).
"""

from datetime import datetime, timedelta, timezone

IGNORED_MAX = 5


def record(db, account, addr, tid):
    db.x("INSERT INTO notify_log(at, account, addr, tid) VALUES(?,?,?,?)",
         (datetime.now(timezone.utc).isoformat(), account, (addr or "").lower(), tid))


def muted(db, account, addr):
    r = db.one("SELECT notify FROM sender_rules WHERE account=? AND addr=?", (account, (addr or "").lower()))
    return bool(r) and r["notify"] == 0


def _ignored(db, row):
    """Na een dag nog ongelezen en nog in de inbox."""
    m = db.one("""SELECT MIN(m.seen) AS seen, MAX(m.in_inbox) AS inbox FROM threads t
                  JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                  WHERE t.tid=? AND m.outgoing=0 AND m.date <= ?""", (row["tid"], row["at"]))
    return bool(m) and m["seen"] == 0 and m["inbox"] == 1


def check(db):
    """Afzenders met IGNORED_MAX genegeerde meldingen op rij stil zetten. Geeft hun adressen terug."""
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    out = []
    for s in db.q("""SELECT account, addr FROM notify_log WHERE at <= ? GROUP BY account, addr HAVING COUNT(*) >= ?""",
                  (day_ago, IGNORED_MAX)):
        rule = db.one("SELECT notify FROM sender_rules WHERE account=? AND addr=?", (s["account"], s["addr"]))
        if rule and rule["notify"] is not None:
            continue  # je koos zelf (stil of altijd)
        last = db.q("""SELECT tid, at FROM notify_log WHERE account=? AND addr=? AND at <= ? ORDER BY at DESC LIMIT ?""",
                    (s["account"], s["addr"], day_ago, IGNORED_MAX))
        if len(last) == IGNORED_MAX and all(_ignored(db, r) for r in last):
            db.x("INSERT OR IGNORE INTO sender_rules(account, addr) VALUES(?,?)", (s["account"], s["addr"]))
            db.x("UPDATE sender_rules SET notify=0, notify_auto=1 WHERE account=? AND addr=?", (s["account"], s["addr"]))
            out.append(s["addr"])
    db.x("DELETE FROM notify_log WHERE at < ?", ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),))
    return out


def priority(db, account, addr):
    r = db.one("SELECT notify FROM sender_rules WHERE account=? AND addr=?", (account, (addr or "").lower()))
    return bool(r) and r["notify"] == 1


def muted_list(db):
    return [dict(r) for r in db.q("""SELECT account, addr, notify_auto FROM sender_rules WHERE notify=0 ORDER BY addr""")]


def priority_list(db):
    return [dict(r) for r in db.q("""SELECT r.account, r.addr, (SELECT MAX(m.from_name) FROM messages m
                                     WHERE m.account=r.account AND m.from_addr=r.addr) AS name
                                     FROM sender_rules r WHERE r.notify=1 ORDER BY r.addr""")]
