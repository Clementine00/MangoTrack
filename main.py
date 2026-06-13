"""MangoTrack — manga chapter tracker (learning project).

A starting point for learning devops, growing into a manga tracker that will
eventually notify when new chapters drop.

Endpoints:
    /health             — liveness probe (process up and serving HTTP?)
    /ready              — readiness probe (DB reachable + schema present?)
    /version            — what image is running: version, commit, link
    /latest             — latest chapter for the default title (Hitoner)
    /latest/{manga_id}  — latest chapter for any title, by MangaDex UUID
    /track/{manga_id}   — start tracking a manga (POST); baseline = current latest
    /track              — list tracked manga (GET)
    /check              — scheduled sweep for new chapters (POST, secret-gated)
"""
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

# --- MangaDex config -------------------------------------------------------
MANGADEX_API = "https://api.mangadex.org"

# The title the bare /latest route defaults to ("Hitoner").
# /latest/{manga_id} lets callers track any title instead.
TRACKED_MANGA_ID = "59ef045c-0712-4f15-bb54-52bffd87481b"

# MangaDex asks API clients to identify themselves with a User-Agent.
# Being a good API citizen also makes our traffic debuggable on their side.
USER_AGENT = "MangoTrack/0.1 (https://github.com/Clementine00/MangoTrack; learning project)"

GITHUB_REPO = "https://github.com/Clementine00/MangoTrack"

# Local default; on Fly we'll point this at the mounted volume via env var.
DB_PATH = os.getenv("DB_PATH", "mangotrack.db")

# Shared secret guarding POST /check. Unset locally/in tests means the endpoint
# refuses every caller (fail closed). In prod it's a Fly secret + a GitHub
# Actions secret, sent by the cron as `Authorization: Bearer <token>`.
CHECK_TOKEN = os.getenv("CHECK_TOKEN", "")

# ntfy push delivery. The base server is a constant; only the topic is secret
# (on ntfy.sh the topic name IS the access control). NTFY_TOPIC unset means
# delivery is skipped entirely (fail closed) — local/test runs never push.
NTFY_URL = "https://ntfy.sh"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")


# --- database --------------------------------------------------------------
@contextmanager
def get_db():
    """Open a SQLite connection, commit on success, always close.

    Connection-per-operation: sqlite3.connect is cheap, and this avoids the
    cross-thread headaches a single shared connection brings. `row_factory`
    makes rows accessible by column name (row["manga_id"]) instead of by index.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Each entry is one schema change, applied in order, exactly once per database.
# The database remembers how many it has applied via PRAGMA user_version.
# NEVER edit or reorder existing entries — only append. A database that has
# already applied a step will never re-run it, so editing history only affects
# fresh databases and silently diverges them from prod.
MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS tracked_manga (
        manga_id          TEXT PRIMARY KEY,
        last_seen_chapter TEXT,
        last_checked_at   TEXT
    )
    """,
    # Each detected new chapter becomes a pending notification row. Slice 1
    # writes these (delivered=0); a later slice delivers them and flips the flag.
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        manga_id    TEXT NOT NULL,
        chapter     TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        delivered   INTEGER NOT NULL DEFAULT 0
    )
    """,
    # Human-readable name for notifications, fetched once at /track time.
    # Nullable: rows tracked before this column existed fall back to the id.
    "ALTER TABLE tracked_manga ADD COLUMN title TEXT",
]


def init_db() -> None:
    """Apply any schema migrations this database hasn't seen yet."""
    with get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for n, statement in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.execute(statement)
            # PRAGMA doesn't accept ? placeholders; n is our own loop counter,
            # never external input, so the f-string is safe here.
            conn.execute(f"PRAGMA user_version = {n}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()      # runs once, when the app boots
    yield          # app serves requests here
    # (nothing to tear down for SQLite)


app = FastAPI(title="MangoTrack", lifespan=lifespan)


# --- response models -------------------------------------------------------
class LatestChapter(BaseModel):
    """The shape of every /latest response — our 'departures manifest'.

    manga_id and latest_chapter are always present; chapter_title and
    published_at can be null (MangaDex doesn't always supply them, which is
    why fetch_latest reads them with .get()).
    """
    manga_id: str
    latest_chapter: str
    chapter_title: str | None
    published_at: str | None


class TrackedManga(BaseModel):
    manga_id: str
    last_seen_chapter: str | None
    last_checked_at: str | None
    title: str | None


# --- health -----------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: is THIS PROCESS up and serving HTTP? No dependency checks, on
    purpose — if the DB were down, restarting the process wouldn't help, so
    failing liveness on it would just cause a pointless restart loop.
    """
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Readiness: can we actually do real work — i.e. is the DB reachable and the
    schema present? If not, return 503 so the platform stops routing traffic here
    (but does NOT restart us — we're alive, just not ready).
    """
    try:
        with get_db() as conn:
            conn.execute("SELECT 1 FROM tracked_manga LIMIT 1")
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail=f"Database not ready: {exc}") from exc
    return {"status": "ready"}


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


# --- latest (read) ----------------------------------------------------------
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


async def fetch_manga_title(manga_id: str | UUID) -> str | None:
    """Fetch a manga's display name from MangaDex, best-effort.

    Hits /manga/{id} (the manga's own record) — NOT /feed, which only carries
    chapters; the manga's name lives nowhere in the feed. The name comes back as
    a localization dict ({"en": "Hitoner", ...}), so we pick a language out of
    it: English if present, else whatever's there. Returns None on any failure
    (network, bad status, or unexpected shape) so a missing title degrades to the
    id at read-time instead of breaking /track.
    """
    url = f"{MANGADEX_API}/manga/{manga_id}"
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError:
        return None

    titles = resp.json().get("data", {}).get("attributes", {}).get("title", {})
    if not isinstance(titles, dict):
        return None
    # Prefer English; fall back to the first localized title; None if there's none.
    return titles.get("en") or next(iter(titles.values()), None)


# --- track (write) ----------------------------------------------------------
@app.post("/track/{manga_id}")
async def track(manga_id: UUID) -> TrackedManga:
    """Start tracking a manga. Baseline = its current latest chapter, so we only
    notify on chapters released *after* this point. Idempotent: re-tracking keeps
    the original baseline.
    """
    latest = await fetch_latest(manga_id)          # reuse the read helper; 404s if no chapters
    title = await fetch_manga_title(manga_id)       # best-effort; None if MangaDex won't say
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO tracked_manga (manga_id, last_seen_chapter, last_checked_at, title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(manga_id) DO NOTHING
            """,
            (str(manga_id), latest.latest_chapter, now, title),
        )
        row = conn.execute(
            "SELECT manga_id, last_seen_chapter, last_checked_at, title FROM tracked_manga WHERE manga_id = ?",
            (str(manga_id),),
        ).fetchone()
    return TrackedManga(
        manga_id=row["manga_id"],
        last_seen_chapter=row["last_seen_chapter"],
        last_checked_at=row["last_checked_at"],
        title=row["title"] or row["manga_id"],     # fall back to the id if untitled
    )


@app.get("/track")
def list_tracked() -> list[TrackedManga]:
    """List everything we're tracking. Plain `def` (not async) on purpose: SQLite
    calls block, and FastAPI runs sync endpoints in a threadpool so the blocking
    read doesn't freeze the event loop.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT manga_id, last_seen_chapter, last_checked_at, title FROM tracked_manga ORDER BY manga_id"
        ).fetchall()
    return [
        TrackedManga(
            manga_id=row["manga_id"],
            last_seen_chapter=row["last_seen_chapter"],
            last_checked_at=row["last_checked_at"],
            title=row["title"] or row["manga_id"],     # fall back to the id if untitled
        )
        for row in rows
    ]


# --- scheduled check --------------------------------------------------------
def is_newer(latest: str, stored: str | None) -> bool:
    """True if `latest` is a newer chapter than `stored`.

    Chapters are strings like "42" or "42.5", so compare numerically when both
    parse as floats (string compare would rank "9" above "10"). Fall back to
    "different string = new" for non-numeric chapters, so we never silently miss
    a release we can't parse. Nothing stored yet → anything counts as new.
    """
    if stored is None:
        return True
    try:
        return float(latest) > float(stored)
    except ValueError:
        return latest != stored


def require_check_token(authorization: str = Header(default="")) -> None:
    """Guard /check with a shared secret sent as `Authorization: Bearer <token>`.

    `secrets.compare_digest` is a constant-time comparison (avoids leaking the
    token via response timing). Fails closed: if CHECK_TOKEN is unset, no caller
    can match, so the endpoint is locked rather than wide open.
    """
    expected = f"Bearer {CHECK_TOKEN}"
    if not CHECK_TOKEN or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def notify(title: str, message: str, click: str | None = None) -> bool:
    """Push one notification to ntfy. Best-effort: returns True on success and
    False on any failure, never raises — a delivery failure must not sink the
    sweep, and the caller leaves the row undelivered so a later sweep retries.

    Uses ntfy's JSON publish format (POST a body to the root URL) rather than
    HTTP headers, because titles can be non-ASCII (e.g. Japanese) and header
    values can't carry that; a JSON body is UTF-8 all the way through.
    """
    if not NTFY_TOPIC:
        return False   # fail closed: no topic configured -> nothing to deliver to
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message}
    if click:
        payload["click"] = click
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.post(NTFY_URL, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


async def deliver_pending() -> int:
    """Drain undelivered notifications: push each to ntfy and flip delivered=1
    only on a successful send. A failed push leaves the row at 0, so the next
    /check sweep retries it for free. Returns how many were delivered this pass.

    Joins to tracked_manga for the human-readable title (falling back to the id),
    so the push reads "Hitoner — Chapter 50" rather than a bare UUID.
    """
    if not NTFY_TOPIC:
        return 0
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT n.id, n.manga_id, n.chapter, t.title
            FROM notifications n
            LEFT JOIN tracked_manga t ON t.manga_id = n.manga_id
            WHERE n.delivered = 0
            ORDER BY n.id
            """
        ).fetchall()

    delivered = 0
    for row in rows:
        title = row["title"] or row["manga_id"]
        click = f"https://mangadex.org/title/{row['manga_id']}"
        if await notify(title, f"Chapter {row['chapter']} is out", click):
            with get_db() as conn:
                conn.execute(
                    "UPDATE notifications SET delivered = 1 WHERE id = ?",
                    (row["id"],),
                )
            delivered += 1
    return delivered


@app.post("/check")
async def check(_: None = Depends(require_check_token)) -> dict[str, int]:
    """Scheduled sweep: for each tracked manga, detect a newer latest chapter,
    advance its baseline, and record a pending notification — then drain any
    undelivered notifications to ntfy. Secret-gated and triggered by an external
    cron whose HTTP request wakes the scaled-to-zero machine. One title's
    MangaDex failure is skipped so it can't sink the batch; the delivery pass
    drains everything still pending (including pushes that failed last sweep).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        targets = conn.execute(
            "SELECT manga_id, last_seen_chapter FROM tracked_manga"
        ).fetchall()

    checked = 0
    new = 0
    for target in targets:
        manga_id = target["manga_id"]
        try:
            latest = await fetch_latest(manga_id)
        except HTTPException:
            # This title's upstream call failed (502/404); skip and keep sweeping.
            continue
        checked += 1
        with get_db() as conn:
            if is_newer(latest.latest_chapter, target["last_seen_chapter"]):
                conn.execute(
                    "UPDATE tracked_manga SET last_seen_chapter = ?, last_checked_at = ? WHERE manga_id = ?",
                    (latest.latest_chapter, now, manga_id),
                )
                conn.execute(
                    "INSERT INTO notifications (manga_id, chapter, detected_at, delivered) "
                    "VALUES (?, ?, ?, 0)",
                    (manga_id, latest.latest_chapter, now),
                )
                new += 1
            else:
                conn.execute(
                    "UPDATE tracked_manga SET last_checked_at = ? WHERE manga_id = ?",
                    (now, manga_id),
                )
    delivered = await deliver_pending()
    return {"checked": checked, "new": new, "delivered": delivered}
