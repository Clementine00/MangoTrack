"""MangoTrack — manga chapter tracker (learning project).

A starting point for learning devops, growing into a manga tracker that will
eventually notify when new chapters drop.

Endpoints:
  /health  — liveness probe (is the process up and serving HTTP?)
  /latest  — calls the MangaDex API and returns the latest chapter number
             for the one manga we're tracking.
"""

import httpx
import os
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


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. 200 + {"status": "ok"} means the process can serve HTTP."""
    return {"status": "ok"}

@app.get("/version")
def version() -> dict[str, str]:
    """Report the git commit this running image was built from.
      
      The SHA is baked in at build time (Dockerfile ARG -> ENV). Falls back to
      "dev" for local runs and tests, so any running instance can tell you
      exactly what code it's serving.
      """
    return {"commit": os.getenv("GIT_SHA", "dev")}


@app.get("/latest")
async def latest() -> dict:
    """Return the latest English chapter number for the tracked manga via MangaDex.

    This endpoint is `async` because it makes a *network call*. While we wait on
    MangaDex, an async client frees the server to handle other requests instead
    of blocking.
    """
    url = f"{MANGADEX_API}/manga/{TRACKED_MANGA_ID}/feed"
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
        "manga_id": TRACKED_MANGA_ID,
        "latest_chapter": chapter["chapter"],   # NOTE: a string, e.g. "5", not a number
        "chapter_title": chapter.get("title"),
        "published_at": chapter.get("publishAt"),
    }
