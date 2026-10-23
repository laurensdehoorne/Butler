"""De agenda van Secretaresse als agenda-abonnement (iCalendar), voor de Agenda-app op iPhone, Mac en elders.

Een app op het beginscherm kan niet zelf in de Agenda-app schrijven; een abonnement (webcal://) wel, en de Agenda-app
ververst het zelf. De link bevat een geheim (een HMAC), want een agenda-app kan niet aanmelden: wie de link heeft, ziet
je afspraken (geen mail). Enkel de gebeurtenissen: titel, uur, waar het over gaat en een link terug naar de mail.

Gebeurtenissen met een uur staan in UTC (de Agenda-app toont ze in je eigen tijdzone); een deadline zonder uur is een
gebeurtenis van de hele dag. Geen herinneringen in de agenda: Secretaresse stuurt die zelf, op het juiste moment.
"""

import hashlib
import hmac
from datetime import date, datetime, timedelta, timezone

PRODID = "-//Secretaresse//Agenda//NL"


def token(key):
    return hmac.new(key, b"butler-agenda", hashlib.sha256).hexdigest()[:32]


def valid(key, tok):
    return hmac.compare_digest(token(key), tok or "")


def _esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _fold(line):
    """Regels van hoogstens 75 bytes (RFC 5545), vervolgregels beginnen met een spatie."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def _utc(iso, tz):
    d = datetime.fromisoformat(iso)
    if d.tzinfo is None:  # een deadline met uur: lokale tijd
        d = d.replace(tzinfo=tz)
    return d.astimezone(timezone.utc)


def calendar(items, tz, base_url="", name="Secretaresse"):
    """items: de regels van /api/agenda. → tekst van een .ics-bestand."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(name)}", "X-WR-CALDESC:Afspraken en boekingen uit je mail", f"X-WR-TIMEZONE:{tz.key}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for it in items:
        m = it.get("moment") or {}
        title = m.get("title") or it.get("subject") or "Afspraak"
        detail = "\n".join(x for x in (m.get("detail"), it.get("summary"), it.get("subject")) if x)
        url = f"{base_url}/#t={it['tid']}" if base_url else ""
        lines += ["BEGIN:VEVENT", f"UID:butler-{it['tid']}-{it['sort'].replace(':', '')}@butler", f"DTSTAMP:{now}",
                  f"SUMMARY:{_esc(title)}", f"DESCRIPTION:{_esc(detail + (chr(10) + url if url else ''))}"]
        if url:
            lines.append(f"URL:{url}")
        if it.get("allday"):
            d = date.fromisoformat(it["sort"][:10])
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                      "TRANSP:TRANSPARENT"]
        else:
            start = _utc(it["sort"], tz)
            lines += [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{start + timedelta(hours=1):%Y%m%dT%H%M%SZ}"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
