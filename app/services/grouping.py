"""Agrupación de ofertas en productos maestros.

Se re-ejecuta completa después de cada actualización: es determinística, así
que siempre produce el mismo resultado para los mismos datos, y respeta las
decisiones manuales guardadas.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import settings
from app.core import attributes as attrs_module
from app.core.attributes import ProductAttributes, build_canonical_name
from app.core.matching import (
    METHOD_MANUAL,
    METHOD_SINGLETON,
    Candidate,
    MatchEngine,
    PairScore,
    ScoringConfig,
    stable_key,
)
from app.db.database import Lote, get_connection, log, transaction


# ---------------------------------------------------------------------------
# Carga de candidatos
# ---------------------------------------------------------------------------
def load_candidates() -> List[Candidate]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT sp.*, s.code AS store_code
               FROM store_products sp
               JOIN stores s ON s.id = sp.store_id
               WHERE sp.is_active = 1"""
        ).fetchall()

        identifiers = conn.execute(
            """SELECT pi.store_product_id, pi.kind, pi.normalized_value
               FROM product_identifiers pi
               JOIN store_products sp ON sp.id = pi.store_product_id
               WHERE sp.is_active = 1 AND pi.kind IN ('gtin', 'mpn')"""
        ).fetchall()

    gtins: Dict[int, set] = defaultdict(set)
    mpns: Dict[int, set] = defaultdict(set)
    for row in identifiers:
        target = gtins if row["kind"] == "gtin" else mpns
        target[row["store_product_id"]].add(row["normalized_value"])

    candidates: List[Candidate] = []
    for row in rows:
        try:
            tokens = json.loads(row["tokens"]) or []
        except (json.JSONDecodeError, TypeError):
            tokens = []
        if not tokens:
            tokens = (row["normalized_name"] or row["name"] or "").split()

        candidates.append(
            Candidate(
                id=row["id"],
                store_id=row["store_id"],
                store_code=row["store_code"],
                key=stable_key(row["store_code"], row["external_id"], row["url"]),
                name=row["name"],
                tokens=tokens,
                name_key=row["name_key"] or "",
                game=row["game"],
                set_code=row["set_code"],
                product_type=row["product_type"],
                units_total=row["units_total"],
                quantity_confidence=float(row["quantity_confidence"] or 0.0),
                language=row["language"],
                gtins=gtins.get(row["id"], set()),
                mpns=mpns.get(row["id"], set()),
            )
        )
    return candidates


def load_manual_decisions() -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT a_key, b_key, decision FROM manual_matches").fetchall()
    same = [(r["a_key"], r["b_key"]) for r in rows if r["decision"] == "same"]
    different = [(r["a_key"], r["b_key"]) for r in rows if r["decision"] == "different"]
    return same, different


# ---------------------------------------------------------------------------
# Atributos del producto maestro (voto mayoritario entre sus ofertas)
# ---------------------------------------------------------------------------
def _majority(values: Sequence[Optional[Any]]) -> Optional[Any]:
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return None
    return Counter(present).most_common(1)[0][0]


def _master_attributes(members: Sequence[Dict[str, Any]]) -> ProductAttributes:
    game = _majority([m["game"] for m in members])
    set_code = _majority([m["set_code"] for m in members])
    product_type = _majority([m["product_type"] for m in members])
    language = _majority([m["language"] for m in members])
    multiplier = _majority([m["quantity"] for m in members]) or 1

    # Para las unidades solo consideramos las que se extrajeron con confianza.
    min_conf = float(settings.get("unit_price.min_confidence", 0.7))
    confident = [
        m["units_total"]
        for m in members
        if m["units_total"] and float(m["quantity_confidence"] or 0) >= min_conf
    ]
    units_total = _majority(confident) or _majority([m["units_total"] for m in members])

    attrs = ProductAttributes(
        game=game,
        game_name=attrs_module.game_name(game),
        set_code=set_code,
        set_name=attrs_module.set_name(game, set_code),
        product_type=product_type,
        product_type_name=attrs_module.type_name(product_type),
        multiplier=int(multiplier or 1),
        units_total=int(units_total) if units_total else None,
        quantity_confidence=max((float(m["quantity_confidence"] or 0) for m in members), default=0.0),
        language=language,
    )

    type_entry = next(
        (t for t in attrs_module.all_types() if t["code"] == product_type), None
    )
    attrs.unit_name = type_entry.get("unit_name") if type_entry else None
    return attrs


# ---------------------------------------------------------------------------
# Reconstrucción de productos maestros
# ---------------------------------------------------------------------------
# Puntajes de la última pasada. Comparar decenas de miles de pares cuesta
# segundos; las decisiones manuales no cambian ningún puntaje, así que se
# reutilizan mientras las ofertas y la configuración sigan igual.
_score_cache: Dict[str, Any] = {"fingerprint": None, "pairs": None}


def _fingerprint(candidates: Sequence[Candidate], cfg: ScoringConfig) -> str:
    """Huella de todo lo que influye en el puntaje de un par."""
    h = hashlib.sha256()
    for c in sorted(candidates, key=lambda x: x.id):
        h.update(
            f"{c.id}|{c.name_key}|{c.set_code}|{c.product_type}|{c.language}|"
            f"{c.units_total}|{c.quantity_confidence}|"
            f"{','.join(sorted(c.gtins))}|{','.join(sorted(c.mpns))}\n".encode()
        )
    h.update(repr(sorted(cfg.__dict__.items())).encode())
    return h.hexdigest()


def invalidate_score_cache() -> None:
    _score_cache.update({"fingerprint": None, "pairs": None})


def rebuild_groups() -> Dict[str, Any]:
    started = time.monotonic()
    candidates = load_candidates()
    if not candidates:
        return {"products": 0, "offers": 0, "reviews": 0, "duration_ms": 0}

    manual_same, manual_different = load_manual_decisions()
    cfg = ScoringConfig.from_settings()
    engine = MatchEngine(cfg)

    huella = _fingerprint(candidates, cfg)
    reutilizados = _score_cache["pairs"] if _score_cache["fingerprint"] == huella else None
    result = engine.run(candidates, manual_same, manual_different, precomputed=reutilizados)
    _score_cache.update({"fingerprint": huella, "pairs": result.scored})

    for a_key, b_key in getattr(engine, "conflicts", []):
        log(
            "warn",
            "matching",
            f"Decisión manual contradictoria: «{a_key}» y «{b_key}» están marcados como "
            f"el mismo producto, pero otra decisión los separa. Manda la separación.",
        )

    with get_connection() as conn:
        member_rows = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM store_products WHERE is_active = 1"
            ).fetchall()
        }

    new_matches = 0
    with transaction() as conn:
        lote = Lote(conn)
        # --- reutilizar los IDs de producto maestro existentes -------------
        claimed: set = set()
        assignments: List[Tuple[List[int], Optional[int]]] = []
        for group in result.groups:
            counts = Counter(
                member_rows[sp_id]["product_id"]
                for sp_id in group
                if member_rows.get(sp_id) and member_rows[sp_id]["product_id"]
            )
            reuse = None
            for product_id, _count in counts.most_common():
                if product_id not in claimed:
                    reuse = product_id
                    claimed.add(product_id)
                    break
            assignments.append((group, reuse))

        for group, reuse_id in assignments:
            members = [member_rows[sp_id] for sp_id in group if sp_id in member_rows]
            if not members:
                continue

            attrs = _master_attributes(members)
            fallback = min((m["name"] for m in members), key=len)
            canonical = build_canonical_name(attrs, fallback)
            normalized = _majority([m["normalized_name"] for m in members]) or ""
            image = next((m["image_url"] for m in members if m["image_url"]), None)

            if reuse_id is not None:
                product_id = reuse_id
                lote.execute(
                    """UPDATE products SET
                           canonical_name = ?, display_name = ?, normalized_name = ?,
                           game = ?, set_code = ?, set_name = ?,
                           product_type = ?, product_type_name = ?,
                           quantity = ?, units_total = ?, unit_name = ?, language = ?,
                           image_url = COALESCE(?, image_url),
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (
                        canonical, canonical, normalized, attrs.game, attrs.set_code,
                        attrs.set_name, attrs.product_type, attrs.product_type_name,
                        attrs.multiplier, attrs.units_total, attrs.unit_name,
                        attrs.language, image, product_id,
                    ),
                )
            else:
                product_id = conn.execute(
                    """INSERT INTO products (
                           canonical_name, display_name, normalized_name, game,
                           set_code, set_name, product_type, product_type_name,
                           quantity, units_total, unit_name, language, image_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        canonical, canonical, normalized, attrs.game, attrs.set_code,
                        attrs.set_name, attrs.product_type, attrs.product_type_name,
                        attrs.multiplier, attrs.units_total, attrs.unit_name,
                        attrs.language, image,
                    ),
                ).lastrowid

            for sp_id in group:
                decision = result.decisions.get(sp_id)
                score = decision.score if decision else 100.0
                method = decision.method if decision else METHOD_SINGLETON
                breakdown = json.dumps(decision.breakdown, ensure_ascii=False) if decision else None
                anchor = (
                    decision.a_id if decision and decision.a_id != sp_id else
                    (decision.b_id if decision else None)
                )

                previous = member_rows.get(sp_id, {}).get("product_id")
                if previous != product_id and len(group) > 1:
                    new_matches += 1

                lote.execute(
                    """UPDATE store_products
                       SET product_id = ?, match_score = ?, match_method = ?
                       WHERE id = ?""",
                    (product_id, score, method, sp_id),
                )
                lote.execute(
                    """INSERT INTO product_matches
                           (product_id, store_product_id, score, method, breakdown, anchor_id)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(store_product_id) DO UPDATE SET
                           product_id = excluded.product_id,
                           score = excluded.score,
                           method = excluded.method,
                           breakdown = excluded.breakdown,
                           anchor_id = excluded.anchor_id""",
                    (product_id, sp_id, score, method, breakdown, anchor),
                )

        # El DELETE de abajo mira `store_products.product_id`, que se acaba de
        # asignar en el bucle: hay que vaciar el lote antes o borraría lo que
        # todavía no se ha escrito.
        lote.flush()

        # --- productos maestros que se quedaron sin ofertas ----------------
        conn.execute(
            """DELETE FROM products
               WHERE id NOT IN (SELECT DISTINCT product_id FROM store_products
                                WHERE product_id IS NOT NULL AND is_active = 1)"""
        )

    reviews_added = _sync_reviews(result.reviews)
    recompute_aggregates()

    duration_ms = int((time.monotonic() - started) * 1000)
    with get_connection() as conn:
        products = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]

    origen = "puntajes reutilizados" if result.from_cache else "puntajes recalculados"
    log(
        "info",
        "matching",
        f"{len(candidates)} ofertas procesadas · {products} productos maestros · "
        f"{new_matches} agrupaciones nuevas · {reviews_added} pendientes de revisión "
        f"({result.compared_pairs} pares, {origen}, {duration_ms / 1000:.1f}s)",
    )

    return {
        "products": products,
        "offers": len(candidates),
        "groups": len(result.groups),
        "new_matches": new_matches,
        "reviews": reviews_added,
        "compared_pairs": result.compared_pairs,
        "from_cache": result.from_cache,
        "duration_ms": duration_ms,
    }


def _sync_reviews(reviews) -> int:
    """Sincroniza la cola de revisión con el resultado del matching.

    La cola debe reflejar el estado ACTUAL: además de añadir los pares nuevos,
    retira los que ya no se generan. Si solo se añadiera, un par dejaría de
    tener sentido y seguiría preguntándose para siempre — por ejemplo tras
    declarar el idioma de una oferta, que convierte al par en incompatible.
    """
    vigentes = {tuple(sorted((p.a_id, p.b_id))) for p in reviews}
    added = 0
    with transaction() as conn:
        # --- pendientes que el matching ya no propone -------------------
        obsoletos = [
            row["id"]
            for row in conn.execute(
                "SELECT id, a_id, b_id FROM match_reviews WHERE status = 'pending'"
            ).fetchall()
            if tuple(sorted((row["a_id"], row["b_id"]))) not in vigentes
        ]
        for lote in range(0, len(obsoletos), 400):
            trozo = obsoletos[lote : lote + 400]
            conn.execute(
                f"DELETE FROM match_reviews WHERE id IN ({','.join('?' * len(trozo))})",
                trozo,
            )

        # Los pendientes que ya están en el mismo grupo dejan de tener sentido.
        conn.execute(
            """DELETE FROM match_reviews
               WHERE status = 'pending' AND a_id IN (
                   SELECT sp1.id FROM store_products sp1
                   JOIN store_products sp2 ON sp1.product_id = sp2.product_id
                   WHERE sp2.id = match_reviews.b_id)"""
        )
        conn.execute(
            "DELETE FROM match_reviews WHERE status = 'pending' AND "
            "(a_id NOT IN (SELECT id FROM store_products WHERE is_active = 1) OR "
            " b_id NOT IN (SELECT id FROM store_products WHERE is_active = 1))"
        )

        for pair in reviews:
            a_id, b_id = sorted((pair.a_id, pair.b_id))
            cur = conn.execute(
                """INSERT INTO match_reviews (a_id, b_id, score, method, breakdown)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(a_id, b_id) DO UPDATE SET
                       score = excluded.score,
                       breakdown = excluded.breakdown
                   WHERE match_reviews.status = 'pending'""",
                (a_id, b_id, pair.score, pair.method,
                 json.dumps(pair.breakdown, ensure_ascii=False)),
            )
            if cur.rowcount:
                added += 1
    return added


# ---------------------------------------------------------------------------
# Agregados denormalizados (mejor precio, stock, etc.)
# ---------------------------------------------------------------------------
def recompute_aggregates() -> None:
    """Recalcula mejor precio, stock y demás agregados de cada producto.

    Se hace en UNA pasada sobre las ofertas y un UPDATE por producto. La
    versión anterior usaba subconsultas correlacionadas —ocho por producto—
    y se llevaba sola casi todo el tiempo de la reagrupación: 14,7 s de 14,7.
    """
    available = set(settings.get("stock.available_states", ["in_stock", "preorder"]) or [])

    with get_connection() as conn:
        ofertas = conn.execute(
            """SELECT product_id, store_id, price, stock_status, last_seen_at
               FROM store_products
               WHERE is_active = 1 AND product_id IS NOT NULL
               ORDER BY product_id, (price IS NULL), price"""
        ).fetchall()

    resumen: Dict[int, Dict[str, Any]] = {}
    for row in ofertas:
        dato = resumen.setdefault(
            row["product_id"],
            {
                "offers": 0, "stores": set(), "in_stock": 0,
                "precios": [], "best_store": None,
                "best_avail": None, "best_avail_store": None,
                "last_seen": None,
            },
        )
        dato["offers"] += 1
        dato["stores"].add(row["store_id"])
        disponible = row["stock_status"] in available
        if disponible:
            dato["in_stock"] += 1
        if row["price"] is not None:
            # Las filas vienen ordenadas por precio: la primera es la más barata.
            dato["precios"].append(row["price"])
            if dato["best_store"] is None:
                dato["best_store"] = row["store_id"]
            if disponible and dato["best_avail"] is None:
                dato["best_avail"] = row["price"]
                dato["best_avail_store"] = row["store_id"]
        if row["last_seen_at"] and (
            dato["last_seen"] is None or row["last_seen_at"] > dato["last_seen"]
        ):
            dato["last_seen"] = row["last_seen_at"]

    filas = [
        (
            d["offers"], len(d["stores"]), d["in_stock"],
            d["precios"][0] if d["precios"] else None,
            d["best_store"],
            d["best_avail"], d["best_avail_store"],
            d["precios"][-1] if d["precios"] else None,
            (sum(d["precios"]) / len(d["precios"])) if d["precios"] else None,
            d["last_seen"], product_id,
        )
        for product_id, d in resumen.items()
    ]

    with transaction() as conn:
        conn.executemany(
            """UPDATE products SET
                   offers_count = ?, stores_count = ?, in_stock_count = ?,
                   best_price = ?, best_store_id = ?,
                   best_available_price = ?, best_available_store_id = ?,
                   worst_price = ?, avg_price = ?,
                   last_scraped_at = ?, updated_at = datetime('now')
               WHERE id = ?""",
            filas,
        )
        # Productos que se quedaron sin ofertas activas.
        conn.execute(
            """UPDATE products SET offers_count = 0, stores_count = 0, in_stock_count = 0,
                   best_price = NULL, best_store_id = NULL,
                   best_available_price = NULL, best_available_store_id = NULL,
                   worst_price = NULL, avg_price = NULL
               WHERE id NOT IN (SELECT DISTINCT product_id FROM store_products
                                WHERE is_active = 1 AND product_id IS NOT NULL)"""
        )


# ---------------------------------------------------------------------------
# Acciones manuales
# ---------------------------------------------------------------------------
def _keys_for(store_product_ids: Sequence[int]) -> Dict[int, str]:
    if not store_product_ids:
        return {}
    placeholders = ",".join("?" * len(store_product_ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT sp.id, sp.external_id, sp.url, s.code AS store_code
                FROM store_products sp JOIN stores s ON s.id = sp.store_id
                WHERE sp.id IN ({placeholders})""",
            tuple(store_product_ids),
        ).fetchall()
    return {
        row["id"]: stable_key(row["store_code"], row["external_id"], row["url"])
        for row in rows
    }


def save_manual_decision(a_id: int, b_id: int, decision: str, note: Optional[str] = None) -> None:
    """Guarda 'son el mismo producto' o 'son productos distintos' para siempre."""
    keys = _keys_for([a_id, b_id])
    if len(keys) < 2:
        raise ValueError("Alguna de las ofertas ya no existe")
    a_key, b_key = sorted((keys[a_id], keys[b_id]))
    with transaction() as conn:
        conn.execute(
            """INSERT INTO manual_matches (a_key, b_key, decision, note)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(a_key, b_key) DO UPDATE SET
                   decision = excluded.decision,
                   note = excluded.note,
                   created_at = datetime('now')""",
            (a_key, b_key, decision, note),
        )


def set_manual_attribute(
    store_product_id: int,
    attribute: str,
    value: Optional[str],
    regroup: bool = True,
) -> Dict[str, Any]:
    """Corrige a mano un atributo de una oferta y reagrupa.

    `value=None` borra la corrección y devuelve el atributo a la detección
    automática. La decisión se guarda contra la clave estable de la oferta,
    así que sobrevive a los siguientes scrapings.
    """
    # `url` es un caso aparte: NO se toca la columna `url`, que es la identidad
    # de la oferta. Si se cambiara, el siguiente scraping no reconocería la
    # ficha y la volvería a dar de alta como producto nuevo. La corrección se
    # guarda al margen y solo cambia el enlace que se enseña.
    editables = ("language", "set_code", "product_type", "url")
    if attribute not in editables:
        raise ValueError(f"Atributo no editable: {attribute}")

    keys = _keys_for([store_product_id])
    key = keys.get(store_product_id)
    if key is None:
        raise ValueError("La oferta ya no existe")

    if attribute == "url" and value:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("El enlace tiene que empezar por http:// o https://")

    with transaction() as conn:
        if value is None:
            conn.execute(
                "DELETE FROM manual_attributes WHERE entity_key = ? AND attribute = ?",
                (key, attribute),
            )
        else:
            conn.execute(
                """INSERT INTO manual_attributes (entity_key, attribute, value, note)
                   VALUES (?, ?, ?, 'Corregido a mano')
                   ON CONFLICT(entity_key, attribute) DO UPDATE SET
                       value = excluded.value, created_at = datetime('now')""",
                (key, attribute, value),
            )
            if attribute != "url":
                # Efecto inmediato, sin esperar al próximo scraping.
                conn.execute(
                    f"UPDATE store_products SET {attribute} = ? WHERE id = ?",
                    (value, store_product_id),
                )

    if value is None and attribute != "url":
        # Al revertir hay que recalcular el atributo desde el nombre.
        _reextract_attribute(store_product_id, attribute)

    # El enlace no interviene en la agrupación: no hace falta reagrupar.
    if attribute == "url":
        return {"saved": True}
    return rebuild_groups() if regroup else {"saved": True}


def rejoin_offer(store_product_id: int) -> Dict[str, Any]:
    """Deshace las separaciones manuales de una oferta.

    `split_offer` guarda un «son distintos» contra cada compañera del grupo.
    Esto borra todos esos registros de esta oferta, de modo que vuelva a
    agruparse por parecido como cualquier otra. No toca los «son el mismo».
    """
    keys = _keys_for([store_product_id])
    key = keys.get(store_product_id)
    if key is None:
        raise ValueError("La oferta ya no existe")

    with transaction() as conn:
        cur = conn.execute(
            """DELETE FROM manual_matches
               WHERE decision = 'different' AND (a_key = ? OR b_key = ?)""",
            (key, key),
        )
        borradas = cur.rowcount

    result = rebuild_groups()
    result["forgotten"] = borradas
    return result


def _reextract_attribute(store_product_id: int, attribute: str) -> None:
    """Vuelve a deducir un atributo del nombre tras borrar la corrección."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT sp.name, sp.description, s.code AS store_code
               FROM store_products sp JOIN stores s ON s.id = sp.store_id
               WHERE sp.id = ?""",
            (store_product_id,),
        ).fetchone()
    if row is None:
        return

    from app.core.normalize import normalize_name
    from app.services.ingest import store_config

    cfg = store_config({"code": row["store_code"]})
    extracted = attrs_module.extract(
        normalize_name(row["name"]),
        description=row["description"],
        game_hint=cfg.get("default_game"),
        language_hint=cfg.get("default_language"),
    )
    valor = getattr(extracted, attribute, None)
    with transaction() as conn:
        conn.execute(
            f"UPDATE store_products SET {attribute} = ? WHERE id = ?",
            (valor, store_product_id),
        )


def forget_decision_by_id(decision_id: int) -> Dict[str, Any]:
    """Deshace una decisión manual a partir de su id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM manual_matches WHERE id = ?", (decision_id,)
        ).fetchone()
    if row is None:
        raise ValueError("Esa decisión ya no existe")

    with transaction() as conn:
        conn.execute("DELETE FROM manual_matches WHERE id = ?", (decision_id,))
        # El par vuelve a la cola de revisión en la próxima reagrupación.
        conn.execute(
            """UPDATE match_reviews SET status = 'pending', decided_at = NULL
               WHERE id IN (
                   SELECT r.id FROM match_reviews r
                   JOIN store_products a ON a.id = r.a_id
                   JOIN store_products b ON b.id = r.b_id
                   JOIN stores sa ON sa.id = a.store_id
                   JOIN stores sb ON sb.id = b.store_id
                   WHERE (sa.code || '::' || COALESCE(a.external_id, a.url) = ?
                          AND sb.code || '::' || COALESCE(b.external_id, b.url) = ?)
                      OR (sa.code || '::' || COALESCE(a.external_id, a.url) = ?
                          AND sb.code || '::' || COALESCE(b.external_id, b.url) = ?))""",
            (row["a_key"], row["b_key"], row["b_key"], row["a_key"]),
        )
    return {"forgotten": decision_id, "decision": row["decision"]}


def manual_attributes(limit: int = 200) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_attributes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def forget_manual_decision(a_id: int, b_id: int) -> bool:
    keys = _keys_for([a_id, b_id])
    if len(keys) < 2:
        return False
    a_key, b_key = sorted((keys[a_id], keys[b_id]))
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM manual_matches WHERE a_key = ? AND b_key = ?", (a_key, b_key)
        )
    return cur.rowcount > 0


def split_offer(store_product_id: int) -> Dict[str, Any]:
    """Separa una oferta de su producto maestro y registra la decisión.

    La separación se guarda contra el resto de las ofertas del grupo, de modo
    que el matching no vuelva a unirlas en la siguiente actualización.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT product_id FROM store_products WHERE id = ?", (store_product_id,)
        ).fetchone()
        if row is None or row["product_id"] is None:
            raise ValueError("La oferta no pertenece a ningún producto maestro")
        siblings = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM store_products WHERE product_id = ? AND id != ? AND is_active = 1",
                (row["product_id"], store_product_id),
            ).fetchall()
        ]

    for sibling in siblings:
        save_manual_decision(store_product_id, sibling, "different", "Separado manualmente")

    rebuild_groups()
    return {"separated_from": len(siblings)}


def _comprobar_identidad(product_id: int, other_id: int) -> None:
    """Rechaza de antemano las uniones que el agrupador no va a aceptar.

    Expansión, tipo de producto e idioma son identidad: dos artículos que
    difieren en cualquiera de ellos NO son el mismo, y el agrupador los
    mantiene separados incluso ante una decisión manual. Sin esta comprobación
    la unión se guardaba, no surtía efecto, y el usuario se quedaba sin saber
    por qué.

    El idioma solo cuenta si `matching.language_is_identity` está activo, que
    es lo normal: un ETB en español y uno en inglés valen distinto.
    """
    with get_connection() as conn:
        filas = {
            f["id"]: dict(f) for f in conn.execute(
                """SELECT id, display_name, set_code, set_name, product_type,
                          product_type_name, language
                   FROM products WHERE id IN (?, ?)""",
                (product_id, other_id),
            ).fetchall()
        }
    a, b = filas.get(product_id), filas.get(other_id)
    if not a or not b:
        raise ValueError("Alguno de los dos productos ya no existe")

    campos = [("set_code", "la expansión", "set_name"),
              ("product_type", "el tipo de producto", "product_type_name")]
    if settings.get("matching.language_is_identity", True):
        campos.append(("language", "el idioma", "language"))

    for campo, etiqueta, mostrar in campos:
        if a[campo] and b[campo] and a[campo] != b[campo]:
            izq = attrs_module.language_name(a[campo]) if campo == "language" else (a.get(mostrar) or a[campo])
            der = attrs_module.language_name(b[campo]) if campo == "language" else (b.get(mostrar) or b[campo])
            raise ValueError(
                f"No se pueden unir: no coincide {etiqueta} ({izq} contra {der}). "
                f"Son artículos distintos y el agrupador los mantendría separados igualmente. "
                f"Si de verdad es el mismo, corrige antes ese dato en Administración → Productos."
            )


def merge_products(product_id: int, other_id: int) -> Dict[str, Any]:
    """Une dos productos maestros en uno solo, para siempre.

    Por dentro no existe «unir productos»: lo que se guarda es que una oferta
    de uno y una del otro son el mismo artículo. El agrupador ya mantiene
    juntas las ofertas de cada producto, así que enlazar una de cada lado
    arrastra a las demás. Y como se guarda contra claves estables, la unión
    sobrevive a los siguientes scrapings.
    """
    if product_id == other_id:
        raise ValueError("Es el mismo producto")

    _comprobar_identidad(product_id, other_id)

    with get_connection() as conn:
        def ofertas(pid: int) -> List[Dict[str, Any]]:
            return [
                dict(r) for r in conn.execute(
                    """SELECT sp.id, sp.name, s.code AS store_code, sp.external_id, sp.url
                       FROM store_products sp JOIN stores s ON s.id = sp.store_id
                       WHERE sp.product_id = ? AND sp.is_active = 1
                       ORDER BY sp.id""",
                    (pid,),
                ).fetchall()
            ]

        aqui, alla = ofertas(product_id), ofertas(other_id)
        nombres = {
            r["id"]: r["display_name"] for r in conn.execute(
                "SELECT id, display_name FROM products WHERE id IN (?, ?)",
                (product_id, other_id),
            ).fetchall()
        }

    if not aqui or not alla:
        raise ValueError("Alguno de los dos productos no tiene ofertas activas")

    claves_aqui = {
        stable_key(o["store_code"], o["external_id"], o["url"]) for o in aqui
    }
    claves_alla = {
        stable_key(o["store_code"], o["external_id"], o["url"]) for o in alla
    }

    # Si antes se marcaron como distintos, esa decisión gana sobre la unión y
    # el «unir» no haría nada. Al pedir explícitamente juntarlos, se retira.
    olvidadas = 0
    with transaction() as conn:
        for a in claves_aqui:
            for b in claves_alla:
                x, y = sorted((a, b))
                cur = conn.execute(
                    "DELETE FROM manual_matches "
                    "WHERE a_key = ? AND b_key = ? AND decision = 'different'",
                    (x, y),
                )
                olvidadas += cur.rowcount or 0

    save_manual_decision(aqui[0]["id"], alla[0]["id"], "same", "Unidos manualmente")
    matching = rebuild_groups()

    # Comprobar que de verdad quedaron juntos: una separación manual entre
    # otras ofertas del grupo podría estar impidiéndolo.
    with get_connection() as conn:
        fila = conn.execute(
            "SELECT product_id FROM store_products WHERE id = ?", (alla[0]["id"],)
        ).fetchone()
        destino = fila["product_id"] if fila else None
        final = conn.execute(
            "SELECT product_id FROM store_products WHERE id = ?", (aqui[0]["id"],)
        ).fetchone()
        unidos = bool(destino and final and destino == final["product_id"])

    log("info", "matching",
        f"Unidos «{nombres.get(product_id, product_id)}» y "
        f"«{nombres.get(other_id, other_id)}»"
        + (f" (se olvidaron {olvidadas} separaciones)" if olvidadas else ""))

    return {
        "merged": unidos,
        "product_id": destino,
        "offers": len(aqui) + len(alla),
        "forgotten_separations": olvidadas,
        "matching": matching,
    }


def _manual_same_components() -> Dict[str, str]:
    """Componentes conexas de las decisiones «es el mismo producto».

    Si marcaste A=B y B=C, los tres pertenecen al mismo producto aunque nunca
    hayas comparado A con C. Esto lo calcula sobre las claves estables, así
    que vale igual entre tiendas distintas.
    """
    padre: Dict[str, str] = {}

    def raiz(x: str) -> str:
        padre.setdefault(x, x)
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    with get_connection() as conn:
        for row in conn.execute(
            "SELECT a_key, b_key FROM manual_matches WHERE decision = 'same'"
        ):
            ra, rb = raiz(row["a_key"]), raiz(row["b_key"])
            if ra != rb:
                padre[rb] = ra

    return {clave: raiz(clave) for clave in list(padre)}


def close_implied_reviews() -> List[Dict[str, Any]]:
    """Cierra los pares pendientes que ya se deducen de tus decisiones.

    Tras marcar A=B y B=C, el par A–C sobra: la respuesta ya está dada. Se
    resuelve aquí, en el acto, sin esperar a la reagrupación completa.
    """
    componentes = _manual_same_components()
    if not componentes:
        return []

    with get_connection() as conn:
        filas = conn.execute(
            """SELECT r.id, r.a_id, r.b_id,
                      sa.code || '::' || COALESCE(a.external_id, a.url) AS a_key,
                      sb.code || '::' || COALESCE(b.external_id, b.url) AS b_key,
                      a.name AS a_name, b.name AS b_name
               FROM match_reviews r
               JOIN store_products a ON a.id = r.a_id
               JOIN store_products b ON b.id = r.b_id
               JOIN stores sa ON sa.id = a.store_id
               JOIN stores sb ON sb.id = b.store_id
               WHERE r.status = 'pending'"""
        ).fetchall()

    implicados = [
        row
        for row in filas
        if componentes.get(row["a_key"])
        and componentes.get(row["a_key"]) == componentes.get(row["b_key"])
    ]
    if not implicados:
        return []

    with transaction() as conn:
        conn.executemany(
            """UPDATE match_reviews
               SET status = 'confirmed', decided_at = datetime('now')
               WHERE id = ?""",
            [(row["id"],) for row in implicados],
        )

    return [
        {"a_id": row["a_id"], "b_id": row["b_id"],
         "a_name": row["a_name"], "b_name": row["b_name"]}
        for row in implicados
    ]


def decide_pair(a_id: int, b_id: int, decision: str) -> Dict[str, Any]:
    """Guarda la decisión del usuario. NO reagrupa: eso se hace aparte.

    Reagrupar recorre todo el catálogo y tarda varios segundos. Al revisar
    pares en serie eso se sufre en cada clic, así que la decisión se guarda
    al instante —que es lo único irreversible— y la reagrupación se agenda
    para cuando el usuario deje de marcar.
    """
    if decision not in ("same", "different"):
        raise ValueError(f"Decisión no válida: {decision}")

    nota = "Confirmado manualmente" if decision == "same" else "Rechazado manualmente"
    save_manual_decision(a_id, b_id, decision, nota)

    estado = "confirmed" if decision == "same" else "rejected"
    with transaction() as conn:
        conn.execute(
            """UPDATE match_reviews SET status = ?, decided_at = datetime('now')
               WHERE (a_id = ? AND b_id = ?) OR (a_id = ? AND b_id = ?)""",
            (estado, a_id, b_id, b_id, a_id),
        )

    # Al confirmar «mismo producto» pueden quedar respondidos otros pares por
    # transitividad: si ya tenías A=B y ahora marcas B=C, el par A–C sobra.
    implicados = close_implied_reviews() if decision == "same" else []

    with get_connection() as conn:
        pendientes = conn.execute(
            "SELECT COUNT(*) AS n FROM match_reviews WHERE status = 'pending'"
        ).fetchone()["n"]

    return {
        "decision": decision,
        "a_id": a_id,
        "b_id": b_id,
        "pending": pendientes,
        "implied": implicados,
    }


def merge_offers(a_id: int, b_id: int) -> Dict[str, Any]:
    result = decide_pair(a_id, b_id, "same")
    result["matching"] = rebuild_groups()
    return result


def reject_pair(a_id: int, b_id: int) -> Dict[str, Any]:
    result = decide_pair(a_id, b_id, "different")
    result["matching"] = rebuild_groups()
    return result
