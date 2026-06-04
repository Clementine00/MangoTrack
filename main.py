"""MangoTrack — manga chapter tracker (learning project).

A starting point for learning devops, growing into a manga tracker that will
eventually notify when new chapters drop.

Endpoints:
    /health             — liveness probe (is the process up and serving HTTP?)
    /version            — what image is running: version, commit, link
    /latest             — latest chapter for the one title we hardcode (Hitoner)
    /latest/{manga_id}  — latest chapter for any title, by MangaDex UUID
"""
import os
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException

from pydantic import BaseModel

import sqlite3
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone


# --- MangaDex config -------------------------------------------------------
MANGADEX_API = "https://api.mangadex.org"

# Hardcoded for now: the single manga we're tracking ("Hitoner").
# Next step once this works: make this a path parameter, e.g. /latest/{manga_id},
# so MangoTrack can track any title.
TRACKED_MANGA_ID = "59ef045c-0712-4f15-bb54-52bffd87481b"

# MangaDex asks API clients to identify themselves with a User-Agent.
# Being a good API citizen also makes our traffic debuggable on their side.
USER_AGENT = "MangoTrack/0.1 (https://github.com/Clementine00/MangoTrack; learning project)"

GITHUB_REPO = "https://github.com/Clementine00/MangoTrack"

 # Local default; on Fly we'll point this at the mounted volume via env var.
DB_PATH = os.getenv("DB_PATH", "mangotrack.db")


@contextmanager
def get_db():
    """Open a SQLite connection, commit on success, always close.

    Connection-per-operation: sqlite3.connect is cheap, and this 
avoids the
    cross-thread headaches a single shared connection brings. 
`row_factory`
    makes rows accessible by column name (row["manga_id"]) instead 
of by index.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
      """Create the table if it's missing. Safe to run on every startup."""
      with get_db() as conn:
          conn.execute(
              """
              CREATE TABLE IF NOT EXISTS tracked_manga (
                  manga_id          TEXT PRIMARY KEY,
                  last_seen_chapter TEXT,
                  last_checked_at   TEXT
              )
              """
          )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()      # runs once, when the app boots
    yield          # app serves requests here
    # (nothing to tear down for SQLite)


app = FastAPI(title="MangoTrack", lifespan=lifespan)


class TrackedManga(BaseModel):
      manga_id: str
      last_seen_chapter: str | None
      last_checked_at: str | None


class LatestChapter(BaseModel):
      """The shape of every /latest response — our 'departures 
  manifest'.

      manga_id and latest_chapter are always present; chapter_title 
  and
      published_at can be null (MangaDex doesn't always supply them, 
  which is
      why fetch_latest reads them with .get()).
      """
      manga_id: str
      latest_chapter: str
      chapter_title: str | None
      published_at: str | None


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. 200 + {"status": "ok"} means the process can serve HTTP."""
    return {"status": "ok"}


@app.get("/version")
def version() -> dict[str, str]:
    """Report what this running image is: release version, commit, and a link.

    Both values are baked in at build time (Dockerfile ARG -> ENV) and fall
    back to "dev" locally/in tests. `url` lets you click straight to the exact
    commit on GitHub instead of eyeballing a hash against `git log`.
    """
    sha = os.getenv("GIT_SHA", "dev")
    return {
        "version": os.getenv("APP_VERSION", "dev"),
        "commit": sha[:7],
        "url": f"{GITHUB_REPO}/commit/{sha}",
    }


async def fetch_latest(manga_id: str | UUID) -> LatestChapter:
    """Ask MangaDex for the latest English chapter of `manga_id`.

    All the network call + error translation lives here so both /latest routes
    share one implementation. `manga_id` may be a str (the hardcoded default) or
    a UUID (from the path route); an f-string stringifies either the same way.

    This is `async` because it makes a *network call*. While we wait on MangaDex,
    an async client frees the server to handle other requests instead of blocking.
    """
    url = f"{MANGADEX_API}/manga/{manga_id}/feed"
    params = {
        "translatedLanguage[]": "en",   # only English-translated chapters
        "order[chapter]": "desc",       # sort highest chapter number first
        "limit": 1,                     # we only need the top one
    }

    # ALWAYS set a timeout on an external call. Without one, a hung upstream
    # could make *our* request hang forever and pile up connections.
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()  # turn a 4xx/5xx from MangaDex into an exception
    except httpx.HTTPError as exc:
        # The service we depend on failed (network error, timeout, or bad status).
        # 502 Bad Gateway is the honest signal: *we're* fine, the upstream isn't.
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach MangaDex: {exc}",
        ) from exc

    data = resp.json().get("data", [])
    if not data:
        # The call succeeded but there are no chapters to report.
        raise HTTPException(status_code=404, detail="No English chapters found for this manga")

    chapter = data[0]["attributes"]
    return LatestChapter(
          manga_id=str(manga_id),
          latest_chapter=chapter["chapter"],
          chapter_title=chapter.get("title"),
          published_at=chapter.get("publishAt"),
      )


@app.get("/latest")
async def latest() -> LatestChapter:
      """Convenience: latest chapter for the one title we hardcode (Hitoner)."""
      return await fetch_latest(TRACKED_MANGA_ID)


@app.get("/latest/{manga_id}")
async def latest_for(manga_id: UUID) -> LatestChapter:
    """Latest chapter for any title, by MangaDex UUID.

    Typing `manga_id` as UUID means a malformed id 422s here *before* we ever
    call MangaDex — the network request never leaves the process.
    """
    return await fetch_latest(manga_id)


@app.post("/track/{manga_id}")
async def track(manga_id: UUID) -> TrackedManga:
    """Start tracking a manga. Baseline = its current latest 
chapter, so we
    only notify on chapters released *after* this point. 
Idempotent: re-tracking
    keeps the original baseline."""
    latest = await fetch_latest(manga_id)          # reuse the readhelper; 404s if no chapters
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tracked_manga (manga_id, last_seen_chapter,
last_checked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(manga_id) DO NOTHING
            """,
            (str(manga_id), latest.latest_chapter, now), 
        )
        row = conn.execute(
            "SELECT manga_id, last_seen_chapter, last_checked_at FROM tracked_manga WHERE manga_id = ?",
            (str(manga_id),),
        ).fetchone()
    return TrackedManga(
        manga_id=row["manga_id"],
        last_seen_chapter=row["last_seen_chapter"],
        last_checked_at=row["last_checked_at"],
    )


@app.get("/track")
def list_tracked() -> list[TrackedManga]:
    """List everything we're tracking. Plain `def` (not async) on 
purpose — see note."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT manga_id, last_seen_chapter, last_checked_at FROM tracked_manga ORDER BY manga_id"
        ).fetchall()
    return [
        TrackedManga(
            manga_id=row["manga_id"],
            last_seen_chapter=row["last_seen_chapter"],
            last_checked_at=row["last_checked_at"],
        )
        for row in rows
    ]
    
