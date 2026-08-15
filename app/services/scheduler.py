"""Actualización automática programada (APScheduler, todo local)."""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import settings
from app.db.database import log, query_one
from app.services import pipeline

JOB_ID = "tcg_update"
ALLOWED_INTERVALS = (1, 2, 3, 4, 6, 12, 24)

_scheduler: Optional[AsyncIOScheduler] = None


def _interval_hours() -> int:
    value = int(settings.get("scheduler.interval_hours", 6) or 6)
    if value not in ALLOWED_INTERVALS:
        # Nos quedamos con el intervalo permitido más cercano.
        value = min(ALLOWED_INTERVALS, key=lambda option: abs(option - value))
    return value


def last_update() -> Optional[datetime]:
    """Última actualización realmente terminada, leída de la base de datos.

    El planificador vive en memoria y muere con el proceso; la base es lo único
    que recuerda cuándo se actualizó de verdad.
    """
    row = query_one(
        "SELECT MAX(finished_at) AS last FROM scrape_runs WHERE status != 'running'"
    )
    valor = row["last"] if row else None
    if not valor:
        return None
    try:
        # Las marcas de tiempo se guardan como UTC sin zona ('YYYY-MM-DD HH:MM:SS').
        return datetime.fromisoformat(str(valor)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _next_fire(hours: int) -> Tuple[datetime, bool]:
    """Calcula la próxima ejecución a partir de la última actualización real.

    Sin esto, `IntervalTrigger` cuenta desde el arranque del proceso: cada
    reinicio devolvía el reloj a cero y la actualización se posponía otras
    `hours` horas, por muy vencida que estuviera.

    Devuelve (momento, vencida).
    """
    ahora = datetime.now(timezone.utc)
    margen = timedelta(seconds=max(0, int(settings.get("scheduler.catch_up_delay_seconds", 60) or 0)))
    anterior = last_update()

    if anterior is None:
        # Base recién creada: conviene poblarla cuanto antes.
        return ahora + margen, True

    vencimiento = anterior + timedelta(hours=hours)
    if vencimiento <= ahora:
        if settings.get("scheduler.catch_up", True):
            return ahora + margen, True
        # Sin recuperación, se espera al siguiente hueco de la rejilla.
        saltos = int((ahora - anterior).total_seconds() // (hours * 3600)) + 1
        return anterior + timedelta(hours=hours * saltos), True
    return vencimiento, False


async def _job() -> None:
    jitter = int(settings.get("scheduler.jitter_seconds", 0) or 0)
    if jitter:
        import asyncio

        await asyncio.sleep(random.uniform(0, jitter))
    await pipeline.run_all(trigger="scheduled")


def start() -> Optional[AsyncIOScheduler]:
    global _scheduler
    # En un despliegue sin proceso permanente (Vercel) el planificador no
    # tiene sentido: quien dispara la actualización es el cron de GitHub.
    if os.environ.get("TCG_DISABLE_SCHEDULER"):
        log("info", "scheduler", "Planificador apagado por entorno (lo dispara el cron externo)")
        return None
    if not settings.get("scheduler.enabled", True):
        log("info", "scheduler", "Actualización automática desactivada por configuración")
        return None

    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")

    hours = _interval_hours()
    momento, vencida = _next_fire(hours)
    _scheduler.add_job(
        _job,
        trigger=IntervalTrigger(hours=hours, start_date=momento),
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if not _scheduler.running:
        _scheduler.start()

    if vencida:
        log("info", "scheduler",
            f"Actualización automática cada {hours} h — vencida, se recupera "
            f"a las {momento.astimezone().strftime('%H:%M')}")
    else:
        log("info", "scheduler",
            f"Actualización automática cada {hours} h — próxima a las "
            f"{momento.astimezone().strftime('%H:%M')}")
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def reschedule(hours: int) -> Dict[str, Any]:
    if hours not in ALLOWED_INTERVALS:
        raise ValueError(f"Intervalo no permitido. Usa uno de: {ALLOWED_INTERVALS}")
    settings.save_override("scheduler.interval_hours", hours)
    if _scheduler is not None and _scheduler.get_job(JOB_ID):
        momento, _ = _next_fire(hours)
        _scheduler.reschedule_job(
            JOB_ID, trigger=IntervalTrigger(hours=hours, start_date=momento)
        )
    else:
        start()
    return info()


def set_enabled(enabled: bool) -> Dict[str, Any]:
    settings.save_override("scheduler.enabled", bool(enabled))
    if enabled:
        start()
    else:
        shutdown()
    return info()


def info() -> Dict[str, Any]:
    job = _scheduler.get_job(JOB_ID) if _scheduler else None
    next_run = getattr(job, "next_run_time", None)
    ahora = datetime.now(timezone.utc)
    seconds_left = None
    if next_run is not None:
        seconds_left = max(0, int((next_run - ahora).total_seconds()))

    hours = _interval_hours()
    anterior = last_update()
    atrasada = anterior is not None and (ahora - anterior) > timedelta(hours=hours)
    return {
        "enabled": bool(settings.get("scheduler.enabled", True)),
        "interval_hours": hours,
        "allowed_intervals": list(ALLOWED_INTERVALS),
        "next_run_at": next_run.isoformat() if next_run else None,
        "seconds_until_next_run": seconds_left,
        "running_now": pipeline.is_running(),
        "last_update_at": anterior.isoformat() if anterior else None,
        "overdue": bool(atrasada),
        "catch_up": bool(settings.get("scheduler.catch_up", True)),
    }
