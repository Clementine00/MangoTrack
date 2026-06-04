"""Tests for MangoTrack.

Two ideas do all the work here:

1. `TestClient` loads the FastAPI app *in-process* — no uvicorn, no port, no
   network. Requests go straight to the ASGI app and come back as responses.

2. `respx` intercepts the outbound httpx call to MangaDex and returns a canned
   response we control. We never touch the real API, so these tests are fast,
   deterministic, and don't depend on MangaDex being up (or on which chapter
   happens to be newest today).
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import main
from main import MANGADEX_API, TRACKED_MANGA_ID, app

client = TestClient(app)

# The exact URL /latest calls. respx matches on this path regardless of the
# query string, so we don't have to restate the params here.
FEED_URL = f"{MANGADEX_API}/manga/{TRACKED_MANGA_ID}/feed"


def test_health():
    """The liveness probe is pure and makes no network call — easiest baseline."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

def test_version():
    """/version reports version+commit+url, all falling back to "dev" locally."""
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "dev"
    assert body["commit"] == "dev"
    assert body["url"].endswith("/commit/dev")


@respx.mock
def test_latest_happy_path():
    """A normal MangaDex response maps to 200 + the fields we expose."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "chapter": "42",
                            "title": "The Big One",
                            "publishAt": "2026-06-01T00:00:00+00:00",
                        }
                    }
                ]
            },
        )
    )

    resp = client.get("/latest")

    assert resp.status_code == 200
    assert resp.json() == {
        "manga_id": TRACKED_MANGA_ID,
        "latest_chapter": "42",
        "chapter_title": "The Big One",
        "published_at": "2026-06-01T00:00:00+00:00",
    }


@respx.mock
def test_latest_no_chapters_returns_404():
    """MangaDex answers fine but has nothing for us → honest 404, not a crash."""
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    resp = client.get("/latest")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "No English chapters found for this manga"


@respx.mock
def test_latest_upstream_error_returns_502():
    """When MangaDex 500s, *we* aren't broken — surface it as 502 Bad Gateway."""
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))

    resp = client.get("/latest")

    assert resp.status_code == 502
    assert "Could not reach MangaDex" in resp.json()["detail"]


@respx.mock
def test_latest_timeout_returns_502():
    """A hung upstream (timeout) is the other failure mode 502 must cover."""
    respx.get(FEED_URL).mock(side_effect=httpx.TimeoutException("timed out"))

    resp = client.get("/latest")

    assert resp.status_code == 502


 # A second, valid-but-different UUID to prove the path id actually steers the call.
OTHER_MANGA_ID = "11111111-1111-1111-1111-111111111111"
OTHER_FEED_URL = f"{MANGADEX_API}/manga/{OTHER_MANGA_ID}/feed"


@respx.mock
def test_latest_by_id_routes_to_that_manga():
    """The id in the path flows through to the matching MangaDex feed URL."""
    route = respx.get(OTHER_FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "chapter": "7",
                            "title": "Elsewhere",
                            "publishAt": "2026-06-02T00:00:00+00:00",
                        }
                    }
                ]
            },
        )
    )

    resp = client.get(f"/latest/{OTHER_MANGA_ID}")

    assert route.called          # we hit the right upstream URL, not the default one
    assert resp.status_code == 200
    body = resp.json()
    assert body["manga_id"] == OTHER_MANGA_ID
    assert body["latest_chapter"] == "7"


def test_latest_by_id_rejects_non_uuid():
    """A malformed id is the caller's fault: 422 here, and no MangaDex call at 
all.

    No respx mock is needed precisely because nothing leaves the process —
    FastAPI rejects it on the UUID type before latest_for() ever runs.
    """
    resp = client.get("/latest/banana")
    assert resp.status_code == 422


# --- persistence: /track ---------------------------------------------------
#
# The DB tests run against a throwaway SQLite *file* (not :memory:): get_db()
# opens a fresh connection per call, and every :memory: connection is its own
# separate, empty database — so the table init_db() creates wouldn't be visible
# to the next connection.


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file and create the schema.

    monkeypatch swaps main.DB_PATH for this test only (get_db reads it at call
    time, so the swap takes effect), then restores it. We call init_db()
    explicitly because the module-level TestClient doesn't run the lifespan.
    """
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.db"))
    main.init_db()
    yield


def test_list_tracked_empty(db):
    """Nothing tracked yet → empty list."""
    assert client.get("/track").json() == []


@respx.mock
def test_track_creates_and_lists(db):
    """POST /track captures the current chapter as a baseline and persists it."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "attributes": {
                            "chapter": "42",
                            "title": "The Big One",
                            "publishAt": "2026-06-01T00:00:00+00:00",
                        }
                    }
                ]
            },
        )
    )

    resp = client.post(f"/track/{TRACKED_MANGA_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["manga_id"] == TRACKED_MANGA_ID
    assert body["last_seen_chapter"] == "42"      # baseline captured at track-time
    assert body["last_checked_at"] is not None

    listed = client.get("/track").json()
    assert len(listed) == 1
    assert listed[0]["manga_id"] == TRACKED_MANGA_ID


@respx.mock
def test_track_is_idempotent(db):
    """Re-tracking must NOT reset the baseline or create a duplicate row."""
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    client.post(f"/track/{TRACKED_MANGA_ID}")          # baseline = 42

    # MangaDex later reports 99, but re-tracking should keep the original baseline.
    route.mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "99", "title": None, "publishAt": None}}]},
        )
    )
    resp = client.post(f"/track/{TRACKED_MANGA_ID}")

    assert resp.json()["last_seen_chapter"] == "42"    # ON CONFLICT DO NOTHING held the line
    assert len(client.get("/track").json()) == 1       # no duplicate row