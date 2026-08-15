"""Endpoints de sistema: dashboard, actualización, configuración, alertas, logs."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app import settings
from app.core import attributes as attrs_module
from app.core import normalize as normalize_module
from app.services import alerts as alerts_service
from app.services import notify as notify_service
from app.services import pipeline, queries, scheduler

router = APIRouter(prefix="/api", tags=["sistema"])


# ---------------------------------------------------------------------------
# Dashboard y estado
# ---------------------------------------------------------------------------
@router.get("/dashboard")
def dashboard():
    data = queries.dashboard()
    data["scheduler"] = scheduler.info()
    data["pipeline"] = pipeline.status()
    return data


@router.get("/home")
def home(game: Optional[str] = None, per_section: int = 12):
    """Secciones de la portada, ya filtradas por el TCG elegido."""
    return queries.home(game=game, per_section=per_section)


@router.get("/opportunities")
def opportunities(
    limit: int = 24,
    sort: str = Query("amount", pattern="^(amount|percent|unit)$"),
):
    """Mejores oportunidades: ahorro frente al precio mediano del mercado."""
    return queries.opportunities(limit=limit, sort=sort)


@router.get("/status")
def status():
    from app.db.database import query_one

    # `pipeline.status()` vive en memoria y se pierde al reiniciar: la última
    # actualización real se lee de la base de datos.
    row = query_one(
        "SELECT MAX(finished_at) AS last FROM scrape_runs WHERE status != 'running'"
    )
    return {
        "pipeline": pipeline.status(),
        "scheduler": scheduler.info(),
        "regroup": pipeline.regroup_status(),
        "last_update": row["last"] if row else None,
    }


@router.post("/update")
async def update_now(store_codes: Optional[List[str]] = Body(None, embed=True)):
    if pipeline.is_running():
        raise HTTPException(status_code=409, detail="Ya hay una actualización en curso")
    asyncio.create_task(pipeline.run_all(trigger="manual", store_codes=store_codes))
    return {"started": True}


@router.get("/events")
def events(limit: int = 50, type: Optional[str] = None):
    return queries.events(limit, type)


@router.get("/logs")
def logs(limit: int = 200, level: Optional[str] = None):
    return queries.logs(limit, level)


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
@router.get("/config")
def get_config():
    return {
        "settings": settings.load_settings(),
        "games": settings.load_games(),
        "product_types": attrs_module.all_types(),
        "sets": attrs_module.all_sets(),
    }


@router.put("/config")
def update_config(changes: Dict[str, Any] = Body(...)):
    """Guarda overrides con rutas separadas por puntos.

    Ejemplo: {"matching.auto_threshold": 92, "scheduler.interval_hours": 3}
    """
    applied = {}
    for key, value in changes.items():
        if not isinstance(key, str) or not key:
            continue
        settings.save_override(key, value)
        applied[key] = value

    if "scheduler.interval_hours" in applied:
        try:
            scheduler.reschedule(int(applied["scheduler.interval_hours"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "scheduler.enabled" in applied:
        scheduler.set_enabled(bool(applied["scheduler.enabled"]))

    return {"applied": applied, "settings": settings.load_settings(force=True)}


@router.get("/normalization")
def get_normalization():
    return settings.load_normalization()


@router.put("/normalization")
def update_normalization(data: Dict[str, Any] = Body(...)):
    """Guarda el diccionario de equivalencias/stopwords y recarga el normalizador."""
    for key in ("equivalences", "stopwords", "protected_tokens"):
        if key not in data:
            raise HTTPException(status_code=400, detail=f"Falta la clave '{key}'")
    settings.write_normalization(data)
    normalize_module.invalidate()
    attrs_module.invalidate()
    return {"saved": True, "hint": "Ejecuta /api/rematch para reagrupar con el nuevo diccionario"}


@router.post("/normalization/preview")
def preview_normalization(name: str = Body(..., embed=True)):
    """Muestra cómo queda un nombre tras normalizar y qué atributos se extraen."""
    normalized = normalize_module.normalize_name(name)
    extracted = attrs_module.extract(normalized)
    return {
        "raw": name,
        "basic": normalized.basic,
        "canonical": normalized.canonical,
        "tokens": normalized.tokens,
        "core_tokens": normalized.core_tokens,
        "name_key": normalized.name_key,
        "attributes": extracted.to_dict(),
    }


# ---------------------------------------------------------------------------
# Programación
# ---------------------------------------------------------------------------
@router.get("/scheduler")
def scheduler_info():
    return scheduler.info()


@router.post("/scheduler/interval")
def set_interval(hours: int = Body(..., embed=True)):
    try:
        return scheduler.reschedule(hours)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scheduler/enabled")
def set_scheduler_enabled(enabled: bool = Body(..., embed=True)):
    return scheduler.set_enabled(enabled)


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------
@router.get("/alerts")
def list_alerts():
    return alerts_service.list_alerts()


@router.post("/alerts")
def create_alert(
    product_id: int = Body(...),
    target_price: float = Body(...),
    only_in_stock: bool = Body(True),
):
    alert_id = alerts_service.create_alert(product_id, target_price, only_in_stock)
    alerts_service.evaluate_alerts()
    return {"id": alert_id}


@router.delete("/alerts/{alert_id}")
def delete_alert(alert_id: int):
    if not alerts_service.delete_alert(alert_id):
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    return {"deleted": True}


@router.post("/alerts/{alert_id}/active")
def toggle_alert(alert_id: int, active: bool = Body(..., embed=True)):
    if not alerts_service.set_active(alert_id, active):
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    return {"id": alert_id, "active": active}


@router.get("/alerts/hits")
def alert_hits(limit: int = 50):
    return alerts_service.pending_hits(limit)


@router.post("/alerts/hits/seen")
def mark_seen(hit_ids: Optional[List[int]] = Body(None, embed=True)):
    return {"updated": alerts_service.mark_hits_seen(hit_ids)}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
@router.get("/telegram")
def telegram_status():
    """Cómo está configurada la publicación, sin revelar el token."""
    return notify_service.estado()


@router.get("/telegram/preview")
def telegram_preview():
    """El mensaje que se publicaría ahora mismo, sin enviarlo."""
    eventos = notify_service.eventos_pendientes()
    return {
        "events": len(eventos),
        "message": notify_service.componer(eventos) if eventos else None,
    }


@router.post("/telegram/publish")
async def telegram_publish(force: bool = False):
    """Publica ahora. Con `force=true` envía aunque esté en simulación."""
    try:
        return await notify_service.publicar(forzar_envio=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
