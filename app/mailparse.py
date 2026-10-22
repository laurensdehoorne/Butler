import re
from datetime import timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup

MID_RE = re.compile(r"<[^<>\s]+>")
NOREPLY_RE = re.compile(r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster@)", re.I)
NOTIF_FROM_RE = re.compile(r"(notifications?@|notify@|alerts?@|noreply|no-reply|security@|account@|accounts@|support@|service@|system@|monitor)", re.I)
MARKETING_FROM_RE = re.compile(r"(newsletter|nieuwsbrief|marketing|promo|offers?@|deals?@|news@|mailing|campaign|e-?mail\.)", re.I)
TRANS_RE = re.compile(
    r"(bestelling|order|factuur|invoice|rekening|betaling|payment|betaald|verzend|verzonden|shipped|levering|geleverd|"
    r"delivery|track|pakket|parcel|zending|code|verificatie|verif|wachtwoord|password|login|inlog|aanmeld|sign.?in|"
    r"beveiliging|security|alert|reservat|booking|boeking|bevestig|confirm|ticket|afspraak|appointment|receipt|"
    r"ontvangstbewijs|statement|uittreksel|domiciliëring|abonnement verlengd|backup|storing|outage)",
    re.I,
)


def classify(from_addr, subject, has_list, unsub):
    """'' = persoonlijk, 'notificatie' = automatisch/transactioneel, 'reclame' = nieuwsbrief/marketing."""
    noreply = bool(NOREPLY_RE.search(from_addr or ""))
    marketing = bool(MARKETING_FROM_RE.search(from_addr or ""))
    if not (has_list or noreply or marketing or NOTIF_FROM_RE.search(from_addr or "")):
        return ""
    if TRANS_RE.search(subject or "") or (NOTIF_FROM_RE.search(from_addr or "") and not marketing):
        return "notificatie"
    if marketing or unsub:
        return "reclame"
    return "notificatie"
BIG_NOTE = "(Groot bericht met bijlagen — open het in je mailapp voor de volledige inhoud.)"


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "head", "title"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    txt = soup.get_text("\n")
    txt = re.sub(r"[ \t ​]+", " ", txt)
    txt = re.sub(r" *\n *", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _decode_legacy(v):
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return str(v)


def _body(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        cs = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(cs, "replace")
        except LookupError:
            content = payload.decode("utf-8", "replace")
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return content.replace("\r\n", "\n").strip()


def make_snippet(body):
    lines = [l for l in body.split("\n") if l.strip() and not l.lstrip().startswith(">")]
    return " ".join(" ".join(lines).split())[:220]


def strip_quotes(body):
    """Verwijdert geciteerde tekst (>-regels en alles na 'Op ... schreef')."""
    out = []
    for line in body.split("\n"):
        s = line.strip()
        if re.match(r"^(Op|On|Le|Am) .{5,200}(schreef|wrote|a écrit|schrieb)", s):
            break
        if s.startswith("-----Original Message") or s.startswith("-----Oorspronkelijk bericht"):
            break
        if s.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def parse(raw, header_only=False):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    legacy = None

    def h(name):
        nonlocal legacy
        try:
            v = msg.get(name)
            return str(v).strip() if v is not None else ""
        except Exception:
            if legacy is None:
                legacy = message_from_bytes(raw)
            v = legacy.get(name)
            return _decode_legacy(v).strip() if v else ""

    def addrs(name):
        try:
            return [[n, a.lower()] for n, a in getaddresses([h(name)]) if a and "@" in a]
        except Exception:
            return []

    frm = addrs("From")
    from_name, from_addr = frm[0] if frm else ("", "")
    rt = addrs("Reply-To")

    date = None
    try:
        d = parsedate_to_datetime(h("Date"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        date = d.astimezone(timezone.utc).isoformat()
    except Exception:
        pass

    body = BIG_NOTE if header_only else _body(msg)
    prec = h("Precedence").lower()
    unsub = h("List-Unsubscribe")
    subject = h("Subject") or "(geen onderwerp)"
    has_list = bool(unsub or h("List-Id") or prec in ("bulk", "list", "junk"))
    kind = classify(from_addr, subject, has_list, unsub)
    return {
        "message_id": (MID_RE.findall(h("Message-ID")) or [""])[0],
        "refs": MID_RE.findall(h("References")),
        "in_reply_to": (MID_RE.findall(h("In-Reply-To")) or [""])[0],
        "from_name": from_name or from_addr,
        "from_addr": from_addr,
        "to": addrs("To"),
        "cc": addrs("Cc"),
        "reply_to": rt[0][1] if rt else "",
        "subject": subject,
        "date": date,
        "body": body[:50000],
        "snippet": make_snippet(body),
        "kind": kind,
        "bulk": bool(kind),
        "unsub": unsub[:1000],
        "unsub_post": "one-click" in h("List-Unsubscribe-Post").lower(),
    }
