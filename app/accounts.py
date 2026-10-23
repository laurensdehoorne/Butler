import logging
import threading

from imapclient import IMAPClient

log = logging.getLogger("butler.accounts")

PRESETS = {
    "gmail": dict(imap_host="imap.gmail.com", smtp_host="smtp.gmail.com", smtp_port=587, append_sent=False),
    "icloud": dict(imap_host="imap.mail.me.com", smtp_host="smtp.mail.me.com", smtp_port=587, append_sent=True),
}

FALLBACK = {
    "sent": ["Sent Messages", "Sent", "Sent Items", "[Gmail]/Sent Mail", "[Gmail]/Verzonden berichten", "Verzonden", "Verzonden items"],
    "archive": ["Archive", "Archief", "[Gmail]/All Mail", "[Gmail]/Alle berichten"],
    "drafts": ["Drafts", "[Gmail]/Drafts", "[Gmail]/Concepten", "Concepten", "Draft"],
    "trash": ["Deleted Messages", "Trash", "Deleted Items", "[Gmail]/Trash", "[Gmail]/Prullenbak", "Prullenbak", "Verwijderde items"],
}


class Account:
    def __init__(self, cfg):
        provider = cfg.get("provider", "custom")
        p = PRESETS.get(provider, {})
        self.id = str(cfg["id"])
        self.name = cfg.get("name", self.id)
        self.email = cfg["email"].lower()
        self.display_name = cfg.get("display_name", "")
        self.aliases = {a.lower() for a in cfg.get("aliases", [])} | {self.email}
        self.user = cfg.get("username", cfg["email"])
        self.password = cfg["password"]
        self.imap_host = cfg.get("imap_host", p.get("imap_host"))
        self.imap_port = int(cfg.get("imap_port", 993))
        self.smtp_host = cfg.get("smtp_host", p.get("smtp_host"))
        self.smtp_port = int(cfg.get("smtp_port", p.get("smtp_port", 587)))
        self.smtp_user = cfg.get("smtp_username", cfg["email"])
        self.append_sent = bool(cfg.get("append_sent", p.get("append_sent", True)))
        self.provider = provider
        self.gmail = provider == "gmail"
        self.ai = bool(cfg.get("ai", True))
        self.folders = {}
        self.status = "starten"
        self.error = None
        self.last_sync = None
        self.backlog = {}  # map -> aantal oude berichten dat nog moet volgen (dagbudget)
        self.wake = threading.Event()

    def connect(self):
        c = IMAPClient(self.imap_host, port=self.imap_port, ssl=True, timeout=90)
        c.normalise_times = False
        c.login(self.user, self.password)
        if not self.folders:
            self.resolve_folders(c)
        return c

    def resolve_folders(self, c):
        lst = c.list_folders()
        names = [n for _, _, n in lst]

        def by_flag(flag):
            for flags, _, n in lst:
                if flag in flags:
                    return n

        def by_name(cands):
            low = {n.lower(): n for n in names}
            for cand in cands:
                if cand.lower() in low:
                    return low[cand.lower()]

        f = {"inbox": "INBOX"}
        f["sent"] = by_flag(b"\\Sent") or by_name(FALLBACK["sent"])
        f["trash"] = by_flag(b"\\Trash") or by_name(FALLBACK["trash"])
        f["drafts"] = by_flag(b"\\Drafts") or by_name(FALLBACK["drafts"])
        if self.gmail:
            f["archive"] = by_flag(b"\\All") or by_name(FALLBACK["archive"])
        else:
            f["archive"] = by_flag(b"\\Archive") or by_name(FALLBACK["archive"])
        if not f["archive"]:
            c.create_folder("Archive")
            f["archive"] = "Archive"
        self.folders = f
        log.info("%s: mappen %s", self.id, f)

    def is_me(self, addr):
        return (addr or "").lower() in self.aliases

    def run_imap(self, fn):
        """Korte, aparte verbinding voor acties vanuit de webinterface."""
        c = self.connect()
        try:
            return fn(c)
        finally:
            try:
                c.logout()
            except Exception:
                pass

    def public(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "provider": self.provider,
            "status": self.status,
            "error": self.error,
            "last_sync": self.last_sync,
            "backlog": sum(self.backlog.values()),
            "ai": self.ai,
        }
