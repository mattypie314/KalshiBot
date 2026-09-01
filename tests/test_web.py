from fastapi.testclient import TestClient

from kalshibot.campaign.tracker import Tracker
from kalshibot.web import app


def test_index_serves_live_ui():
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert b"KalshiBot" in page.content
        assert b'data-fire="fifteen"' in page.content
        assert b"title=\"Fire 15m now\"" in page.content
        # Phone Safari over Tailscale often drops extra /static requests, and Pi
        # hosts may serve .css as octet-stream. The shell inlines both assets.
        assert b"color-scheme: dark" in page.content
        assert b"--yes:" in page.content
        assert b"function money(" in page.content
        assert b"Go LIVE?" in page.content
        assert b'<button class="live-pill dry" id="live-pill"' in page.content
        assert b'<link rel="stylesheet" href="/static/styles.css"' not in page.content
        assert b'<script src="/static/app.js">' not in page.content
        assert client.get("/api/health").json() == {"status": "ok"}
        css = client.get("/static/styles.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]
        assert b"--yes:" in css.content
        assert b"safe-area-inset-top" in css.content
        js = client.get("/static/app.js")
        assert js.status_code == 200
        assert "javascript" in js.headers["content-type"]
        assert client.get("/manifest.webmanifest").status_code == 200
        assert client.get("/portfolio").status_code == 200
        assert b"color-scheme: dark" in client.get("/portfolio").content
        assert client.get("/market/KXBTC-26SEP0117-T87749.99").status_code == 200


def test_campaign_control_halt(tmp_path):
    with TestClient(app) as client:
        engine = app.state.campaign
        engine.tracker = Tracker(tmp_path / "book.json", bankroll=25.0)
        engine.tracker.save()
        halted = client.post("/api/campaign/control", json={"halted": True})
        assert halted.status_code == 200
        assert halted.json()["status"]["halted"] is True
        resumed = client.post("/api/campaign/control", json={"halted": False})
        assert resumed.json()["status"]["halted"] is False
        assert "Campaign is live again" in " ".join(resumed.json()["notes"])


def test_campaign_control_live_needs_keys(tmp_path):
    with TestClient(app) as client:
        engine = app.state.campaign
        engine.tracker = Tracker(tmp_path / "book.json", bankroll=25.0)
        engine.tracker.save()
        denied = client.post("/api/campaign/control", json={"live": True})
        assert denied.status_code == 400
        assert "private key" in denied.json()["detail"]
        assert client.get("/api/campaign").json()["live"] is False


def test_campaign_control_live_toggle(tmp_path):
    with TestClient(app) as client:
        engine = app.state.campaign
        engine.tracker = Tracker(tmp_path / "book.json", bankroll=25.0)
        engine.tracker.save()
        engine.kalshi.api_key_id = "test-key"
        engine.kalshi._private_key = object()
        on = client.post("/api/campaign/control", json={"live": True})
        assert on.status_code == 200
        assert on.json()["status"]["live"] is True
        assert on.json()["status"]["can_trade"] is True
        assert any("LIVE" in note for note in on.json()["notes"])
        off = client.post("/api/campaign/control", json={"live": False})
        assert off.json()["status"]["live"] is False
        assert any("DRY" in note for note in off.json()["notes"])


def test_campaign_status_auto_off_in_tests():
    with TestClient(app) as client:
        status = client.get("/api/campaign").json()
        assert status["auto"] is False
