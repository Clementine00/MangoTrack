"""Tests for MangoTrack.

Two ideas do all the work here:

1. `TestClient` loads the FastAPI app *in-process* — no uvicorn, no port, no
   network. Requests go straight to the ASGI app and come back as responses.

2. `respx` intercepts the outbound httpx call to MangaDex and returns a canned
   response we control. We never touch the real API, so these tests are fast,
   deterministic, and don't depend on MangaDex being up (or on which chapter
   happens to be newest today).
"""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import main
from main import MANGADEX_API, NTFY_URL, TRACKED_MANGA_ID, app

client = TestClient(app)

# The exact URL /latest calls. respx matches on this path regardless of the
# query string, so we don't have to restate the params here.
FEED_URL = f"{MANGADEX_API}/manga/{TRACKED_MANGA_ID}/feed"

# /track also fetches the manga's display name from the manga record (NOT /feed).
# respx matches the full path, so this route is distinct from FEED_URL above.
MANGA_URL = f"{MANGADEX_API}/manga/{TRACKED_MANGA_ID}"


def _manga_title_response(title_dict):
    """A MangaDex /manga/{id} body whose attributes.title is `title_dict`."""
    return httpx.Response(200, json={"data": {"attributes": {"title": title_dict}}})


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
OTHER_MANGA_URL = f"{MANGADEX_API}/manga/{OTHER_MANGA_ID}"


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


def test_migrations_upgrade_pre_title_db(tmp_path, monkeypatch):
    """A database created before the title column existed gains it on startup.

    This rehearses prod: the real volume holds the original 3-column
    tracked_manga with user_version still at its default of 0. The runner must
    bring it up to date and record how far it got.
    """
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "old.db"))

    # Build the pre-migration schema by hand. This SQL is a historical
    # artifact — it must stay the OLD 3-column shape even as MIGRATIONS grows.
    with main.get_db() as conn:
        conn.execute(
            """
            CREATE TABLE tracked_manga (
                manga_id          TEXT PRIMARY KEY,
                last_seen_chapter TEXT,
                last_checked_at   TEXT
            )
            """
        )

    main.init_db()

    with main.get_db() as conn:
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(tracked_manga)")]
        assert "title" in cols
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(main.MIGRATIONS)


def test_init_db_is_safe_to_rerun(db):
    """Every startup calls init_db(); a second run must not re-apply anything.

    Regression test for the naive approach: an unconditional ALTER TABLE would
    raise 'duplicate column name: title' on the second call.
    """
    main.init_db()  # the db fixture already ran it once; this is restart #2

    with main.get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(main.MIGRATIONS)


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
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))

    resp = client.post(f"/track/{TRACKED_MANGA_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["manga_id"] == TRACKED_MANGA_ID
    assert body["last_seen_chapter"] == "42"      # baseline captured at track-time
    assert body["last_checked_at"] is not None
    assert body["title"] == "Hitoner"             # display name fetched at track-time

    listed = client.get("/track").json()
    assert len(listed) == 1
    assert listed[0]["manga_id"] == TRACKED_MANGA_ID
    assert listed[0]["title"] == "Hitoner"


@respx.mock
def test_track_is_idempotent(db):
    """Re-tracking must NOT reset the baseline or create a duplicate row."""
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
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


@respx.mock
def test_track_title_falls_back_when_no_english(db):
    """No "en" key → use the first localized title we do have."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"ja": "ヒトナー"}))

    resp = client.post(f"/track/{TRACKED_MANGA_ID}")

    assert resp.json()["title"] == "ヒトナー"


@respx.mock
def test_track_title_is_best_effort(db):
    """The title lookup failing must NOT break /track: it succeeds, title → id.

    The baseline still comes from the feed; only the (best-effort) name is
    missing, so the response falls back to the manga id.
    """
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=httpx.Response(500))   # name lookup breaks

    resp = client.post(f"/track/{TRACKED_MANGA_ID}")

    assert resp.status_code == 200                  # tracking still works
    assert resp.json()["last_seen_chapter"] == "42"
    assert resp.json()["title"] == TRACKED_MANGA_ID  # untitled → falls back to the id


# --- untrack (delete) ------------------------------------------------------


@respx.mock
def test_untrack_removes_manga(db):
    """DELETE /track/{id} stops tracking: the manga drops out of GET /track."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")
    assert len(client.get("/track").json()) == 1

    resp = client.delete(f"/track/{TRACKED_MANGA_ID}")

    assert resp.status_code == 200
    assert resp.json()["manga_id"] == TRACKED_MANGA_ID
    assert client.get("/track").json() == []


def test_untrack_unknown_manga_404(db):
    """Deleting a manga we're not tracking is an honest 404, not a silent no-op."""
    resp = client.delete(f"/track/{TRACKED_MANGA_ID}")
    assert resp.status_code == 404


@respx.mock
def test_untrack_removes_pending_notifications(db, check_auth, ntfy_topic):
    """Untracking forgets pending notifications too, so a manga you stopped
    tracking can't still buzz your phone on a later sweep (the LEFT-JOIN orphan).
    """
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")          # baseline = 42

    # A new chapter is detected, but the push fails → notification left pending.
    route.mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "50", "title": None, "publishAt": None}}]},
        )
    )
    push = respx.post(NTFY_URL).mock(return_value=httpx.Response(500))
    client.post("/check", headers=check_auth)
    assert push.call_count == 1                        # tried once, failed

    # Untrack it — the DELETE reports the pending notification it cleaned up.
    resp = client.delete(f"/track/{TRACKED_MANGA_ID}")
    assert resp.json()["notifications_removed"] == 1

    # ntfy recovers, but there's nothing left to deliver for the gone manga.
    push.mock(return_value=httpx.Response(200, json={}))
    client.post("/check", headers=check_auth)
    assert push.call_count == 1                        # no orphan push fired


# --- liveness vs readiness -------------------------------------------------


def test_ready_ok_when_db_reachable(db):
    """DB up + schema present → 200 ready."""
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_ready_503_when_schema_missing(tmp_path, monkeypatch):
    """Point at a fresh DB file but DON'T init_db → table missing → 503, not a crash.

    No `db` fixture here on purpose: we *want* an uninitialized database so the
    readiness query fails and we can prove it degrades to 503 gracefully.
    """
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "empty.db"))
    resp = client.get("/ready")
    assert resp.status_code == 503


# --- scheduled check -------------------------------------------------------

CHECK_TOKEN = "test-secret"


@pytest.fixture
def check_auth(monkeypatch):
    """Set a known CHECK_TOKEN for the test and return the matching auth header."""
    monkeypatch.setattr(main, "CHECK_TOKEN", CHECK_TOKEN)
    return {"Authorization": f"Bearer {CHECK_TOKEN}"}


@pytest.fixture
def ntfy_topic(monkeypatch):
    """Configure a topic so delivery is enabled. Without this fixture NTFY_TOPIC
    is "" (fail-closed), so /check detects but never pushes."""
    monkeypatch.setattr(main, "NTFY_TOPIC", "test-topic")


def test_is_newer():
    """Chapter comparison is numeric, with a string-difference fallback."""
    assert main.is_newer("43", "42") is True
    assert main.is_newer("42", "42") is False
    assert main.is_newer("41", "42") is False
    assert main.is_newer("42.5", "42") is True       # decimals compare numerically
    assert main.is_newer("10", "9") is True          # numeric, not lexical ("10" < "9")
    assert main.is_newer("42", None) is True         # nothing stored yet → new
    assert main.is_newer("extra", "42") is True      # non-numeric + different → new
    assert main.is_newer("oneshot", "oneshot") is False  # non-numeric + same → not new


def test_check_requires_secret(db):
    """No token → 401, and fail-closed: CHECK_TOKEN is unset here."""
    resp = client.post("/check")
    assert resp.status_code == 401


def test_check_rejects_wrong_token(db, check_auth):
    """A token that doesn't match → 401."""
    resp = client.post("/check", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@respx.mock
def test_check_detects_new_chapter(db, check_auth):
    """New chapter → baseline advances and a pending notification is recorded."""
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")          # baseline = 42

    route.mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "50", "title": None, "publishAt": None}}]},
        )
    )
    resp = client.post("/check", headers=check_auth)

    assert resp.status_code == 200
    assert resp.json() == {"checked": 1, "new": 1, "delivered": 0}       # no topic -> not pushed
    assert client.get("/track").json()[0]["last_seen_chapter"] == "50"   # baseline advanced
    with main.get_db() as conn:
        rows = conn.execute("SELECT chapter, delivered FROM notifications").fetchall()
    assert len(rows) == 1
    assert rows[0]["chapter"] == "50"
    assert rows[0]["delivered"] == 0                   # pending, for slice 2 to deliver


@respx.mock
def test_check_no_new_chapter(db, check_auth):
    """Same chapter → nothing new, no notification row."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")

    resp = client.post("/check", headers=check_auth)
    assert resp.json() == {"checked": 1, "new": 0, "delivered": 0}
    with main.get_db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM notifications").fetchone()["c"]
    assert count == 0


@respx.mock
def test_check_skips_failing_manga(db, check_auth):
    """One title's MangaDex call failing doesn't sink the batch — the rest run."""
    r1 = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    r2 = respx.get(OTHER_FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    respx.get(OTHER_MANGA_URL).mock(return_value=_manga_title_response({"en": "Elsewhere"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")
    client.post(f"/track/{OTHER_MANGA_ID}")

    r1.mock(return_value=httpx.Response(500))          # this title's upstream breaks
    r2.mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "43", "title": None, "publishAt": None}}]},
        )
    )
    resp = client.post("/check", headers=check_auth)

    assert resp.status_code == 200                      # batch survived the failure
    assert resp.json() == {"checked": 1, "new": 1, "delivered": 0}  # failing one skipped, healthy one processed


# --- notification delivery (ntfy) ------------------------------------------


@respx.mock
def test_check_delivers_to_ntfy(db, check_auth, ntfy_topic):
    """A detected chapter is pushed to ntfy and its row flips to delivered=1."""
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")          # baseline = 42

    route.mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "50", "title": None, "publishAt": None}}]},
        )
    )
    push = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    resp = client.post("/check", headers=check_auth)

    assert resp.json() == {"checked": 1, "new": 1, "delivered": 1}
    assert push.called
    sent = json.loads(push.calls.last.request.content)
    assert sent["topic"] == "test-topic"
    assert sent["title"] == "Hitoner"                  # human-readable name, not the id
    assert "50" in sent["message"]                     # the new chapter number
    with main.get_db() as conn:
        delivered = conn.execute("SELECT delivered FROM notifications").fetchone()["delivered"]
    assert delivered == 1


@respx.mock
def test_check_delivery_failure_retries_next_sweep(db, check_auth, ntfy_topic):
    """A failed push leaves the row at delivered=0; a later sweep drains it."""
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "42", "title": None, "publishAt": None}}]},
        )
    )
    respx.get(MANGA_URL).mock(return_value=_manga_title_response({"en": "Hitoner"}))
    client.post(f"/track/{TRACKED_MANGA_ID}")

    route.mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"attributes": {"chapter": "50", "title": None, "publishAt": None}}]},
        )
    )
    push = respx.post(NTFY_URL).mock(return_value=httpx.Response(500))   # ntfy is down

    resp = client.post("/check", headers=check_auth)
    assert resp.json() == {"checked": 1, "new": 1, "delivered": 0}       # detected, not delivered
    with main.get_db() as conn:
        assert conn.execute("SELECT delivered FROM notifications").fetchone()["delivered"] == 0

    # Next sweep: no new chapter, but ntfy recovers and the pending row drains.
    push.mock(return_value=httpx.Response(200, json={}))
    resp = client.post("/check", headers=check_auth)
    assert resp.json() == {"checked": 1, "new": 0, "delivered": 1}       # retried successfully
    with main.get_db() as conn:
        assert conn.execute("SELECT delivered FROM notifications").fetchone()["delivered"] == 1