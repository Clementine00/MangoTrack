# MangoTrack 🥭

A small FastAPI service that tracks manga chapters via the [MangaDex API](https://api.mangadex.org).
It's a hands-on project for learning devops, growing toward a real product: a manga
tracker that notifies you when new chapters drop.

**Live:** https://mangotrack.fly.dev

## Endpoints

| Endpoint   | What it does |
|------------|--------------|
| `/health`  | Liveness probe — `200 {"status": "ok"}` if the process is serving HTTP. |
| `/version` | Reports the running release: `{version, commit, url}` (the `url` links to the exact commit on GitHub). |
| `/latest`  | Returns the latest English chapter for the tracked manga, fetched live from MangaDex. |
| `/docs`    | Auto-generated interactive API docs (Swagger UI). |

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
