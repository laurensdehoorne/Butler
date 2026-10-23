"""Aanmelden met naam en wachtwoord, met per gebruiker een eigen Butler: eigen database, mailaccounts en instellingen.

De gateway staat voor alles. Per gebruiker draait een gewone Butler (app.main) als apart proces op 127.0.0.1, met
eigen BUTLER_DB en BUTLER_CONFIG; de gateway stuurt elk verzoek door naar de Butler van wie aangemeld is. Zo blijft de
mail van de ene gebruiker volledig gescheiden van die van de andere, en hoeft Butler zelf niets van gebruikers te weten.

- /data/users.json: de gebruikers, met een scrypt-hash van het wachtwoord. De eerste gebruiker (de beheerder) gebruikt
  de bestaande /data; elke volgende krijgt /data/users/<id>.
- Een sessie is een ondertekende cookie (HttpOnly, 180 dagen): de app op je beginscherm blijft aangemeld.
- Een Butler die herstart (na het bewaren van instellingen) start de gateway meteen opnieuw; tot hij klaar is, wachten
  de verzoeken even.

Starten: python -m app.gateway (zo in de Dockerfile).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from . import config

log = logging.getLogger("butler.gateway")

DATA = Path(os.environ.get("BUTLER_DATA", "/data"))
MAIN_CONFIG = os.environ.get("BUTLER_CONFIG", "/config/config.yaml")
USERS_PATH = DATA / "users.json"
KEY_PATH = DATA / "gateway.key"
STATIC = Path(__file__).parent / "static"
COOKIE = "butler_session"
SESSION_DAYS = 180
FIRST_PORT = 9101
# wat iedereen mag zien zonder aan te melden: de app-schil (icoon, manifest, lettertype, service worker)
PUBLIC = {"/manifest.webmanifest", "/icon.svg", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png",
          "/badge-96.png", "/sw.js", "/fonts/bodoni-moda-latin.woff2", "/health"}
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length", "cookie"}
# deze AI-instellingen neemt een nieuwe gebruiker over van de beheerder (de sleutels); naam en handtekening niet
AI_SHARED = ("enabled", "provider", "api_key", "triage_model", "draft_model", "openai_api_key", "openai_triage_model",
             "openai_draft_model", "initial_days")


# --- gebruikers ---------------------------------------------------------------
def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(str.maketrans("áàâäéèêëíìîïóòôöúùûüç", "aaaaeeeeiiiioooouuuuc"))).strip("-")
    return s[:40] or "gebruiker"


def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_password(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_password(pw, base64.b64decode(salt)), stored)
    except Exception:
        return False


class Users:
    def __init__(self, path=USERS_PATH):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = self._read()

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"users": []}

    def _write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def all(self):
        return list(self.data["users"])

    def get(self, uid):
        return next((u for u in self.data["users"] if u["id"] == uid), None)

    def find(self, login):
        login = (login or "").strip().lower()
        return next((u for u in self.data["users"] if login in (u["id"], u["name"].lower())), None)

    def add(self, name, password, admin=False):
        name = name.strip()
        if not name or len(password) < 8:
            raise ValueError("Geef een naam en een wachtwoord van minstens 8 tekens.")
        with self.lock:
            uid = slug(name)
            if self.get(uid):
                raise ValueError(f"Er is al een gebruiker {name}.")
            first = not self.data["users"]
            port = max([u["port"] for u in self.data["users"]], default=FIRST_PORT - 1) + 1
            data = DATA if first else DATA / "users" / uid
            u = {"id": uid, "name": name, "hash": hash_password(password), "admin": admin or first, "port": port,
                 "data": str(data), "config": MAIN_CONFIG if first else str(data / "start.yaml")}
            if not first:
                data.mkdir(parents=True, exist_ok=True)
                seed_config(data, name)
            self.data["users"].append(u)
            self._write()
            return u

    def set_password(self, uid, password):
        if len(password) < 8:
            raise ValueError("Een wachtwoord heeft minstens 8 tekens.")
        with self.lock:
            u = self.get(uid)
            if not u:
                raise ValueError("Onbekende gebruiker.")
            u["hash"] = hash_password(password)
            self._write()


def seed_config(data_dir, name):
    """Een nieuwe gebruiker start met de AI-sleutels van de beheerder (aan te passen in Instellingen), zonder mail."""
    admin_cfg, _ = config.load(str(DATA), MAIN_CONFIG)
    ai = {k: v for k, v in (admin_cfg.get("ai") or {}).items() if k in AI_SHARED}
    ai["name"] = name.split()[0].split("-")[0] if name else ""
    cfg = {"app": {"timezone": (admin_cfg.get("app") or {}).get("timezone", "Europe/Brussels")}, "ai": ai, "accounts": []}
    config.save(cfg, str(data_dir))


def migrate(users):
    """Eerste start met de gateway: de bestaande Butler wordt de beheerder, met het oude toegangstoken als wachtwoord."""
    if users.all():
        return
    cfg, _ = config.load(str(DATA), MAIN_CONFIG)
    token = ((cfg.get("app") or {}).get("token") or "").strip()
    name = ((cfg.get("ai") or {}).get("name") or "").strip() or "Beheerder"
    if len(token) >= 8:
        users.add(name, token, admin=True)
        log.info("gebruiker %s aangemaakt; wachtwoord = het vroegere toegangstoken", name)


# --- sessies ------------------------------------------------------------------
def _key():
    try:
        return KEY_PATH.read_bytes()
    except FileNotFoundError:
        k = secrets.token_bytes(32)
        KEY_PATH.write_bytes(k)
        os.chmod(KEY_PATH, 0o600)
        return k


def sign(uid, key, days=SESSION_DAYS):
    exp = str(int(time.time()) + days * 86400)
    sig = hmac.new(key, f"{uid}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{uid}|{exp}|{sig}"


def verify(cookie, key):
    try:
        uid, exp, sig = (cookie or "").split("|")
    except ValueError:
        return None
    good = hmac.new(key, f"{uid}|{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or int(exp) < time.time():
        return None
    return uid


# --- Butler per gebruiker -------------------------------------------------------
class Backends:
    """Start per gebruiker een Butler en zet hem terug op als hij stopt (bv. na het bewaren van instellingen)."""

    def __init__(self, users, secret):
        self.users, self.secret = users, secret
        self.procs, self.stopping = {}, False

    def start_all(self):
        for u in self.users.all():
            self.start(u)

    def start(self, u):
        if u["id"] in self.procs:
            return
        self.procs[u["id"]] = None
        threading.Thread(target=self._keep, args=(u,), daemon=True, name=f"butler-{u['id']}").start()

    def _keep(self, u):
        env = {**os.environ, "BUTLER_DB": str(Path(u["data"]) / "butler.db"), "BUTLER_CONFIG": u["config"],
               "BUTLER_GATEWAY_SECRET": self.secret}
        while not self.stopping:
            p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                  "--port", str(u["port"]), "--proxy-headers", "--log-level", "warning"], env=env)
            self.procs[u["id"]] = p
            p.wait()
            if not self.stopping:
                log.info("Butler van %s stopte (%s); opnieuw starten", u["id"], p.returncode)
                time.sleep(1)

    def stop(self):
        self.stopping = True
        for p in self.procs.values():
            if p and p.poll() is None:
                p.terminate()


# --- de app ---------------------------------------------------------------------
def create_app(users=None, key=None, backends=None, transport=None):
    users = users or Users()
    key = key or _key()
    secret = secrets.token_urlsafe(32)
    backends = backends if backends is not None else Backends(users, secret)
    secret = getattr(backends, "secret", secret)
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=2), transport=transport)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.state.users, app.state.backends = users, backends

    def current(request):
        return users.get(verify(request.cookies.get(COOKIE), key) or "")

    def need_login(request):
        if request.url.path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"detail": "Meld je aan."}, status_code=401, headers={"X-Butler-Login": "/login"})
        return RedirectResponse("/login", status_code=303)

    def session(request, resp, u):
        resp.set_cookie(COOKIE, sign(u["id"], key), max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")
        return resp

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/login")
    def login_page():
        html = (STATIC / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{SETUP}}", "1" if not users.all() else "0"), headers={"Cache-Control": "no-store"})

    class LoginIn(BaseModel):
        name: str
        password: str

    @app.post("/login")
    async def login(body: LoginIn, request: Request):
        if not users.all():  # eerste keer: de beheerder aanmaken
            try:
                u = users.add(body.name, body.password, admin=True)
            except ValueError as e:
                raise HTTPException(400, str(e))
            backends.start(u)
            return session(request, JSONResponse({"ok": True}), u)
        u = users.find(body.name)
        if not u or not check_password(body.password, u["hash"]):
            await asyncio.sleep(1)  # raden traag maken
            raise HTTPException(401, "Naam of wachtwoord klopt niet.")
        return session(request, JSONResponse({"ok": True}), u)

    @app.post("/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me(request: Request):
        u = current(request)
        if not u:
            return need_login(request)
        # iedereen ziet de anderen (om mail mee te delen); enkel de beheerder beheert ze
        return {"id": u["id"], "name": u["name"], "admin": u["admin"],
                "users": [{"id": x["id"], "name": x["name"], "admin": x["admin"]} for x in users.all()]}

    class ShareIn(BaseModel):
        tid: int
        to: str
        note: str = ""

    @app.post("/api/share")
    async def share(body: ShareIn, request: Request):
        """Een kopie van een thread naar de Butler van een andere gebruiker: tekst, HTML en bijlagen."""
        u = current(request)
        if not u:
            return need_login(request)
        to = users.get(body.to)
        if not to or to["id"] == u["id"]:
            raise HTTPException(400, "Kies iemand anders om mee te delen.")
        h = {"x-butler-gateway": secret}
        r = await client.get(f"http://127.0.0.1:{u['port']}/api/threads/{body.tid}/export", headers=h)
        if r.status_code != 200:
            raise HTTPException(r.status_code, "Kon de mail niet ophalen om te delen.")
        backends.start(to)
        for _ in range(40):
            try:
                r2 = await client.post(f"http://127.0.0.1:{to['port']}/api/shared", headers=h,
                                       json={"sender": u["name"], "note": body.note, "thread": r.json()})
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.5)
        else:
            raise HTTPException(503, f"De Butler van {to['name']} start nog op; probeer zo opnieuw.")
        if r2.status_code != 200:
            raise HTTPException(502, "Delen mislukte.")
        return {"ok": True, "to": to["name"]}

    class UserIn(BaseModel):
        name: str = ""
        password: str

    @app.post("/api/users")
    def add_user(body: UserIn, request: Request):
        u = current(request)
        if not u or not u["admin"]:
            raise HTTPException(403, "Enkel de beheerder kan gebruikers toevoegen.")
        try:
            new = users.add(body.name, body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        backends.start(new)
        return {"id": new["id"], "name": new["name"]}

    @app.put("/api/users/{uid}/password")
    def set_password(uid: str, body: UserIn, request: Request):
        u = current(request)
        if not u or (u["id"] != uid and not u["admin"]):
            raise HTTPException(403, "Niet toegestaan.")
        try:
            users.set_password(uid, body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        if "/" + path in PUBLIC:
            f = STATIC / path
            if f.is_file():
                return FileResponse(f, headers={"Cache-Control": "no-cache"})
        u = current(request)
        if not u:
            return need_login(request)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "authorization"}
        headers["x-butler-gateway"] = secret
        headers["x-forwarded-proto"] = request.url.scheme
        q = request.url.query
        url = f"http://127.0.0.1:{u['port']}/{path}" + (f"?{q}" if q else "")
        body = await request.body()
        for attempt in range(40):  # Butler start (opnieuw) op: even wachten
            try:
                upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                             stream=True)
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.5)
        else:
            return JSONResponse({"detail": "Butler start op, probeer zo opnieuw."}, status_code=503)
        out = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP}

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(stream(), status_code=upstream.status_code, headers=out)

    return app


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    DATA.mkdir(parents=True, exist_ok=True)
    users = Users()
    migrate(users)
    backends = Backends(users, secrets.token_urlsafe(32))
    backends.start_all()
    app = create_app(users, backends=backends)
    try:  # uvicorn stopt netjes bij SIGTERM; daarna de Butlers van de gebruikers
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8088")), proxy_headers=True,
                    forwarded_allow_ips="*")
    finally:
        backends.stop()


if __name__ == "__main__":
    main()
