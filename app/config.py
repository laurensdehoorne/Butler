"""Configuratie: lezen, controleren en bewaren, zodat alles ook in de app kan.

Butler leest eerst /data/config.yaml (wat in de app bewaard werd). Bestaat dat niet, dan het meegegeven bestand
(BUTLER_CONFIG, in Docker /config/config.yaml, alleen-lezen); en zonder enig bestand start Butler leeg, zodat je alles
in de app kunt instellen. Bewaren gebeurt altijd in /data/config.yaml.

Wachtwoorden en sleutels gaan nooit naar de browser: die krijgt een leeg veld met de vermelding dat er een waarde is.
Een leeg veld terugsturen betekent "niet wijzigen".

SECTIONS beschrijft elk veld voor de instellingenpagina (label, soort, uitleg); de webinterface bouwt er haar
formulieren mee, zodat een nieuwe instelling op één plaats bijkomt.
"""

import copy
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ConfigError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def F(key, label, kind="text", help="", default=None, **extra):
    return {"key": key, "label": label, "kind": kind, "help": help, "default": default, **extra}


SECTIONS = [
    {"id": "sync", "title": "Synchroniseren", "fields": [
        F("app.sync_days", "Hoeveel dagen mail bijhouden", "number", "0 = alle mail. Gemarkeerde mail blijft altijd.", 0, min=0),
        F("app.sync_archive", "Ook het archief", "bool", "Alle e-mail bij Gmail, Archief bij iCloud: daar zet Klaar in Spark je mail.", True),
        F("app.sync_daily_mb", "Oude mail per dag (MB)", "number",
          "Zo wordt de eerste sync over dagen gespreid. Gmail laat ongeveer 2.500 MB per dag toe. 0 = geen limiet.", 1000, min=0),
        F("app.poll_seconds", "Maximale tijd tussen syncs (seconden)", "number",
          "Nieuwe mail komt meestal binnen enkele seconden binnen; dit is de veiligheidsmarge.", 300, min=30),
    ]},
    {"id": "followup", "title": "Opvolgen en opruimen", "fields": [
        F("app.followup_days", "Wacht op antwoord na (dagen)", "number",
          "Na hoeveel dagen zonder antwoord een verstuurde mail in Wacht op antwoord komt.", 3, min=1),
        F("app.cleanup.notificatie_days", "Notificaties archiveren na (dagen)", "number",
          "Voor afzenders zonder eigen regel. 0 = nooit.", 0, min=0),
        F("app.cleanup.reclame_days", "Reclame archiveren na (dagen)", "number", "Voor afzenders zonder eigen regel. 0 = nooit.", 0, min=0),
        F("app.avatars.logos", "Bedrijfslogo's", "bool", "Logo per afzenderdomein; enkel de domeinnaam gaat naar buiten.", True),
        F("app.avatars.gravatar", "Gravatar-foto's", "bool",
          "Profielfoto's van personen; stuurt een hash van elk afzenderadres naar Gravatar.", True),
    ]},
    {"id": "ai", "title": "AI", "fields": [
        F("ai.enabled", "AI gebruiken", "bool", "Samenvattingen, prioriteit, momenten en antwoordvoorstellen.", True),
        F("ai.provider", "Standaarddienst", "select", "", "claude", options=[["claude", "Claude"], ["openai", "ChatGPT"]]),
        F("ai.api_key", "Claude API-sleutel", "secret", "Van console.anthropic.com.", ""),
        F("ai.triage_model", "Claude-model voor samenvatten", "text", "", "claude-haiku-5-5"),
        F("ai.draft_model", "Claude-model voor antwoorden", "text", "", "claude-sonnet-5-5"),
        F("ai.openai_api_key", "ChatGPT API-sleutel", "secret", "Van platform.openai.com.", ""),
        F("ai.openai_triage_model", "ChatGPT-model voor samenvatten", "text", "", "gpt-6-luna"),
        F("ai.openai_draft_model", "ChatGPT-model voor antwoorden", "text", "", "gpt-6-luna"),
        F("ai.initial_days", "Bij de eerste sync samenvatten (dagen)", "number", "Enkel mail van de laatste dagen, om kosten te sparen.", 7, min=0),
        F("ai.name", "Je voornaam", "text", "Zo spreekt Butler je aan in samenvattingen.", ""),
        F("ai.owner", "Wie je bent", "text", "Bv. Jan Peeters, zelfstandige in Gent. Helpt de AI de toon te kiezen.", ""),
        F("ai.signature", "Handtekening", "textarea", "Onder elk antwoordvoorstel.", ""),
    ]},
    {"id": "notify", "title": "Meldingen", "fields": [
        F("notify.mode", "Waarvoor een melding", "select", "", "important",
          options=[["important", "Enkel belangrijke mail"], ["all", "Elke persoonlijke mail"], ["off", "Geen meldingen"]]),
        F("notify.quiet_hours", "Stille uren", "text", "Bv. 22:00-07:00. Leeg = geen stille uren.", "", pattern=r"^(\d{2}:\d{2}-\d{2}:\d{2})?$"),
        F("notify.digest_time", "Ochtendsamenvatting om", "text", "Bv. 07:30. Leeg = geen ochtendsamenvatting.", "", pattern=r"^(\d{2}:\d{2})?$"),
        F("notify.digest_days", "Ochtendsamenvatting op", "days", "", [1, 2, 3, 4, 5]),
        F("notify.webpush", "Meldingen in de app", "bool", "Web Push naar de app op je beginscherm.", True),
        F("notify.push_contact", "Contactadres voor pushdiensten", "text", "Bv. mailto:jij@voorbeeld.be. Apple vraagt er één.", ""),
        F("notify.ntfy_server", "ntfy-server", "text", "", "https://ntfy.sh"),
        F("notify.ntfy_topic", "ntfy-topic", "secret", "Een lange, geheime naam. Leeg = geen ntfy.", ""),
        F("notify.ntfy_token", "ntfy-token", "secret", "Enkel voor een eigen ntfy-server met aanmelding.", ""),
        F("notify.ha_webhook", "Home Assistant-webhook", "secret", "Bv. http://homeassistant.local:8123/api/webhook/butler. Leeg = uit.", ""),
    ]},
    {"id": "general", "title": "Algemeen", "fields": [
        F("app.base_url", "Adres van Butler", "text", "Voor de link in meldingen, bv. https://butler.jouw-tailnet.ts.net", ""),
        F("app.timezone", "Tijdzone", "text", "Bv. Europe/Brussels.", "Europe/Brussels"),
        F("app.token", "Toegangstoken", "secret",
          "Leeg = geen aanmelding, enkel als Butler uitsluitend via Tailscale bereikbaar is.", ""),
    ]},
]

ACCOUNT_FIELDS = [
    F("name", "Naam", "text", "Zo heet het account in Butler, bv. Gmail.", ""),
    F("provider", "Soort", "select", "", "gmail", options=[["gmail", "Gmail"], ["icloud", "iCloud"], ["custom", "Andere (IMAP)"]]),
    F("email", "E-mailadres", "text", "", ""),
    F("display_name", "Je naam als afzender", "text", "", ""),
    F("password", "App-wachtwoord", "secret",
      "Nooit je gewone wachtwoord: maak een app-wachtwoord bij Google of een app-specifiek wachtwoord bij Apple.", ""),
    F("username", "IMAP-gebruikersnaam", "text", "Enkel als die afwijkt van je adres. iCloud: het deel vóór @icloud.com.", "",
      providers=["icloud", "custom"]),
    F("smtp_username", "SMTP-gebruikersnaam", "text", "Enkel als die afwijkt van je adres.", "", providers=["icloud", "custom"]),
    F("imap_host", "IMAP-server", "text", "", "", providers=["custom"]),
    F("imap_port", "IMAP-poort", "number", "", 993, providers=["custom"]),
    F("smtp_host", "SMTP-server", "text", "", "", providers=["custom"]),
    F("smtp_port", "SMTP-poort", "number", "", 587, providers=["custom"]),
    F("append_sent", "Verstuurde mail zelf in Verzonden zetten", "bool", "Uit als je server dat zelf doet.", True, providers=["custom"]),
    F("aliases", "Andere adressen van jou", "list", "Komma-gescheiden; mail vanaf deze adressen telt als door jou verstuurd.", []),
    F("ai", "AI gebruiken voor dit account", "bool", "Uit voor mailboxen met gevoelige inhoud: die gaan dan nooit naar de AI.", True),
]

FIELDS = {f["key"]: f for s in SECTIONS for f in s["fields"]}
SECRETS = [k for k, f in FIELDS.items() if f["kind"] == "secret"]
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")


# --- paden en lezen ------------------------------------------------------------
def data_path(data_dir):
    return Path(data_dir) / "config.yaml"


def load(data_dir, fallback):
    """(config, pad waaruit gelezen): eerst /data/config.yaml, dan het meegegeven bestand, anders leeg."""
    for p in (data_path(data_dir), Path(fallback)):
        if p.is_file():
            with open(p, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}), str(p)
    return {}, ""


def get(cfg, key, default=None):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def put(cfg, key, value):
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


# --- naar en van de browser --------------------------------------------------------
def public(cfg):
    """Config voor de browser: elk veld uit SECTIONS met zijn waarde, geheimen leeg met een vlag."""
    values, secrets_set = {}, {}
    for key, f in FIELDS.items():
        v = get(cfg, key, f["default"])
        if f["kind"] == "select" and v is False and ["off"] in [o[:1] for o in f["options"]]:
            v = "off"  # YAML leest een kaal off als false
        if f["kind"] == "secret":
            secrets_set[key] = bool(v)
            v = ""
        values[key] = v
    accounts = []
    for a in cfg.get("accounts") or []:
        acc = {f["key"]: a.get(f["key"], f["default"]) for f in ACCOUNT_FIELDS}
        acc["id"] = str(a.get("id", ""))
        acc["password_set"] = bool(a.get("password"))
        acc["password"] = ""
        accounts.append(acc)
    return {"values": values, "secrets_set": secrets_set, "accounts": accounts}


def apply(cfg, values=None, accounts=None):
    """Nieuwe config uit wat de browser terugstuurt; een leeg geheim behoudt de huidige waarde."""
    new = copy.deepcopy(cfg)
    for key, v in (values or {}).items():
        f = FIELDS.get(key)
        if not f:
            continue
        if f["kind"] == "secret" and v in ("", None):
            continue
        put(new, key, _coerce(f, v, key))
    if accounts is not None:
        old = {str(a.get("id")): a for a in cfg.get("accounts") or []}
        out = []
        for i, a in enumerate(accounts):
            prev = old.get(str(a.get("id") or ""), {})
            acc = {k: v for k, v in prev.items() if k not in {f["key"] for f in ACCOUNT_FIELDS}}  # onbekende sleutels bewaren
            acc["id"] = str(a.get("id") or "").strip() or _new_id(a, out, old)
            provider = a.get("provider") or "gmail"
            for f in ACCOUNT_FIELDS:
                if f.get("providers") and provider not in f["providers"]:
                    acc.pop(f["key"], None)  # bv. append_sent: Gmail bewaart verstuurde mail zelf
                    continue
                v = a.get(f["key"], f["default"])
                if f["kind"] == "secret" and v in ("", None):
                    v = prev.get(f["key"], "")
                v = _coerce(f, v, f"accounts.{i}.{f['key']}")
                if v in ("", None, []) and f["key"] not in ("password", "aliases"):
                    continue  # lege velden niet wegschrijven: de standaard van de provider geldt
                acc[f["key"]] = v
            out.append(acc)
        new["accounts"] = out
    return new


def _coerce(f, v, key):
    kind = f["kind"]
    try:
        if kind == "number":
            return int(v)
        if kind == "bool":
            return bool(v)
        if kind == "days":
            return sorted({int(d) for d in v if 1 <= int(d) <= 7})
        if kind == "list":
            items = v.split(",") if isinstance(v, str) else v
            return [s.strip() for s in items if str(s).strip()]
        return "" if v is None else str(v).strip() if kind != "textarea" else str(v)
    except (TypeError, ValueError) as e:
        raise ConfigError({key: "ongeldige waarde"}) from e


def _new_id(a, taken, old):
    base = re.sub(r"[^a-z0-9]+", "-", (a.get("name") or a.get("email", "").split("@")[0] or "account").lower()).strip("-")[:24]
    base = base or "account"
    ids = {x["id"] for x in taken} | set(old)
    cand, n = base, 2
    while cand in ids:
        cand, n = f"{base}-{n}", n + 1
    return cand


# --- controleren en bewaren ---------------------------------------------------------
def validate(cfg):
    errors = {}
    for key, f in FIELDS.items():
        v = get(cfg, key)
        if v is None:
            continue
        if f["kind"] == "number" and (not isinstance(v, int) or v < f.get("min", -10**9)):
            errors[key] = f"minstens {f.get('min', 0)}"
        if f.get("pattern") and not re.match(f["pattern"], str(v)):
            errors[key] = "ongeldige notatie"
        if f["kind"] == "select" and v not in [o[0] for o in f["options"]]:
            errors[key] = "ongeldige keuze"
    try:
        ZoneInfo(str(get(cfg, "app.timezone", "Europe/Brussels")))
    except Exception:
        errors["app.timezone"] = "onbekende tijdzone"
    seen = set()
    for i, a in enumerate(cfg.get("accounts") or []):
        p = f"accounts.{i}"
        if not ID_RE.match(str(a.get("id", ""))):
            errors[f"{p}.id"] = "enkel kleine letters, cijfers, - en _"
        elif a["id"] in seen:
            errors[f"{p}.id"] = "komt twee keer voor"
        seen.add(a.get("id"))
        if "@" not in str(a.get("email", "")):
            errors[f"{p}.email"] = "geen geldig e-mailadres"
        if not a.get("password"):
            errors[f"{p}.password"] = "vul een app-wachtwoord in"
        if a.get("provider") not in ("gmail", "icloud", "custom"):
            errors[f"{p}.provider"] = "ongeldige keuze"
        if a.get("provider") == "custom":
            for k in ("imap_host", "smtp_host"):
                if not a.get(k):
                    errors[f"{p}.{k}"] = "nodig voor een andere provider"
    if errors:
        raise ConfigError(errors)
    return cfg


def save(cfg, data_dir):
    path = data_path(data_dir)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Bewaard door Butler. Wijzig liefst via Instellingen in de app; bevat wachtwoorden.\n")
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return str(path)
