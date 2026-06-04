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

app = FastAPI(title="MangoTrack")

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


async def fetch_latest(manga_id: str | UUID) -> dict:
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
    return {
        # str() so both routes emit a string id (a raw UUID also serializes, but
        # this keeps the two routes' output — and the tests — identical).
        "manga_id": str(manga_id),
        "latest_chapter": chapter["chapter"],   # NOTE: a string, e.g. "5", not a number
        "chapter_title": chapter.get("title"),
        "published_at": chapter.get("publishAt"),
    }


@app.get("/latest")
async def latest() -> dict:
      """Convenience: latest chapter for the one title we hardcode (Hitoner)."""
      return await fetch_latest(TRACKED_MANGA_ID)


@app.get("/latest/{manga_id}")
async def latest_for(manga_id: UUID) -> dict:
    """Latest chapter for any title, by MangaDex UUID.

    Typing `manga_id` as UUID means a malformed id 422s here *before* we ever
    call MangaDex — the network request never leaves the process.
    """
    return await fetch_latest(manga_id)
    
