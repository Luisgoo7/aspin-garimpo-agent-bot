from fastapi import FastAPI
from .config import settings

app = FastAPI(title="ASPIN Garimpo Judicial", version="0.2.0")

@app.get("/health")
def health():
    return {"status": "ok", "service": "aspin-garimpo", "environment": settings.app_env}

@app.get("/")
def root():
    return {"name": "ASPIN Garimpo Judicial", "status": "prototype"}
