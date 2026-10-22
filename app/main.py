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

from . import actions
from .accounts import Account
from .ai import AIWorker
from .db import DB, thread_messages
from .notify import Notifier
from .sync import Syncer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("postvak")

CFG_PATH = os.environ.get("POSTVAK_CONFIG", "/config/config.yaml")
with open(CFG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}
APP = CFG.get("app") or {}
AI_CFG = CFG.get("ai") or {}

db = DB(os.environ.get("POSTVAK_DB", "/data/postvak.db"))
accounts = {str(a["id"]): Account(a) for a in (CFG.get("accounts") or [])}
notifier = Notifier(CFG.get("notify") or {}, APP)
ai = AIWorker(db, AI_CFG, accounts, notifier)
syncer = Syncer(db, APP, AI_CFG, accounts, ai, notifier)
FOLLOWUP_DAYS = int(APP.get("followup_days", 3))
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_):
    if os.environ.get("POSTVAK_NO_SYNC") != "1":
        ai.start()
        syncer.start()
        import threading
        threading.Thread(target=digest_loop, daemon=True, name="digest").start()
    yield


app = FastAPI(title="Postvak", lifespan=lifespan, docs_url=None, redoc_url=None)


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


@app.get("/health")
def health():
    return {"ok": True}


# --- API ------------------------------------------------------------------
@app.get("/api/accounts", dependencies=[Depends(auth)])
def get_accounts():
    return {"accounts": [a.public() for a in accounts.values()], "ai": ai.enabled, "followup_days": FOLLOWUP_DAYS}


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
        if filter == "nieuwsbrieven" and not card["bulk"]:
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
                    notifier.send("Postvak – opvolgen", text, extra={"type": "digest"}, force=True)
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
