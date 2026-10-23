from email.message import EmailMessage
from unittest import mock

from app.mailhtml import MailImages
from app.mailparse import clean_html, parse
from tests.conftest import H, put, raw

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40


def html_mail(html, cid_png=None, subject="Boeking bevestigd", mid="<h1@af>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "Air France <info@airfrance.fr>", "me@icloud.com", subject, mid
    m["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    m.set_content("Booking confirmed Y9G296")
    m.add_alternative(html, subtype="html")
    if cid_png:
        m.get_payload()[1].add_related(cid_png, "image", "png", cid="<logo@af>")
    return m.as_bytes()


def test_clean_html_removes_everything_active():
    out = clean_html('<p onclick="x()">Hoi</p><script>alert(1)</script><a href="javascript:x()">a</a>'
                     '<form action="https://evil"><input name="pw"><b>tekst</b></form><iframe src="https://x"></iframe>'
                     '<meta http-equiv="refresh" content="0;url=https://x"><link rel="stylesheet" href="https://x/a.css">'
                     '<img src="cid:logo@af">', {"logo@af": "data:image/png;base64,AAA"})
    assert "script" not in out and "onclick" not in out and "javascript" not in out and "iframe" not in out
    assert "<form" not in out and "<input" not in out and "<b>tekst</b>" in out
    assert "refresh" not in out and "stylesheet" not in out
    assert 'src="data:image/png;base64,AAA"' in out


def test_parse_keeps_html_with_inline_images():
    p = parse(html_mail('<h1>Booking confirmed</h1><img src="cid:logo@af">', cid_png=PNG))
    assert "<h1>Booking confirmed</h1>" in p["html"] and "data:image/png;base64," in p["html"]
    assert p["body"] == "Booking confirmed Y9G296"  # de tekst blijft voor AI, zoeken en snippet
    assert parse(raw("A <a@x.be>", "tekst", "<t@x>"))["html"] == ""


def test_remote_images_are_signed_and_proxied():
    im = MailImages(b"k" * 32)
    out = im.rewrite('<img src="https://cdn.af.com/logo.png" srcset="x 2x"><td background="http://x.be/bg.jpg">'
                     '<div style="background:url(https://x.be/a.png)"></div><style>.h{background:url("https://x.be/b.png")}</style>'
                     '<img src="data:image/png;base64,AAA">')
    assert "cdn.af.com" not in out and "x.be" not in out and "srcset" not in out
    assert out.count("/img/") == 4 and 'src="data:image/png;base64,AAA"' in out
    sig, enc = out.split('src="/img/')[1].split('"')[0].split("/")
    assert im.unpack(sig, enc) == "https://cdn.af.com/logo.png"
    assert im.unpack("0" * 32, enc) is None  # geen open proxy
    off = MailImages(b"k" * 32, enabled=False).rewrite('<img src="https://cdn.af.com/logo.png">')
    assert 'src=""' in off


def test_image_route(app, client):
    url = app.images.proxied("https://cdn.af.com/logo.png")
    assert client.get(url.replace(url.split("/")[2], "0" * 32)).status_code == 404
    with mock.patch("app.main.fetch_image", return_value=(PNG, "image/png")) as fetch:
        r = client.get(url)  # zonder token: een <img> kan er geen meesturen
    fetch.assert_called_once_with("https://cdn.af.com/logo.png")
    assert r.status_code == 200 and r.content == PNG and "sandbox" in r.headers["content-security-policy"]


def test_thread_html_inbox_now_archive_on_demand(app, client, acc):
    tid, _ = put(acc, "INBOX", 1, html_mail('<p>Inbox <img src="https://cdn.af.com/a.png"></p>'))
    msg = client.get(f"/api/threads/{tid}", headers=H).json()["messages"][0]
    assert "<p>Inbox" in msg["html"] and "/img/" in msg["html"]

    tid2, _ = put(acc, "Archive", 7, html_mail("<p>Archief</p>", subject="Oud", mid="<h2@af>"))
    msg2 = client.get(f"/api/threads/{tid2}", headers=H).json()["messages"][0]
    assert msg2["html"] is None  # nog niet opgehaald
    data = html_mail("<p>Archief</p>", subject="Oud", mid="<h2@af>")
    imap = mock.MagicMock()
    imap.fetch.side_effect = [{7: {b"RFC822.SIZE": len(data)}}, {7: {b"BODY[]": data}}]
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)):
        r = client.get(f"/api/messages/{msg2['id']}/html", headers=H).json()
        again = client.get(f"/api/messages/{msg2['id']}/html", headers=H).json()  # nu uit de cache
    assert "<p>Archief</p>" in r["html"] and again == r
    imap.select_folder.assert_called_once_with("Archive", readonly=True)
    assert client.get(f"/api/threads/{tid2}", headers=H).json()["messages"][0]["html"].startswith("<p>Archief")


def test_empty_text_part_falls_back_to_html():
    from email.message import EmailMessage

    from app.mailparse import parse
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "Home Connect <info@hc.com>", "me@x.be", "Migration", "<m@hc>"
    m.set_content("͏͏  \n")
    m.add_alternative("<html><body><p>Your Home Connect account requires migration by 26.11.2026.</p></body></html>", subtype="html")
    assert "requires migration" in parse(m.as_bytes())["body"]
