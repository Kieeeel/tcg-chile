"""Avisa al administrador por privado cuando algo se rompe.

Corriendo desatendido en GitHub Actions, un fallo es silencioso: las tiendas
dejan de entrar, los precios se quedan viejos, y el grupo sigue pareciendo
vivo porque el bot publica oportunidades igualmente. Se puede tardar días en
notarlo.

Esto lo cuenta en cuanto pasa, por chat privado. Nunca al grupo: a los socios
no les interesa que un scraper devuelva 403.

Lo que vigila:
  · Tiendas que normalmente funcionan y hoy devuelven cero.
  · Que la agrupación no se haya desplomado de golpe.
  · Que la actualización entera no haya reventado.

Las tiendas que ya se sabe que fallan —las de Cloudflare— se listan en
`salud.ignorar` y no cuentan: si no, avisaría en cada pasada y acabarías
callándolo, que es como se pierden los avisos que sí importan.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app import settings
from app.db.database import log

CLAVE_ULTIMO = "salud.ultimo_aviso"
CLAVE_OFERTAS = "salud.ultimas_ofertas"


def config() -> Dict[str, Any]:
    return dict(settings.get("salud", {}) or {})


def _ignoradas() -> set:
    return {str(c).strip() for c in (config().get("ignorar") or [])}


def revisar(resumen: Dict[str, Any]) -> List[str]:
    """Devuelve los problemas encontrados, en texto llano."""
    cfg = config()
    problemas: List[str] = []
    tiendas = resumen.get("stores") or []
    ignorar = _ignoradas()

    caidas = [
        t for t in tiendas
        if t.get("store") not in ignorar and not t.get("found")
    ]
    if caidas:
        nombres = ", ".join(t.get("store_name") or t.get("store") for t in caidas)
        problemas.append(
            f"<b>{len(caidas)} tienda(s) sin productos:</b> {nombres}"
        )

    con_errores = [
        t for t in tiendas
        if t.get("store") not in ignorar and t.get("errors") and t.get("found")
    ]
    if len(con_errores) >= int(cfg.get("min_tiendas_con_errores", 4) or 4):
        problemas.append(
            f"<b>{len(con_errores)} tiendas con errores</b> aunque trajeron datos"
        )

    # Un desplome de productos suele significar que algo se rompió aguas
    # arriba, no que las tiendas se hayan quedado vacías de verdad.
    matching = resumen.get("matching") or {}
    ofertas = matching.get("offers")
    if ofertas is not None:
        caida = _caida_de_ofertas(ofertas, float(cfg.get("caida_max_pct", 25) or 25))
        if caida:
            problemas.append(caida)

    return problemas


def _caida_de_ofertas(ahora: int, tope_pct: float) -> Optional[str]:
    """Compara con el total de la pasada anterior.

    Se guarda el último valor en vez de calcularlo de `scrape_runs`: ahí hay
    una fila por tienda y por pasada, y sumarlas daba el acumulado del día
    entero, no un total comparable.

    El nuevo valor se guarda siempre, también cuando hay caída. Así una bajada
    real —quitas tiendas, una cierra— avisa una vez y deja de dar la lata.
    """
    anterior = settings.get(CLAVE_OFERTAS)
    settings.save_override(CLAVE_OFERTAS, int(ahora))

    if not anterior or int(anterior) <= 0:
        return None  # primera pasada: no hay con qué comparar
    perdido = (int(anterior) - ahora) / int(anterior) * 100
    if perdido < tope_pct:
        return None
    return (
        f"<b>Caída de catálogo:</b> {ahora} ofertas frente a {int(anterior)} "
        f"de la pasada anterior ({perdido:.0f}% menos)"
    )


def _firma(problemas: List[str]) -> str:
    """Para no repetir el mismo aviso cada pasada."""
    return "|".join(sorted(problemas))[:400]


def _toca_avisar(problemas: List[str]) -> bool:
    """Solo si el problema cambió, o si ya pasaron las horas de descanso."""
    cfg = config()
    horas = float(cfg.get("repetir_cada_horas", 12) or 0)
    anterior = settings.get(CLAVE_ULTIMO) or {}
    if not isinstance(anterior, dict):
        return True

    if anterior.get("firma") != _firma(problemas):
        return True
    if horas <= 0:
        return False
    try:
        cuando = datetime.fromisoformat(str(anterior.get("cuando"))).replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - cuando).total_seconds() / 3600 >= horas


async def avisar(resumen: Dict[str, Any]) -> Dict[str, Any]:
    """Revisa y, si hay algo que contar, lo manda por privado."""
    cfg = config()
    if not cfg.get("enabled", True):
        return {"avisado": False}

    problemas = revisar(resumen)
    if not problemas:
        # Si venimos de un aviso, conviene decir que ya está arreglado.
        anterior = settings.get(CLAVE_ULTIMO) or {}
        if isinstance(anterior, dict) and anterior.get("firma"):
            settings.save_override(CLAVE_ULTIMO, {})
            await _mandar("✅ <b>Todo vuelve a la normalidad</b>\n\n"
                          "Las tiendas que fallaban han vuelto a entrar.")
            return {"avisado": True, "recuperado": True}
        return {"avisado": False}

    if not _toca_avisar(problemas):
        return {"avisado": False, "problemas": len(problemas)}

    total = sum(t.get("found", 0) for t in (resumen.get("stores") or []))
    texto = (
        "⚠️ <b>Algo va mal en la actualización</b>\n\n"
        + "\n".join(f"· {p}" for p in problemas)
        + f"\n\nTotal recogido: {total} productos."
    )
    if await _mandar(texto):
        settings.save_override(CLAVE_ULTIMO, {
            "firma": _firma(problemas),
            "cuando": datetime.now(timezone.utc).isoformat(),
        })
    return {"avisado": True, "problemas": len(problemas)}


async def avisar_fallo(mensaje: str) -> None:
    """Para cuando la actualización revienta entera y no hay resumen."""
    if not config().get("enabled", True):
        return
    await _mandar(
        "🛑 <b>La actualización falló por completo</b>\n\n"
        f"<code>{mensaje[:600]}</code>"
    )


async def _mandar(texto: str) -> bool:
    """Al administrador, siempre en privado. Nunca al grupo."""
    from app.services import membership, notify

    destino = membership.admin_id()
    if not destino or not notify.token():
        log("warn", "salud", "No se puede avisar: falta TELEGRAM_ADMIN_ID o el token")
        return False
    try:
        return await membership._privado(destino, texto)
    except Exception as exc:  # noqa: BLE001
        log("warn", "salud", f"No se pudo avisar al administrador: {exc}")
        return False
