"""Orquestador del pipeline completo.

    SCRAPING -> EXTRACCIÓN -> NORMALIZACIÓN -> IDENTIFICACIÓN
             -> MATCHING -> AGRUPACIÓN -> COMPARACIÓN
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional  # noqa: F401

from app import settings
from app.db.database import log
from app.services import alerts as alerts_service
from app.services import grouping, ingest
from app.services import notify as notify_service

# Estado en memoria de la ejecución actual (lo consulta el dashboard).
_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "current_store": None,
    "last_summary": None,
}
_lock = asyncio.Lock()


def status() -> Dict[str, Any]:
    return dict(_state)


def is_running() -> bool:
    return bool(_state["running"])


async def run_all(trigger: str = "manual", store_codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """Actualiza todas las tiendas habilitadas y reconstruye la agrupación."""
    if _lock.locked():
        return {"skipped": True, "reason": "Ya hay una actualización en curso"}

    async with _lock:
        started = time.monotonic()
        _state.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "current_store": None,
            }
        )
        try:
            ingest.sync_stores_from_config()
            stores = ingest.list_stores(only_enabled=True)
            if store_codes:
                wanted = set(store_codes)
                stores = [s for s in stores if s["code"] in wanted]

            if not stores:
                log("warn", "pipeline", "No hay tiendas habilitadas para actualizar")
                return {"stores": [], "matching": None, "duration_ms": 0}

            log("info", "pipeline", f"Actualización iniciada ({trigger}) — {len(stores)} tiendas")

            limit = asyncio.Semaphore(int(settings.get("scraping.global_concurrency", 3)))

            async def run_store(store: Dict[str, Any]) -> Dict[str, Any]:
                async with limit:
                    _state["current_store"] = store["name"]
                    try:
                        return await ingest.scrape_store(store, trigger)
                    except Exception as exc:  # noqa: BLE001
                        log("error", store["code"], f"Fallo inesperado: {exc}")
                        return {
                            "store": store["code"],
                            "store_name": store["name"],
                            "found": 0,
                            "errors": 1,
                            "status": "error",
                            "message": str(exc),
                        }

            results = await asyncio.gather(*(run_store(store) for store in stores))

            # El matching es CPU-bound y síncrono: lo sacamos del event loop.
            matching = await asyncio.to_thread(grouping.rebuild_groups)

            triggered = []
            if settings.get("alerts.enabled", True) and settings.get(
                "alerts.check_after_each_run", True
            ):
                triggered = await asyncio.to_thread(alerts_service.evaluate_alerts)

            # Y se cuenta al grupo, si está configurado. Un fallo aquí no debe
            # dar por fracasada una actualización que sí trajo los precios.
            publicado = {"sent": 0}
            try:
                publicado = await notify_service.publicar()
            except Exception as exc:  # noqa: BLE001
                log("warn", "telegram", f"No se pudo publicar: {exc}")

            # Altas, avisos y expulsiones del grupo de pago. Igual que arriba:
            # un fallo aquí no puede tumbar una actualización de precios.
            try:
                from app.services import membership

                await membership.ejecutar()
            except Exception as exc:  # noqa: BLE001
                log("warn", "membresia", f"No se pudo revisar: {exc}")

            duration_ms = int((time.monotonic() - started) * 1000)
            summary = {
                "trigger": trigger,
                "stores": results,
                "matching": matching,
                "alerts_triggered": len(triggered),
                "telegram_sent": publicado.get("sent", 0),
                "duration_ms": duration_ms,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            _state["last_summary"] = summary
            log(
                "info",
                "pipeline",
                f"Actualización completa en {duration_ms / 1000:.1f}s — "
                f"{sum(r.get('found', 0) for r in results)} productos, "
                f"{matching.get('products', 0)} productos maestros",
            )
            return summary
        finally:
            _state["running"] = False
            _state["current_store"] = None
            _state["finished_at"] = datetime.now(timezone.utc).isoformat()


async def run_store(code: str, trigger: str = "manual") -> Dict[str, Any]:
    """Actualiza una sola tienda y vuelve a agrupar."""
    store = ingest.get_store(code)
    if store is None:
        raise ValueError(f"Tienda desconocida: {code}")
    result = await ingest.scrape_store(store, trigger)
    matching = await asyncio.to_thread(grouping.rebuild_groups)
    if settings.get("alerts.enabled", True):
        await asyncio.to_thread(alerts_service.evaluate_alerts)
    return {"store": result, "matching": matching}


async def rematch_only() -> Dict[str, Any]:
    """Vuelve a agrupar con los datos ya descargados (útil al cambiar umbrales)."""
    return await asyncio.to_thread(grouping.rebuild_groups)


# ---------------------------------------------------------------------------
# Reagrupación diferida
#
# Reagrupar recorre todo el catálogo. Hacerlo en cada clic de la pantalla de
# revisión añadiría segundos a cada decisión. En vez de eso se agenda: si
# llegan más decisiones, el temporizador se reinicia y al final se reagrupa
# una sola vez. La decisión en sí ya quedó guardada, así que no se pierde
# nada si el usuario cierra la aplicación antes.
# ---------------------------------------------------------------------------
_regroup_task: Optional[asyncio.Task] = None
_regroup_lock = asyncio.Lock()
_regroup_state: Dict[str, Any] = {"pending": False, "running": False, "last_result": None}


def regroup_status() -> Dict[str, Any]:
    return dict(_regroup_state)


async def _delayed_regroup(delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # llegó otra decisión: se reagenda

    async with _regroup_lock:
        _regroup_state.update({"pending": False, "running": True})
        try:
            resultado = await asyncio.to_thread(grouping.rebuild_groups)
            _regroup_state["last_result"] = resultado
        except Exception as exc:  # noqa: BLE001
            log("error", "matching", f"Fallo al reagrupar: {exc}")
        finally:
            _regroup_state["running"] = False


def schedule_regroup(delay: float = 2.0) -> Dict[str, Any]:
    """Agenda una reagrupación, cancelando la anterior si seguía esperando."""
    global _regroup_task

    if _regroup_task is not None and not _regroup_task.done():
        _regroup_task.cancel()

    _regroup_state["pending"] = True
    try:
        _regroup_task = asyncio.get_running_loop().create_task(_delayed_regroup(delay))
    except RuntimeError:
        # Sin event loop (por ejemplo desde la CLI): reagrupamos en el momento.
        _regroup_state.update({"pending": False, "running": False})
        return {"scheduled": False, "matching": grouping.rebuild_groups()}
    return {"scheduled": True, "in_seconds": delay}
