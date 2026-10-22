import pytest

from app.mailparse import classify, parse, strip_quotes
from tests.conftest import raw


@pytest.mark.parametrize("frm,subject,has_list,unsub,want", [
    ("piet@kabinet.be", "Vraag over patiënt", False, "", ""),
    ("newsletter@zalando.be", "Tot -50% dit weekend", True, "<https://x>", "reclame"),
    ("info@coolblue.be", "Nieuwe deals voor jou", True, "<https://x>", "reclame"),
    ("noreply@bol.com", "Je bestelling is verzonden", True, "<https://x>", "notificatie"),
    ("notifications@github.com", "[repo] PR #3 merged", True, "<mailto:x>", "notificatie"),
    ("no-reply@accounts.google.com", "Beveiligingsmelding", False, "", "notificatie"),
    ("hello@brand.com", "Our autumn collection", True, "<https://x>", "reclame"),
])
def test_classify(frm, subject, has_list, unsub, want):
    assert classify(frm, subject, has_list, unsub) == want


def test_parse_encoded_subject_and_html():
    p = parse(raw("Shop <news@shop.be>", "=?utf-8?q?Promo_=E2=82=AC?=", "<n@s>",
                  body="<html><style>x{}</style><p>Hallo<br>wereld</p></html>", html=True,
                  unsub="<https://shop.be/u>", one_click=True))
    assert p["subject"] == "Promo €"
    assert p["body"] == "Hallo\n\nwereld"
    assert p["kind"] == "reclame" and p["bulk"] and p["unsub_post"]
    assert p["from_addr"] == "news@shop.be" and p["message_id"] == "<n@s>"


def test_strip_quotes():
    body = "Prima.\n\nOp 22 okt. 2026 om 10:00 schreef Piet <p@x.be>:\n> oud\n> nog oud"
    assert strip_quotes(body) == "Prima."
