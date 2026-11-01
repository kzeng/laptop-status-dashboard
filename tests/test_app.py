import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

import app as dashboard


def sample_snapshot(marker="fresh"):
    return {
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "collection_ms": 123,
        "marker": marker,
        "system": {"hostname": "test"},
        "summary": {"level": "healthy", "title": "运行良好", "message": "测试"},
    }


def test_password_hash_round_trip():
    stored = dashboard.make_password_hash("correct horse battery staple", iterations=1_000)
    assert dashboard.verify_password("correct horse battery staple", stored)
    assert not dashboard.verify_password("wrong", stored)


def test_snapshot_store_uses_recent_cache(tmp_path: Path):
    calls = []

    def collector():
        calls.append(time.time())
        return sample_snapshot(str(len(calls)))

    store = dashboard.SnapshotStore(tmp_path / "snapshot.json", collector, 60)
    first, first_source = store.refresh()
    second, second_source = store.refresh()

    assert first_source == "fresh"
    assert second_source == "recent-cache"
    assert first == second
    assert len(calls) == 1


def test_page_load_does_not_collect_and_api_collects_once(tmp_path: Path):
    calls = []

    def collector():
        calls.append(1)
        return sample_snapshot()

    dashboard.PASSWORD_HASH = dashboard.make_password_hash("test-password", iterations=1_000)
    dashboard.SESSION_SECRET = "test-session-secret-at-least-32-bytes"
    dashboard.COOKIE_SECURE = False
    dashboard.store = dashboard.SnapshotStore(tmp_path / "snapshot.json", collector, 10)

    with TestClient(dashboard.app) as client:
        unauthenticated = client.get("/", follow_redirects=False)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/login"

        bad_login = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert bad_login.status_code == 401

        login = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
        assert login.status_code == 303

        page = client.get("/")
        assert page.status_code == 200
        assert "笔记本运行状态" in page.text
        assert calls == []

        refresh = client.post("/api/refresh")
        assert refresh.status_code == 200
        assert refresh.json()["source"] == "fresh"
        assert calls == [1]

        cached = client.post("/api/refresh")
        assert cached.status_code == 200
        assert cached.json()["source"] == "recent-cache"
        assert calls == [1]


def test_static_assets_are_immutable(tmp_path: Path):
    dashboard.SESSION_SECRET = "test-session-secret-at-least-32-bytes"
    with TestClient(dashboard.app) as client:
        response = client.get("/static/dashboard.20261101.css")
        assert response.status_code == 200
        assert "max-age=31536000" in response.headers["cache-control"]
