import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("postvak.notify")


class Notifier:
    def __init__(self, cfg, app_cfg):
        self.mode = cfg.get("mode", "important")
        self.ntfy_server = (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = cfg.get("ntfy_topic") or ""
        self.ntfy_token = cfg.get("ntfy_token") or ""
        self.ha_webhook = cfg.get("ha_webhook") or ""
        self.base_url = (app_cfg.get("base_url") or "").rstrip("/")
        self.tz = ZoneInfo(app_cfg.get("timezone", "Europe/Brussels"))
        self.quiet = self._parse_quiet(cfg.get("quiet_hours") or "")

    @staticmethod
    def _parse_quiet(s):
        try:
            a, b = s.split("-")
            return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
        except Exception:
            return None

    def in_quiet_hours(self):
        if not self.quiet:
            return False
        now = datetime.now(self.tz).time()
        start, end = self.quiet
        return (start <= now or now < end) if start > end else (start <= now < end)

    def wants(self, priority):
        if self.mode == "off":
            return False
        if self.mode == "all":
            return True
        return priority == "hoog"

    def send(self, title, message, tid=None, high=False, extra=None):
        if self.in_quiet_hours():
            log.info("stille uren, melding overgeslagen: %s", title)
            return
        url = f"{self.base_url}/#t={tid}" if (self.base_url and tid) else self.base_url
        if self.ntfy_topic:
            payload = {
                "topic": self.ntfy_topic,
                "title": title[:250],
                "message": message[:1000],
                "priority": 4 if high else 3,
                "tags": ["email"],
            }
            if url:
                payload["click"] = url
            headers = {"Authorization": f"Bearer {self.ntfy_token}"} if self.ntfy_token else {}
            try:
                httpx.post(self.ntfy_server, json=payload, headers=headers, timeout=10).raise_for_status()
            except Exception as e:
                log.warning("ntfy mislukt: %s", e)
        if self.ha_webhook:
            data = {"title": title, "message": message, "url": url, "high": high, "tid": tid}
            data.update(extra or {})
            try:
                httpx.post(self.ha_webhook, json=data, timeout=10)
            except Exception as e:
                log.warning("HA-webhook mislukt: %s", e)
