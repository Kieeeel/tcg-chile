"""Endpoints de productos: búsqueda, comparación, historial, favoritos, exportación."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

from app.db.database import transaction
from app.services import export as export_service
from app.services import queries

router = APIRouter(prefix="/api", tags=["productos"])


@router.get("/products")
def list_products(
    q: str = Query("", description="Texto libre: '151', 'booster bundle', 'ETB'..."),
    game: Optional[str] = None,
    set_code: Optional[str] = None,
    product_type: Optional[str] = None,
    language: Optional[str] = Query(None, description="es, en, jp… o 'unknown'"),
    store_id: Optional[int] = None,
    only_in_stock: bool = False,
    favorites_only: bool = False,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort: str = Query("relevance", pattern="^(relevance|price_asc|price_desc|name|stores|updated|discount)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
):
    return queries.search_products(
        q,
        game=game,
        set_code=set_code,
        product_type=product_type,
        language=language,
        store_id=store_id,
        only_in_stock=only_in_stock,
        favorites_only=favorites_only,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/products/{product_id}")
def get_product(product_id: int):
    detail = queries.product_detail(product_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    # Abrir la ficha cuenta como una visita: es lo que alimenta «Lo más
    # visto» de la portada.
    queries.register_view(product_id)
    return detail


@router.get("/products/{product_id}/history")
def get_history(product_id: int, days: int = Query(90, ge=1, le=1095)):
    return queries.price_history(product_id, days)


@router.post("/products/{product_id}/favorite")
def add_favorite(product_id: int):
    with transaction() as conn:
        conn.execute(
            "INSERT INTO favorites (product_id) VALUES (?) ON CONFLICT DO NOTHING",
            (product_id,),
        )
    return {"product_id": product_id, "is_favorite": True}


@router.delete("/products/{product_id}/favorite")
def remove_favorite(product_id: int):
    with transaction() as conn:
        conn.execute("DELETE FROM favorites WHERE product_id = ?", (product_id,))
    return {"product_id": product_id, "is_favorite": False}


@router.get("/favorites")
def list_favorites(page: int = Query(1, ge=1), page_size: int = Query(48, ge=1, le=200)):
    return queries.search_products(
        "", favorites_only=True, sort="name", page=page, page_size=page_size
    )


@router.get("/suggest")
def suggest(q: str = "", limit: int = Query(8, ge=1, le=25)):
    return queries.suggest(q, limit)


@router.get("/facets")
def get_facets():
    return queries.facets()


@router.get("/export")
def export(
    format: str = Query("csv", pattern="^(csv|json|xlsx)$"),
    product_id: Optional[int] = None,
):
    rows = export_service.collect_rows([product_id] if product_id else None)

    if format == "json":
        return Response(
            export_service.to_json(rows),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="tcg-comparativa.json"'},
        )
    if format == "xlsx":
        return Response(
            export_service.to_xlsx(rows),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="tcg-comparativa.xlsx"'},
        )
    return Response(
        export_service.to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tcg-comparativa.csv"'},
    )


# ---------------------------------------------------------------------------
# Comentarios
#
# Se guardan en la misma base SQLite que todo lo demás: sin servicio externo,
# sin cuenta que crear y sin que la ficha tenga que pedir nada por internet.
# ---------------------------------------------------------------------------
@router.get("/products/{product_id}/comments")
def list_comments(product_id: int, limit: int = 100):
    return queries.comments(product_id, limit)


@router.post("/products/{product_id}/comments")
def create_comment(
    product_id: int,
    body: str = Body(..., embed=True),
    author: Optional[str] = Body(None, embed=True),
):
    try:
        return queries.add_comment(product_id, body, author)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/comments/{comment_id}")
def remove_comment(comment_id: int):
    if not queries.delete_comment(comment_id):
        raise HTTPException(status_code=404, detail="Ese comentario ya no existe")
    return {"deleted": True}
