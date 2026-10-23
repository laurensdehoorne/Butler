# Butler

Self-hosted mailassistent die **Gmail en iCloud** (en elke andere IMAP-mailbox) samenbrengt in één webapp:

- **Gecombineerde inbox** – lezen, archiveren, verwijderen, als gelezen markeren, eigen tags, zoeken in alle mail.
- **Opvolglijst** – *Wacht op jou* (ontvangen mail die nog actie vraagt) en *Wacht op antwoord* (jij stuurde als laatste, al X dagen niets terug). Snoozen ("morgen", "+3 d", "+1 week") en "klaar"-markering.
- **AI via de Claude API** – per thread een korte samenvatting, prioriteit, categorie, verwachte actie en deadline; antwoordvoorstellen op basis van een korte instructie ("akkoord, voorstel dinsdag 14u").
- **Pushmeldingen** – via [ntfy](https://ntfy.sh) en/of een Home Assistant-webhook, enkel voor belangrijke mail of voor alles, met stille uren.
- **Antwoorden** – via SMTP met correcte threading (`In-Reply-To`/`References`), zodat het antwoord ook in Gmail, Apple Mail en Spark netjes in de conversatie staat.

Alles wat je in Butler doet (archiveren, gelezen, verwijderen) gebeurt echt op de mailserver, dus je andere mailapps blijven in sync.

## Samen met je gewone mailapp (bv. Spark)

Butler is bedoeld als **assistent naast** je mailapp, niet als vervanger:

- **Niets verandert vanzelf.** Butler leest met `BODY.PEEK` en in read-only-modus: mails worden niet als gelezen gemarkeerd, verplaatst of gelabeld tenzij je zelf op een knop drukt.
- **Opvolgen is het startscherm.** "Klaar", snoozen en tags zijn lokaal in Butler en raken je mailbox niet.
- **Antwoorden die je in Spark verstuurt worden herkend** via de Verzonden-map: de thread verdwijnt dan vanzelf uit *Wacht op jou* en verschijnt na `followup_days` in *Wacht op antwoord* als er niets terugkomt.
- **Concepten in plaats van versturen.** Een antwoordvoorstel kun je met *Zet als concept* in de Concepten-map van je mailbox zetten; je vindt het dan in Spark terug om na te kijken en te versturen. Rechtstreeks versturen kan ook nog.
- **Geen dubbele meldingen.** Laat Spark gewoon elke mail melden en zet Butler op `notify.mode: important`: dan krijg je enkel een extra (time-sensitive) melding voor wat écht dringend is, plus de ochtendsamenvatting (`digest_time`).

## Gemarkeerde mail op het juiste moment

Markeer in Spark een mail die je later nodig hebt (een vlucht, een concert, een reservatie): Spark zet er een vlag op, in Gmail een ster. Butler ziet die vlag, ook als de mail al gearchiveerd (*Klaar*) is, en laat de AI bepalen wanneer je de mail nodig hebt:

- **Vlucht**: wanneer het online inchecken opengaat (of 24 uur voor vertrek) en 3 uur voor vertrek.
- **Concert of evenement**: om 9:00 op de dag zelf en 2 uur voor de start.
- **Afspraak, reservatie, afhaling**: 2 uur ervoor.

Op dat moment krijg je een melding (ook tijdens stille uren, maar 's nachts pas om 7:00, tenzij het dan al te laat is) en staat de mail bovenaan *Opvolgen* onder **Nu nodig**; wat binnen de week komt, staat onder **Binnenkort**. Na afloop verdwijnt het vanzelf. Haal je de vlag weg, dan verdwijnen ook de momenten. Gemarkeerde mail gaat naar de AI, ook als het een notificatie is (bv. van een luchtvaartmaatschappij): de vlag is een bewuste keuze.

## Reclame en notificaties opruimen

Elke mail wordt herkend als **persoonlijk**, **notificatie** (bestellingen, verzendingen, beveiligingsmeldingen, systemen, …) of **reclame** (nieuwsbrieven, promoties), op basis van `List-Unsubscribe`/`List-Id`, de afzender (`noreply@`, `newsletter@`, …) en het onderwerp. Klopt het niet voor een afzender, dan corrigeer je dat één keer en geldt het voortaan voor al zijn mails.

Het tabblad **Opruimen** toont per afzender hoeveel mails er binnenkwamen, hoeveel nog in je inbox staan en welk deel je effectief leest. Per afzender kun je:

- **Uitschrijven**: via RFC 8058 *one-click* (één POST, zonder webpagina), via een uitschrijfmail (`mailto:`), of door de uitschrijfpagina te openen als de afzender enkel dat aanbiedt. Na uitschrijven gaat wat nog binnenkomt automatisch naar het archief.
- **Alles archiveren of verwijderen** wat nu in je inbox staat.
- **Een regel instellen**: altijd meteen archiveren, altijd meteen verwijderen, archiveren na X dagen, of nooit automatisch opruimen.

Regels worden bij elke sync toegepast en verplaatsen mail echt op de server, dus Spark ziet hetzelfde. Zonder regel gebeurt er niets, tenzij je in `app.cleanup` een standaard instelt (bv. `notificatie_days: 7`). Nieuwsbrieven en notificaties gaan nooit naar de AI, verschijnen niet in de opvolglijst en geven geen pushmelding.

Uitschrijven bevestigt aan de afzender dat je adres actief is. Gebruik het voor legitieme afzenders (winkels, nieuwsbrieven); echte spam markeer je beter als spam in je mailapp.

## Hoe het werkt

```
Gmail  ─┐ IMAP IDLE                         ┌─ Claude API (samenvatting, antwoord)
iCloud ─┼──────────► Butler (FastAPI) ─────┼─ ntfy / Home Assistant (push)
andere ─┘ SMTP ◄──── SQLite-cache /data ────┘─ Webinterface (browser / PWA)
```

- Per account een IMAP-verbinding die met **IDLE** op nieuwe mail wacht (meestal binnen enkele seconden), met een volledige sync minstens elke `poll_seconds`.
- Alle mail (INBOX, Verzonden en het archief: *Alle e-mail* bij Gmail, *Archief* bij iCloud) wordt lokaal gecachet in SQLite (`/data/butler.db`); met `sync_days` beperk je dat tot de laatste X dagen, gemarkeerde mail blijft dan altijd. De eerste sync van een groot archief wordt gespreid over dagen (`sync_daily_mb`, standaard 1000 MB per account per dag; Gmail laat ongeveer 2,5 GB IMAP-download per dag toe): gemarkeerde mail eerst, dan van nieuw naar oud. Nieuwe mail komt altijd meteen binnen; bovenaan staat hoeveel oude mail nog volgt. Threads worden opgebouwd via `X-GM-THRID` (Gmail) of `References`/`In-Reply-To` (iCloud en andere).
- Grote berichten (> 5 MB) worden enkel met headers opgehaald.
- Bij de eerste sync worden geen meldingen verstuurd en enkel threads van de laatste `ai.initial_days` dagen samengevat, om kosten te beperken. Nieuwsbrieven (herkend aan `List-Unsubscribe` e.d.) gaan nooit naar de AI.

## Installatie (Docker)

```bash
git clone https://github.com/laurensdehoorne/butler.git
cd butler
cp config.example.yaml config.yaml && chmod 600 config.yaml
mkdir -p data && sudo chown 1000:1000 data     # container draait als uid 1000
nano config.yaml
docker compose up -d --build
docker compose logs -f
```

Open daarna `http://<host>:8088` en geef het token uit `app.token` in. Op iPhone: Delen → *Zet op beginscherm* voor een app-icoon.

### Enkel via Tailscale (aanbevolen)

`docker-compose.tailscale.yml` zet een Tailscale-container naast Butler. Butler deelt diens netwerk, opent geen poort op de host en is enkel bereikbaar op `https://butler.<jouw-tailnet>.ts.net` (geldig certificaat via `tailscale serve`).

```bash
cp .env.example .env            # TS_AUTHKEY invullen (Tailscale-admin → Settings → Keys, met tag:butler)
docker compose -f docker-compose.tailscale.yml up -d --build
```

Zet daarna `app.base_url` op `https://butler.<jouw-tailnet>.ts.net`. Voeg `tag:butler` toe aan `tagOwners` in je tailnet policy en geef enkel jezelf toegang. HTTPS moet aanstaan in de Tailscale-admin (*DNS → HTTPS Certificates*).

### Wachtwoorden

Gebruik nooit je gewone wachtwoord:

- **Gmail**: zet tweestapsverificatie aan en maak een app-wachtwoord via <https://myaccount.google.com/apppasswords>.
- **iCloud**: maak een app-specifiek wachtwoord via <https://account.apple.com> → *Inloggen en beveiliging* → *App-specifieke wachtwoorden*. De IMAP-gebruikersnaam is het deel vóór `@icloud.com`; SMTP gebruikt het volledige adres (`smtp_username`).

### Bereikbaarheid

Butler heeft volledige toegang tot je mail. Zet hem **niet rechtstreeks op internet**. Aanbevolen:

- enkel via **Tailscale** (eventueel `tailscale serve` voor HTTPS), of
- achter een **Cloudflare Tunnel met Cloudflare Access** (e-mail-OTP of SSO) ervoor.

Het `app.token` is een extra laag, geen vervanging daarvoor.

### Claude of ChatGPT

Butler werkt met **Claude** (Anthropic) of **ChatGPT** (OpenAI, via de Responses API), net als CellarPeaks. Vul één of beide sleutels in onder `ai` in `config.yaml` (of als `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in de omgeving). Zijn beide ingevuld, dan staat rechtsboven in de app een keuzelijst *AI-dienst*; de keuze wordt bewaard. Standaardmodellen: `claude-haiku-5-5` en `claude-sonnet-5-5`, of `gpt-6-luna` (voor allebei; `gpt-6.1-sol` is sterker). Fouten van de dienst (ongeldige sleutel, krediet op, model onbekend) verschijnen in gewone taal.

### Kosten

Onder **⚙︎ → AI-kosten** zie je per maand wat samenvatten en antwoorden kostte, per dienst en model, in euro. Elke aanroep wordt bijgehouden met het tokenverbruik dat de API meldt, maal de lijstprijs (dezelfde prijslijst als CellarPeaks, oktober 2026). Het is een schatting; de werkelijke factuur staat in de console van Anthropic of OpenAI. Prijzen aanpassen zonder de code te wijzigen kan in `data/prices.json`, bv. `{"openai": {"gpt-6-luna": {"input": 0.1, "output": 0.5}}}`; de koers met `USD_EUR` (standaard 0.87).

### Privacy

Met `ai.enabled: true` gaat de inhoud van persoonlijke mails naar de gekozen dienst (Claude API van Anthropic of OpenAI API). Beide trainen standaard niet op API-data en verwijderen ze na hoogstens 30 dagen; bij OpenAI stuurt Butler `store: false` mee, zodat het antwoord niet als opgeslagen *response* bij OpenAI blijft staan. Zet `ai: false` op accounts met gevoelige inhoud (bv. patiënt- of klantgegevens); die worden dan nooit doorgestuurd en krijgen gewoon een melding per persoonlijke mail.

Afzenderfoto's (`app.avatars`) haalt de server op en bewaart ze in `/data/avatars`; je toestel laadt niets van buiten. Voor een **logo** (`logos: true`) gaat enkel de domeinnaam van de afzender naar buiten: Butler zoekt het BIMI-logo in de DNS en anders het pictogram van de website. Gratis mailboxen (gmail.com, icloud.com, …) krijgen geen logo. Voor een **profielfoto** (`gravatar: true`) stuurt Butler een SHA-256-hash van het afzenderadres naar Gravatar; zo weet Gravatar welke adressen je mail sturen. Zet `gravatar: false` als je dat niet wilt. Butler haalt enkel iets op bij publieke adressen, nooit in je eigen netwerk, en wat niet gevonden wordt, probeert hij pas na 14 dagen opnieuw.

## Pushmeldingen

### In de app (Web Push)

Zoals in CellarPeaks stuurt Butler meldingen rechtstreeks naar de app op je beginscherm, zonder extra app:

1. Open Butler in Safari, tik op Delen → **Zet op beginscherm** en open Butler via dat icoon (iOS 16.4 of nieuwer; op Android of desktop werkt het ook in de browser).
2. **⚙︎ → Meldingen → Meldingen aanzetten**, en sta meldingen toe. Met **Testmelding** controleer je meteen of het werkt.

Een tik op een melding opent de mail in Butler. De server maakt de eerste keer zelf een sleutelpaar om zijn meldingen te ondertekenen (`data/vapid.pem`); wis dat bestand niet, anders moet elk toestel de meldingen opnieuw aanzetten. Zet `notify.push_contact` op een `mailto:`-adres (Apple vraagt een contactadres). Onder **⚙︎ → Toestellen met meldingen** zie en vergeet je toestellen; een verwijderde app wordt vanzelf vergeten.

De service worker toont tijdens een update of herstart een pagina *Butler wordt bijgewerkt* die zichzelf herlaadt, in plaats van de foutpagina van Tailscale, en houdt de app ook offline open.

ntfy en Home Assistant hieronder zijn optioneel, bv. om meldingen via HA te routeren.

### ntfy

1. Installeer de ntfy-app (iOS/Android) en abonneer je op een lange, willekeurige topicnaam.
2. Zet in `config.yaml`: `ntfy_topic: "die-naam"`.

Met een eigen ntfy-server op iOS: zet `upstream-base-url: "https://ntfy.sh"` in de serverconfig, anders komen meldingen niet direct binnen.

Een tik op de melding opent de thread in Butler (via `app.base_url`).

### Home Assistant

Zet `ha_webhook` op `http://<ha>:8123/api/webhook/butler_nieuwe_mail` en voeg deze automatisering toe:

```yaml
alias: Butler – nieuwe mail
triggers:
  - trigger: webhook
    webhook_id: butler_nieuwe_mail
    allowed_methods: [POST]
    local_only: true
actions:
  - action: notify.mobile_app_iphone
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
      data:
        url: "{{ trigger.json.url }}"
        push:
          interruption-level: "{{ 'time-sensitive' if trigger.json.high else 'active' }}"
```

De payload bevat ook `account`, `from`, `subject`, `priority` en `summary`, handig om bv. enkel mail van bepaalde afzenders te melden of 's avonds anders te routeren.

## Configuratie

Zie [`config.example.yaml`](config.example.yaml). Belangrijkste opties:

| Sleutel | Betekenis |
|---|---|
| `app.followup_days` | Na hoeveel dagen zonder antwoord een verstuurde mail in *Wacht op antwoord* verschijnt |
| `app.sync_days` | Hoeveel dagen mail lokaal bijgehouden wordt; `0` = alle mail |
| `app.sync_archive` | Ook het archief synchroniseren (standaard aan) |
| `app.sync_daily_mb` | Zoveel MB oude mail per account per dag; `0` = geen limiet |
| `ai.triage_model` / `ai.draft_model` | Claude-modellen voor samenvatten en antwoordvoorstellen |
| `ai.owner`, `ai.name`, `ai.signature` | Context voor de AI: wie je bent en hoe je ondertekent |
| `notify.mode` | `all`, `important` (enkel prioriteit *hoog*) of `off` |
| `accounts[].ai` | `false` = deze mailbox nooit naar de AI sturen |
| `accounts[].aliases` | Extra adressen die ook als "jij" gelden |
| `accounts[].provider` | `gmail`, `icloud` of `custom` (dan `imap_host`, `smtp_host`, … zelf invullen) |

## Ontwikkelen

```bash
pip install -r requirements.txt
BUTLER_CONFIG=./config.yaml BUTLER_DB=./data/butler.db uvicorn app.main:app --reload --port 8088
```

`BUTLER_NO_SYNC=1` start de webinterface zonder IMAP-verbindingen.

### Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

De tests draaien met een tijdelijke config en database, zonder echte mailservers of AI-diensten (IMAP, SMTP, Claude, OpenAI en Web Push worden nagebootst). Ze dekken onder meer threading, de opvolglijst, antwoorden en concepten, de IMAP-sync, herkenning van reclame en notificaties, uitschrijven, opruimregels, beide AI-diensten, Web Push en de kostenberekening.

## Beperkingen

- Bijlagen worden niet getoond of gedownload; open die in je gewone mailapp.
- Nieuwe mails opstellen (zonder bestaande thread) zit er nog niet in, enkel antwoorden.
- Gmail-labels worden niet gesynchroniseerd; tags in Butler zijn lokaal.
