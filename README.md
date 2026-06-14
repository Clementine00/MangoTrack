# MangoTrack 🥭

A small FastAPI service that tracks manga chapters via the [MangaDex API](https://api.mangadex.org)
and notifies you (via [ntfy](https://ntfy.sh)) when new chapters drop. It's a hands-on
project for learning devops.

**Live:** https://mangotrack.fly.dev

## Endpoints

| Endpoint   | What it does |
|------------|--------------|
| `GET /health`  | Liveness probe — `200 {"status": "ok"}` if the process is serving HTTP. |
| `GET /ready`   | Readiness probe — `200` if the DB is reachable and the schema is present, else `503` (alive but not ready to serve). |
| `GET /version` | Reports the running release: `{version, commit, url}` (the `url` links to the exact commit on GitHub). |
| `GET /latest`  | Latest English chapter for the default tracked manga (Hitoner), fetched live from MangaDex. |
| `GET /latest/{manga_id}` | Latest English chapter for any title, by MangaDex UUID. A malformed id returns `422` before any upstream call. |
| `POST /track/{manga_id}` | Start tracking a manga. Baselines it at the current latest chapter (so you're only notified about *future* releases) and fetches its title from MangaDex. Idempotent. |
| `GET /track`   | List everything currently tracked. |
| `DELETE /track/{manga_id}` | Stop tracking a manga and forget its pending notifications. `404` if it wasn't tracked. |
| `POST /check`  | Sweep every tracked manga for new chapters, queue a notification per new chapter, and deliver pending ones. Secret-gated (`Authorization: Bearer <token>`), fail-closed. See below. |
| `GET /docs`    | Auto-generated interactive API docs (Swagger UI). |

## Notifications

When `/check` finds a chapter newer than a manga's stored baseline, it records a
notification and pushes it to your phone via [ntfy](https://ntfy.sh) — title,
chapter, and a tap-through link to MangaDex.

- **Scheduled, not manual.** A GitHub Actions cron (`.github/workflows/check.yml`)
  hits `POST /check` every 2 hours, which also wakes the scaled-to-zero machine.
  The workflow has a manual `workflow_dispatch` trigger for on-demand sweeps.
- **Decoupled detect/deliver.** Detection queues notifications; delivery drains
  the queue and only marks a row delivered on a *successful* push, so a failed
  send simply retries on the next sweep.
- **Fail-closed config.** `CHECK_TOKEN` guards `/check`; `NTFY_TOPIC` is the ntfy
  destination. Both are Fly secrets — unset means the feature does nothing rather
  than misfiring, so local/test runs never push.

## Error tracking

Unhandled exceptions are reported to [Sentry](https://sentry.io) with full stack
traces and the request that triggered them, so a crash alerts you instead of
scrolling past in the logs.

- **Auto-captured.** The Sentry FastAPI integration hooks the app, so genuine
  bugs (the real `500`s) are reported without instrumenting any endpoint.
- **Deliberate errors are filtered.** The `HTTPException`s we raise on purpose —
  `404` for an unknown id, `502` when MangaDex is down, `401` on a bad token — are
  handled control flow, not bugs. A `before_send` hook drops them so they never page.
- **Tagged for triage.** Every event carries its `environment` (`production` vs
  `development`) and `release` (the git commit), pinning an error to the exact
  deployed code.
- **Fail-closed config.** `SENTRY_DSN` is a Fly secret; unset (local/test) means
  the SDK no-ops and ships nothing.

## Local development

Uses a stdlib virtual environment (`.venv/`, gitignored). Python 3.13.

```bash
# One-time setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # app + test deps

# Run with auto-reload
uvicorn main:app --reload             # serves on http://127.0.0.1:8000
```

> Note: dependencies are fully pinned (`==`) because the production image builds
> from scratch. Runtime deps live in `requirements.txt`; test-only deps in
> `requirements-dev.txt` (which pulls in the runtime deps via `-r`).

## Testing

```bash
.venv/bin/pytest -v
```

Tests use FastAPI's `TestClient` and mock the MangaDex call with `respx`, so they're
fast, deterministic, and never hit the network.

> If `pytest` resolves to the wrong interpreter (e.g. Anaconda), use the explicit
> `.venv/bin/pytest` or `.venv/bin/python -m pytest`.

## Deployment & CI/CD

Hosted on [Fly.io](https://fly.io) (app `mangotrack`, region `sjc`), built from the
`Dockerfile` on Fly's remote builders.

CI/CD runs via GitHub Actions (`.github/workflows/ci.yml`):

- **Every push / PR** → the `test` job runs the suite on a clean runner.
- **Push to `main`** → after tests pass, the `deploy` job ships to Fly automatically.

Trunk-based: do feature work on a branch (CI tests it, no deploy), then merge to
`main` to release. `main` always reflects what's live.

## Releasing

Deploys happen automatically — the only manual act is *naming* a release with a tag.

- **Merge to `main`** → deploys. `/version` shows `vX.Y.Z-N-gSHA`
  (i.e. _N commits past the last release_).
- **Push a version tag** → deploys a clean, named release.

To cut a release:

```bash
git checkout main && git pull
git tag vX.Y.Z          # patch = Z (fixes), minor = Y (features), major = X (breaking)
git push origin vX.Y.Z
```

Then verify what's live:

```bash
curl https://mangotrack.fly.dev/version
```

## Tech stack

FastAPI · Uvicorn · httpx · pytest · respx · Docker · Fly.io · GitHub Actions


## Dev notes
Problem: Altering table in live prod DB causes issues. ALTER TABLE must only run once per update. I could just manually update DB as there are no clients but this isn't a solution when there is. Solution: user_version. Using user_version as a counter, I can update the counter when specific schema changes runs for the first time. Then using the updated counter, I can ensure that the query never runs again causing the conflict. Rule: Never edit, only append.
