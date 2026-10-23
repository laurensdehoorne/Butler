from datetime import datetime, timedelta, timezone

from tests.conftest import H, put, raw


def test_calendar_feed_with_secret_link(app, client, acc):
    tid, _ = put(acc, "INBOX", 1, raw("Hotel <h@molina.es>", "Bevestiging Malaga", "<h@x>"))
    app.db.x("UPDATE threads SET deadline='2099-11-14T15:00', summary='Hotel Molina Lario, Malaga' WHERE tid=?", (tid,))
    day, _ = put(acc, "INBOX", 2, raw("School <s@school.be>", "Oudercontact", "<o@x>"))
    app.db.x("UPDATE threads SET deadline='2099-12-01' WHERE tid=?", (day,))
    path = client.get("/api/calendar", headers=H).json()["path"]
    assert client.get(path.replace(path[5:9], "0000")).status_code == 404  # verkeerd geheim
    ics = client.get(path).text  # zonder token: een agenda-app meldt niet aan
    assert ics.startswith("BEGIN:VCALENDAR") and "SUMMARY:Bevestiging Malaga" in ics
    assert "DTSTART:20991114T140000Z" in ics  # 15:00 in Brussel = 14:00 UTC
    assert "DTSTART;VALUE=DATE:20991201" in ics and "DTEND;VALUE=DATE:20991202" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
