from unittest import mock

from tests.conftest import H

SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"


def test_key_and_subscription_lifecycle(app, client):
    key = client.get("/api/push/key", headers=H).json()["key"]
    assert len(key) == 87 and "=" not in key  # 65 bytes, base64url zonder opvulling
    assert client.get("/api/push/key", headers=H).json()["key"] == key  # sleutel blijft dezelfde
    r = client.post("/api/push/subscribe", headers={**H, "User-Agent": IPHONE}, json={"subscription": SUB}).json()
    assert r["devices"][0]["device"] == "iPhone · app op beginscherm"
    did = r["devices"][0]["id"]
    assert client.post("/api/push/subscribe", headers=H, json={"subscription": {"endpoint": "http://x"}}).status_code == 400
    assert client.delete(f"/api/push/devices/{did}", headers=H).json()["devices"] == []


def test_send_forgets_gone_devices(app, client):
    from pywebpush import WebPushException

    client.post("/api/push/subscribe", headers=H, json={"subscription": SUB})
    gone = WebPushException("gone", response=mock.Mock(status_code=410))
    with mock.patch("pywebpush.webpush", side_effect=gone):
        assert client.post("/api/push/test", headers=H).status_code == 400  # niets afgeleverd
    assert app.push.count() == 0


def test_notifier_uses_web_push(app, client):
    client.post("/api/push/subscribe", headers=H, json={"subscription": SUB})
    with mock.patch("pywebpush.webpush") as wp, mock.patch.object(app.notifier, "in_quiet_hours", return_value=False):
        app.notifier.send("iCloud: Piet", "Afspraak", tid=42, high=True)
    import json
    msg = json.loads(wp.call_args[0][1])
    assert msg == {"title": "iCloud: Piet", "body": "Afspraak", "url": "/#t=42", "tag": "butler-42", "badge": 0}


def test_service_worker_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["service-worker-allowed"] == "/" and "showNotification" in r.text
    assert client.get("/badge-96.png").status_code == 200


def test_bare_email_becomes_mailto():
    from app.push import contact_uri

    assert contact_uri("laurensdehoorne@gmail.com") == "mailto:laurensdehoorne@gmail.com"
    assert contact_uri(" mailto:x@y.be ") == "mailto:x@y.be"
    assert contact_uri("https://butler.example") == "https://butler.example"
    assert contact_uri("") == "https://github.com"


def test_push_error_is_reported_not_counted(app, client):
    client.post("/api/push/subscribe", headers=H, json={"subscription": SUB})
    with mock.patch("pywebpush.webpush", side_effect=ValueError("Missing 'sub' from claims")):
        r = client.post("/api/push/test", headers=H)
    assert r.status_code == 502 and "Missing 'sub'" in r.json()["detail"]
    assert app.push.count() == 1  # het toestel blijft: het lag niet aan het toestel


def test_push_carries_badge_count(app, client, acc):
    from tests.conftest import put, raw

    put(acc, "INBOX", 1, raw("Piet <piet@x.be>", "Vraag", "<v1@x>"))  # nog niet getrieerd: wacht op jou
    put(acc, "INBOX", 2, raw("Ann <ann@y.be>", "Nog een vraag", "<v2@x>"))
    client.post("/api/push/subscribe", headers=H, json={"subscription": SUB})
    with mock.patch("pywebpush.webpush") as wp:
        client.post("/api/push/test", headers=H)
    import json
    assert json.loads(wp.call_args[0][1])["badge"] == 2
