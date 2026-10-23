import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import actions, costs
from .accounts import Account
from .ai import PROVIDERS, AIError, AIWorker
from .db import DB, sender_rule, thread_messages, upsert_rule
from .notify import Notifier
from .push import Push, device_name
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("butler")

CFG_PATH = os.environ.get("BUTLER_CONFIG", "/config/config.yaml")
with open(CFG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}
APP = CFG.get("app") or {}
AI_CFG = CFG.get("ai") or {}

DB_PATH = os.environ.get("BUTLER_DB", "/data/butler.db")
DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
db = DB(DB_PATH)
accounts = {str(a["id"]): Account(a) for a in (CFG.get("accounts") or [])}
push = Push(db, DATA_DIR, (CFG.get("notify") or {}).get("push_contact", ""))
notifier = Notifier(CFG.get("notify") or {}, APP, push)
costs.load_overrides(DATA_DIR)
meter = costs.Meter(db)
ai = AIWorker(db, AI_CFG, accounts, notifier, meter)
syncer = Syncer(db, APP, AI_CFG, accounts, ai, notifier)
FOLLOWUP_DAYS = int(APP.get("followup_days", 3))
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_):
    if os.environ.get("BUTLER_NO_SYNC") != "1":
        ai.start()
        syncer.start()
        import threading
        threading.Thread(target=digest_loop, daemon=True, name="digest").start()
    yield


app = FastAPI(title="Butler", lifespan=lifespan, docs_url=None, redoc_url=None)


def auth(request: Request):
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
        "snippet": last["snippet"],
        "date": last["date"],
        "n": len(msgs),
        "unread": sum(1 for m in msgs if m["in_inbox"] and not m["seen"]),
        "in_inbox": any(m["in_inbox"] for m in msgs),
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
           WHERE t.done=0 AND (?='' OR t.account=?) GROUP BY t.tid ORDER BY MAX(m.date) DESC LIMIT 1500""",
        (account, account),
    )
    mine, theirs, reminders = [], [], []
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
        acc = accounts.get(t["account"])
        if not last["outgoing"]:
            if last["bulk"] or not any(m["in_inbox"] for m in msgs):
                continue
            if t["action"] in (None, "antwoorden", "actie"):
                mine.append(_card(t, msgs))
        else:
            if _age_days(last["date"]) < FOLLOWUP_DAYS:
                continue
            to = [a for _, a in json.loads(last["to_addrs"] or "[]")]
            if acc and not [a for a in to if not acc.is_me(a)]:
                continue
            theirs.append(_card(t, msgs))
    mine.sort(key=lambda c: (c["priority"] != "hoog", c["date"] or ""))
    theirs.sort(key=lambda c: c["date"] or "")
    return {"waiting_on_me": mine[:150], "waiting_on_them": theirs[:150], "reminders": reminders[:50],
            "followup_days": FOLLOWUP_DAYS}


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
            "to": json.loads(m["to_addrs"] or "[]"),
            "cc": json.loads(m["cc_addrs"] or "[]"),
            "date": m["date"],
            "subject": m["subject"],
            "body": (m["body"] or "")[:30000],
            "outgoing": bool(m["outgoing"]),
            "pending": m["folder"] == "_pending",
        }
        for m in msgs
    ]
    card["ai_available"] = ai.active_for(t["account"])
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
    return {"senders": out[:300], "defaults": {"notificatie_days": int(cl.get("notificatie_days", 0) or 0),
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
        raise HTTPException(400, "Geen toestel met meldingen aan.")
    return {"ok": True, "sent": n}


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
