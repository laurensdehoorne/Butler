from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from app import moments as MO
from tests.conftest import H, put, raw
from tests.test_ai import claude_reply
from tests.test_sync import FakeIMAP

BXL = ZoneInfo("Europe/Brussels")


def iso_local(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def flight_reply(depart_local):
    """Wat de AI teruggeeft voor een vlucht die vertrekt op depart_local (lokale tijd Lissabon)."""
    return ('{"momenten": ['
            f'{{"soort":"vlucht","titel":"Online inchecken voor TP643","detail":"Vertrek 07:10, boeking ABC123",'
            f'"begin":"{iso_local(depart_local - timedelta(hours=24))}","einde":"{iso_local(depart_local)}","tijdzone":"Europe/Lisbon"}},'
            f'{{"soort":"vlucht","titel":"Vertrek TP643 naar Brussel","detail":"Terminal 1, poort 14",'
            f'"begin":"{iso_local(depart_local - timedelta(hours=3))}","einde":"{iso_local(depart_local)}","tijdzone":"Europe/Lisbon"}}'
            ']}')


def test_parse_converts_local_time_and_drops_past_and_invalid():
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    raw_reply = """Hier: {"momenten": [
      {"soort":"concert","titel":"Concert","detail":"Deuren 19:00","begin":"2026-10-30T09:00","einde":"2026-10-30T23:00","tijdzone":"Europe/Brussels"},
      {"soort":"vlucht","titel":"Al voorbij","begin":"2026-10-01T09:00","einde":"2026-10-01T10:00"},
      {"soort":"raar","titel":"Geen tijdzone en geen einde","begin":"2026-10-25T08:00"},
      {"titel":"Geen datum","begin":"morgen"},
      {"soort":"afspraak","titel":"","begin":"2026-10-26T10:00"}]}"""
    got = MO.parse(raw_reply, BXL, now)
    assert [g["title"] for g in got] == ["Geen tijdzone en geen einde", "Concert"]
    assert got[0]["kind"] == "ander" and got[0]["at"] == "2026-10-25T07:00:00+00:00"  # 25 oktober 2026 is al wintertijd (UTC+1)
    assert got[0]["until"] == "2026-10-25T13:00:00+00:00"  # zonder einde: 6 uur
    assert got[1]["at"] == "2026-10-30T08:00:00+00:00"
    assert MO.parse("geen json", BXL, now) == [] and MO.parse('{"momenten": "x"}', BXL, now) == []


def test_ai_finds_moments_for_flagged_notification(app, client, acc):
    depart = datetime.now(ZoneInfo("Europe/Lisbon")).replace(second=0, microsecond=0) + timedelta(hours=10)
    # een notificatie van een luchtvaartmaatschappij, al gearchiveerd (Klaar in Spark), maar gemarkeerd
    tid, _ = put(acc, "Archive", 5, raw("TAP <noreply@flytap.com>", "Je boeking ABC123", "<f1@tap>",
                                        unsub="<https://flytap.com/u>", body="Vlucht TP643 Lissabon-Brussel"), flagged=True)
    fake = mock.MagicMock()
    fake.messages.create.return_value = claude_reply(flight_reply(depart))
    app.ai._clients["claude"] = fake
    items = app.ai.moments(tid)
    assert [i["title"] for i in items] == ["Online inchecken voor TP643", "Vertrek TP643 naar Brussel"]
    assert "(Europe/Brussels)" in fake.messages.create.call_args.kwargs["system"]

    fu = client.get("/api/followup", headers=H).json()
    assert [c["moment"]["title"] for c in fu["moments_now"]] == ["Online inchecken voor TP643"]
    assert [c["moment"]["title"] for c in fu["moments_soon"]] == ["Vertrek TP643 naar Brussel"]
    assert fu["moments_now"][0]["tid"] == tid and fu["moments_now"][0]["flagged"]
    thread = client.get(f"/api/threads/{tid}", headers=H).json()
    assert len(thread["moments"]) == 2

    # opnieuw bepaald (nieuw bericht): wat al gemeld was, blijft gemeld
    app.db.x("UPDATE moments SET notified=1 WHERE title LIKE 'Online%'")
    app.ai.moments(tid)
    assert app.db.one("SELECT notified FROM moments WHERE title LIKE 'Online%'")["notified"] == 1


def test_due_notifies_once_and_waits_for_morning(app, acc):
    tid, _ = put(acc, "INBOX", 1, raw("Club <info@club.be>", "Tickets", "<t1@x>"), flagged=True)
    now = datetime.now(timezone.utc)
    MO.save(app.db, tid, [{"kind": "concert", "title": "Concert vanavond", "detail": "Deuren 19:00",
                           "at": (now - timedelta(minutes=5)).isoformat(), "until": (now + timedelta(hours=8)).isoformat()}])
    sent = []
    with mock.patch.object(app.notifier, "send", side_effect=lambda *a, **k: sent.append((a, k))), \
         mock.patch.object(MO, "hold_for_night", return_value=False):
        assert MO.due(app.db, app.notifier, now) == 1
        assert MO.due(app.db, app.notifier, now) == 0  # één keer
    (title, detail), kw = sent[0]
    assert (title, detail, kw["tid"], kw["force"]) == ("Concert vanavond", "Deuren 19:00", tid, True)

    day = datetime(2026, 10, 23, 14, 0, tzinfo=BXL)
    assert not MO.hold_for_night(day, (day + timedelta(hours=2)).isoformat())  # overdag: meteen

    night = datetime(2026, 10, 23, 23, 30, tzinfo=BXL)
    assert MO.hold_for_night(night, datetime(2026, 10, 24, 12, 0, tzinfo=BXL).isoformat())  # kan tot 7:00 wachten
    assert not MO.hold_for_night(night, datetime(2026, 10, 24, 6, 0, tzinfo=BXL).isoformat())  # vroege vlucht: nu
    early = datetime(2026, 10, 24, 3, 0, tzinfo=BXL)
    assert MO.hold_for_night(early, datetime(2026, 10, 24, 9, 0, tzinfo=BXL).isoformat())


def test_sync_reads_flags_everywhere_and_unflag_removes_moments(app, acc):
    imap = FakeIMAP()
    imap.box["Archive"][7] = raw("TAP <noreply@flytap.com>", "Boeking", "<f7@tap>", days=200)  # oud, gearchiveerd
    imap.flagged = {("Archive", 7), ("INBOX", 11)}
    with mock.patch.object(app.ai, "enqueue"), mock.patch.object(app.ai, "enqueue_moments") as em:
        app.syncer.sync_account(imap, acc)
    flagged = {r["subject"] for r in app.db.q("SELECT subject FROM messages WHERE flagged=1")}
    assert flagged == {"Boeking", "twee"}
    assert app.db.one("SELECT in_inbox FROM messages WHERE folder='Archive'")["in_inbox"] == 0
    assert em.call_count == 2

    tid = app.db.one("SELECT t.tid FROM threads t JOIN messages m ON m.thread_key=t.thread_key WHERE m.uid=7")["tid"]
    now = datetime.now(timezone.utc)
    MO.save(app.db, tid, [{"kind": "vlucht", "title": "Inchecken", "detail": "", "at": now.isoformat(),
                           "until": (now + timedelta(hours=3)).isoformat()}])
    app.db.x("UPDATE threads SET moments_ai=1 WHERE tid=?", (tid,))
    imap.flagged = {("INBOX", 11)}  # vlag weggehaald in Spark
    with mock.patch.object(app.ai, "enqueue"), mock.patch.object(app.ai, "enqueue_moments") as em:
        app.syncer.sync_account(imap, acc)
    assert app.db.one("SELECT COUNT(*) n FROM moments")["n"] == 0
    assert em.call_count == 1  # enkel de inbox-thread, die nog niet bepaald was


def test_followup_ignores_old_sent_threads(app, client, acc):
    # met alle mail in de cache mag "wacht op antwoord" niet volstromen met jaren oude verzonden mail
    put(acc, "Sent Messages", 1, raw("Me <me@icloud.com>", "Oud", "<o1@me>", to="ann@y.be", days=400))
    put(acc, "Sent Messages", 2, raw("Me <me@icloud.com>", "Recent", "<r1@me>", to="ann@y.be", days=10))
    theirs = client.get("/api/followup", headers=H).json()["waiting_on_them"]
    assert [c["subject"] for c in theirs] == ["Recent"]
