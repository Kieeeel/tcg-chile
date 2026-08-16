"""Consultas de lectura para la API: búsqueda, comparación, historial, dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app import settings
from app.core import attributes as attrs_module
from app.core.normalize import normalize_name
from app.core.similarity import search_score
from app.core.units import savings, unit_price
from app.db.database import get_connection, query, query_one, transaction

STOCK_LABELS = {
    "in_stock": "En stock",
    "out_of_stock": "Agotado",
    "preorder": "Preventa",
    "coming_soon": "Próximamente",
    "unknown": "Desconocido",
}

MATCH_LABELS = {
    "EAN_MATCH": "Identificador EAN/UPC/GTIN idéntico",
    "SKU_MATCH": "SKU de fabricante idéntico",
    "NAME_MATCH": "Nombre normalizado idéntico",
    "ATTRIBUTE_MATCH": "Coinciden set, tipo y cantidad",
    "FUZZY_MATCH": "Similitud de texto y atributos",
    "MANUAL_MATCH": "Confirmado manualmente",
    "SINGLETON": "Única oferta encontrada",
}


def _available_states() -> List[str]:
    return list(settings.get("stock.available_states", ["in_stock", "preorder"]) or [])


# ---------------------------------------------------------------------------
# Categorías del filtro de tipo
#
# Por dentro cada producto conserva su tipo exacto —es lo que impide agrupar
# una Booster Box con un Booster Bundle—, pero 26 tipos no son un menú.
# `config/product_types.yaml` los reparte en siete categorías y aquí solo se
# traduce de una a otra.
# ---------------------------------------------------------------------------
def categorias_de_tipo() -> List[Dict[str, Any]]:
    return list((settings.load_product_types() or {}).get("categories") or [])


def tipos_de_categoria(codigo: str) -> List[str]:
    """Tipos que contiene una categoría; lista vacía si no es una categoría."""
    for categoria in categorias_de_tipo():
        if categoria.get("code") == codigo:
            return list(categoria.get("types") or [])
    return []


# ---------------------------------------------------------------------------
# Búsqueda y listado
# ---------------------------------------------------------------------------
def search_products(
    q: str = "",
    *,
    game: Optional[str] = None,
    set_code: Optional[str] = None,
    product_type: Optional[str] = None,
    language: Optional[str] = None,
    store_id: Optional[int] = None,
    only_in_stock: bool = False,
    favorites_only: bool = False,
    min_stores: int = 0,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort: str = "relevance",
    page: int = 1,
    page_size: int = 24,
) -> Dict[str, Any]:
    where: List[str] = ["1=1"]
    params: List[Any] = []

    if game:
        where.append("p.game = ?")
        params.append(game)
    if set_code:
        where.append("p.set_code = ?")
        params.append(set_code)
    if product_type:
        # El filtro trabaja con categorías ("boosters"), pero la columna
        # guarda el tipo exacto ("booster_box"). Se acepta cualquiera de los
        # dos: así siguen valiendo los enlaces guardados con el tipo suelto.
        miembros = tipos_de_categoria(product_type)
        if miembros:
            where.append(f"p.product_type IN ({','.join('?' * len(miembros))})")
            params.extend(miembros)
        else:
            where.append("p.product_type = ?")
            params.append(product_type)
    if language:
        # "unknown" filtra los productos sin idioma declarado (accesorios).
        if language == "unknown":
            where.append("p.language IS NULL")
        else:
            where.append("p.language = ?")
            params.append(language)
    if favorites_only:
        where.append("f.product_id IS NOT NULL")
    if only_in_stock:
        where.append("p.in_stock_count > 0")
    if min_stores > 1:
        # Comparar exige al menos dos tiendas: si no, la «diferencia» es
        # entre dos ofertas de la misma tienda y no dice dónde comprar.
        where.append("p.stores_count >= ?")
        params.append(min_stores)
    if min_price is not None:
        where.append("COALESCE(p.best_available_price, p.best_price) >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("COALESCE(p.best_available_price, p.best_price) <= ?")
        params.append(max_price)
    if store_id:
        where.append(
            "EXISTS (SELECT 1 FROM store_products sp WHERE sp.product_id = p.id "
            "AND sp.store_id = ? AND sp.is_active = 1)"
        )
        params.append(store_id)

    # Prefiltro por texto: cada token debe aparecer en el nombre o en el
    # nombre normalizado del maestro o de alguna de sus ofertas.
    tokens = normalize_name(q).core_tokens if q else []
    for token in tokens[:6]:
        where.append(
            """(p.normalized_name LIKE ? OR LOWER(p.display_name) LIKE ?
                OR EXISTS (SELECT 1 FROM store_products sp2
                           WHERE sp2.product_id = p.id AND sp2.is_active = 1
                             AND (sp2.normalized_name LIKE ? OR LOWER(sp2.name) LIKE ?)))"""
        )
        like = f"%{token}%"
        params.extend([like, like, like, like])

    sql = f"""
        SELECT p.*, CASE WHEN f.product_id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
               bs.name AS best_store_name, bas.name AS best_available_store_name
        FROM products p
        LEFT JOIN favorites f ON f.product_id = p.id
        LEFT JOIN stores bs ON bs.id = p.best_store_id
        LEFT JOIN stores bas ON bas.id = p.best_available_store_id
        WHERE {' AND '.join(where)}
    """

    with get_connection() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

    # Ranking difuso en Python (búsqueda tradicional por tokens, sin IA).
    if tokens:
        for row in rows:
            target = (row.get("normalized_name") or "").split()
            row["_score"] = search_score(tokens, target or (row["display_name"] or "").lower().split())
        rows = [row for row in rows if row["_score"] >= 0.45]
    else:
        for row in rows:
            row["_score"] = 0.0

    rows.sort(key=_sort_key(sort))
    total = len(rows)
    start = max(0, (page - 1) * page_size)
    page_rows = rows[start : start + page_size]

    return {
        "items": [_product_summary(row) for row in page_rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "query": q,
    }


class _desc:
    """Invierte el orden de un valor dentro de una clave de ordenación.

    `rows.sort` solo ordena ascendente, y las fechas son cadenas: esto permite
    pedir «lo más nuevo primero» sin ordenar dos veces.
    """

    __slots__ = ("valor",)

    def __init__(self, valor: Any) -> None:
        self.valor = valor or ""

    def __lt__(self, otro: "_desc") -> bool:
        return self.valor > otro.valor

    def __eq__(self, otro: object) -> bool:
        return isinstance(otro, _desc) and self.valor == otro.valor


def _sort_key(sort: str):
    def price_of(row: Dict[str, Any]) -> float:
        value = row.get("best_available_price") or row.get("best_price")
        return value if value is not None else float("inf")

    if sort == "price_asc":
        return lambda row: (price_of(row), row["display_name"] or "")
    if sort == "price_desc":
        return lambda row: (-(price_of(row) if price_of(row) != float("inf") else -1),)
    if sort == "name":
        return lambda row: (row["display_name"] or "").lower()
    if sort == "stores":
        return lambda row: (-int(row.get("stores_count") or 0), price_of(row))
    if sort == "updated":
        # Lo tocado hace menos, primero: ascendente ponía arriba lo más viejo.
        return lambda row: (_desc(row.get("last_scraped_at")), row["display_name"] or "")
    if sort == "new":
        # Novedades: por cuándo se dio de alta el producto, no por el último
        # scraping, que tras una actualización completa es igual para todos.
        return lambda row: (_desc(row.get("created_at")), row["display_name"] or "")
    if sort == "discount":
        return lambda row: (
            -_discount_pct(row),
            price_of(row),
        )
    # relevancia por defecto
    return lambda row: (-(row.get("_score") or 0), -int(row.get("stores_count") or 0), price_of(row))


def _discount_pct(row: Dict[str, Any]) -> float:
    best = row.get("best_available_price") or row.get("best_price")
    worst = row.get("worst_price")
    if not best or not worst or worst <= 0 or worst <= best:
        return 0.0
    return (worst - best) / worst * 100.0


def _product_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    best = row.get("best_available_price")
    best_store = row.get("best_available_store_name")
    if best is None:
        best = row.get("best_price")
        best_store = row.get("best_store_name")

    return {
        "id": row["id"],
        "name": row["display_name"],
        "game": row["game"],
        "set_code": row["set_code"],
        "set_name": row["set_name"],
        "product_type": row["product_type"],
        "product_type_name": row["product_type_name"],
        "language": row["language"],
        "language_name": attrs_module.language_name(row["language"]),
        "units_total": row["units_total"],
        "unit_name": row["unit_name"],
        "image_url": row["image_url"],
        "best_price": best,
        "best_store": best_store,
        "worst_price": row.get("worst_price"),
        "avg_price": row.get("avg_price"),
        "unit_price": unit_price(best, row.get("units_total"), 1.0),
        "offers_count": row.get("offers_count"),
        "stores_count": row.get("stores_count"),
        "in_stock_count": row.get("in_stock_count"),
        "max_savings": _discount_pct(row),
        "is_favorite": bool(row.get("is_favorite")),
        "last_scraped_at": row.get("last_scraped_at"),
        "relevance": round(float(row.get("_score") or 0), 3),
    }


# ---------------------------------------------------------------------------
# Detalle y comparación
# ---------------------------------------------------------------------------
def _variant_labels(offers: List[Dict[str, Any]]) -> Dict[int, str]:
    """Qué opción hay que elegir en la tienda, para las fichas compartidas.

    Bsale vende varias versiones en una sola página y no admite una URL por
    variante (comprobado: ni `?variant=`, ni `?sku=`, ni `#var2`; el tema
    tampoco mira la URL). Nuestras ofertas se distinguen con `#<sku>`, que al
    navegador no le dice nada, así que al usuario hay que decirle con
    palabras cuál de las opciones es la suya.

    La etiqueta sale de comparar el nombre de la oferta con el de sus
    hermanas de la misma ficha: lo que NO comparten es justo lo que las
    distingue.

        …Colección con Póster Prémium de Megaevolución ESPAÑOL, Mega Gardevoir
        …Colección con Póster Prémium de Megaevolución ESPAÑOL, Mega Lucario
                                                                   ^^^^^^^^^
    """
    compartidas = [o for o in offers if "#" in (o.get("url") or "")]
    if not compartidas:
        return {}

    # Las hermanas pueden estar en OTROS productos maestros (justo el caso del
    # español y el inglés), así que se buscan por la ficha, no por el grupo.
    fichas = {(o["store_id"], o["url"].split("#", 1)[0]) for o in compartidas}
    familia: Dict[tuple, List[str]] = {}
    with get_connection() as conn:
        for store_id, base in fichas:
            filas = conn.execute(
                """SELECT name FROM store_products
                   WHERE store_id = ? AND is_active = 1
                     AND (url = ? OR url LIKE ?)""",
                (store_id, base, f"{base}#%"),
            ).fetchall()
            familia[(store_id, base)] = [f["name"] for f in filas if f["name"]]

    etiquetas: Dict[int, str] = {}
    for offer in compartidas:
        hermanas = familia.get((offer["store_id"], offer["url"].split("#", 1)[0]), [])
        etiquetas[offer["id"]] = _distinctive_tail(offer["name"], hermanas)
    return etiquetas


def _distinctive_tail(nombre: str, familia: List[str]) -> str:
    """Parte del nombre que no comparte con el resto de su ficha."""
    otras = [n for n in familia if n != nombre]
    if not otras:
        return nombre

    palabras = nombre.split()
    # Prefijo común (en palabras) con TODAS las hermanas.
    comun = len(palabras)
    for otra in otras:
        otras_palabras = otra.split()
        i = 0
        while i < min(comun, len(otras_palabras)) and palabras[i] == otras_palabras[i]:
            i += 1
        comun = min(comun, i)

    cola = " ".join(palabras[comun:]).strip(" ,-–—")
    return cola or nombre


def product_detail(product_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT p.*, CASE WHEN f.product_id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
                      bs.name AS best_store_name, bas.name AS best_available_store_name
               FROM products p
               LEFT JOIN favorites f ON f.product_id = p.id
               LEFT JOIN stores bs ON bs.id = p.best_store_id
               LEFT JOIN stores bas ON bas.id = p.best_available_store_id
               WHERE p.id = ?""",
            (product_id,),
        ).fetchone()
        if row is None:
            return None

        offers = [
            dict(item)
            for item in conn.execute(
                """SELECT sp.*, s.name AS store_name, s.code AS store_code
                   FROM store_products sp
                   JOIN stores s ON s.id = sp.store_id
                   WHERE sp.product_id = ? AND sp.is_active = 1
                   ORDER BY (sp.price IS NULL), sp.price ASC""",
                (product_id,),
            ).fetchall()
        ]

        stats = conn.execute(
            """SELECT MIN(ph.price) AS min_price, MAX(ph.price) AS max_price,
                      AVG(ph.price) AS avg_price, COUNT(*) AS points
               FROM price_history ph
               JOIN store_products sp ON sp.id = ph.store_product_id
               WHERE sp.product_id = ?""",
            (product_id,),
        ).fetchone()

    # Enlaces corregidos a mano (Administración → Productos). Se guardan al
    # margen de la columna `url`, que es la identidad de la oferta.
    overrides = offer_overrides()
    for offer in offers:
        clave = f"{offer['store_code']}::{offer['external_id'] or offer['url']}"
        manual = overrides.get(clave, {})
        if manual.get("url"):
            offer["url"] = manual["url"]

    etiquetas = _variant_labels(offers)

    available = _available_states()

    # Primero lo que se puede comprar y, dentro de eso, lo más barato.
    #
    # Ordenar solo por precio ponía arriba ofertas agotadas: un precio
    # inmejorable en algo que nadie te va a vender no encabeza una
    # comparación de compra. Las que no tienen precio van al final.
    offers.sort(
        key=lambda o: (
            0 if o["stock_status"] in available else 1,
            o["price"] is None,
            o["price"] if o["price"] is not None else 0,
        )
    )

    best_available = min(
        (o["price"] for o in offers if o["price"] is not None and o["stock_status"] in available),
        default=None,
    )
    best_any = min((o["price"] for o in offers if o["price"] is not None), default=None)
    reference = best_available if best_available is not None else best_any

    enriched: List[Dict[str, Any]] = []
    rank = 0
    for offer in offers:
        is_available = offer["stock_status"] in available
        if offer["price"] is not None:
            rank += 1
        diff = savings(reference, offer["price"])
        enriched.append(
            {
                "id": offer["id"],
                "store_id": offer["store_id"],
                "store": offer["store_name"],
                "store_code": offer["store_code"],
                "name": offer["name"],
                "url": offer["url"],
                "image_url": offer["image_url"],
                "price": offer["price"],
                "currency": offer["currency"],
                "unit_price": unit_price(
                    offer["price"], offer["units_total"], float(offer["quantity_confidence"] or 0)
                ),
                "units_total": offer["units_total"],
                "language": offer["language"],
                "language_name": attrs_module.language_name(offer["language"]),
                "stock_status": offer["stock_status"],
                "stock_label": STOCK_LABELS.get(offer["stock_status"], offer["stock_status"]),
                "is_available": is_available,
                "sku": offer["sku"],
                "ean": offer["ean"] or offer["gtin"] or offer["upc"],
                # Qué opción hay que elegir al llegar a la tienda, cuando la
                # ficha es compartida (Bsale). None si el enlace basta.
                "pick_variant": etiquetas.get(offer["id"]),
                "rank": rank if offer["price"] is not None else None,
                "is_best": offer["price"] is not None and offer["price"] == reference and is_available,
                "difference": diff["amount"],
                "difference_pct": diff["percent"],
                "match_score": offer["match_score"],
                "match_method": offer["match_method"],
                "match_method_label": MATCH_LABELS.get(offer["match_method"], offer["match_method"]),
                "last_seen_at": offer["last_seen_at"],
                "last_price_change_at": offer["last_price_change_at"],
            }
        )

    prices = [o["price"] for o in enriched if o["price"] is not None]
    max_savings = savings(min(prices), max(prices)) if len(prices) > 1 else {"amount": None, "percent": None}

    detail = _product_summary(dict(row))
    detail.update(
        {
            "offers": enriched,
            "best_available_price": best_available,
            "best_any_price": best_any,
            "max_savings_amount": max_savings["amount"],
            "max_savings_pct": max_savings["percent"],
            "history_stats": {
                "min_price": stats["min_price"] if stats else None,
                "max_price": stats["max_price"] if stats else None,
                "avg_price": round(stats["avg_price"], 2) if stats and stats["avg_price"] else None,
                "points": stats["points"] if stats else 0,
            },
            "other_languages": _same_product_other_languages(dict(row)),
        }
    )
    return detail


def _same_product_other_languages(producto: Dict[str, Any]) -> List[Dict[str, Any]]:
    """El mismo artículo publicado en otro idioma.

    Son productos maestros distintos a propósito —un ETB en español y uno en
    inglés no son la misma compra—, pero desde uno se quiere llegar al otro.

    Se buscan por los atributos que sí comparten (juego, set, tipo y unidades)
    y se ordenan por parecido del nombre, para no confundir dos productos del
    mismo set y tipo que en realidad son distintos (por ejemplo dos Mini Tin
    de personajes diferentes).
    """
    if not producto.get("product_type"):
        return []

    # El set NO entra en la consulta: hay productos a los que no se les
    # detecta y aun así tienen su gemelo en el otro idioma (la Ultra Premium
    # Collection de Mega Charizard X, por ejemplo). Se comprueba después, y
    # solo cuando los dos lo declaran.
    with get_connection() as conn:
        filas = conn.execute(
            """SELECT id, display_name, normalized_name, language, units_total,
                      set_code, best_price, best_available_price,
                      in_stock_count, stores_count
               FROM products
               WHERE game IS ? AND product_type = ?
                 AND id != ? AND language IS NOT NULL AND language IS NOT ?""",
            (
                producto.get("game"),
                producto["product_type"],
                producto["id"],
                producto.get("language"),
            ),
        ).fetchall()

    propios = (producto.get("normalized_name") or "").split()
    candidatos: List[Dict[str, Any]] = []
    for fila in filas:
        # Mismo contenido: un Booster Bundle de 6 sobres no es el de 8.
        if (fila["units_total"] or 0) != (producto.get("units_total") or 0):
            continue
        # Si los dos declaran set y no coincide, son de expansiones distintas.
        if fila["set_code"] and producto.get("set_code") and fila["set_code"] != producto["set_code"]:
            continue
        parecido = search_score(propios, (fila["normalized_name"] or "").split()) if propios else 0
        candidatos.append({**dict(fila), "_score": parecido})

    # Uno por idioma: el que más se parece.
    mejor: Dict[str, Dict[str, Any]] = {}
    for candidato in candidatos:
        actual = mejor.get(candidato["language"])
        if actual is None or candidato["_score"] > actual["_score"]:
            mejor[candidato["language"]] = candidato

    return [
        {
            "id": c["id"],
            "name": c["display_name"],
            "language": c["language"],
            "language_name": attrs_module.language_name(c["language"]),
            "best_price": c["best_available_price"] or c["best_price"],
            "in_stock_count": c["in_stock_count"] or 0,
            "stores_count": c["stores_count"] or 0,
        }
        for c in sorted(mejor.values(), key=lambda c: -c["_score"])
        if c["_score"] >= 0.55
    ]


# ---------------------------------------------------------------------------
# Historial de precios
# ---------------------------------------------------------------------------
def price_history(product_id: int, days: Optional[int] = None) -> Dict[str, Any]:
    days = days or int(settings.get("history.default_chart_days", 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        rows = conn.execute(
            """SELECT ph.recorded_at, ph.price, s.name AS store_name, s.id AS store_id
               FROM price_history ph
               JOIN store_products sp ON sp.id = ph.store_product_id
               JOIN stores s ON s.id = sp.store_id
               WHERE sp.product_id = ? AND ph.recorded_at >= ?
               ORDER BY ph.recorded_at ASC""",
            (product_id, since),
        ).fetchall()

    series: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        series.setdefault(row["store_name"], []).append(
            {"t": row["recorded_at"], "price": row["price"]}
        )

    # Serie del "mejor precio disponible" en cada momento registrado.
    best_series: List[Dict[str, Any]] = []
    running: Dict[str, float] = {}
    for row in rows:
        running[row["store_name"]] = row["price"]
        best_series.append({"t": row["recorded_at"], "price": min(running.values())})

    return {
        "product_id": product_id,
        "days": days,
        "series": [{"store": store, "points": points} for store, points in series.items()],
        "best": best_series,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def dashboard() -> Dict[str, Any]:
    available = _available_states()
    placeholders = ",".join("?" * len(available)) or "''"

    with get_connection() as conn:
        totals = conn.execute(
            f"""SELECT
                  (SELECT COUNT(*) FROM products) AS products,
                  (SELECT COUNT(*) FROM store_products WHERE is_active = 1) AS offers,
                  (SELECT COUNT(*) FROM stores) AS stores,
                  (SELECT COUNT(*) FROM stores WHERE enabled = 1) AS stores_enabled,
                  (SELECT COUNT(*) FROM store_products
                    WHERE is_active = 1 AND stock_status IN ({placeholders})) AS in_stock,
                  (SELECT COUNT(*) FROM products WHERE stores_count > 1) AS compared,
                  (SELECT COUNT(*) FROM match_reviews WHERE status = 'pending') AS pending_reviews,
                  (SELECT COUNT(*) FROM favorites) AS favorites,
                  (SELECT COUNT(*) FROM alerts WHERE active = 1) AS alerts,
                  (SELECT COUNT(*) FROM alert_hits WHERE seen = 0) AS alert_hits,
                  (SELECT COUNT(*) FROM events
                    WHERE type = 'price_drop' AND created_at >= datetime('now', '-7 days')) AS price_drops,
                  (SELECT COUNT(*) FROM events
                    WHERE type = 'new_product' AND created_at >= datetime('now', '-7 days')) AS new_products,
                  (SELECT COUNT(*) FROM scrape_errors
                    WHERE created_at >= datetime('now', '-1 day')) AS recent_errors,
                  (SELECT MAX(finished_at) FROM scrape_runs WHERE status != 'running') AS last_update
            """,
            tuple(available),
        ).fetchone()

        drops = conn.execute(
            """SELECT e.id, e.message, e.pct_change, e.created_at, e.old_value, e.new_value,
                      sp.name AS product_name, sp.url, sp.product_id, s.name AS store_name
               FROM events e
               LEFT JOIN store_products sp ON sp.id = e.store_product_id
               LEFT JOIN stores s ON s.id = e.store_id
               WHERE e.type = 'price_drop'
               ORDER BY e.created_at DESC LIMIT 10"""
        ).fetchall()

        runs = conn.execute(
            """SELECT r.*, s.name AS store_name, s.code AS store_code
               FROM scrape_runs r LEFT JOIN stores s ON s.id = r.store_id
               ORDER BY r.started_at DESC LIMIT 8"""
        ).fetchall()

    # Resumen ligero: lo consulta la interfaz a menudo (badges, contadores),
    # así que no debe recorrer todo el catálogo. El cálculo de oportunidades
    # vive aparte, en /api/opportunities.
    return {
        "totals": dict(totals) if totals else {},
        "price_drops": [dict(row) for row in drops],
        "recent_runs": [dict(row) for row in runs],
    }


# ---------------------------------------------------------------------------
# Filtros disponibles
# ---------------------------------------------------------------------------
def _offers_by_product(only_in_stock: bool = True) -> Dict[int, List[Dict[str, Any]]]:
    """Ofertas activas agrupadas por producto maestro."""
    available = _available_states()
    filtro = (
        f"AND sp.stock_status IN ({','.join('?' * len(available))})" if only_in_stock else ""
    )
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT sp.product_id, sp.price, sp.url, sp.units_total,
                       sp.quantity_confidence, sp.stock_status, sp.store_id,
                       s.name AS store
                FROM store_products sp JOIN stores s ON s.id = sp.store_id
                WHERE sp.is_active = 1 AND sp.price IS NOT NULL
                  AND sp.product_id IS NOT NULL {filtro}
                ORDER BY sp.price ASC""",
            tuple(available) if only_in_stock else (),
        ).fetchall()

    agrupado: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        agrupado.setdefault(row["product_id"], []).append(dict(row))
    return agrupado


def opportunities(limit: int = 12, sort: str = "amount") -> List[Dict[str, Any]]:
    """Mejores oportunidades de compra reales.

    El ahorro se mide contra la MEDIANA de las tiendas, no contra la más cara:
    una sola tienda con un precio disparatado no debe inflar la oportunidad.
    La mediana es lo que pagarías de no conocer esta herramienta.

    Solo entran productos comprables ahora (con stock) y presentes en dos
    tiendas o más, que es cuando comparar significa algo.
    """
    import statistics

    por_producto = _offers_by_product(only_in_stock=True)
    if not por_producto:
        return []

    ids = list(por_producto.keys())
    with get_connection() as conn:
        productos = {
            row["id"]: dict(row)
            for row in conn.execute(
                f"""SELECT id, display_name, game, set_name, product_type_name,
                           language, image_url, units_total, unit_name
                    FROM products WHERE id IN ({','.join('?' * len(ids))})""",
                ids,
            ).fetchall()
        }

    from app.core.units import unit_price

    salida: List[Dict[str, Any]] = []
    for product_id, ofertas in por_producto.items():
        tiendas = {o["store_id"] for o in ofertas}
        if len(tiendas) < 2:
            continue

        precios = [o["price"] for o in ofertas]
        mejor = ofertas[0]
        mediana = statistics.median(precios)
        ahorro = mediana - mejor["price"]
        if ahorro <= 0:
            continue

        producto = productos.get(product_id)
        if not producto:
            continue

        salida.append(
            {
                "id": product_id,
                "name": producto["display_name"],
                "set_name": producto["set_name"],
                "product_type_name": producto["product_type_name"],
                "language": producto["language"],
                "language_name": attrs_module.language_name(producto["language"]),
                "image_url": producto["image_url"],
                "best_price": mejor["price"],
                "best_store": mejor["store"],
                "best_url": mejor["url"],
                "median_price": round(mediana, 2),
                "max_price": max(precios),
                "stores_count": len(tiendas),
                "savings_amount": round(ahorro, 2),
                "savings_pct": round(ahorro / mediana * 100, 1),
                "unit_price": unit_price(
                    mejor["price"], mejor["units_total"],
                    float(mejor["quantity_confidence"] or 0),
                ),
                "unit_name": producto["unit_name"],
                # Para la barra comparativa de la interfaz.
                "prices": [
                    {"store": o["store"], "price": o["price"]} for o in ofertas[:6]
                ],
            }
        )

    claves = {
        "amount": lambda o: -o["savings_amount"],
        "percent": lambda o: -o["savings_pct"],
        "unit": lambda o: (o["unit_price"] is None, o["unit_price"] or 0),
    }
    salida.sort(key=claves.get(sort, claves["amount"]))
    return salida[:limit]


def unit_price_ranking(limit: int = 10) -> List[Dict[str, Any]]:
    """Dónde sale más barato el sobre ahora mismo.

    Es la comparación que de verdad importa en TCG: permite enfrentar un
    Booster Bundle con una Booster Box o con sobres sueltos.
    """
    from app.core.units import unit_price

    minima = float(settings.get("unit_price.min_confidence", 0.7))
    available = _available_states()

    # Solo los envases cuyo contenido son sobres. Una lata o una colección
    # traen cartas fijas: su "precio por sobre" no significa nada.
    comparables = [
        t["code"] for t in attrs_module.all_types() if t.get("unit_comparable")
    ]
    if not comparables:
        return []

    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.display_name, p.set_name, p.product_type_name,
                       p.language, p.image_url, p.unit_name,
                       sp.price, sp.units_total, sp.quantity_confidence, sp.url,
                       s.name AS store
                FROM store_products sp
                JOIN products p ON p.id = sp.product_id
                JOIN stores s ON s.id = sp.store_id
                WHERE sp.is_active = 1 AND sp.price IS NOT NULL
                  AND sp.units_total > 1 AND sp.quantity_confidence >= ?
                  AND sp.product_type IN ({','.join('?' * len(comparables))})
                  AND sp.stock_status IN ({','.join('?' * len(available))})""",
            (minima, *comparables, *available),
        ).fetchall()

    mejores: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        precio_unitario = unit_price(
            row["price"], row["units_total"], float(row["quantity_confidence"] or 0)
        )
        if precio_unitario is None:
            continue
        actual = mejores.get(row["id"])
        if actual is None or precio_unitario < actual["unit_price"]:
            mejores[row["id"]] = {
                "id": row["id"],
                "name": row["display_name"],
                "set_name": row["set_name"],
                "product_type_name": row["product_type_name"],
                "language_name": attrs_module.language_name(row["language"]),
                "image_url": row["image_url"],
                "store": row["store"],
                "url": row["url"],
                "price": row["price"],
                "units_total": row["units_total"],
                "unit_price": precio_unitario,
                "unit_name": row["unit_name"] or "booster",
            }

    return sorted(mejores.values(), key=lambda x: x["unit_price"])[:limit]


def historic_lows(limit: int = 8, game: Optional[str] = None) -> List[Dict[str, Any]]:
    """Productos en su precio más bajo desde que se les sigue la pista.

    Se exige que hayan tenido al menos dos precios distintos: si solo se ha
    registrado uno, «mínimo histórico» no significa nada todavía.
    """
    available = _available_states()
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.display_name, p.set_name, p.product_type_name,
                       p.language, p.image_url,
                       p.best_available_price AS actual,
                       MIN(ph.price) AS minimo,
                       MAX(ph.price) AS maximo,
                       COUNT(DISTINCT ph.price) AS distintos,
                       COUNT(*) AS puntos
                FROM products p
                JOIN store_products sp ON sp.product_id = p.id AND sp.is_active = 1
                JOIN price_history ph ON ph.store_product_id = sp.id
                WHERE p.best_available_price IS NOT NULL
                  AND sp.stock_status IN ({','.join('?' * len(available))})
                  {'AND p.game = ?' if game else ''}
                GROUP BY p.id
                -- Postgres no admite alias del SELECT en HAVING (`distintos`):
                -- hay que repetir la expresión.
                HAVING COUNT(DISTINCT ph.price) >= 2
                   AND p.best_available_price <= MIN(ph.price) * 1.02
                ORDER BY (MAX(ph.price) - p.best_available_price) / MAX(ph.price) DESC
                LIMIT ?""",
            (*available, *( (game,) if game else () ), limit),
        ).fetchall()

    return [
        {
            **dict(row),
            "language_name": attrs_module.language_name(row["language"]),
            "drop_pct": round(
                (row["maximo"] - row["actual"]) / row["maximo"] * 100, 1
            ) if row["maximo"] else 0,
        }
        for row in rows
    ]


def facets() -> Dict[str, Any]:
    with get_connection() as conn:
        games = conn.execute(
            """SELECT game AS code, COUNT(*) AS count FROM products
               WHERE game IS NOT NULL GROUP BY game ORDER BY count DESC"""
        ).fetchall()
        sets_rows = conn.execute(
            """SELECT set_code AS code, set_name AS name, game, COUNT(*) AS count
               FROM products WHERE set_code IS NOT NULL
               GROUP BY set_code, set_name, game ORDER BY count DESC"""
        ).fetchall()
        types = conn.execute(
            """SELECT product_type AS code, product_type_name AS name, COUNT(*) AS count
               FROM products WHERE product_type IS NOT NULL
               GROUP BY product_type, product_type_name ORDER BY count DESC"""
        ).fetchall()
        languages = conn.execute(
            """SELECT COALESCE(language, 'unknown') AS code, COUNT(*) AS count
               FROM products GROUP BY language ORDER BY count DESC"""
        ).fetchall()
        stores = conn.execute(
            """SELECT s.id, s.code, s.name, s.enabled,
                      COUNT(sp.id) AS products
               FROM stores s
               LEFT JOIN store_products sp ON sp.store_id = s.id AND sp.is_active = 1
               GROUP BY s.id ORDER BY s.name"""
        ).fetchall()
        price_range = conn.execute(
            """SELECT MIN(COALESCE(best_available_price, best_price)) AS min,
                      MAX(COALESCE(best_available_price, best_price)) AS max
               FROM products"""
        ).fetchone()

    # Todos los juegos configurados, tengan productos o no: la portada los
    # muestra como banners y los que aún no tienen catálogo salen inactivos.
    conteo = {row["code"]: row["count"] for row in games}
    juegos = [
        {
            "code": g["code"],
            "name": g.get("name", g["code"]),
            "color": g.get("color", "#6b7280"),
            "count": conteo.get(g["code"], 0),
            "available": conteo.get(g["code"], 0) > 0,
        }
        for g in settings.load_games()
    ]
    # Primero los que tienen productos, y dentro de esos el mayor catálogo.
    juegos.sort(key=lambda g: (not g["available"], -g["count"]))

    # El desplegable de tipo enseña las siete categorías, con el total de cada
    # una y conservando el orden del YAML (ETB primero, Otros al final). Las
    # que no tienen ningún producto no se listan: un filtro que no filtra nada
    # solo estorba.
    por_tipo = {row["code"]: row["count"] for row in types}
    categorias = []
    for categoria in categorias_de_tipo():
        total = sum(por_tipo.get(t, 0) for t in categoria.get("types") or [])
        if total:
            categorias.append(
                {"code": categoria["code"], "name": categoria["name"], "count": total}
            )

    return {
        "games": juegos,
        "sets": [dict(row) for row in sets_rows],
        "types": categorias,
        # El detalle por tipo exacto sigue disponible para quien lo necesite
        # (la ficha de producto lo enseña y las exportaciones lo llevan).
        "product_types": [dict(row) for row in types],
        "languages": [
            {
                **dict(row),
                "name": "Sin idioma declarado"
                if row["code"] == "unknown"
                else attrs_module.language_name(row["code"]),
            }
            for row in languages
        ],
        "stores": [dict(row) for row in stores],
        "stock_states": [{"code": key, "name": label} for key, label in STOCK_LABELS.items()],
        "price_range": dict(price_range) if price_range else {"min": None, "max": None},
    }


def suggest(q: str, limit: int = 8) -> List[Dict[str, Any]]:
    if not q or len(q) < 2:
        return []
    result = search_products(q, page_size=limit, sort="relevance")
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "set_name": item["set_name"],
            "product_type_name": item["product_type_name"],
            "best_price": item["best_price"],
            "stores_count": item["stores_count"],
        }
        for item in result["items"]
    ]


def merge_candidates(product_id: int, q: str = "", limit: int = 12) -> List[Dict[str, Any]]:
    """Con qué otros productos tendría sentido unir este.

    Sin texto, propone los más parecidos: mismo juego, misma expansión y mismo
    tipo, que es donde se esconden los duplicados de verdad —el mismo artículo
    que dos tiendas escribieron distinto—. Con texto, busca por nombre, para
    cuando sabes exactamente cuál es el gemelo.
    """
    base = query_one(
        """SELECT id, display_name, game, set_code, product_type, language
           FROM products WHERE id = ?""",
        (product_id,),
    )
    if base is None:
        return []
    base = dict(base)

    if q and len(q.strip()) >= 2:
        encontrados = search_products(q.strip(), page_size=limit + 1)["items"]
    else:
        donde = ["p.id != ?"]
        params: List[Any] = [product_id]
        for columna in ("game", "set_code", "product_type"):
            if base.get(columna):
                donde.append(f"p.{columna} = ?")
                params.append(base[columna])
        params.append(limit + 1)
        filas = query(
            f"""SELECT p.*, 0 AS is_favorite,
                       NULL AS best_store_name, NULL AS best_available_store_name
                FROM products p
                WHERE {' AND '.join(donde)}
                ORDER BY p.display_name
                LIMIT ?""",
            params,
        )
        encontrados = [_product_summary(dict(f)) for f in filas]

    return [
        {
            "id": item["id"],
            "name": item["name"],
            "set_name": item.get("set_name"),
            "product_type_name": item.get("product_type_name"),
            "language_name": item.get("language_name"),
            "image_url": item.get("image_url"),
            "best_price": item.get("best_price"),
            "stores_count": item.get("stores_count"),
        }
        for item in encontrados
        if item["id"] != product_id
    ][:limit]


# ---------------------------------------------------------------------------
# Eventos y logs
# ---------------------------------------------------------------------------
def events(limit: int = 50, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = """SELECT e.*, s.name AS store_name, sp.product_id AS linked_product_id
             FROM events e
             LEFT JOIN stores s ON s.id = e.store_id
             LEFT JOIN store_products sp ON sp.id = e.store_product_id"""
    params: List[Any] = []
    if event_type:
        sql += " WHERE e.type = ?"
        params.append(event_type)
    sql += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)

    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def logs(limit: int = 200, level: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM app_log"
    params: List[Any] = []
    if level:
        sql += " WHERE level = ?"
        params.append(level)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Tiendas
# ---------------------------------------------------------------------------
def store_overview() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM store_products sp
                        WHERE sp.store_id = s.id AND sp.is_active = 1) AS products,
                      (SELECT COUNT(*) FROM store_products sp
                        WHERE sp.store_id = s.id AND sp.is_active = 1
                          AND sp.price IS NULL) AS products_without_price,
                      (SELECT COUNT(*) FROM scrape_errors se
                        WHERE se.store_id = s.id
                          AND se.created_at >= datetime('now', '-7 days')) AS recent_errors,
                      (SELECT MAX(finished_at) FROM scrape_runs r
                        WHERE r.store_id = s.id AND r.status != 'running') AS last_run_at,
                      (SELECT status FROM scrape_runs r WHERE r.store_id = s.id
                        ORDER BY started_at DESC LIMIT 1) AS last_status,
                      (SELECT AVG(duration_ms) FROM scrape_runs r
                        WHERE r.store_id = s.id AND r.duration_ms IS NOT NULL) AS avg_duration_ms
               FROM stores s ORDER BY s.name"""
        ).fetchall()
    return [dict(row) for row in rows]


def store_errors(store_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM scrape_errors WHERE store_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (store_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Revisión manual
# ---------------------------------------------------------------------------
def pending_reviews(limit: int = 50) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT r.*,
                      a.name AS a_name, a.url AS a_url, a.price AS a_price,
                      a.image_url AS a_image, a.set_code AS a_set, a.product_type AS a_type,
                      a.units_total AS a_units, a.stock_status AS a_stock,
                      a.language AS a_lang, sa.name AS a_store,
                      b.name AS b_name, b.url AS b_url, b.price AS b_price,
                      b.image_url AS b_image, b.set_code AS b_set, b.product_type AS b_type,
                      b.units_total AS b_units, b.stock_status AS b_stock,
                      b.language AS b_lang, sb.name AS b_store
               FROM match_reviews r
               JOIN store_products a ON a.id = r.a_id
               JOIN store_products b ON b.id = r.b_id
               JOIN stores sa ON sa.id = a.store_id
               JOIN stores sb ON sb.id = b.store_id
               WHERE r.status = 'pending'
               ORDER BY r.score DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    import json as _json

    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["breakdown"] = _json.loads(item.get("breakdown") or "{}")
        except _json.JSONDecodeError:
            item["breakdown"] = {}
        out.append(item)
    return out


def manual_decisions(limit: int = 200) -> List[Dict[str, Any]]:
    """Decisiones manuales, con los nombres reales en vez de las claves.

    En la base se guardan claves estables ("sidedeck::10149439766839") para
    que sobrevivan a los re-scrapings, pero eso no le dice nada a nadie: aquí
    se traducen al nombre y la tienda de cada oferta.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_matches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        ofertas = conn.execute(
            """SELECT sp.id, sp.name, sp.url, sp.price, sp.language,
                      s.code || '::' || COALESCE(sp.external_id, sp.url) AS key,
                      s.name AS store
               FROM store_products sp JOIN stores s ON s.id = sp.store_id
               WHERE sp.is_active = 1"""
        ).fetchall()

    por_clave = {row["key"]: dict(row) for row in ofertas}

    def lado(key: str) -> Dict[str, Any]:
        oferta = por_clave.get(key)
        if oferta is None:
            # La tienda retiró el producto: la decisión sigue siendo válida
            # por si vuelve, pero ya no hay nombre que mostrar.
            return {"key": key, "name": None, "store": key.split("::")[0], "missing": True}
        return {
            "key": key,
            "id": oferta["id"],
            "name": oferta["name"],
            "store": oferta["store"],
            "url": oferta["url"],
            "price": oferta["price"],
            "language": oferta["language"],
            "language_name": attrs_module.language_name(oferta["language"]),
            "missing": False,
        }

    return [
        {
            "id": row["id"],
            "decision": row["decision"],
            "note": row["note"],
            "created_at": row["created_at"],
            "a": lado(row["a_key"]),
            "b": lado(row["b_key"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Edición manual de ofertas (Administración → Productos)
# ---------------------------------------------------------------------------
def offer_overrides() -> Dict[str, Dict[str, Any]]:
    """{clave_estable: {atributo: valor}} de las correcciones manuales."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT entity_key, attribute, value FROM manual_attributes"
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        out.setdefault(row["entity_key"], {})[row["attribute"]] = row["value"]
    return out


def admin_offers(
    q: str = "",
    *,
    store_id: Optional[int] = None,
    language: Optional[str] = None,
    edited_only: bool = False,
    page: int = 1,
    page_size: int = 30,
) -> Dict[str, Any]:
    """Ofertas tal como están guardadas, para poder corregirlas a mano.

    Esta pantalla trabaja con OFERTAS (lo que publica cada tienda), no con
    productos maestros: una corrección de idioma o de enlace es siempre sobre
    la oferta de una tienda concreta.
    """
    where: List[str] = ["sp.is_active = 1"]
    params: List[Any] = []

    if q:
        where.append("(sp.name LIKE ? OR sp.url LIKE ? OR sp.sku LIKE ?)")
        params.extend([f"%{q}%"] * 3)
    if store_id:
        where.append("sp.store_id = ?")
        params.append(int(store_id))
    if language == "unknown":
        where.append("sp.language IS NULL")
    elif language:
        where.append("sp.language = ?")
        params.append(language)

    clause = " AND ".join(where)
    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM store_products sp WHERE {clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT sp.id, sp.store_id, sp.external_id, sp.url, sp.name, sp.price,
                       sp.stock_status, sp.language, sp.set_code, sp.product_type,
                       sp.product_id, sp.sku, sp.last_seen_at,
                       s.code AS store_code, s.name AS store_name,
                       p.display_name AS product_name, p.stores_count
                FROM store_products sp
                JOIN stores s ON s.id = sp.store_id
                LEFT JOIN products p ON p.id = sp.product_id
                WHERE {clause}
                ORDER BY sp.name
                LIMIT ? OFFSET ?""",
            [*params, page_size, max(0, (page - 1) * page_size)],
        ).fetchall()

    overrides = offer_overrides()
    items = []
    for row in rows:
        clave = f"{row['store_code']}::{row['external_id'] or row['url']}"
        manual = overrides.get(clave, {})
        if edited_only and not manual:
            continue
        items.append(
            {
                "id": row["id"],
                "key": clave,
                "store_id": row["store_id"],
                "store": row["store_name"],
                "name": row["name"],
                "url": manual.get("url") or row["url"],
                "original_url": row["url"],
                "price": row["price"],
                "stock_status": row["stock_status"],
                "stock_label": STOCK_LABELS.get(row["stock_status"], row["stock_status"]),
                "language": row["language"],
                "language_name": attrs_module.language_name(row["language"]),
                "set_code": row["set_code"],
                "product_type": row["product_type"],
                "product_type_name": attrs_module.type_name(row["product_type"]),
                "sku": row["sku"],
                "product_id": row["product_id"],
                "product_name": row["product_name"],
                "stores_count": row["stores_count"] or 0,
                # Qué se ha tocado a mano: la pantalla lo marca para que se
                # distinga de lo que dedujo el sistema.
                "manual": manual,
                "last_seen_at": row["last_seen_at"],
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


# ---------------------------------------------------------------------------
# Comentarios
# ---------------------------------------------------------------------------
def comments(product_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    rows = query(
        """SELECT id, author, body, created_at FROM comments
           WHERE product_id = ? ORDER BY created_at DESC, id DESC LIMIT ?""",
        (product_id, limit),
    )
    return [dict(row) for row in rows]


def add_comment(product_id: int, body: str, author: Optional[str] = None) -> Dict[str, Any]:
    texto = (body or "").strip()
    if not texto:
        raise ValueError("El comentario está vacío")
    if len(texto) > 4000:
        raise ValueError("El comentario es demasiado largo (máximo 4.000 caracteres)")

    with transaction() as conn:
        existe = conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone()
        if existe is None:
            raise ValueError("Ese producto no existe")
        cur = conn.execute(
            "INSERT INTO comments (product_id, author, body) VALUES (?, ?, ?)",
            (product_id, (author or "").strip() or None, texto),
        )
        fila = conn.execute(
            "SELECT id, author, body, created_at FROM comments WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(fila)


def delete_comment(comment_id: int) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Portada
#
# Las secciones se arman con `search_products`, que ya sabe filtrar por juego
# y ordenar: así la portada respeta el TCG elegido en la barra superior sin
# duplicar consultas ni criterios.
# ---------------------------------------------------------------------------
def home(game: Optional[str] = None, per_section: int = 12) -> Dict[str, Any]:
    def seccion(**kwargs) -> List[Dict[str, Any]]:
        return search_products(game=game, page_size=per_section, **kwargs)["items"]

    # Categorías con producto del juego elegido: las vacías no se enseñan,
    # porque una portada llena de puertas cerradas no ayuda a nadie.
    conteos = {
        c["code"]: c["count"]
        for c in _type_counts(game)
    }
    categorias = [
        {"code": cat["code"], "name": cat["name"],
         "count": sum(conteos.get(t, 0) for t in cat.get("types") or [])}
        for cat in categorias_de_tipo()
    ]

    # Bajadas de precio de verdad, tomadas del registro de cambios. Una base
    # joven casi no tiene, así que la sección se completa con lo que más
    # difiere entre tiendas —que también es «dónde está la ganga»— y se avisa
    # de qué se está enseñando para no llamar bajada a lo que no lo es.
    bajadas = daily_deals(limit=per_section, game=game)
    origen = "drops"
    if len(bajadas) < 4:
        relleno = seccion(sort="discount", only_in_stock=True, min_stores=2)
        vistos = {p["id"] for p in bajadas}
        bajadas += [p for p in relleno if p["id"] not in vistos][: per_section - len(bajadas)]
        origen = "mixed" if any(p.get("drop_amount") for p in bajadas) else "spread"

    return {
        "game": game,
        "categories": [c for c in categorias if c["count"]],
        "viewed": most_viewed(limit=per_section, game=game),
        "deals": bajadas,
        "deals_source": origen,
        "recent": seccion(sort="new"),
    }


def _type_counts(game: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = """SELECT product_type AS code, COUNT(*) AS count FROM products
             WHERE product_type IS NOT NULL"""
    params: List[Any] = []
    if game:
        sql += " AND game = ?"
        params.append(game)
    sql += " GROUP BY product_type"
    return [dict(row) for row in query(sql, params)]


# ---------------------------------------------------------------------------
# Vistas y ofertas del día
# ---------------------------------------------------------------------------
def register_view(product_id: int) -> None:
    """Suma una visita a la ficha. Silencioso: nunca debe romper la consulta."""
    try:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO product_views (product_id, views, last_viewed_at)
                   VALUES (?, 1, datetime('now'))
                   ON CONFLICT(product_id) DO UPDATE SET
                       views = views + 1, last_viewed_at = datetime('now')""",
                (product_id,),
            )
    except Exception:  # noqa: BLE001 - una visita no contada no es un error
        pass


def most_viewed(limit: int = 12, game: Optional[str] = None) -> List[Dict[str, Any]]:
    """Las fichas más abiertas, con las recientes por delante a igual cuenta."""
    sql = """SELECT p.*, CASE WHEN f.product_id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
                    bs.name AS best_store_name, bas.name AS best_available_store_name,
                    v.views
             FROM product_views v
             JOIN products p ON p.id = v.product_id
             LEFT JOIN favorites f ON f.product_id = p.id
             LEFT JOIN stores bs ON bs.id = p.best_store_id
             LEFT JOIN stores bas ON bas.id = p.best_available_store_id
             WHERE v.views > 0"""
    params: List[Any] = []
    if game:
        sql += " AND p.game = ?"
        params.append(game)
    sql += " ORDER BY v.views DESC, v.last_viewed_at DESC LIMIT ?"
    params.append(limit)

    salida = []
    for row in query(sql, params):
        item = _product_summary(dict(row))
        item["views"] = row["views"]
        item["badge"] = f"{row['views']} {'visita' if row['views'] == 1 else 'visitas'}"
        salida.append(item)
    return salida


def daily_deals(limit: int = 12, game: Optional[str] = None, days: int = 7) -> List[Dict[str, Any]]:
    """Productos que han bajado de precio, con cuánto bajaron.

    Sale del registro de cambios, no de comparar tiendas: aquí lo que importa
    es que el precio de una ficha es hoy más bajo que antes.
    """
    sql = f"""SELECT p.*, CASE WHEN f.product_id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
                     bs.name AS best_store_name, bas.name AS best_available_store_name,
                     -- old_value/new_value son TEXT (guardan también estados de
                     -- stock, no solo precios): hay que convertirlos. El filtro
                     -- por type = 'price_drop' garantiza que aquí son números.
                     MAX(CAST(e.old_value AS REAL) - CAST(e.new_value AS REAL)) AS bajada
              FROM events e
              JOIN store_products sp ON sp.id = e.store_product_id AND sp.is_active = 1
              JOIN products p ON p.id = sp.product_id
              LEFT JOIN favorites f ON f.product_id = p.id
              LEFT JOIN stores bs ON bs.id = p.best_store_id
              LEFT JOIN stores bas ON bas.id = p.best_available_store_id
              WHERE e.type = 'price_drop'
                AND e.created_at >= datetime('now', '-{int(days)} days')
                AND e.old_value IS NOT NULL AND e.new_value IS NOT NULL
                AND p.in_stock_count > 0"""
    params: List[Any] = []
    if game:
        sql += " AND p.game = ?"
        params.append(game)
    # Postgres exige agrupar por todo lo que no sea agregado; las columnas de
    # `p` las cubre su clave primaria, pero las de los JOIN hay que nombrarlas.
    sql += (" GROUP BY p.id, f.product_id, bs.name, bas.name"
            " ORDER BY bajada DESC LIMIT ?")
    params.append(limit)

    salida = []
    for row in query(sql, params):
        item = _product_summary(dict(row))
        item["drop_amount"] = row["bajada"]
        item["badge"] = "Bajó $" + f"{int(row['bajada']):,}".replace(",", ".")
        salida.append(item)
    return salida
