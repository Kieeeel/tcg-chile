"""Aplicación FastAPI local de TCG Chile.

Arranca en http://127.0.0.1:8000 y no envía datos a ningún servicio externo:
las únicas conexiones salientes son las de scraping hacia las tiendas que tú
configures.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import settings
from app.api import matching as matching_routes
from app.api import products as products_routes
from app.api import stores as stores_routes
from app.api import system as system_routes
from app.db.database import init_db, log, transaction
from app.services import ingest, pipeline, scheduler

WEB_DIR = Path(__file__).resolve().parent / "web"


def seed_catalogs() -> None:
    """Vuelca games.yaml y config/sets/*.yaml a la base de datos."""
    with transaction() as conn:
        for game in settings.load_games():
            conn.execute(
                """INSERT INTO games (code, name) VALUES (?, ?)
                   ON CONFLICT(code) DO UPDATE SET name = excluded.name""",
                (game["code"], game.get("name", game["code"])),
            )
        for game_code, catalog in settings.load_sets().items():
            for entry in catalog.get("sets", []):
                conn.execute(
                    """INSERT INTO sets (game, code, name, released) VALUES (?, ?, ?, ?)
                       ON CONFLICT(game, code) DO UPDATE SET
                           name = excluded.name, released = excluded.released""",
                    (game_code, entry["code"], entry["name"], entry.get("released")),
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.invalidate()
    seed_catalogs()
    stores = ingest.sync_stores_from_config()
    log("info", "app", f"TCG Chile iniciado — {stores} tiendas configuradas")

    scheduler.start()
    if settings.get("scheduler.run_on_startup", False):
        asyncio.create_task(pipeline.run_all(trigger="startup"))

    try:
        yield
    finally:
        scheduler.shutdown()
        log("info", "app", "TCG Chile detenido")


app = FastAPI(
    title="TCG Chile",
    description="Comparador local de precios de productos TCG. Sin IA, 100% algorítmico.",
    version="1.0.0",
    lifespan=lifespan,
)

# Solo para permitir un frontend servido en otro puerto local (ej. Vite en 5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(products_routes.router)
app.include_router(stores_routes.router)
app.include_router(matching_routes.router)
app.include_router(system_routes.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "ai_used": False, "local_only": True}


# La interfaz web se sirve desde la misma aplicación.
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        """Sirve la interfaz sellando los recursos con su fecha de cambio.

        El navegador cachea los módulos con mucha insistencia: sin este sello,
        editar app.js y recargar seguía ejecutando la versión anterior. Con
        `?v=<mtime>` la URL cambia sola en cuanto se toca el archivo.
        """
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        for recurso in ("app.js", "styles.css"):
            ruta = WEB_DIR / "assets" / recurso
            sello = int(ruta.stat().st_mtime) if ruta.exists() else 0
            html = html.replace(f"/assets/{recurso}", f"/assets/{recurso}?v={sello}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
