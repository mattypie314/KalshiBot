from fastapi.testclient import TestClient

from kalshibot.campaign.tracker import Tracker
from kalshibot.web import app


def test_index_serves_live_ui():
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert b"KalshiBot" in page.content
        assert b"Yes" in page.content or b"Fire 15m" in page.content
        assert client.get("/api/health").json() == {"status": "ok"}
        css = client.get("/static/styles.css")
        assert css.status_code == 200
        assert b"--yes:" in css.content
        assert b"color-scheme: dark" in css.content
        assert client.get("/manifest.webmanifest").status_code == 200
        assert client.get("/portfolio").status_code == 200
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
