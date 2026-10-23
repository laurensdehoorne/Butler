from email.message import EmailMessage
from unittest import mock

from app.mailparse import parse
from tests.conftest import H, put
from tests.test_ai import claude_reply


def make_pdf(text):
    stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs):
        offs.append(len(out))
        out += f"{i + 1} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF"
    return out.encode()


def hotel_mail():
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "medegandt@icloud.com", "me@icloud.com", "Online-Confirmation-Belgium 1432008-1.pdf", "<h@x>"
    m["Date"] = "Fri, 23 Oct 2026 14:12:00 +0000"
    m.set_content("")
    m.add_attachment(make_pdf("Hotel Molina Lario Malaga check-in 14-11-2026 15:00"), maintype="application",
                     subtype="octet-stream", filename="Online-Confirmation-Belgium 1432008-1.pdf")
    return m.as_bytes()


def test_pdf_text_reaches_ai_search_and_agenda(app, client, acc):
    assert "Molina Lario" in parse(hotel_mail())["att_text"]
    tid, _ = put(acc, "INBOX", 1, hotel_mail())
    assert [t["tid"] for t in client.get("/api/inbox", headers=H, params={"q": "molina malaga"}).json()["threads"]] == [tid]
    fake = mock.MagicMock()
    app.ai._clients["claude"] = fake
    fake.messages.create.return_value = claude_reply(
        '{"samenvatting":"Hotelbevestiging Malaga","prioriteit":"normaal","categorie":"reis","actie":"lezen",'
        '"deadline":"2026-11-14T15:00","wacht_op_antwoord":false,"gebeurtenis":true,"dossiers":[]}')
    with mock.patch.object(app.ai, "enqueue_moments") as moments:
        app.ai.triage(tid)
    assert "Tekst uit de bijlagen" in fake.messages.create.call_args.kwargs["messages"][0]["content"]
    moments.assert_called_once_with(tid)  # een boeking: meteen naar de agenda, ook zonder vlag
    assert app.db.one("SELECT has_event FROM threads WHERE tid=?", (tid,))["has_event"] == 1
