import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  folder TEXT NOT NULL,
  uid INTEGER NOT NULL,
  message_id TEXT,
  thread_key TEXT,
  refs TEXT,
  from_addr TEXT,
  from_name TEXT,
  to_addrs TEXT,
  cc_addrs TEXT,
  reply_to TEXT,
  subject TEXT,
  date TEXT,
  snippet TEXT,
  body TEXT,
  seen INTEGER DEFAULT 0,
  outgoing INTEGER DEFAULT 0,
  in_inbox INTEGER DEFAULT 0,
  bulk INTEGER DEFAULT 0,
  kind TEXT DEFAULT '',
  unsub TEXT DEFAULT '',
  unsub_post INTEGER DEFAULT 0,
  UNIQUE(account, folder, uid)
);
CREATE INDEX IF NOT EXISTS ix_m_thread ON messages(account, thread_key);
CREATE INDEX IF NOT EXISTS ix_m_mid ON messages(account, message_id);
CREATE INDEX IF NOT EXISTS ix_m_inbox ON messages(in_inbox);

CREATE TABLE IF NOT EXISTS threads(
  tid INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  thread_key TEXT NOT NULL,
  summary TEXT,
  priority TEXT,
  category TEXT,
  action TEXT,
  deadline TEXT,
  ai_count INTEGER DEFAULT 0,
  tags TEXT DEFAULT '[]',
  snooze_until TEXT,
  done INTEGER DEFAULT 0,
  UNIQUE(account, thread_key)
);

CREATE TABLE IF NOT EXISTS sender_rules(
  account TEXT NOT NULL,
  addr TEXT NOT NULL,
  kind TEXT,                 -- NULL = automatische herkenning; '' = persoonlijk; 'reclame' | 'notificatie'
  action TEXT DEFAULT 'none',-- none | keep | archive | trash | archive_after
  after_days INTEGER DEFAULT 7,
  unsubscribed_at TEXT,
  PRIMARY KEY(account, addr)
);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS folder_state(
  account TEXT NOT NULL,
  folder TEXT NOT NULL,
  uidvalidity INTEGER,
  last_uid INTEGER DEFAULT 0,
  initialized INTEGER DEFAULT 0,
  PRIMARY KEY(account, folder)
);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            # migratie van oudere databases (vóór reclame/notificaties)
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
            if have and "kind" not in have:
                for col in ("kind TEXT DEFAULT ''", "unsub TEXT DEFAULT ''", "unsub_post INTEGER DEFAULT 0"):
                    self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col}")
                self.conn.execute("UPDATE messages SET kind='reclame' WHERE bulk=1")
            self.conn.executescript(SCHEMA)
            self.conn.execute("CREATE INDEX IF NOT EXISTS ix_m_from ON messages(account, from_addr)")

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def xm(self, sql, seq):
        with self.lock:
            self.conn.executemany(sql, seq)


def thread_messages(db, tid):
    """Thread-meta + berichten (oud → nieuw), ontdubbeld op Message-ID."""
    t = db.one("SELECT * FROM threads WHERE tid=?", (tid,))
    if not t:
        return None, []
    rows = db.q(
        "SELECT * FROM messages WHERE account=? AND thread_key=? ORDER BY date ASC, id ASC",
        (t["account"], t["thread_key"]),
    )
    index, out = {}, []
    for r in rows:
        r = dict(r)
        k = r["message_id"] or f"row{r['id']}"
        if k in index:
            if r["in_inbox"] and not out[index[k]]["in_inbox"]:
                out[index[k]] = r
            continue
        index[k] = len(out)
        out.append(r)
    return dict(t), out


def sender_rule(db, account, addr):
    r = db.one("SELECT * FROM sender_rules WHERE account=? AND addr=?", (account, (addr or "").lower()))
    return dict(r) if r else None


def upsert_rule(db, account, addr, **fields):
    addr = addr.lower()
    db.x("INSERT OR IGNORE INTO sender_rules(account, addr) VALUES(?,?)", (account, addr))
    for k, v in fields.items():
        if k in ("kind", "action", "after_days", "unsubscribed_at"):
            db.x(f"UPDATE sender_rules SET {k}=? WHERE account=? AND addr=?", (v, account, addr))
    return sender_rule(db, account, addr)
