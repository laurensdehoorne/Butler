import hmac
import json
import logging
import os
import re
import signal
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import actions, autopilot, config, costs, dossiers, forward, moments, shared, tone
from .accounts import Account
from .avatars import MEDIA, Avatars
from .mailhtml import MailImages, fetch_image
from .ai import PROVIDERS, AIError, AIWorker
from .db import DB, sender_rule, thread_messages, upsert_rule
from .notify import Notifier
from .push import Push, device_name
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("butler")

DB_PATH = os.environ.get("BUTLER_DB", "/data/butler.db")
DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
CFG_PATH = os.environ.get("BUTLER_CONFIG", "/config/config.yaml")
CFG, CFG_SOURCE = config.load(DATA_DIR, CFG_PATH)  # wat in de app bewaard werd, gaat voor
log.info("configuratie uit %s", CFG_SOURCE or "(nog niets: stel Butler in via Instellingen)")
APP = CFG.get("app") or {}
AI_CFG = CFG.get("ai") or {}
RESTART = os.environ.get("BUTLER_RESTART", "1") != "0"  # na bewaren herstarten; Docker zet Butler meteen terug op
SAVED = {"cfg": CFG}  # laatst bewaarde config; CFG zelf geldt tot de herstart
db = DB(DB_PATH)
accounts = {str(a["id"]): Account(a) for a in (CFG.get("accounts") or [])}
_mine = sorted(set().union(*(a.aliases for a in accounts.values())))
if _mine:  # mail van je eigen andere account telt als van jou (ook wat al in de cache staat)
    db.x(f"UPDATE messages SET outgoing=1, seen=1 WHERE outgoing=0 AND from_addr IN ({','.join('?' * len(_mine))})", _mine)
push = Push(db, DATA_DIR, (CFG.get("notify") or {}).get("push_contact", ""))
notifier = Notifier(CFG.get("notify") or {}, APP, push)
costs.load_overrides(DATA_DIR)
avatars = Avatars(DATA_DIR, APP.get("avatars") or {}, background=os.environ.get("BUTLER_NO_SYNC") != "1")
meter = costs.Meter(db)
images = MailImages(avatars.key, bool(APP.get("remote_images", True)))
ai = AIWorker(db, AI_CFG, accounts, notifier, meter)
syncer = Syncer(db, APP, AI_CFG, accounts, ai, notifier)
autopilot.mark_enabled(db, APP)  # vanaf nu: enkel nieuwe mail
ai.after_triage.append(lambda tid: autopilot.after_triage(db, APP, ai, accounts, actions, tid, quiet_recipients() | _quiet_senders()))
FOLLOWUP_DAYS = int(APP.get("followup_days", 3))
FOLLOWUP_WINDOW = 60  # "wacht op antwoord" kijkt niet verder terug, ook al staat alle mail in de cache
# antwoorden op agenda-uitnodigingen: daar komt geen antwoord meer op
CALENDAR_REPLY = re.compile(r"^(accepted|declined|tentative|tentatively accepted|geaccepteerd|geweigerd|afgewezen|voorlopig"
                            r"|voorlopig geaccepteerd|akzeptiert|abgelehnt|accepté|refusé)\s*:", re.I)
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_):
    if os.environ.get("BUTLER_NO_SYNC") != "1":
        ai.start()
        syncer.start()
        import threading
        threading.Thread(target=digest_loop, daemon=True, name="digest").start()
        moments.loop(db, notifier)
        threading.Thread(target=tone_loop, daemon=True, name="tone").start()
    yield


app = FastAPI(title="Butler", lifespan=lifespan, docs_url=None, redoc_url=None)


GATEWAY_SECRET = os.environ.get("BUTLER_GATEWAY_SECRET", "")  # achter de gateway (app.gateway): die meldt aan


def auth(request: Request):
    if GATEWAY_SECRET and hmac.compare_digest(request.headers.get("x-butler-gateway", "").encode(), GATEWAY_SECRET.encode()):
        return
    token = APP.get("token") or ""
    if not token:
        return
    got = request.headers.get("authorization", "")
    if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "Ongeldig token")


# --- helpers --------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc)


def _age_days(iso):
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 86400
    except Exception:
        return 0


def is_me(addr):
    """Een adres van jou, in eender welk account (mail van Gmail naar je eigen iCloud wacht op niemand)."""
    return any(a.is_me(addr) for a in accounts.values())


QUIET_MIN = 3  # zoveel threads naar een adres zonder ooit antwoord: daar wacht je niet op
_quiet = {"at": 0.0, "addrs": set()}


def quiet_recipients():
    """Adressen waar je vaak naar stuurt zonder dat er ooit iets terugkomt (Billit, een scanner, een ticketsysteem).

    Butler leert dat uit je eigen mail: QUIET_MIN of meer threads in het voorbije jaar en in geen enkele daarvan een
    bericht van dat adres. Eén keer per tien minuten berekend."""
    if time.time() - _quiet["at"] < 600:
        return _quiet["addrs"]
    since = (_now() - timedelta(days=365)).isoformat()
    sent = {}
    for r in db.q("SELECT account, thread_key, to_addrs FROM messages WHERE outgoing=1 AND date >= ?", (since,)):
        for _, a in json.loads(r["to_addrs"] or "[]"):
            a = (a or "").lower()
            if a and not is_me(a):
                sent.setdefault(a, set()).add((r["account"], r["thread_key"]))
    heard = {(r["account"], r["thread_key"], r["from_addr"])
             for r in db.q("SELECT DISTINCT account, thread_key, from_addr FROM messages WHERE outgoing=0 AND date >= ?", (since,))}
    _quiet["addrs"] = {a for a, threads in sent.items()
                       if len(threads) >= QUIET_MIN and not any((acc, key, a) in heard for acc, key in threads)}
    _quiet["at"] = time.time()
    return _quiet["addrs"]


def _quiet_senders():
    """Afzenders die jou nooit een antwoord vragen (no-reply): daar zet Butler geen concept voor klaar."""
    return {r["addr"] for r in db.q("SELECT addr FROM sender_rules WHERE kind IN ('reclame', 'notificatie')")}


def _snoozed(t):
    return bool(t["snooze_until"]) and t["snooze_until"] > _now().isoformat()


def _acc(account_id):
    acc = accounts.get(account_id)
    if not acc:
        raise HTTPException(404, "account onbekend")
    return acc


def _thread_or_404(tid):
    t = db.one("SELECT * FROM threads WHERE tid=?", (tid,))
    if not t:
        raise HTTPException(404, "thread niet gevonden")
    return t


def _card(t, msgs, extra=None):
    last = msgs[-1]
    inc = [m for m in msgs if not m["outgoing"]]
    if last["outgoing"]:
        to = json.loads(last["to_addrs"] or "[]")
        who = "Jij → " + ", ".join((n or a) for n, a in to[:2])
    else:
        who = last["from_name"] or last["from_addr"]
    acc = accounts.get(t["account"])
    d = {
        "tid": t["tid"],
        "account": t["account"],
        "account_name": acc.name if acc else t["account"],
        "subject": (inc[-1] if inc else last)["subject"],
        "who": who,
        "avatar": None if last["outgoing"] else avatars.url(last["from_addr"]),
        "snippet": last["snippet"],
        "date": last["date"],
        "n": len(msgs),
        "unread": sum(1 for m in msgs if m["in_inbox"] and not m["seen"]),
        "in_inbox": any(m["in_inbox"] for m in msgs),
        "flagged": any(m.get("flagged") for m in msgs),
        "dossiers": dossiers.of_thread(db, t["tid"]),
        "bulk": all(m["bulk"] for m in inc) if inc else False,
        "kind": (inc[-1]["kind"] if inc else "") or "",
        "last_outgoing": bool(last["outgoing"]),
        "summary": t["summary"],
        "summary_stale": bool(t["summary"]) and (t["ai_count"] or 0) < len(msgs),
        "priority": t["priority"],
        "category": t["category"],
        "action": t["action"],
        "deadline": t["deadline"],
        "tags": json.loads(t["tags"] or "[]"),
        "snooze_until": t["snooze_until"],
        "done": bool(t["done"]),
        "age_days": round(_age_days(last["date"]), 1),
    }
    if extra:
        d.update(extra)
    return d


# --- pagina's -------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icon.svg")
def icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/{name}.png")
def png_icon(name: str):
    if name not in ("apple-touch-icon", "icon-192", "icon-512", "badge-96"):
        raise HTTPException(404)
    return FileResponse(STATIC / f"{name}.png", media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/fonts/{name}.woff2")
def font(name: str):
    if name != "bodoni-moda-latin":
        raise HTTPException(404)
    return FileResponse(STATIC / "fonts" / f"{name}.woff2", media_type="font/woff2",
                        headers={"Cache-Control": "max-age=31536000, immutable"})


@app.get("/avatars/{name}")
def avatar(name: str):
    # Zonder token: de namen zijn niet te raden (HMAC) en enkel wat al in de cache staat, wordt getoond.
    # Een SVG-logo komt van buiten, dus nooit scripts: sandbox, ook als iemand het bestand rechtstreeks opent.
    path = avatars.path(name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type=MEDIA[path.suffix[1:]], headers={
        "Cache-Control": "max-age=604800",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/img/{sig}/{enc}")
def mail_image(sig: str, enc: str):
    """Een afbeelding uit een mail, door Butler opgehaald; enkel adressen die Butler zelf ondertekend heeft."""
    url = images.unpack(sig, enc)
    if not url or not images.enabled:
        raise HTTPException(404)
    got = fetch_image(url)
    if not got:
        raise HTTPException(404)
    data, ctype = got
    return Response(data, media_type=ctype, headers={
        "Cache-Control": "private, max-age=86400",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/api/messages/{mid}/html", dependencies=[Depends(auth)])
def message_html(mid: int):
    row = db.one("SELECT account FROM messages WHERE id=?", (mid,))
    if not row:
        raise HTTPException(404, "bericht niet gevonden")
    html, atts = _run(lambda: actions.fetch_html(db, _acc(row["account"]), mid))
    return {"html": images.rewrite(html) if html else "", "attachments": attachment_links(mid, atts)}


# --- bijlagen ----------------------------------------------------------------
ATT_DAYS = 1  # zo lang blijft een bijlagelink geldig
INLINE_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "text/plain")


def _att_sig(mid, idx, exp):
    return hmac.new(avatars.key, f"att:{mid}:{idx}:{exp}".encode(), "sha256").hexdigest()[:32]


def attachment_links(mid, atts):
    """Bijlagen met een ondertekende link die een dag geldig is: zo opent een pdf in de pdf-lezer van iOS, wat een
    link met een token in de header niet kan. None = de lijst is nog niet bekend."""
    if atts is None:
        return None
    exp = int(time.time()) + ATT_DAYS * 86400
    return [{**a, "idx": i, "url": f"/att/{_att_sig(mid, i, exp)}/{mid}/{i}?e={exp}",
             "inline": a["type"] in INLINE_TYPES} for i, a in enumerate(atts)]


@app.get("/att/{sig}/{mid}/{idx}")
def attachment(sig: str, mid: int, idx: int, e: int = 0):
    """Een bijlage, van de server gehaald op het moment dat je ze opent. Zonder token (een gewone link), maar enkel met
    een handtekening die Butler zelf gaf en die nog geldig is."""
    if e < time.time() or not hmac.compare_digest(sig, _att_sig(mid, idx, e)):
        raise HTTPException(404)
    row = db.one("SELECT account FROM messages WHERE id=?", (mid,))
    if not row:
        raise HTTPException(404)
    got = _run(lambda: actions.fetch_attachment(db, _acc(row["account"]), mid, idx))
    if not got:
        raise HTTPException(404, "bijlage niet (meer) gevonden")
    data, ctype, name = got
    from urllib.parse import quote

    inline = ctype in INLINE_TYPES  # pdf en afbeeldingen meteen bekijken; html, svg en de rest downloaden
    return Response(data, media_type=ctype if inline else "application/octet-stream", headers={
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
        # sandbox enkel voor downloads: Chrome weigert een pdf in een sandbox te tonen, en pdf en afbeeldingen voeren
        # toch geen scripts uit in Butler
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'" + ("" if inline else "; sandbox"),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600",
    })


# --- wat Butler zelf deed --------------------------------------------------------
@app.get("/api/activity", dependencies=[Depends(auth)])
def activity():
    return {"entries": autopilot.entries(db), "enabled": {k: autopilot.enabled(APP, k) for k in autopilot.KINDS}}


@app.post("/api/activity/{eid}/undo", dependencies=[Depends(auth)])
def activity_undo(eid: int):
    try:
        _run(lambda: autopilot.undo(db, accounts, eid))
    except HTTPException as e:
        raise HTTPException(400, e.detail)
    return {"ok": True}


# --- gedeeld tussen gebruikers (via de gateway) -----------------------------------
@app.get("/api/threads/{tid}/export", dependencies=[Depends(auth)])
def export_thread(tid: int):
    t = _thread_or_404(tid)
    acc = _acc(t["account"])
    out = _run(lambda: shared.export(db, tid, lambda mid: actions.fetch_html(db, acc, mid),
                                     lambda mid, idx: actions.fetch_attachment(db, acc, mid, idx)))
    if not out:
        raise HTTPException(404, "thread niet gevonden")
    return out


class SharedIn(BaseModel):
    sender: str
    note: str = ""
    thread: dict


@app.post("/api/shared", dependencies=[Depends(auth)])
def receive_shared(body: SharedIn):
    sid = shared.save(db, DATA_DIR, body.sender, body.note, body.thread)
    subject = body.thread.get("subject") or "(geen onderwerp)"
    try:
        notifier.send(f"{body.sender} deelde een mail", (body.note + "\n" if body.note else "") + subject,
                      extra={"type": "shared"}, force=True)
    except Exception:
        log.exception("melding over gedeelde mail mislukt")
    return {"id": sid}


@app.get("/api/shared", dependencies=[Depends(auth)])
def list_shared():
    return {"shared": shared.listing(db)}


@app.get("/api/shared/{sid}", dependencies=[Depends(auth)])
def get_shared(sid: int):
    d = shared.get(db, sid)
    if not d:
        raise HTTPException(404, "niet gevonden")
    for mi, m in enumerate(d["messages"]):
        m["html"] = images.rewrite(m["html"]) if m.get("html") else ""
        m["attachments"] = [{**a, "url": f"/api/shared/{sid}/att/{mi}/{ai}" if a.get("stored") else None,
                             "inline": a["type"] in INLINE_TYPES} for ai, a in enumerate(m.get("attachments") or [])]
    return d


@app.get("/api/shared/{sid}/att/{mi}/{ai}", dependencies=[Depends(auth)])
def shared_attachment(sid: int, mi: int, ai: int):
    from urllib.parse import quote

    got = shared.attachment(db, DATA_DIR, sid, mi, ai)
    if not got:
        raise HTTPException(404, "bijlage niet gevonden")
    data, ctype, name = got
    inline = ctype in INLINE_TYPES
    return Response(data, media_type=ctype if inline else "application/octet-stream", headers={
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'" + ("" if inline else "; sandbox"),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=3600",
    })


@app.post("/api/shared/{sid}/done", dependencies=[Depends(auth)])
def shared_done(sid: int, undo: bool = False):
    db.x("UPDATE shared SET done=? WHERE id=?", (0 if undo else 1, sid))
    return {"ok": True}


@app.delete("/api/shared/{sid}", dependencies=[Depends(auth)])
def shared_delete(sid: int):
    shared.remove(db, DATA_DIR, sid)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


# --- API ------------------------------------------------------------------
@app.get("/api/accounts", dependencies=[Depends(auth)])
def get_accounts():
    return {"accounts": [a.public() for a in accounts.values()], "ai": ai.enabled, "followup_days": FOLLOWUP_DAYS,
            "ai_provider": ai.provider, "ai_providers": [{"id": p, "name": PROVIDERS[p], "triage_model": ai.model("triage", p),
                                                           "draft_model": ai.model("draft", p)} for p in ai.available()]}


class ProviderIn(BaseModel):
    provider: str


@app.post("/api/settings/ai", dependencies=[Depends(auth)])
def set_ai_provider(body: ProviderIn):
    try:
        ai.set_provider(body.provider)
    except AIError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ai_provider": ai.provider}


@app.post("/api/sync", dependencies=[Depends(auth)])
def trigger_sync():
    for a in accounts.values():
        a.wake.set()
    return {"ok": True}


@app.get("/api/inbox", dependencies=[Depends(auth)])
def inbox(account: str = "", filter: str = "alle", q: str = "", limit: int = 150):
    args = [account, account]
    sql = """SELECT t.tid, MAX(m.date) AS last_date FROM threads t
             JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
             WHERE (?='' OR t.account=?)"""
    if q.strip():
        like = f"%{q.strip()}%"
        sql += """ AND t.tid IN (SELECT t2.tid FROM threads t2 JOIN messages m2
                   ON m2.account=t2.account AND m2.thread_key=t2.thread_key
                   WHERE m2.subject LIKE ? OR m2.from_name LIKE ? OR m2.from_addr LIKE ? OR m2.body LIKE ?)"""
        args += [like] * 4
        sql += " GROUP BY t.tid"
    else:
        sql += " GROUP BY t.tid HAVING SUM(m.in_inbox) > 0"
    sql += " ORDER BY last_date DESC LIMIT ?"
    args.append(min(limit, 500) * 2)

    out = []
    for r in db.q(sql, args):
        t, msgs = thread_messages(db, r["tid"])
        if not msgs:
            continue
        card = _card(t, msgs)
        if not q and _snoozed(t):
            continue
        if filter == "ongelezen" and not card["unread"]:
            continue
        if filter == "belangrijk" and card["priority"] != "hoog":
            continue
        if filter == "persoonlijk" and card["bulk"]:
            continue
        if filter == "reclame" and card["kind"] != "reclame":
            continue
        if filter == "notificaties" and card["kind"] != "notificatie":
            continue
        out.append(card)
        if len(out) >= limit:
            break
    return {"threads": out}


@app.get("/api/followup", dependencies=[Depends(auth)])
def followup(account: str = ""):
    rows = db.q(
        """SELECT t.tid FROM threads t JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
           WHERE t.done=0 AND (?='' OR t.account=?) GROUP BY t.tid
           HAVING MAX(m.date) >= ? OR SUM(m.in_inbox) > 0 OR t.snooze_until IS NOT NULL
           ORDER BY MAX(m.date) DESC LIMIT 1500""",
        (account, account, (_now() - timedelta(days=FOLLOWUP_WINDOW)).isoformat()),
    )
    mine, theirs, reminders = [], [], []
    quiet = quiet_recipients()
    # mail met een moment dat nog komt (een geboekte reis, tickets) staat niet in "wacht op jou": die komt vanzelf
    # terug in Binnenkort en Nu nodig wanneer het zover is, en staat altijd in de agenda en het dossier
    later = {r["tid"] for r in db.q("SELECT DISTINCT tid FROM moments WHERE until > ?", (_now().isoformat(),))}
    week = (datetime.now(notifier.tz) + timedelta(days=7)).date().isoformat()
    now = _now().isoformat()
    for r in rows:
        t, msgs = thread_messages(db, r["tid"])
        if not msgs:
            continue
        if t["snooze_until"]:
            if t["snooze_until"] > now:
                continue
            reminders.append(_card(t, msgs))
            continue
        last = msgs[-1]
        if not last["outgoing"]:
            if last["bulk"] or not any(m["in_inbox"] for m in msgs):
                continue
            if t["tid"] in later:
                continue
            # een afspraak (deadline met uur) verder dan een week weg, waar niets van je verwacht wordt: wacht ook
            if t["deadline"] and len(t["deadline"]) > 10 and t["deadline"][:10] > week and t["action"] != "antwoorden":
                continue
            if t["action"] in (None, "antwoorden", "actie"):
                mine.append(_card(t, msgs))
        else:
            if _age_days(last["date"]) < FOLLOWUP_DAYS:
                continue
            # de AI las je laatste mail: een bedanking, bevestiging, afzegging of test wacht op niemand
            if t["awaits"] == 0 and t["ai_count"] == len(msgs):
                continue
            to = [a for _, a in json.loads(last["to_addrs"] or "[]")]
            waiting = [a for a in to if not is_me(a) and a.lower() not in quiet]
            if not waiting or CALENDAR_REPLY.match(last["subject"] or ""):
                continue
            theirs.append(_card(t, msgs))
    # belangrijkst eerst: wat je zelf markeerde, dan wat de AI dringend vond, dan wat het eerst nodig is
    soonest = {r["tid"]: r["at"] for r in db.q("SELECT tid, MIN(at) AS at FROM moments WHERE until > ? GROUP BY tid", (now,))}
    mine.sort(key=lambda c: (not c["flagged"], c["priority"] != "hoog", soonest.get(c["tid"], "9999"), c["date"] or ""))
    theirs.sort(key=lambda c: c["date"] or "")
    return {"waiting_on_me": mine[:150], "waiting_on_them": theirs[:150], "reminders": reminders[:50],
            "followup_days": FOLLOWUP_DAYS, **moment_cards(account)}


@app.get("/api/agenda", dependencies=[Depends(auth)])
def agenda(account: str = ""):
    """Alles wat aan een datum hangt, in volgorde: momenten uit gemarkeerde mail en deadlines die de AI vond.
    Een thread met momenten krijgt geen aparte deadline-regel (dat zou hetzelfde twee keer zijn)."""
    now = _now().isoformat()
    today = datetime.now(notifier.tz).date().isoformat()
    cards, items = {}, []

    def card_for(tid):
        if tid not in cards:
            t, msgs = thread_messages(db, tid)
            cards[tid] = _card(t, msgs) if msgs and (not account or t["account"] == account) else None
        return cards[tid]

    with_moments = set()
    rows = [dict(r) for r in db.q("SELECT * FROM moments WHERE until > ? ORDER BY at, id", (now,))]
    for event, group in moments.events(rows):  # één regel per gebeurtenis, op het uur van de gebeurtenis zelf
        main = group[-1]  # het moment het dichtst bij de gebeurtenis heeft de beste titel ("Vertrek", niet "Inchecken")
        c = card_for(main["tid"])
        if c:
            with_moments.update(r["tid"] for r in group)
            items.append({**c, "moment": _moment(main), "sort": event, "allday": False,
                          "reminders": sorted({r["at"] for r in group})})
    # een afspraak (met uur) blijft staan als je de mail op Klaar zet; een gewone deadline is dan afgehandeld
    for r in db.q("SELECT tid, deadline FROM threads WHERE deadline >= ? AND (done=0 OR length(deadline) > 10) ORDER BY deadline",
                  (today,)):
        c = None if r["tid"] in with_moments else card_for(r["tid"])
        if c:  # met een uur (een afspraak) of de hele dag
            items.append({**c, "moment": None, "sort": r["deadline"], "allday": len(r["deadline"]) <= 10})
    items.sort(key=lambda x: x["sort"])
    return {"items": items[:300]}


def tone_loop():
    """Het schrijfprofiel leren zodra er mail is, en daarna maandelijks opnieuw (tenzij je het zelf aanpaste)."""
    time.sleep(120)  # eerst de eerste sync laten lopen
    while True:
        try:
            if ai.enabled and tone.stale(db):
                ai.enqueue_tone()
        except Exception:
            log.exception("schrijfprofiel plannen mislukt")
        time.sleep(86400)


@app.get("/api/tone", dependencies=[Depends(auth)])
def get_tone():
    return {"tone": tone.get(db), "samples_available": len(tone.samples(db))}


class ToneIn(BaseModel):
    text: str


@app.put("/api/tone", dependencies=[Depends(auth)])
def put_tone(body: ToneIn):
    """Zelf aangepast profiel: blijft staan tot je het opnieuw laat leren."""
    if not body.text.strip():
        raise HTTPException(400, "Leeg profiel")
    return {"tone": tone.put(db, body.text, (tone.get(db) or {}).get("samples", 0), edited=True)}


@app.post("/api/tone/build", dependencies=[Depends(auth)])
def build_tone():
    if not ai.enabled:
        raise HTTPException(400, "Zet eerst de AI aan.")
    t = _run(lambda: tone.build(ai, db))
    if not t:
        raise HTTPException(400, "Te weinig verstuurde mail om je stijl uit te leren (minstens 5 mails).")
    return {"tone": t}


class QuickDraftIn(BaseModel):
    instruction: str
    reply_all: bool = False


@app.post("/api/threads/{tid}/quick-draft", dependencies=[Depends(auth)])
def quick_draft(tid: int, body: QuickDraftIn):
    """Snel antwoorden: een paar woorden worden een volledig antwoord in jouw stijl, als concept in je Concepten-map."""
    t = _thread_or_404(tid)
    if not body.instruction.strip():
        raise HTTPException(400, "Schrijf in een paar woorden wat je wilt antwoorden.")
    text = _run(lambda: ai.draft(tid, body.instruction))
    res = _run(lambda: actions.save_draft(db, _acc(t["account"]), tid, text, body.reply_all))
    return {"ok": True, "folder": res["folder"], "draft": text}


def badge_count():
    """Het getal op het app-icoon: hetzelfde als op de tab Opvolgen."""
    d = followup()
    return len(d["waiting_on_me"]) + len(d["reminders"]) + len(d["moments_now"])


push.badge_fn = badge_count


def _moment(r):
    r = dict(r)
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "detail": r["detail"], "at": r["at"], "until": r["until"],
            "event_at": r.get("event_at") or r["until"]}


def moment_cards(account=""):
    """Gemarkeerde mail die nu nodig is, en wat binnen de week komt."""
    out = {}
    for key, rows in zip(("moments_now", "moments_soon"), moments.current(db)):
        cards = []
        for r in rows:
            t, msgs = thread_messages(db, r["tid"])
            if msgs and (not account or t["account"] == account):
                cards.append(_card(t, msgs, {"moment": _moment(r)}))
        out[key] = cards
    return out


@app.get("/api/threads/{tid}", dependencies=[Depends(auth)])
def get_thread(tid: int):
    t, msgs = thread_messages(db, tid)
    if not t:
        raise HTTPException(404, "thread niet gevonden")
    card = _card(t, msgs)
    card["messages"] = [
        {
            "id": m["id"],
            "from_name": m["from_name"],
            "from_addr": m["from_addr"],
            "avatar": None if m["outgoing"] else avatars.url(m["from_addr"]),
            "to": json.loads(m["to_addrs"] or "[]"),
            "cc": json.loads(m["cc_addrs"] or "[]"),
            "date": m["date"],
            "subject": m["subject"],
            "body": (m["body"] or "")[:30000],
            "outgoing": bool(m["outgoing"]),
            "pending": m["folder"] == "_pending",
            # None = nog niet opgehaald (de app vraagt het op via /api/messages/<id>/html), "" = geen HTML-versie
            "html": images.rewrite(m["html"]) if m.get("html") else m.get("html"),
            "attachments": attachment_links(m["id"], json.loads(m["attachments"]) if m.get("attachments") is not None else None),
        }
        for m in msgs
    ]
    card["ai_available"] = ai.active_for(t["account"])
    card["context"] = t.get("context") or ""
    card["dossier_threads"] = []  # per dossier de andere mails, nieuwste eerst
    for d in card["dossiers"]:
        related = []
        for r in db.q("""SELECT td.tid FROM thread_dossiers td JOIN threads t ON t.tid=td.tid
                         JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                         WHERE td.dossier_id=? AND td.tid!=? GROUP BY td.tid ORDER BY MAX(m.date) DESC LIMIT 12""",
                      (d["id"], tid)):
            ot, om = thread_messages(db, r["tid"])
            if om:
                c = _card(ot, om)
                related.append({k: c[k] for k in ("tid", "subject", "who", "date", "avatar", "last_outgoing")})
        if related:
            card["dossier_threads"].append({"dossier": d, "threads": related})
    incoming = [m for m in msgs if not m["outgoing"]]
    if incoming:
        rule = sender_rule(db, t["account"], incoming[-1]["from_addr"]) or {}
        card["sender"] = {"addr": incoming[-1]["from_addr"], "name": incoming[-1]["from_name"] or incoming[-1]["from_addr"],
                          "context": rule.get("context") or ""}
    card["moments"] = [_moment(r) for r in db.q("SELECT * FROM moments WHERE tid=? AND until > ? ORDER BY at",
                                                  (tid, _now().isoformat()))]
    card["invoice"] = forward.suggestion(db, APP, t, msgs)  # factuur: met één tik naar je boekhouding
    card["auto"] = [dict(r) for r in db.q("SELECT id, at, kind, detail, can_undo, undone_at FROM auto_log WHERE tid=? ORDER BY at", (tid,))]
    return card


def _run(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except AIError as e:
        log.warning("AI: %s", e)
        raise HTTPException(502, str(e)[:300])
    except Exception as e:
        log.exception("actie mislukt")
        raise HTTPException(502, str(e)[:300])


@app.post("/api/threads/{tid}/archive", dependencies=[Depends(auth)])
def archive(tid: int):
    t = _thread_or_404(tid)
    n = _run(lambda: actions.move_thread(db, _acc(t["account"]), tid, "archive"))
    return {"ok": True, "moved": n}


@app.post("/api/threads/{tid}/trash", dependencies=[Depends(auth)])
def trash(tid: int):
    t = _thread_or_404(tid)
    n = _run(lambda: actions.move_thread(db, _acc(t["account"]), tid, "trash"))
    db.x("UPDATE threads SET done=1 WHERE tid=?", (tid,))
    return {"ok": True, "moved": n}


@app.post("/api/threads/{tid}/read", dependencies=[Depends(auth)])
def read(tid: int):
    t = _thread_or_404(tid)
    _run(lambda: actions.mark_read(db, _acc(t["account"]), tid))
    return {"ok": True}


class ContextIn(BaseModel):
    text: str = ""
    sender: bool = False  # voor alle mail van deze afzender in plaats van enkel deze mail


KIND_HINTS = re.compile(r"\b(reclame|nieuwsbrief|promo\w*|notificatie\w*|melding(en)?|automatische mail)\b", re.I)


@app.post("/api/threads/{tid}/context", dependencies=[Depends(auth)])
def set_context(tid: int, body: ContextIn):
    """Context voor Butler; daarna meteen opnieuw samenvatten (en momenten zoeken als de mail gemarkeerd is)."""
    t = _thread_or_404(tid)
    text = body.text.strip()[:2000] or None
    _, msgs = thread_messages(db, tid)
    incoming = [m for m in msgs if not m["outgoing"]]
    if body.sender:
        if not incoming:
            raise HTTPException(400, "Deze mail heeft geen afzender behalve jezelf.")
        upsert_rule(db, t["account"], incoming[-1]["from_addr"], context=text)
    else:
        db.x("UPDATE threads SET context=? WHERE tid=?", (text, tid))
    # "dit is reclame" of "label als notificatie": dan ook zo sorteren (de afzender), niet enkel in de samenvatting
    kind = KIND_HINTS.search(text or "")
    if kind and incoming:
        kind = "reclame" if kind.group(0).lower().startswith(("reclame", "nieuwsbrief", "promo")) else "notificatie"
        sender_set_rule(RuleIn(account=t["account"], addr=incoming[-1]["from_addr"], kind=kind))
    if ai.active_for(t["account"]):
        _run(lambda: ai.triage(tid))
        if any(m.get("flagged") for m in msgs):
            ai.enqueue_moments(tid)
    return get_thread(tid)


@app.get("/api/dossiers", dependencies=[Depends(auth)])
def list_dossiers():
    """Alle dossiers, met de meest recente activiteit eerst: hoeveel mails, wanneer de laatste, wat eraan komt."""
    now = _now().isoformat()
    rows = db.q("""SELECT d.id, d.name, d.closed_at, COUNT(DISTINCT td.tid) AS n, MAX(m.date) AS last
                   FROM dossiers d JOIN thread_dossiers td ON td.dossier_id=d.id JOIN threads t ON t.tid=td.tid
                   JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                   GROUP BY d.id ORDER BY d.closed_at IS NOT NULL, last DESC""")
    out = []
    for r in rows:
        nxt = db.one("""SELECT mo.title, COALESCE(mo.event_at, mo.until) AS at FROM moments mo
                        JOIN thread_dossiers td ON td.tid=mo.tid WHERE td.dossier_id=? AND mo.until > ?
                        ORDER BY at LIMIT 1""", (r["id"], now))
        out.append({"id": r["id"], "name": r["name"], "n": r["n"], "last": r["last"], "closed": bool(r["closed_at"]),
                    "next": dict(nxt) if nxt else None})
    return {"dossiers": out}


@app.get("/api/dossiers/{did}", dependencies=[Depends(auth)])
def get_dossier(did: int):
    d = dossiers.get(db, did)
    if not d:
        raise HTTPException(404, "dossier niet gevonden")
    cards = []
    for r in db.q("""SELECT td.tid FROM thread_dossiers td JOIN threads t ON t.tid=td.tid
                     JOIN messages m ON m.account=t.account AND m.thread_key=t.thread_key
                     WHERE td.dossier_id=? GROUP BY td.tid ORDER BY MAX(m.date) DESC LIMIT 200""", (did,)):
        t, msgs = thread_messages(db, r["tid"])
        if msgs:
            cards.append(_card(t, msgs))
    tids = {c["tid"] for c in cards}
    upcoming = [r for r in moments.unique([dict(r) for r in db.q("SELECT * FROM moments WHERE until > ? ORDER BY at, id",
                                                                (_now().isoformat(),))]) if r["tid"] in tids]
    by_tid = {c["tid"]: c for c in cards}
    return {"dossier": d, "threads": cards, "moments": [{**by_tid[r["tid"]], "moment": _moment(r)} for r in upcoming]}


@app.post("/api/dossiers/{did}/close", dependencies=[Depends(auth)])
def close_dossier(did: int):
    if not dossiers.get(db, did):
        raise HTTPException(404, "dossier niet gevonden")
    dossiers.close(db, did)
    return get_dossier(did)


@app.post("/api/dossiers/{did}/reopen", dependencies=[Depends(auth)])
def reopen_dossier(did: int):
    if not dossiers.get(db, did):
        raise HTTPException(404, "dossier niet gevonden")
    dossiers.reopen(db, did)
    return get_dossier(did)


class DossierIn(BaseModel):
    name: str


@app.post("/api/threads/{tid}/dossier", dependencies=[Depends(auth)])
def choose_dossier(tid: int, body: DossierIn):
    """Zelf een dossier kiezen (bestaand of nieuw); de AI verandert dat niet meer."""
    _thread_or_404(tid)
    if not body.name.strip():
        raise HTTPException(400, "Geef een naam voor het dossier.")
    dossiers.choose(db, tid, body.name)
    return get_thread(tid)


@app.get("/api/dossier-names", dependencies=[Depends(auth)])
def dossier_names():
    return {"names": [r["name"] for r in db.q("SELECT name FROM dossiers ORDER BY last_at DESC LIMIT 200")]}


@app.post("/api/threads/{tid}/dossier/remove", dependencies=[Depends(auth)])
def remove_from_dossier(tid: int):
    _thread_or_404(tid)
    dossiers.remove(db, tid)
    return get_thread(tid)


class FlagIn(BaseModel):
    flagged: bool = True


@app.post("/api/threads/{tid}/flag", dependencies=[Depends(auth)])
def flag(tid: int, body: FlagIn):
    t = _thread_or_404(tid)
    _run(lambda: actions.set_flag(db, _acc(t["account"]), tid, body.flagged))
    if body.flagged:
        if ai.active_for(t["account"]):
            ai.enqueue_moments(tid)  # meteen kijken wanneer je deze mail nodig hebt
    else:
        db.x("DELETE FROM moments WHERE tid=?", (tid,))
        db.x("UPDATE threads SET moments_ai=0 WHERE tid=?", (tid,))
    return {"ok": True, "flagged": body.flagged}


class DoneIn(BaseModel):
    done: bool = True


@app.post("/api/threads/{tid}/done", dependencies=[Depends(auth)])
def done(tid: int, body: DoneIn):
    _thread_or_404(tid)
    db.x("UPDATE threads SET done=?, snooze_until=NULL WHERE tid=?", (1 if body.done else 0, tid))
    return {"ok": True}


class SnoozeIn(BaseModel):
    days: float | None = None
    until: str | None = None  # ISO-datum


@app.post("/api/threads/{tid}/snooze", dependencies=[Depends(auth)])
def snooze(tid: int, body: SnoozeIn):
    _thread_or_404(tid)
    if body.until:
        until = datetime.fromisoformat(body.until)
        if until.tzinfo is None:
            until = until.replace(hour=8).astimezone()  # 08:00 lokale tijd
        until = until.astimezone(timezone.utc).isoformat()
    elif body.days:
        until = (_now() + timedelta(days=body.days)).isoformat()
    else:
        until = None
    db.x("UPDATE threads SET snooze_until=?, done=0 WHERE tid=?", (until, tid))
    return {"ok": True, "snooze_until": until}


class TagsIn(BaseModel):
    tags: list[str]


@app.post("/api/threads/{tid}/tags", dependencies=[Depends(auth)])
def set_tags(tid: int, body: TagsIn):
    _thread_or_404(tid)
    tags = sorted({t.strip()[:30] for t in body.tags if t.strip()})
    db.x("UPDATE threads SET tags=? WHERE tid=?", (json.dumps(tags), tid))
    return {"ok": True, "tags": tags}


@app.get("/api/tags", dependencies=[Depends(auth)])
def all_tags():
    tags = set()
    for r in db.q("SELECT tags FROM threads WHERE tags != '[]'"):
        tags.update(json.loads(r["tags"]))
    return {"tags": sorted(tags)}


@app.post("/api/threads/{tid}/summarize", dependencies=[Depends(auth)])
def summarize(tid: int):
    t = _thread_or_404(tid)
    if not ai.active_for(t["account"]):
        raise HTTPException(400, "AI staat uit voor deze mailbox")
    _run(lambda: ai.triage(tid))
    return get_thread(tid)


class DraftIn(BaseModel):
    instruction: str = ""


@app.post("/api/threads/{tid}/draft", dependencies=[Depends(auth)])
def draft(tid: int, body: DraftIn):
    _thread_or_404(tid)
    text = _run(lambda: ai.draft(tid, body.instruction))
    return {"draft": text}


class SendIn(BaseModel):
    body: str
    reply_all: bool = False
    archive: bool = False


class DraftSaveIn(BaseModel):
    body: str
    reply_all: bool = False


@app.post("/api/threads/{tid}/save-draft", dependencies=[Depends(auth)])
def save_draft(tid: int, body: DraftSaveIn):
    t = _thread_or_404(tid)
    if not body.body.strip():
        raise HTTPException(400, "Leeg bericht")
    res = _run(lambda: actions.save_draft(db, _acc(t["account"]), tid, body.body, body.reply_all))
    return {"ok": True, **res}


# --- afzenders: reclame & notificaties --------------------------------------
@app.get("/api/senders", dependencies=[Depends(auth)])
def senders(kind: str = "alle", account: str = ""):
    rows = db.q(
        """SELECT m.account, m.from_addr, MAX(m.from_name) AS from_name,
              COUNT(DISTINCT COALESCE(m.message_id, m.id)) AS n,
              SUM(m.in_inbox) AS in_inbox,
              SUM(CASE WHEN m.in_inbox=1 AND m.seen=0 THEN 1 ELSE 0 END) AS unread,
              SUM(CASE WHEN m.folder='INBOX' THEN 1 ELSE 0 END) AS inbox_rows,
              SUM(CASE WHEN m.folder='INBOX' AND m.seen=1 THEN 1 ELSE 0 END) AS inbox_seen,
              MAX(m.date) AS last_date,
              (SELECT m2.kind FROM messages m2 WHERE m2.account=m.account AND m2.from_addr=m.from_addr
                 ORDER BY m2.date DESC LIMIT 1) AS kind,
              (SELECT m3.unsub FROM messages m3 WHERE m3.account=m.account AND m3.from_addr=m.from_addr AND m3.unsub!=''
                 ORDER BY m3.date DESC LIMIT 1) AS unsub,
              MAX(m.unsub_post) AS unsub_post
           FROM messages m
           WHERE m.outgoing=0 AND m.folder != '_pending' AND (?='' OR m.account=?)
             AND (m.bulk=1 OR m.from_addr IN (SELECT addr FROM sender_rules r WHERE r.account=m.account))
           GROUP BY m.account, m.from_addr""",
        (account, account),
    )
    out = []
    for r in rows:
        rule = sender_rule(db, r["account"], r["from_addr"]) or {}
        k = rule.get("kind") if rule.get("kind") is not None else (r["kind"] or "")
        if kind in ("reclame", "notificatie") and k != kind:
            continue
        if kind == "alle" and not k and not rule:
            continue
        method, _ = actions.unsub_method(r["unsub"], r["unsub_post"])
        acc = accounts.get(r["account"])
        out.append({
            "account": r["account"],
            "account_name": acc.name if acc else r["account"],
            "addr": r["from_addr"],
            "name": r["from_name"] or r["from_addr"],
            "avatar": avatars.url(r["from_addr"]),
            "kind": k,
            "n": r["n"],
            "in_inbox": r["in_inbox"] or 0,
            "unread": r["unread"] or 0,
            "read_pct": round(100 * r["inbox_seen"] / r["inbox_rows"]) if r["inbox_rows"] else None,
            "last_date": r["last_date"],
            "unsub_method": method,
            "unsubscribed_at": rule.get("unsubscribed_at"),
            "action": rule.get("action") or "none",
            "after_days": rule.get("after_days") or 7,
        })
    out.sort(key=lambda x: (-x["in_inbox"], -x["n"]))
    cl = APP.get("cleanup") or {}
    # al gearchiveerde reclame of notificaties: kan in één keer naar de prullenbak
    archived = sum(len(actions.kind_rows(db, a, kind, a.folders["archive"])) for a in accounts.values()
                   if kind in ("reclame", "notificatie") and a.folders.get("archive") and (not account or a.id == account))
    return {"senders": out[:300], "archived": archived, "defaults": {"notificatie_days": int(cl.get("notificatie_days", 0) or 0),
                                               "reclame_days": int(cl.get("reclame_days", 0) or 0)}}


class SenderIn(BaseModel):
    account: str
    addr: str


class RuleIn(SenderIn):
    action: str | None = None        # none | keep | archive | trash | archive_after
    after_days: int | None = None
    kind: str | None = None          # 'reclame' | 'notificatie' | 'persoonlijk' | 'auto'


@app.post("/api/senders/rule", dependencies=[Depends(auth)])
def sender_set_rule(body: RuleIn):
    acc = _acc(body.account)
    fields = {}
    if body.action is not None:
        if body.action not in ("none", "keep", "archive", "trash", "archive_after"):
            raise HTTPException(400, "ongeldige actie")
        fields["action"] = body.action
    if body.after_days is not None:
        fields["after_days"] = max(1, min(body.after_days, 365))
    if body.kind is not None:
        kind = {"persoonlijk": "", "auto": None}.get(body.kind, body.kind)
        if kind not in (None, "", "reclame", "notificatie"):
            raise HTTPException(400, "ongeldige soort")
        fields["kind"] = kind
        if kind is not None:  # bestaande mails meteen herclassificeren
            db.x("UPDATE messages SET kind=?, bulk=? WHERE account=? AND from_addr=? AND outgoing=0",
                 (kind, 1 if kind else 0, acc.id, body.addr.lower()))
    rule = upsert_rule(db, acc.id, body.addr, **fields)
    acc.wake.set()  # regels meteen toepassen bij de volgende sync
    return {"ok": True, "rule": rule}


@app.post("/api/senders/archive", dependencies=[Depends(auth)])
def sender_archive(body: SenderIn):
    acc = _acc(body.account)
    n = _run(lambda: actions.move_sender(db, acc, body.addr, "archive"))
    return {"ok": True, "moved": n}


@app.post("/api/senders/trash", dependencies=[Depends(auth)])
def sender_trash(body: SenderIn):
    acc = _acc(body.account)
    n = _run(lambda: actions.move_sender(db, acc, body.addr, "trash"))
    return {"ok": True, "moved": n}


class KindIn(BaseModel):
    kind: str
    account: str = ""
    role: str = "archive"  # of "trash"
    archived: bool = False  # ook wat al in het archief staat (enkel met trash)


@app.post("/api/senders/archive-kind", dependencies=[Depends(auth)])
def sender_archive_kind(body: KindIn):
    if body.kind not in ("reclame", "notificatie") or body.role not in ("archive", "trash"):
        raise HTTPException(400, "ongeldige soort")
    targets = [_acc(body.account)] if body.account else list(accounts.values())
    moved = sum(_run(lambda a=a: actions.move_kind(db, a, body.kind, body.role, body.archived)) for a in targets)
    return {"ok": True, "moved": moved}


@app.post("/api/senders/unsubscribe", dependencies=[Depends(auth)])
def sender_unsubscribe(body: SenderIn):
    acc = _acc(body.account)
    res = _run(lambda: actions.unsubscribe(db, acc, body.addr))
    return {"ok": True, **res}


# --- Web Push --------------------------------------------------------------
@app.get("/api/push/key", dependencies=[Depends(auth)])
def push_key():
    return {"key": push.public_key()}


class SubscribeIn(BaseModel):
    subscription: dict


@app.post("/api/push/subscribe", dependencies=[Depends(auth)])
def push_subscribe(body: SubscribeIn, request: Request):
    try:
        push.save(body.subscription, device_name(request.headers.get("user-agent", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "devices": push.devices()}


class EndpointIn(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe", dependencies=[Depends(auth)])
def push_unsubscribe(body: EndpointIn):
    push.remove(body.endpoint)
    return {"ok": True}


@app.get("/api/push/devices", dependencies=[Depends(auth)])
def push_devices():
    return {"devices": push.devices()}


@app.delete("/api/push/devices/{did}", dependencies=[Depends(auth)])
def push_forget(did: str):
    if not push.forget(did):
        raise HTTPException(404, "toestel niet gevonden")
    return {"ok": True, "devices": push.devices()}


@app.post("/api/push/test", dependencies=[Depends(auth)])
def push_test():
    n = push.send({"title": "Butler", "body": "Meldingen werken. Zo ziet een melding van Butler eruit.",
                   "url": "/", "tag": "butler-test"})
    if not n:
        if push.count():
            raise HTTPException(502, f"Versturen mislukt: {push.last_error or 'onbekende fout'}")
        raise HTTPException(400, "Geen toestel met meldingen aan.")
    return {"ok": True, "sent": n}


# --- configuratie ------------------------------------------------------------
@app.get("/api/config", dependencies=[Depends(auth)])
def get_config():
    return {"sections": config.SECTIONS, "account_fields": config.ACCOUNT_FIELDS, **config.public(SAVED["cfg"]),
            "saved_in_app": SAVED["cfg"] is not CFG or CFG_SOURCE == str(config.data_path(DATA_DIR)), "restarts": RESTART}


class ConfigIn(BaseModel):
    values: dict | None = None
    accounts: list[dict] | None = None


def restart_soon():
    """Na het antwoord stoppen; Docker (restart: unless-stopped) start Butler opnieuw met de nieuwe instellingen."""
    def stop():
        time.sleep(0.8)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=stop, daemon=True, name="herstart").start()


@app.put("/api/config", dependencies=[Depends(auth)])
def put_config(body: ConfigIn):
    try:
        new = config.validate(config.apply(SAVED["cfg"], body.values, body.accounts))
    except config.ConfigError as e:
        raise HTTPException(400, {"message": "Niet bewaard: controleer de aangeduide velden.", "errors": e.errors})
    path = config.save(new, DATA_DIR)
    SAVED["cfg"] = new
    log.info("configuratie bewaard in %s", path)
    if RESTART:
        restart_soon()
    return {"ok": True, "restarting": RESTART}


class AccountTestIn(BaseModel):
    account: dict


@app.post("/api/config/test-account", dependencies=[Depends(auth)])
def test_account(body: AccountTestIn):
    """Aanmelden bij IMAP met deze gegevens (een leeg wachtwoord = het bewaarde), zonder iets te wijzigen."""
    try:
        cfg = config.apply({"accounts": SAVED["cfg"].get("accounts") or []}, accounts=[body.account])["accounts"][0]
        acc = Account(cfg)
    except (config.ConfigError, KeyError) as e:
        raise HTTPException(400, f"Onvolledig: {e}")
    if not acc.password:
        raise HTTPException(400, "Vul een app-wachtwoord in.")
    if not acc.imap_host:
        raise HTTPException(400, "Vul de IMAP-server in.")
    try:
        from imapclient import IMAPClient

        c = IMAPClient(acc.imap_host, port=acc.imap_port, ssl=True, timeout=20)
        c.login(acc.user, acc.password)
        n = len(c.list_folders())
        c.logout()
    except Exception as e:
        raise HTTPException(400, f"Aanmelden lukt niet: {str(e)[:200]}")
    return {"ok": True, "folders": n}


# --- kosten ----------------------------------------------------------------
@app.get("/api/costs", dependencies=[Depends(auth)])
def ai_costs():
    return meter.summary()


# --- ochtendsamenvatting ---------------------------------------------------
def digest_text():
    d = followup()
    mine, theirs, rem = d["waiting_on_me"], d["waiting_on_them"], d["reminders"]
    if not (mine or theirs or rem):
        return None
    lines = []
    high = [c for c in mine if c["priority"] == "hoog"]
    if mine:
        lines.append(f"Wacht op jou: {len(mine)}" + (f" ({len(high)} belangrijk)" if high else ""))
        for c in (high + [c for c in mine if c not in high])[:5]:
            lines.append(f"• {c['who']}: {c['subject']}")
    if rem:
        lines.append(f"Herinneringen: {len(rem)}")
        for c in rem[:3]:
            lines.append(f"• {c['subject']}")
    if theirs:
        lines.append(f"Wacht op antwoord: {len(theirs)}")
        for c in theirs[:3]:
            lines.append(f"• {c['subject']} ({int(c['age_days'])} d)")
    return "\n".join(lines)


def digest_loop():
    import time as _t
    from zoneinfo import ZoneInfo

    when = (CFG.get("notify") or {}).get("digest_time") or ""
    days = (CFG.get("notify") or {}).get("digest_days") or [1, 2, 3, 4, 5]
    if not when:
        return
    tz = ZoneInfo(APP.get("timezone", "Europe/Brussels"))
    last = None
    while True:
        now = datetime.now(tz)
        if now.strftime("%H:%M") == when and now.isoweekday() in days and last != now.date():
            last = now.date()
            try:
                text = digest_text()
                if text:
                    notifier.send("Butler – opvolgen", text, extra={"type": "digest"}, force=True)
            except Exception:
                log.exception("ochtendsamenvatting mislukt")
        _t.sleep(20)


class ForwardIn(BaseModel):
    message_id: int
    to: list[str]
    note: str = ""


@app.post("/api/threads/{tid}/forward", dependencies=[Depends(auth)])
def forward_message(tid: int, body: ForwardIn):
    t = _thread_or_404(tid)
    to = [a.strip() for a in body.to if a.strip()]
    if not to or not all(re.match(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$", a) for a in to):
        raise HTTPException(400, "Geef een geldig e-mailadres.")
    if not db.one("SELECT 1 FROM messages WHERE id=? AND account=?", (body.message_id, t["account"])):
        raise HTTPException(404, "bericht niet gevonden")
    res = _run(lambda: actions.forward(db, _acc(t["account"]), body.message_id, to, body.note))
    return {"ok": True, **res}


@app.get("/api/recipients", dependencies=[Depends(auth)])
def recipients():
    return {"recipients": forward.recipients(db)}


@app.post("/api/threads/{tid}/send", dependencies=[Depends(auth)])
def send(tid: int, body: SendIn):
    t = _thread_or_404(tid)
    if not body.body.strip():
        raise HTTPException(400, "Leeg bericht")
    acc = _acc(t["account"])
    res = _run(lambda: actions.send_reply(db, acc, tid, body.body, body.reply_all))
    if body.archive:
        _run(lambda: actions.move_thread(db, acc, tid, "archive"))
    return {"ok": True, **res}
