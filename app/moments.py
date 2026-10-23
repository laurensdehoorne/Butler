"""Momenten uit gemarkeerde mail: Butler haalt een mail naar voren op het moment dat je ze nodig hebt.

Wie een mail markeert in Spark (een ster in Gmail, een vlag in iCloud), zegt daarmee: deze heb ik later nog nodig.
De AI leest zo'n thread en bepaalt wanneer: online inchecken, vertrek, de dag van een concert, een afhaling. Elk
moment heeft een begin (vanaf dan tonen en melden) en een einde (daarna verdwijnt het). De tijden komen in de lokale
tijd van de plaats waar het gebeurt en worden in UTC bewaard.
"""

import json
import logging
import re
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("butler.moments")

KINDS = {"vlucht", "trein", "concert", "evenement", "reservatie", "afspraak", "afhaling", "ander"}
MAX_MOMENTS = 4
DEFAULT_SPAN = timedelta(hours=6)  # zonder einde: zo lang blijft een moment staan
NIGHT = (22, 7)  # 's nachts geen melding, tenzij het moment voor de ochtend al voorbij is

SYSTEM = """Je bent de e-mailassistent van {owner}. Het is nu {now} ({tz}).
{name} heeft deze mail gemarkeerd omdat ze op een bepaald moment nodig is: een vlucht, een treinreis, een concert of evenement, een reservatie, een afspraak, een afhaling.
Bepaal op welke momenten {name} deze mail bij de hand moet hebben. Antwoord met uitsluitend één JSON-object, zonder uitleg of codeblok:
{{"momenten": [{{"soort": "...", "titel": "...", "detail": "...", "begin": "JJJJ-MM-DDTUU:MM", "einde": "JJJJ-MM-DDTUU:MM" of null, "gebeurtenis": "JJJJ-MM-DDTUU:MM", "tijdzone": "Europe/Brussels"}}]}}

- soort: vlucht, trein, concert, evenement, reservatie, afspraak, afhaling of ander.
- titel: kort wat er dan te doen is, bv. "Online inchecken voor SN3583 naar Lissabon", "Vertrek SN3583 naar Lissabon", "Concert Arctic Monkeys".
- detail: in één zin wat je op dat moment nodig hebt: uur, plaats, terminal of poort, zitplaats, boekingsnummer of ticketnummer.
- begin en einde: wanneer Butler de mail toont (de herinnering), in lokale tijd van de plaats waar het gebeurt; tijdzone is die plaats (IANA-naam).
- gebeurtenis: het tijdstip van de gebeurtenis zelf waar dit moment bij hoort (het vertrek van de vlucht, de start van het concert of webinar, het uur van de afspraak), in dezelfde tijdzone. Momenten voor dezelfde gebeurtenis hebben hetzelfde tijdstip.
- Vlucht: een moment wanneer het online inchecken opengaat (staat dat niet in de mail, neem dan 24 uur voor vertrek) tot het vertrek, en een moment 3 uur voor vertrek tot het vertrek. Bij een heen- en terugvlucht doe je dat voor elke vlucht.
- Trein: een moment 1 uur voor vertrek tot het vertrek.
- Concert of evenement: een moment om 9:00 op de dag zelf tot de start, en een moment 2 uur voor de deuren opengaan of de start, tot het einde (of 4 uur na de start).
- Afspraak, reservatie of afhaling: een moment 2 uur ervoor tot het tijdstip zelf; zonder uur om 9:00 op de dag zelf tot 18:00.
- Laat momenten weg die al voorbij zijn. Geen datum te vinden, of al helemaal voorbij: {{"momenten": []}}."""


def parse(raw, default_tz, now=None):
    """AI-antwoord → lijst momenten met begin en einde in UTC (ISO). Ongeldige en voorbije momenten vallen weg."""
    now = now or datetime.now(timezone.utc)
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        items = (json.loads(m.group(0)) if m else {}).get("momenten") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get("titel") or "").strip():
            continue
        try:
            tz = ZoneInfo(str(it.get("tijdzone") or "")) if it.get("tijdzone") else default_tz
        except Exception:
            tz = default_tz
        try:
            at = _local(it.get("begin"), tz)
        except (TypeError, ValueError):
            continue
        try:
            until = _local(it.get("einde"), tz) if it.get("einde") else at + DEFAULT_SPAN
        except (TypeError, ValueError):
            until = at + DEFAULT_SPAN
        if until <= at:
            until = at + DEFAULT_SPAN
        if until <= now:
            continue
        try:  # zonder gebeurtenis: het einde van het moment (meestal de start of het vertrek)
            event = _local(it.get("gebeurtenis"), tz) if it.get("gebeurtenis") else until
        except (TypeError, ValueError):
            event = until
        kind = str(it.get("soort") or "ander").lower()
        out.append({"kind": kind if kind in KINDS else "ander", "title": str(it["titel"]).strip()[:140],
                    "detail": str(it.get("detail") or "").strip()[:300],
                    "at": at.astimezone(timezone.utc).isoformat(), "until": until.astimezone(timezone.utc).isoformat(),
                    "event_at": event.astimezone(timezone.utc).isoformat()})
    out.sort(key=lambda x: x["at"])
    return out[:MAX_MOMENTS]


def _local(value, tz):
    d = datetime.fromisoformat(str(value))
    return d.replace(tzinfo=tz) if d.tzinfo is None else d


def save(db, tid, items):
    """Momenten van een thread vervangen; wat al gemeld was, blijft gemeld."""
    keep = [(tid, x["at"], x["title"]) for x in items]
    for r in db.q("SELECT id, at, title FROM moments WHERE tid=?", (tid,)):
        if (tid, r["at"], r["title"]) not in keep:
            db.x("DELETE FROM moments WHERE id=?", (r["id"],))
    for x in items:
        event = x.get("event_at") or x["until"]
        db.x("INSERT OR IGNORE INTO moments(tid, kind, title, detail, at, until, event_at) VALUES(?,?,?,?,?,?,?)",
             (tid, x["kind"], x["title"], x["detail"], x["at"], x["until"], event))
        db.x("UPDATE moments SET kind=?, detail=?, until=?, event_at=? WHERE tid=? AND at=? AND title=?",
             (x["kind"], x["detail"], x["until"], event, tid, x["at"], x["title"]))


def current(db, now=None, horizon_days=7):
    """(nu, binnenkort): wat nu nodig is, en wat binnen horizon_days begint."""
    now = now or datetime.now(timezone.utc)
    later = (now + timedelta(days=horizon_days)).isoformat()
    n = now.isoformat()
    rows = unique([dict(r) for r in db.q("SELECT * FROM moments WHERE until > ? AND at <= ? ORDER BY at, id", (n, later))])
    return [r for r in rows if r["at"] <= n], [r for r in rows if r["at"] > n]


def unique(rows):
    """Eén moment per soort en tijdstip: een bevestiging en een herinnering voor hetzelfde webinar zijn twee threads,
    maar één afspraak. De eerste blijft (die met de uitgebreidste uitleg als ze tegelijk komen)."""
    best = {}
    for r in rows:
        k = (r["kind"], r["at"])
        if k not in best or len(r.get("detail") or "") > len(best[k].get("detail") or ""):
            best[k] = r
    keep = {id(r) for r in best.values()}
    return [r for r in rows if id(r) in keep]


def events(rows):
    """Momenten per gebeurtenis: de herinnering 's ochtends en die van twee uur ervoor (en dezelfde gebeurtenis uit
    een andere mail) zijn één regel in de agenda. → [(gebeurtenis_utc, [momenten])], in volgorde."""
    groups = {}
    for r in rows:
        groups.setdefault((r["kind"], r.get("event_at") or r["until"]), []).append(r)
    return sorted(((ev, sorted(rs, key=lambda r: r["at"])) for (_, ev), rs in groups.items()), key=lambda g: g[0])


def hold_for_night(local_now, until_utc):
    """'s Nachts wachten tot de ochtend, behalve als het moment dan al voorbij zou zijn (vroege vlucht)."""
    late, early = NIGHT
    if early <= local_now.hour < late:
        return False
    morning = local_now.replace(hour=early, minute=0, second=0, microsecond=0)
    if local_now.hour >= late:
        morning += timedelta(days=1)
    return datetime.fromisoformat(until_utc) > morning.astimezone(timezone.utc)


def due(db, notifier, now=None):
    """Meld wat nu begint (één keer per moment). Geeft het aantal verstuurde meldingen terug."""
    now = now or datetime.now(timezone.utc)
    sent, done = 0, set()
    rows = unique([dict(r) for r in db.q("SELECT * FROM moments WHERE notified=0 AND at <= ? AND until > ? ORDER BY at, id",
                                         (now.isoformat(),) * 2)])
    for r in rows:
        if (r["kind"], r["at"]) in done:
            continue
        done.add((r["kind"], r["at"]))
        if notifier.topic_on("moment"):
            if hold_for_night(now.astimezone(notifier.tz), r["until"]):
                continue
            notifier.send(r["title"], r["detail"] or "", tid=r["tid"], high=True,
                          extra={"type": "moment", "kind": r["kind"]}, force=True)
            sent += 1
        # dezelfde afspraak uit een andere thread: niet nog eens melden
        db.x("UPDATE moments SET notified=1 WHERE id=? OR (notified=0 AND kind=? AND at=?)", (r["id"], r["kind"], r["at"]))
    return sent


def loop(db, notifier, every=30):
    def run():
        while True:
            try:
                due(db, notifier)
            except Exception:
                log.exception("momenten melden mislukt")
            _time.sleep(every)

    threading.Thread(target=run, daemon=True, name="moments").start()
