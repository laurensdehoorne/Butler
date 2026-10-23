import time
from email.message import EmailMessage
from unittest import mock

from app.mailparse import parse
from tests.conftest import H, put

PDF = b"%PDF-1.4\n" + b"0" * 200
JPG = b"\xff\xd8\xff" + b"1" * 200


def mail_with_attachments(mid="<t1@af>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "Air France <af@airfrance.fr>", "me@icloud.com", "E-ticket", mid
    m["Date"] = "Fri, 23 Oct 2026 10:00:00 +0000"
    m.set_content("Uw e-ticket zit in bijlage.")
    m.add_alternative('<p>Uw e-ticket <img src="cid:logo@af"></p>', subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG" + b"2" * 50, "image", "png", cid="<logo@af>")  # in de HTML: geen bijlage
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="E-ticket AF1068.pdf")
    m.add_attachment(JPG, maintype="image", subtype="jpeg", filename="boarding.jpg")
    m.add_attachment(b"<script>alert(1)</script>", maintype="text", subtype="html", filename="factuur.html")
    return m.as_bytes()


def test_parse_lists_real_attachments_only():
    atts = parse(mail_with_attachments())["attachments"]
    assert [(a["name"], a["type"], a["size"]) for a in atts] == [
        ("E-ticket AF1068.pdf", "application/pdf", len(PDF)), ("boarding.jpg", "image/jpeg", len(JPG)),
        ("factuur.html", "text/html", 25)]


def imap_for(data, uid):
    imap = mock.MagicMock()
    imap.fetch.side_effect = lambda uids, items: (
        {uid: {b"RFC822.SIZE": len(data)}} if items == ["RFC822.SIZE"] else {uid: {b"BODY[]": data}})
    return imap


def test_thread_lists_attachments_and_opens_them_on_demand(app, client, acc):
    data = mail_with_attachments()
    tid, _ = put(acc, "INBOX", 5, data)
    atts = client.get(f"/api/threads/{tid}", headers=H).json()["messages"][0]["attachments"]
    assert [(a["name"], a["inline"]) for a in atts] == [("E-ticket AF1068.pdf", True), ("boarding.jpg", True), ("factuur.html", False)]

    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap_for(data, 5))):
        pdf = client.get(atts[0]["url"])  # zonder token: een gewone link
        html = client.get(atts[2]["url"])
    assert pdf.status_code == 200 and pdf.content == PDF and pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("inline") and "sandbox" not in pdf.headers["content-security-policy"]
    # html nooit als pagina in Butler: downloaden, in een sandbox
    assert html.headers["content-type"] == "application/octet-stream" and html.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in html.headers["content-security-policy"]


def test_links_are_signed_and_expire(app, client, acc):
    tid, _ = put(acc, "INBOX", 5, mail_with_attachments())
    url = client.get(f"/api/threads/{tid}", headers=H).json()["messages"][0]["attachments"][0]["url"]
    assert client.get(url.replace(url.split("/")[2], "0" * 32)).status_code == 404  # verkeerde handtekening
    path, exp = url.split("?e=")
    assert client.get(f"{path}?e={int(exp) + 1}").status_code == 404  # vervaldatum aangepast
    with mock.patch("app.main.time.time", return_value=int(exp) + 10):
        assert client.get(url).status_code == 404  # vervallen


def test_older_mail_gets_its_list_once(app, client, acc):
    data = mail_with_attachments("<old@af>")
    tid, _ = put(acc, "Archive", 9, data)
    app.db.x("UPDATE messages SET attachments=NULL, html=NULL")  # van voor de bijlagen
    msg = client.get(f"/api/threads/{tid}", headers=H).json()["messages"][0]
    assert msg["attachments"] is None
    imap = imap_for(data, 9)
    with mock.patch.object(acc, "run_imap", side_effect=lambda fn: fn(imap)):
        r = client.get(f"/api/messages/{msg['id']}/html", headers=H).json()
        client.get(f"/api/messages/{msg['id']}/html", headers=H)  # tweede keer uit de cache
    assert [a["name"] for a in r["attachments"]] == ["E-ticket AF1068.pdf", "boarding.jpg", "factuur.html"]
    assert imap.select_folder.call_count == 1
    assert time.time() < int(r["attachments"][0]["url"].split("?e=")[1])


def test_pdf_sent_as_octet_stream_is_a_pdf():
    """Veel mailprogramma's sturen een pdf als application/octet-stream: toch tonen als pdf."""
    from app.mailparse import att_type

    assert att_type("application/octet-stream", "Klantofferte 270100358.pdf") == "application/pdf"
    assert att_type("application/octet-stream", "data.bin") == "application/octet-stream"
    assert att_type("image/png", "foto.pdf") == "image/png"  # een echt type blijft


def test_attachment_links_fix_old_octet_stream_lists():
    import app.main as M

    a = M.attachment_links(1, [{"name": "offerte.PDF", "type": "application/octet-stream", "size": 10}])[0]
    assert a["type"] == "application/pdf" and a["inline"]


def test_failed_html_fetch_is_not_cached_as_no_html(app, acc):
    from app import actions
    tid, _ = put(acc, "Archive", 7, mail_with_attachments())
    mid = app.db.one("SELECT id FROM messages ORDER BY id DESC")["id"]
    app.db.x("UPDATE messages SET html=NULL, attachments=NULL WHERE id=?", (mid,))
    with mock.patch("app.actions._raw", return_value=None):
        assert actions.fetch_html(app.db, acc, mid) == ("", [])
    row = app.db.one("SELECT html, attachments FROM messages WHERE id=?", (mid,))
    assert row["html"] is None and row["attachments"] is None  # volgende keer opnieuw proberen
