import hashlib
import shutil
import time
from unittest import mock

import httpx
import pytest

from app import avatars as A
from tests.conftest import H, put, raw

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="9" height="9"/></svg>'


@pytest.fixture
def av(app):
    """De gedeelde cache van de app, leeg aan het begin van elke test."""
    a = app.avatars
    for p in a.dir.iterdir():
        if p.name != ".key":
            p.unlink() if p.is_file() else shutil.rmtree(p)
    a._known.clear()
    return a


def test_keys_skip_logo_for_free_mail(av):
    assert av.keys("piet@gmail.com") == [("face", "piet@gmail.com")]
    assert av.keys("nieuws@coolblue.be") == [("face", "nieuws@coolblue.be"), ("logo", "coolblue.be")]
    assert A.domains("nieuws.coolblue.be") == ["nieuws.coolblue.be", "coolblue.be"]
    assert A.domains("mail.bbc.co.uk") == ["mail.bbc.co.uk", "bbc.co.uk"]


def test_unknown_until_fetched_then_cached(av):
    assert av.url("Piet@Firma.be") is None  # nog niets in de cache (en geen achtergronddraad in tests)
    with mock.patch.object(av, "_face", return_value=(PNG, "png")), mock.patch.object(av, "_logo", return_value=None):
        av.fetch("piet@firma.be")
    url = av.url("piet@firma.be")
    assert url.startswith("/avatars/") and url.endswith("-face.png")
    assert "piet" not in url and hashlib.sha256(b"piet@firma.be").hexdigest()[:32] not in url  # niet te raden
    av._known.clear()  # ook na een herstart: van schijf
    assert av.url("piet@firma.be") == url


def test_logo_when_no_face_and_retry_after_negative(av):
    with mock.patch.object(av, "_face", return_value=None), mock.patch.object(av, "_logo", return_value=(SVG, "svg")):
        av.fetch("info@firma.be")
    assert av.url("info@firma.be").endswith("-logo.svg")
    assert av.lookup("face", "info@firma.be") == ""  # niets gevonden: niet meteen opnieuw proberen
    later = time.time() + (A.RETRY_DAYS + 1) * 86400
    with mock.patch.object(A.time, "time", return_value=later):
        assert av.lookup("face", "info@firma.be") is None


def test_served_sandboxed_and_names_validated(av, client):
    with mock.patch.object(av, "_face", return_value=None), mock.patch.object(av, "_logo", return_value=(SVG, "svg")):
        av.fetch("info@firma.be")
    r = client.get(av.url("info@firma.be"))  # zonder token: <img> kan geen header meesturen
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    for bad in ("x.png", "../butler.db", ".key", "0" * 32 + "-face.none", "0" * 32 + "-face.png"):
        assert client.get(f"/avatars/{bad}").status_code == 404


def test_avatar_in_cards_messages_and_senders(av, client, acc):
    tid, _ = put(acc, "INBOX", 1, raw("Piet <piet@firma.be>", "Offerte", "<p1@x>", body="Kan je dit bevestigen?"))
    put(acc, "INBOX", 2, raw("Shop <nieuws@shop.be>", "Promo", "<s1@x>", unsub="<https://shop.be/u>"))
    with mock.patch.object(av, "_face", return_value=(PNG, "png")), mock.patch.object(av, "_logo", return_value=None):
        av.fetch("piet@firma.be")
        av.fetch("nieuws@shop.be")
    card = client.get("/api/followup", headers=H).json()["waiting_on_me"][0]
    assert card["avatar"] == av.url("piet@firma.be")
    assert client.get(f"/api/threads/{tid}", headers=H).json()["messages"][0]["avatar"] == card["avatar"]
    sender = client.get("/api/senders?kind=reclame", headers=H).json()["senders"][0]
    assert sender["avatar"] == av.url("nieuws@shop.be")


def test_icon_links_prefer_apple_touch_icon():
    html = """<link rel="icon" href="/favicon-16.png" sizes="16x16"><link rel="icon" href="/big.png" sizes="192x192">
              <link rel="apple-touch-icon" href="https://cdn.firma.be/touch.png"><link rel="mask-icon" href="/m.svg">
              <link rel="icon" href="http://onveilig.be/i.png">"""
    assert A.icon_links(html, "https://firma.be/") == [
        "https://cdn.firma.be/touch.png", "https://firma.be/big.png", "https://firma.be/favicon-16.png"]


def test_icon_falls_back_to_favicon(av):
    calls = []

    def get(url, want):
        calls.append(url)
        if want == "html":
            return '<link rel="icon" href="/weg.png">', "https://firma.be/"
        return (PNG, "png") if url.endswith("favicon.ico") else None

    with mock.patch.object(av, "_get", side_effect=get), mock.patch.object(av, "_bimi", return_value=None):
        assert av._logo("firma.be") == (PNG, "png")
    assert calls == ["https://firma.be/", "https://firma.be/weg.png", "https://firma.be/favicon.ico"]


def test_gravatar_uses_sha256(av):
    with mock.patch.object(av, "_get", return_value=(PNG, "jpg")) as get:
        av._face("piet@firma.be")
    h = hashlib.sha256(b"piet@firma.be").hexdigest()
    assert get.call_args[0][0] == f"https://gravatar.com/avatar/{h}?s=160&d=404"


def test_never_fetches_from_private_addresses(av):
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "localhost"):
        with pytest.raises(A.NotPublic):
            A.check_public(httpx.Request("GET", f"https://{host}/favicon.ico"))
    with mock.patch.object(httpx.HTTPTransport, "handle_request") as send:
        assert av._get("https://127.0.0.1/favicon.ico", "image") is None
        assert av._get("http://firma.be/favicon.ico", "image") is None  # enkel https
    send.assert_not_called()
