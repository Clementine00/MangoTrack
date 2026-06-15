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
    /track/{manga_id}   — stop tracking a manga (DELETE); also forgets its notifications
    /track              — list tracked manga (GET)
    /check              — scheduled sweep for new chapters + notification delivery (POST, secret-gated)
"""
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import sentry_sdk
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from logging_setup import request_id_var, setup_logging

# App logger. Children (mangotrack.check, ...) propagate up to the root handler
# configured in setup_logging(), so everything comes out as one JSON stream.
log = logging.getLogger("mangotrack")

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
    setup_logging()   # configure log delivery before anything logs
    init_db()         # runs once, when the app boots
    yield             # app serves requests here
    # (nothing to tear down for SQLite)


# --- Sentry (error tracking) -----------------------------------------------
def before_send(event, hint):
    """Drop events for HTTPExceptions we raise on purpose (404/401/502/...).

    Those are handled control flow, not bugs: a typo'd manga id or a MangaDex
    outage is an expected outcome we already signal with a status code. Only
    genuinely unhandled exceptions — the real 500s — should reach Sentry, so a
    title-not-found never pages anyone. hint["exc_info"] is (type, value, tb).
    """
    exc_info = hint.get("exc_info")
    if exc_info and isinstance(exc_info[1], HTTPException):
        return None
    return event


# DSN unset (local/tests) -> the SDK goes no-op and ships nothing, the same
# fail-closed pattern as NTFY_TOPIC/CHECK_TOKEN. The FastAPI/Starlette
# integration auto-enables because those packages are installed, so unhandled
# exceptions are captured without decorating any endpoint. Init must run before
# the FastAPI app is constructed so that integration can patch it.
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),
    environment=os.getenv("SENTRY_ENVIRONMENT", "development"),
    release=os.getenv("GIT_SHA", "dev"),
    traces_sample_rate=0.0,          # errors only; tracing is a later (metrics) concern
    before_send=before_send,
)


app = FastAPI(title="MangoTrack", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Assign each request an id, time it, and log one completion line.

    Setting request_id_var here means every log emitted *during* this request —
    the semantic ones inside endpoints too — is stamped with the same id, so a
    whole request's lines can be grepped together. Health/readiness pings are the
    cron's noise, so we skip logging them (they still get an id, harmlessly).
    """
    request_id = uuid4().hex[:8]
    token = request_id_var.set(request_id)
    start = time.perf_counter()
    status = 500   # if call_next raises, we still log a 500 before re-raising
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id   # let clients correlate too
        return response
    finally:
        if request.url.path not in ("/health", "/ready", "/metrics"):
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
        request_id_var.reset(token)


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


# --- metrics ----------------------------------------------------------------
@app.get("/metrics")
def metrics() -> Response:
    """Prometheus exposition of domain counts, read from the DB at scrape time.

    Hand-rolled like JsonFormatter — the text format is trivial and dodges a
    dependency. Reading from the persistent DB is what makes these survive
    scale-to-zero: the values live on the volume, not in memory, so every scrape
    after a wake reports the correct running total instead of a counter that
    reset to zero when the machine last slept. Plain `def` (not async) because
    the SQLite reads block — FastAPI runs it in a threadpool, like /track.

    notifications_pending is the "delivery errors" signal: a failed push leaves
    its row at delivered=0, so the gauge climbs and stays up until ntfy recovers.

    The time-based metrics are emitted as absolute Unix timestamps, not as a
    pre-computed age: a scraped "seconds since X" would freeze at its last value
    while the machine sleeps, whereas a timestamp lets the query compute true
    elapsed time (`time() - <ts>`). last_check_timestamp is the dead-man's switch
    for the external GitHub-Actions cron — alert when it stops advancing, since a
    broken cron means no wake, no sweep, and silently no notifications. Both
    timestamps are omitted when there's no row yet, so the series is simply absent
    rather than a misleading zero.
    """
    with get_db() as conn:
        detected = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        delivered = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE delivered = 1"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE delivered = 0"
        ).fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM tracked_manga").fetchone()[0]
        last_check = conn.execute("SELECT MAX(last_checked_at) FROM tracked_manga").fetchone()[0]
        oldest_pending = conn.execute(
            "SELECT MIN(detected_at) FROM notifications WHERE delivered = 0"
        ).fetchone()[0]

    lines = [
        "# HELP mangotrack_chapters_detected_total New chapters detected across all sweeps.",
        "# TYPE mangotrack_chapters_detected_total counter",
        f"mangotrack_chapters_detected_total {detected}",
        "# HELP mangotrack_notifications_delivered_total Notifications pushed to ntfy successfully.",
        "# TYPE mangotrack_notifications_delivered_total counter",
        f"mangotrack_notifications_delivered_total {delivered}",
        "# HELP mangotrack_notifications_pending Notifications awaiting delivery (climbs when pushes fail).",
        "# TYPE mangotrack_notifications_pending gauge",
        f"mangotrack_notifications_pending {pending}",
        "# HELP mangotrack_tracked_manga Manga currently being tracked.",
        "# TYPE mangotrack_tracked_manga gauge",
        f"mangotrack_tracked_manga {tracked}",
    ]
    if last_check is not None:
        lines += [
            "# HELP mangotrack_last_check_timestamp_seconds Unix time of the most recent sweep.",
            "# TYPE mangotrack_last_check_timestamp_seconds gauge",
            f"mangotrack_last_check_timestamp_seconds {datetime.fromisoformat(last_check).timestamp()}",
        ]
    if oldest_pending is not None:
        lines += [
            "# HELP mangotrack_oldest_pending_notification_timestamp_seconds Unix time the oldest still-undelivered notification was detected.",
            "# TYPE mangotrack_oldest_pending_notification_timestamp_seconds gauge",
            f"mangotrack_oldest_pending_notification_timestamp_seconds {datetime.fromisoformat(oldest_pending).timestamp()}",
        ]
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


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
        log.warning(
            "mangadex request failed",
            extra={"manga_id": str(manga_id), "error": str(exc)},
        )
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
    log.info(
        "manga tracked",
        extra={"manga_id": str(manga_id), "title": title, "baseline": row["last_seen_chapter"]},
    )
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


@app.delete("/track/{manga_id}")
def untrack(manga_id: UUID) -> dict[str, str | int]:
    """Stop tracking a manga and forget it. 404 if it wasn't tracked, so a
    typo'd id surfaces instead of silently succeeding.

    Also deletes the manga's notifications — both as cleanup and to avoid a real
    bug: deliver_pending LEFT JOINs tracked_manga, so an untracked manga's
    still-pending (delivered=0) notifications would otherwise get pushed on the
    next sweep. Both deletes share one transaction (get_db commits on exit).
    """
    with get_db() as conn:
        cur = conn.execute("DELETE FROM tracked_manga WHERE manga_id = ?", (str(manga_id),))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Not tracking this manga")
        removed = conn.execute(
            "DELETE FROM notifications WHERE manga_id = ?", (str(manga_id),)
        ).rowcount
    log.info(
        "manga untracked",
        extra={"manga_id": str(manga_id), "notifications_removed": removed},
    )
    return {"manga_id": str(manga_id), "notifications_removed": removed}


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
            log.info(
                "notification delivered",
                extra={"manga_id": row["manga_id"], "chapter": row["chapter"]},
            )
        else:
            log.warning(
                "notification delivery failed",
                extra={"manga_id": row["manga_id"], "chapter": row["chapter"]},
            )
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
                log.info(
                    "new chapter detected",
                    extra={
                        "manga_id": manga_id,
                        "chapter": latest.latest_chapter,
                        "previous": target["last_seen_chapter"],
                    },
                )
            else:
                conn.execute(
                    "UPDATE tracked_manga SET last_checked_at = ? WHERE manga_id = ?",
                    (now, manga_id),
                )
    delivered = await deliver_pending()
    log.info("check complete", extra={"checked": checked, "new": new, "delivered": delivered})
    return {"checked": checked, "new": new, "delivered": delivered}
