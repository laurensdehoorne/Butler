import base64
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


MAX_HTML = 2_000_000       # groter: enkel de tekst
MAX_INLINE = 500_000       # per ingebedde afbeelding (cid:)
MAX_INLINE_TOTAL = 1_500_000
DROP_TAGS = ("script", "noscript", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "link", "title",
             "meta", "audio", "video", "source", "track")


def clean_html(html, cids=None):
    """HTML van een mail zonder scripts, gebeurtenissen, formulieren of externe stylesheets; ingebedde afbeeldingen
    (cid:) worden data-URI's. De webinterface toont het daarna nog in een iframe zonder scripts."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(DROP_TAGS):
        t.decompose()
    for t in soup(["form", "input", "button", "select", "textarea"]):
        t.unwrap() if t.name == "form" else t.decompose()
    for t in soup.find_all(True):
        for attr in list(t.attrs):
            v = t.attrs[attr]
            if attr.lower().startswith("on") or attr.lower() in ("srcdoc", "formaction", "ping"):
                del t.attrs[attr]
            elif attr.lower() in ("href", "src", "action") and isinstance(v, str) and v.strip().lower().startswith(("javascript:", "vbscript:")):
                del t.attrs[attr]
        if t.name == "img" and cids and str(t.get("src", "")).lower().startswith("cid:"):
            t["src"] = cids.get(t["src"][4:].strip("<>"), "")
    return str(soup)


def _content(part):
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
    return content


def _html(msg):
    """De HTML-versie van een mail, schoongemaakt, of "" als er geen is."""
    try:
        part = msg.get_body(preferencelist=("html",))
    except Exception:
        part = None
    if part is None:
        return ""
    cids, total = {}, 0
    for p in msg.walk():
        cid = p.get("Content-ID")
        if cid and p.get_content_maintype() == "image":
            data = p.get_payload(decode=True) or b""
            if len(data) <= MAX_INLINE and total + len(data) <= MAX_INLINE_TOTAL:
                total += len(data)
                cids[str(cid).strip().strip("<>")] = f"data:{p.get_content_type()};base64,{base64.b64encode(data).decode()}"
    try:
        html = clean_html(_content(part), cids)
    except Exception:
        return ""
    return html if len(html) <= MAX_HTML else ""


def attachment_parts(msg):
    """De bijlagen van een mail, in een vaste volgorde (de index is hun nummer): elk deel met een bestandsnaam of
    "attachment", behalve de tekst zelf en afbeeldingen die in de HTML staan (die toont de mail al)."""
    body = set()
    for pref in (("plain",), ("html",)):
        try:
            b = msg.get_body(preferencelist=pref)
        except Exception:
            b = None
        if b is not None:
            body.add(id(b))
    used = set()
    try:
        h = msg.get_body(preferencelist=("html",))
        if h is not None:
            used = {c.strip("<>") for c in re.findall(r"cid:([^\"'\s>)]+)", _content(h), re.I)}
    except Exception:
        pass
    out = []
    for p in msg.walk():
        if p.is_multipart() or id(p) in body:
            continue
        disp = p.get_content_disposition()
        cid = str(p.get("Content-ID") or "").strip().strip("<>")
        if cid and cid in used and disp != "attachment":
            continue  # ingebed in de HTML
        if not p.get_filename() and disp != "attachment":
            continue
        out.append(p)
    return out


PDF_MAX_BYTES = 10 * 1024 * 1024
PDF_MAX_PAGES = 8
PDF_MAX_CHARS = 8000


def _is_pdf(p):
    return p.get_content_type() == "application/pdf" or (p.get_filename() or "").lower().endswith(".pdf")


def pdf_text(msg):
    """De tekst uit pdf-bijlagen (een hotelbevestiging, een ticket, een factuur), zodat de AI en het zoeken weten wat
    erin staat ook als de mail zelf leeg is. Enkel de eerste pagina's; "" als er niets te lezen is (een scan)."""
    out = []
    for p in attachment_parts(msg):
        if not _is_pdf(p):
            continue
        try:
            data = p.get_payload(decode=True) or b""
            if not data or len(data) > PDF_MAX_BYTES:
                continue
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:PDF_MAX_PAGES])
        except Exception:
            continue
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
        if text:
            out.append(f"[{p.get_filename() or 'bijlage.pdf'}]\n{text}")
        if sum(len(x) for x in out) >= PDF_MAX_CHARS:
            break
    return "\n\n".join(out)[:PDF_MAX_CHARS]


def attachments(msg):
    """[{name, type, size}] voor de lijst in de app; het bestand zelf wordt pas opgehaald als je het opent."""
    out = []
    for i, p in enumerate(attachment_parts(msg)):
        try:
            size = len(p.get_payload(decode=True) or b"")
        except Exception:
            size = 0
        out.append({"name": (p.get_filename() or f"bijlage-{i + 1}")[:200], "type": p.get_content_type(), "size": size})
    return out


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
    content = _content(part)
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    elif len(re.sub(r"[\s\u034f\u200b-\u200d\ufeff]", "", content)) < 20:
        # een lege tekstversie naast een volledige HTML-versie (veel nieuwsbrieven): de tekst uit de HTML halen
        try:
            h = msg.get_body(preferencelist=("html",))
            if h is not None:
                content = html_to_text(_content(h)) or content
        except Exception:
            pass
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
        "html": "" if header_only else _html(msg),
        "attachments": None if header_only else attachments(msg),  # None = niet bekend (enkel headers opgehaald)
        "att_text": None if header_only else pdf_text(msg),  # tekst uit pdf-bijlagen (hotelbevestiging, ticket)
        "snippet": make_snippet(body),
        "kind": kind,
        "bulk": bool(kind),
        "unsub": unsub[:1000],
        "unsub_post": "one-click" in h("List-Unsubscribe-Post").lower(),
    }
