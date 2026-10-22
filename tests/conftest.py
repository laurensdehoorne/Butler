"""Gedeelde opzet: een tijdelijke config en database, zonder IMAP-verbindingen (BUTLER_NO_SYNC=1).

app.main leest de config bij het importeren, dus de omgeving wordt gezet vóór de import.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="butler-test-")
(Path(_tmp) / "config.yaml").write_text("""
app: {token: "t0k", base_url: "https://butler.test", followup_days: 3}
ai: {enabled: true, provider: claude, api_key: "sk-ant-test", openai_api_key: "sk-oa-test", name: Jan,
     owner: "Jan, test", signature: "Jan"}
notify: {mode: all, webpush: true}
accounts:
  - {id: icloud, name: iCloud, provider: icloud, email: me@icloud.com, display_name: Me, password: x}
""", encoding="utf-8")
os.environ.update(BUTLER_CONFIG=f"{_tmp}/config.yaml", BUTLER_DB=f"{_tmp}/butler.db", BUTLER_NO_SYNC="1")

import app.main as M  # noqa: E402
from app.mailparse import parse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TABLES = ("messages", "threads", "folder_state", "sender_rules", "settings", "push_subs", "ai_usage")
H = {"Authorization": "Bearer t0k"}


@pytest.fixture(autouse=True)
def clean():
    for t in TABLES:
        M.db.x(f"DELETE FROM {t}")
    acc = M.accounts["icloud"]
    acc.folders = {"inbox": "INBOX", "sent": "Sent Messages", "archive": "Archive", "trash": "Deleted Messages",
                   "drafts": "Drafts"}
    M.ai._clients.clear()
    M.ai._no_reasoning.clear()
    yield


@pytest.fixture
def app():
    return M


@pytest.fixture
def acc():
    return M.accounts["icloud"]


@pytest.fixture
def client():
    return TestClient(M.app)


def raw(frm, subject, mid, *, to="me@icloud.com", days=0, body="Hallo", irt=None, refs=None, unsub=None,
        one_click=False, html=False, extra=""):
    d = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    h = f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: {mid}\r\nDate: {d}\r\n"
    if irt:
        h += f"In-Reply-To: {irt}\r\n"
    if refs:
        h += f"References: {refs}\r\n"
    if unsub:
        h += f"List-Unsubscribe: {unsub}\r\n"
    if one_click:
        h += "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
    h += extra
    h += f"Content-Type: {'text/html' if html else 'text/plain'}; charset=utf-8\r\nMIME-Version: 1.0\r\n\r\n"
    return (h + body).encode()


def put(acc, folder, uid, data, seen=False):
    role = "inbox" if folder == "INBOX" else "sent"
    return M.syncer.store(acc, folder, role, uid, parse(data), seen)
