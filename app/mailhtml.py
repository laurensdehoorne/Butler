"""HTML-mails tonen zonder dat je toestel iets van buiten laadt.

Externe afbeeldingen in een mail (logo's, foto's, maar ook meetpixels) worden herschreven naar /img/<handtekening>/<adres>:
Butler haalt ze zelf op. De afzender ziet zo nooit je IP-adres of locatie, wel dat de mail geopend werd. De
handtekening (HMAC met de sleutel van de avatars) zorgt dat /img enkel adressen ophaalt die Butler zelf in een mail
zette: het is geen open proxy, ook al werkt het zonder token (een <img> kan geen token meesturen).
Met app.remote_images: false worden externe afbeeldingen helemaal weggelaten.
"""

import base64
import hashlib
import hmac
import re

import httpx
from bs4 import BeautifulSoup

from .avatars import UA, check_public

URL_RE = re.compile(r"""url\(\s*(['"]?)(https?://[^)'"\s]+)\1\s*\)""", re.I)
MAX_IMAGE = 5 * 1024 * 1024


class MailImages:
    def __init__(self, key, enabled=True):
        self.key = key
        self.enabled = enabled

    def sign(self, url):
        return hmac.new(self.key, url.encode(), hashlib.sha256).hexdigest()[:32]

    def proxied(self, url):
        enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"/img/{self.sign(url)}/{enc}"

    def unpack(self, sig, enc):
        """Het oorspronkelijke adres als de handtekening klopt, anders None."""
        try:
            url = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode()
        except Exception:
            return None
        return url if hmac.compare_digest(sig, self.sign(url)) and url.startswith(("http://", "https://")) else None

    def _remote(self, url):
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.lower().startswith(("http://", "https://")):
            return url
        return self.proxied(url) if self.enabled else ""

    def rewrite(self, html):
        """Externe afbeeldingen via Butler (of weg); de rest van de HTML blijft zoals ze is."""
        soup = BeautifulSoup(html, "html.parser")
        for t in soup.find_all(True):
            if t.name == "img":
                t.attrs.pop("srcset", None)
                if t.get("src"):
                    t["src"] = self._remote(str(t["src"]))
            if t.get("background"):
                t["background"] = self._remote(str(t["background"]))
            if t.get("style") and "url(" in str(t["style"]).lower():
                t["style"] = URL_RE.sub(lambda m: f"url('{self._remote(m.group(2))}')", str(t["style"]))
        for style in soup.find_all("style"):
            if style.string and "url(" in style.string.lower():
                style.string = URL_RE.sub(lambda m: f"url('{self._remote(m.group(2))}')", style.string)
        return str(soup)


def fetch_image(url):
    """(bytes, content-type) van een afbeelding, of None. Enkel publieke adressen, ook na doorverwijzingen."""
    try:
        with httpx.Client(timeout=10, follow_redirects=True, max_redirects=5, headers={"User-Agent": UA},
                          event_hooks={"request": [check_public]}) as client:
            with client.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    return None
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE:
                        return None
                return data, ctype
    except Exception:
        return None

