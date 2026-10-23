"""Afzenderfoto's: een logo per afzenderdomein (BIMI of favicon) en Gravatar voor personen.

De server haalt ze op en bewaart ze in /data/avatars; het toestel laadt nooit iets van buiten. De API geeft enkel een
/avatars/-adres terug voor wat al in de cache staat, en zet ontbrekende afzenders in een wachtrij die een
achtergronddraad afwerkt: de foto verschijnt dan bij de volgende keer laden. Wat niet gevonden wordt, krijgt een
markering en wordt pas na RETRY_DAYS opnieuw geprobeerd.

Bestandsnamen zijn een HMAC van adres of domein met een eigen geheime sleutel (avatars/.key), zodat niemand door een
naam te raden kan nagaan of je mail van iemand kreeg. Omdat het domein uit de mail komt, haalt Secretaresse enkel iets op bij
publieke adressen, nooit bij een toestel in je eigen netwerk.
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import queue
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger("butler.avatars")

# Gratis mailboxen: het logo van zo'n domein zegt niets over de afzender
FREEMAIL = {
    "gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com", "outlook.com", "outlook.be", "hotmail.com",
    "hotmail.be", "live.com", "live.be", "msn.com", "yahoo.com", "yahoo.fr", "ymail.com", "aol.com", "gmx.com",
    "gmx.net", "proton.me", "protonmail.com", "pm.me", "telenet.be", "skynet.be", "proximus.be", "scarlet.be",
    "orange.fr", "free.fr", "web.de", "zoho.com", "fastmail.com", "hey.com", "mail.com",
}
TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg",
         "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico"}
MEDIA = {ext: t for t, ext in TYPES.items()}
MAX_IMAGE = 512 * 1024
MAX_HTML = 1024 * 1024
RETRY_DAYS = 14
NAME_RE = re.compile(r"^[0-9a-f]{32}-(?:logo|face)\.(?:png|jpg|gif|webp|svg|ico)$")
UA = "Secretaresse/1.0 (self-hosted mailassistent; afzenderlogo's)"


class NotPublic(Exception):
    pass


def check_public(request):
    """httpx-hook voor elke aanvraag (ook na een doorverwijzing): enkel publieke IP-adressen."""
    host = request.url.host
    try:
        infos = socket.getaddrinfo(host, request.url.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise NotPublic(host) from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise NotPublic(host)


def domains(domain):
    """Het afzenderdomein en, bij een subdomein (nieuws.firma.be), ook het hoofddomein."""
    parts = domain.split(".")
    n = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") else 2
    return list(dict.fromkeys([domain, ".".join(parts[-n:])]))


def icon_links(html, base):
    """Pictogrammen uit een webpagina, het beste eerst: apple-touch-icon, dan de grootste, dan svg."""
    from bs4 import BeautifulSoup

    found = []
    for link in BeautifulSoup(html, "html.parser").find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "icon" not in rel or "mask-icon" in rel:
            continue
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", link.get("sizes") or "")]
        size = max(sizes) if sizes else (512 if (link.get("type") or "").endswith("svg+xml") else 32)
        found.append(("apple-touch-icon" in rel, size, urljoin(base, link["href"])))
    found.sort(key=lambda f: (f[0], f[1]), reverse=True)
    return [url for _, _, url in found if url.startswith("https://")]


class Avatars:
    def __init__(self, data_dir, cfg=None, background=True):
        cfg = cfg or {}
        self.logos = bool(cfg.get("logos", True))
        self.gravatar = bool(cfg.get("gravatar", True))
        self.dir = Path(data_dir) / "avatars"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = self._secret()
        self.background = background
        self._known = {}  # (soort, waarde) -> (bestandsnaam of "", tijdstip)
        self._pending = set()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None

    def _secret(self):
        path = self.dir / ".key"
        if not path.exists():
            path.write_bytes(os.urandom(32))
            os.chmod(path, 0o600)
        return path.read_bytes()

    # --- opzoeken ---------------------------------------------------------
    def keys(self, addr):
        """Wat er voor een adres geprobeerd wordt, het belangrijkste eerst: de persoon, dan het bedrijf."""
        domain = addr.rsplit("@", 1)[1]
        out = []
        if self.gravatar:
            out.append(("face", addr))
        if self.logos and domain not in FREEMAIL:
            out.append(("logo", domain))
        return out

    def _base(self, kind, value):
        h = hmac.new(self.key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{h}-{kind}"

    def lookup(self, kind, value):
        """Bestandsnaam als die in de cache staat, "" als er (recent) niets gevonden werd, None als het nog onbekend is."""
        now = time.time()
        hit = self._known.get((kind, value))
        if hit is None:
            base = self._base(kind, value)
            hit = None
            for p in self.dir.glob(base + ".*"):
                hit = ("" if p.suffix == ".none" else p.name, p.stat().st_mtime)
                break
            if hit is None:
                return None
            self._known[(kind, value)] = hit
        name, checked = hit
        if not name and now - checked > RETRY_DAYS * 86400:
            return None
        return name

    def url(self, addr):
        """Adres van de foto voor deze afzender als ze al in de cache staat; ontbreekt er iets, dan wordt het opgehaald."""
        addr = (addr or "").strip().lower()
        if "@" not in addr:
            return None
        found, missing = None, False
        for kind, value in self.keys(addr):
            name = self.lookup(kind, value)
            if name is None:
                missing = True
            elif name and not found:
                found = name
        if missing:
            self._enqueue(addr)
        return f"/avatars/{found}" if found else None

    def path(self, name):
        """Pad van een bestand uit de cache, of None voor een ongeldige of onbekende naam."""
        if not NAME_RE.match(name or ""):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    # --- wachtrij ---------------------------------------------------------
    def _enqueue(self, addr):
        if not self.background:
            return
        with self._lock:
            if addr in self._pending:
                return
            self._pending.add(addr)
            if not (self._thread and self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, daemon=True, name="avatars")
                self._thread.start()
        self._queue.put(addr)

    def _run(self):
        while True:
            addr = self._queue.get()
            try:
                self.fetch(addr)
            except Exception:
                log.exception("foto ophalen mislukt voor %s", addr)
            finally:
                with self._lock:
                    self._pending.discard(addr)

    def fetch(self, addr):
        for kind, value in self.keys(addr):
            if self.lookup(kind, value) is None:
                self._store(kind, value, self._face(value) if kind == "face" else self._logo(value))

    def _store(self, kind, value, got):
        base = self._base(kind, value)
        for old in self.dir.glob(base + ".*"):
            old.unlink(missing_ok=True)
        name = f"{base}.{got[1]}" if got else ""
        target = self.dir / (name or base + ".none")
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(got[0] if got else b"")
        os.replace(tmp, target)
        self._known[(kind, value)] = (name, time.time())

    # --- bronnen ----------------------------------------------------------
    def _face(self, addr):
        h = hashlib.sha256(addr.encode()).hexdigest()
        return self._get(f"https://gravatar.com/avatar/{h}?s=160&d=404", "image")

    def _logo(self, domain):
        for d in domains(domain):
            got = self._bimi(d) or self._icon(d)
            if got:
                return got
        return None

    def _bimi(self, domain):
        """Het merklogo dat een bedrijf zelf in zijn DNS publiceert (BIMI, SVG)."""
        try:
            import dns.resolver

            answer = dns.resolver.resolve(f"default._bimi.{domain}", "TXT", lifetime=5)
        except Exception:
            return None
        for record in answer:
            txt = b"".join(record.strings).decode(errors="ignore")
            m = re.search(r"(?:^|;)\s*l=(https://[^;\s]+)", txt)
            if m:
                got = self._get(m.group(1), "image")
                if got:
                    return got
        return None

    def _icon(self, domain):
        page = self._get(f"https://{domain}/", "html")
        candidates = icon_links(*page) if page else []
        for url in candidates + [f"https://{domain}/favicon.ico"]:
            got = self._get(url, "image")
            if got:
                return got
        return None

    def _get(self, url, want):
        """("image") → (bytes, extensie) of None; ("html") → (tekst, eindadres) of None. Nooit naar privé-adressen."""
        if urlparse(url).scheme != "https":
            return None
        limit = MAX_IMAGE if want == "image" else MAX_HTML
        try:
            with httpx.Client(timeout=8, follow_redirects=True, max_redirects=4, headers={"User-Agent": UA},
                              event_hooks={"request": [check_public]}) as client:
                with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        return None
                    data = b""
                    for chunk in r.iter_bytes():
                        data += chunk
                        if len(data) > limit:
                            return None
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    final = str(r.url)
        except Exception as e:
            log.debug("ophalen van %s mislukt: %s", url, e)
            return None
        if want == "html":
            return (data.decode(errors="ignore"), final) if "html" in ctype else None
        ext = TYPES.get(ctype)
        if not ext and ctype in ("application/octet-stream", "") and urlparse(final).path.endswith(".ico"):
            ext = "ico"
        if not ext or len(data) < 64:
            return None
        return data, ext
