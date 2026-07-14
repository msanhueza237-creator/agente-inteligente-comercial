from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import get_settings
from app.db.base import engine
from app.web import routes_dedup, routes_import, routes_monitor, routes_prospects, routes_search

settings = get_settings()

app = FastAPI(title="Clima Activa - Agente de Inteligencia Comercial")
if settings.env in {"development", "test", "testing"}:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

app.mount(
    "/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static"
)

app.include_router(routes_prospects.router)
app.include_router(routes_import.router)
app.include_router(routes_dedup.router)
app.include_router(routes_search.router)
app.include_router(routes_monitor.router)


@app.get("/health")
async def health() -> dict:
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "env": settings.env,
        "database": "ok" if db_ok else "unreachable",
    }
