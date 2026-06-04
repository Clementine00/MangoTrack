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
import respx
from fastapi.testclient import TestClient

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
    """/version reports a commit string — "dev" when GIT_SHA isn't set (local/test)."""
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json() == {"commit": "dev"}


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
