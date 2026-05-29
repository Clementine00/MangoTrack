"""MangoTrack — minimal FastAPI app.

A starting point for learning devops. One endpoint, /health, that returns
200 OK. It exists so automated systems (load balancers, orchestrators like
Fly's proxy) can ask "is this process alive and serving HTTP?" and route
traffic accordingly. This is a *liveness* check: there are no downstream
dependencies to verify yet.
"""

from fastapi import FastAPI

app = FastAPI(title="MangoTrack")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. 200 + {"status": "ok"} means the process can serve HTTP."""
    return {"status": "ok"}
