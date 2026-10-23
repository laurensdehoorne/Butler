"""Web Push naar de app op het beginscherm (iOS 16.4+, Android, desktop), zoals in CellarPeaks.

De server ondertekent zijn meldingen met een eigen VAPID-sleutelpaar, één keer gemaakt en bewaard naast de database
(vapid.pem). Een toestel dat meldingen aanzet, stuurt zijn abonnement (een adres bij de pushdienst van Apple, Google of
Mozilla, plus sleutels); elke melding wordt voor dat toestel versleuteld en daar afgeleverd.
"""

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("butler.push")


def device_name(user_agent):
    """Korte naam voor een toestel, uit de User-Agent (zoals CellarPeaks)."""
    ua = user_agent or ""
    device = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux")) if key in ua),
                  "Onbekend toestel")
    browser = next((name for key, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"),
                                           ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "")
    if device in ("iPhone", "iPad") and "Safari/" not in ua:
        browser = "app op beginscherm"
    return f"{device} · {browser}" if browser else device


def contact_uri(value):
    """De VAPID-contactgegevens die Apple en Google eisen: mailto: of een https-adres. Een kaal e-mailadres krijgt
    zelf mailto: ervoor (zonder faalt elke melding met "Missing 'sub' from claims")."""
    v = (value or "").strip()
    if v.lower().startswith(("mailto:", "https://")):
        return v
    if "@" in v and " " not in v:
        return "mailto:" + v
    return "https://github.com"


def device_id(endpoint):
    """Korte publieke naam voor een abonnement (het endpoint zelf is een geheim adres)."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


class Push:
    def __init__(self, db, data_dir, contact=""):
        self.db = db
        self.key_path = Path(data_dir) / "vapid.pem"
        self.contact = contact_uri(contact or os.environ.get("PUSH_CONTACT", ""))
        self._vapid = None
        self.last_error = ""
        self.badge_fn = None  # telt wat op je wacht: het getal op het app-icoon, mee met elke melding

    # --- sleutels -------------------------------------------------------
    def vapid(self):
        if self._vapid is None:
            from py_vapid import Vapid02

            if not self.key_path.exists():
                key = Vapid02()
                key.generate_keys()
                key.save_key(str(self.key_path))
                os.chmod(self.key_path, 0o600)
            self._vapid = Vapid02.from_file(str(self.key_path))
        return self._vapid

    def public_key(self):
        """applicationServerKey voor het toestel: base64url zonder opvulling."""
        from cryptography.hazmat.primitives import serialization

        raw = self.vapid().public_key.public_bytes(serialization.Encoding.X962,
                                                    serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    # --- abonnementen ---------------------------------------------------
    def save(self, subscription, device=""):
        endpoint = str(subscription.get("endpoint") or "")
        keys = subscription.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("Ongeldig abonnement op meldingen.")
        data = {"endpoint": endpoint, "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])}}
        self.db.x("INSERT OR REPLACE INTO push_subs(endpoint, data, device, created_at) VALUES(?,?,?,?)",
                  (endpoint, json.dumps(data), device, datetime.now(timezone.utc).isoformat()))

    def remove(self, endpoint):
        self.db.x("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))

    def devices(self):
        rows = self.db.q("SELECT endpoint, device, created_at FROM push_subs ORDER BY created_at DESC")
        return [{"id": device_id(r["endpoint"]), "device": r["device"] or "Onbekend toestel",
                 "created_at": r["created_at"]} for r in rows]

    def forget(self, did):
        for r in self.db.q("SELECT endpoint FROM push_subs"):
            if device_id(r["endpoint"]) == did:
                self.remove(r["endpoint"])
                return True
        return False

    def count(self):
        return self.db.one("SELECT COUNT(*) AS n FROM push_subs")["n"]

    # --- versturen ------------------------------------------------------
    def _send_one(self, subscription, message):
        """"ok", "gone" (de pushdienst kent het toestel niet meer: app verwijderd) of "error"."""
        from pywebpush import WebPushException, webpush

        try:
            webpush(subscription, json.dumps(message), vapid_private_key=self.vapid(),
                    vapid_claims={"sub": self.contact}, ttl=24 * 3600, timeout=20)
            return "ok"
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                return "gone"
            self.last_error = f"pushdienst antwoordde {status}"
        except Exception as e:  # bv. een ongeldige VAPID-instelling: nooit de hele aanvraag laten mislukken
            self.last_error = str(e)[:200]
        log.warning("push mislukt: %s", self.last_error)
        return "error"

    def send(self, message):
        """message = {title, body, url, tag?}. Stuurt naar elk toestel; verdwenen toestellen worden vergeten.
        Geeft het aantal toestellen terug dat de melding echt kreeg."""
        if self.badge_fn and "badge" not in message:
            try:
                message = {**message, "badge": int(self.badge_fn())}
            except Exception as e:
                log.warning("badge tellen mislukt: %s", e)
        sent = 0
        for r in self.db.q("SELECT endpoint, data FROM push_subs"):
            result = self._send_one(json.loads(r["data"]), message)
            if result == "ok":
                sent += 1
            elif result == "gone":
                self.remove(r["endpoint"])
        return sent
