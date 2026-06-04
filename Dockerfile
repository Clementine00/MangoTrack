# Recipe for the container image Fly runs. Built from scratch on Fly's
# builders, so the ONLY source of truth for dependencies is requirements.txt.

# Start from a minimal official Python image matching the local 3.13.
FROM python:3.13-slim

# Work inside /app in the container.
WORKDIR /app

# Install deps FIRST, as their own cached layer. Editing main.py later won't
# re-run pip. The pinned versions in requirements.txt make this build
# reproducible: the exact fastapi/uvicorn/httpx tested locally get installed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the application code.
COPY . .

# Stamp the image with the git commit it was built from, so /version can
# report exactly what's running. Defaults to "dev"; the pipeline overrides it
# with --build-arg GIT_SHA=<commit>. Placed late on purpose — changing the SHA
# only rebuilds these tiny layers, never the cached pip-install above.
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA

# Human-readable release version, derived from git tags by the pipeline
# (e.g. v0.2.0, or v0.1.0-3-g60c2eeb between tags). Same ARG->ENV pattern;
# defaults to "dev" for local builds.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

# Document the port the app listens on (must match fly.toml internal_port).
EXPOSE 8080

# Production run command. Two deliberate differences from local dev:
#   --host 0.0.0.0  — inside a container 127.0.0.1 is unreachable from Fly's
#                     proxy; 0.0.0.0 accepts traffic on all interfaces.
#   no --reload     — production code doesn't change under us.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
