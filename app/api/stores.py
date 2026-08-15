"""Endpoints de gestión de tiendas."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app.db.database import transaction
from app.scrapers import registry
from app.services import grouping, ingest, pipeline, queries

router = APIRouter(prefix="/api/stores", tags=["tiendas"])


@router.get("")
def list_stores():
    return queries.store_overview()


@router.post("/reload")
def reload_from_config():
    """Vuelve a leer config/stores/*.yaml (para añadir tiendas sin reiniciar)."""
    from app import settings

    settings.invalidate()
    count = ingest.sync_stores_from_config()
    return {"stores": count, "detail": queries.store_overview()}


@router.get("/adapters")
def list_adapters():
    return registry.available()


@router.post("/{store_id}/toggle")
def toggle_store(store_id: int, enabled: bool):
    with transaction() as conn:
        changed = conn.execute(
            "UPDATE stores SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if enabled else 0, store_id),
        ).rowcount
    if not changed:
        raise HTTPException(status_code=404, detail="Tienda no encontrada")
    return {"id": store_id, "enabled": enabled}


@router.post("/{store_id}/scrape")
async def scrape_store(store_id: int):
    store = ingest.get_store(store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Tienda no encontrada")
    if pipeline.is_running():
        raise HTTPException(status_code=409, detail="Ya hay una actualización en curso")

    asyncio.create_task(pipeline.run_store(store["code"], trigger="manual"))
    return {"started": True, "store": store["code"], "name": store["name"]}


@router.get("/{store_id}/errors")
def store_errors(store_id: int, limit: int = 50):
    return queries.store_errors(store_id, limit)


@router.delete("/{store_id}")
def delete_store(store_id: int):
    """Elimina una tienda y todo lo que cuelga de ella.

    Borrar el YAML de `config/stores/` no basta: los productos, el historial
    de precios y las ejecuciones siguen en la base de datos. Esto los borra
    en cascada y vuelve a agrupar para que los productos maestros que se
    queden sin ofertas desaparezcan.

    Es irreversible: se pierde el historial de precios de esa tienda.
    """
    store = ingest.get_store(store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Tienda no encontrada")

    with transaction() as conn:
        offers = conn.execute(
            "SELECT COUNT(*) AS n FROM store_products WHERE store_id = ?", (store_id,)
        ).fetchone()["n"]
        # `events` guarda store_id sin clave foránea: hay que limpiarlo aparte.
        conn.execute("DELETE FROM events WHERE store_id = ?", (store_id,))
        conn.execute("DELETE FROM stores WHERE id = ?", (store_id,))

    matching = grouping.rebuild_groups()
    return {
        "deleted": store["code"],
        "offers_removed": offers,
        "matching": matching,
    }
