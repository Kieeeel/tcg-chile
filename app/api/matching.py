"""Endpoints de revisión manual, separación y re-agrupación."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException

from app.services import grouping, pipeline, queries

router = APIRouter(prefix="/api", tags=["matching"])


@router.get("/reviews")
def pending_reviews(limit: int = 50):
    """Pares 'posible match' que esperan confirmación del usuario."""
    return queries.pending_reviews(limit)


@router.post("/reviews/confirm")
async def confirm_review(a_id: int = Body(...), b_id: int = Body(...)):
    """Marca dos ofertas como el mismo producto.

    Responde en cuanto la decisión está guardada; la reagrupación se agenda
    para dos segundos después de la última decisión, de modo que revisar
    varios pares seguidos no cueste una reagrupación por clic.
    """
    try:
        result = grouping.decide_pair(a_id, b_id, "same")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["regroup"] = pipeline.schedule_regroup()
    return result


@router.post("/reviews/reject")
async def reject_review(a_id: int = Body(...), b_id: int = Body(...)):
    try:
        result = grouping.decide_pair(a_id, b_id, "different")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["regroup"] = pipeline.schedule_regroup()
    return result


@router.delete("/manual-decisions/{decision_id}")
async def undo_decision(decision_id: int):
    """Deshace una decisión manual: el par vuelve a la cola de revisión."""
    try:
        result = grouping.forget_decision_by_id(decision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result["regroup"] = pipeline.schedule_regroup()
    return result


@router.post("/offers/{store_product_id}/split")
async def split_offer(store_product_id: int):
    """Saca una oferta de su producto maestro y recuerda la decisión."""
    try:
        result = grouping.split_offer(store_product_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/offers/{store_product_id}/language")
async def set_offer_language(
    store_product_id: int, language: Optional[str] = Body(None, embed=True)
):
    """Fija a mano el idioma de una oferta (o lo devuelve a automático).

    Útil cuando la tienda no lo escribe en el nombre: al declararlo, el
    producto deja de poder agruparse con el de otro idioma y desaparecen
    los pares dudosos que generaba.
    """
    try:
        grouping.set_manual_attribute(store_product_id, "language", language, regroup=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Devolvemos el idioma EFECTIVO, no el pedido: al elegir "Automático" el
    # valor vuelve a deducirse del nombre y puede no ser nulo. Así la interfaz
    # muestra lo que de verdad quedó guardado.
    from app.db.database import query_one

    row = query_one("SELECT language FROM store_products WHERE id = ?", (store_product_id,))
    return {
        "id": store_product_id,
        "language": row["language"] if row else None,
        "requested": language,
        "regroup": pipeline.schedule_regroup(),
    }


@router.get("/offers")
def list_offers(
    q: str = "",
    store_id: Optional[int] = None,
    language: Optional[str] = None,
    edited_only: bool = False,
    page: int = 1,
    page_size: int = 30,
):
    """Ofertas guardadas, para corregirlas a mano desde Administración."""
    return queries.admin_offers(
        q,
        store_id=store_id,
        language=language,
        edited_only=edited_only,
        page=page,
        page_size=page_size,
    )


@router.post("/offers/{store_product_id}/attribute")
async def set_offer_attribute(
    store_product_id: int,
    attribute: str = Body(...),
    value: Optional[str] = Body(None),
):
    """Corrige un atributo de una oferta: idioma, set, tipo o enlace.

    `value` vacío borra la corrección y devuelve el dato a la detección
    automática.
    """
    try:
        grouping.set_manual_attribute(
            store_product_id, attribute, value or None, regroup=False
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    respuesta: dict = {"id": store_product_id, "attribute": attribute}
    # El enlace no cambia ninguna agrupación; el resto sí.
    if attribute != "url":
        respuesta["regroup"] = pipeline.schedule_regroup()
    return respuesta


@router.post("/offers/{store_product_id}/rejoin")
async def rejoin_offer(store_product_id: int):
    """Deshace las separaciones manuales de una oferta."""
    try:
        return grouping.rejoin_offer(store_product_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/manual-attributes")
def manual_attributes(limit: int = 200):
    return grouping.manual_attributes(limit)


@router.get("/languages")
def languages():
    """Idiomas disponibles para corregir, tal como están en el diccionario."""
    from app import settings

    return [
        {"code": entry["code"], "name": entry.get("name", entry["code"])}
        for entry in (settings.load_normalization().get("languages", []) or [])
        if isinstance(entry, dict) and entry.get("code")
    ]


@router.get("/manual-decisions")
def manual_decisions(limit: int = 200):
    return queries.manual_decisions(limit)


@router.post("/manual-decisions/forget")
async def forget_decision(a_id: int = Body(...), b_id: int = Body(...)):
    removed = grouping.forget_manual_decision(a_id, b_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No existe esa decisión manual")
    return {"forgotten": True, "matching": grouping.rebuild_groups()}


@router.post("/rematch")
async def rematch(threshold: Optional[float] = None):
    """Vuelve a agrupar con los datos ya descargados (sin volver a scrapear)."""
    if threshold is not None:
        from app import settings

        settings.save_override("matching.auto_threshold", float(threshold))
    return await pipeline.rematch_only()
