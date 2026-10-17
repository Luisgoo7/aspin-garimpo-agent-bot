from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from .config import settings
from . import storage

app = FastAPI(title="ASPIN Garimpo Judicial", version="0.3.0")


@app.get("/health")
def health():
    if settings.configuration_errors():
        return JSONResponse(status_code=503, content={"status": "configuration_missing", "service": "aspin-garimpo"})
    try:
        state = storage.get_state("last_health")
        at = datetime.fromisoformat(state.get("at", ""))
        stale = (datetime.now(timezone.utc) - at).total_seconds() > settings.poll_interval_minutes * 180
        outcome = state.get("outcome", "not_run")
        ok = not stale and outcome in ("success", "partial")
        return JSONResponse(status_code=200 if ok else 503, content={
            "status": "stale" if stale else outcome, "service": "aspin-garimpo", "last_run": at.isoformat(),
        })
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "service": "aspin-garimpo"})


@app.get("/")
def root():
    return {"name": "ASPIN Garimpo Judicial", "version": "0.3.0", "health": "/health"}
