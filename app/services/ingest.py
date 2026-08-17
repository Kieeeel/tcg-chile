"""Scraping + persistencia + detección de cambios de una tienda."""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app import settings
from app.core import attributes as attrs_module
from app.core.matching import normalize_gtin, normalize_mpn, stable_key
from app.core.normalize import normalize_name
from app.db.database import Lote, get_connection, log, transaction
from app.scrapers.base import RawProduct
from app.scrapers.http_client import HttpClient
from app.scrapers.registry import build_adapter


# ---------------------------------------------------------------------------
# Sincronización de tiendas desde config/stores/*.yaml
# ---------------------------------------------------------------------------
def sync_stores_from_config() -> int:
    """Crea o actualiza las tiendas declaradas en YAML. Devuelve cuántas hay."""
    configs = settings.load_store_configs()
    with transaction() as conn:
        for cfg in configs:
            if not cfg.get("code") or not cfg.get("base_url"):
                continue
            conn.execute(
                """INSERT INTO stores (code, name, base_url, adapter, config_file,
                                       currency, enabled, country, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(code) DO UPDATE SET
                       name = excluded.name,
                       base_url = excluded.base_url,
                       adapter = excluded.adapter,
                       config_file = excluded.config_file,
                       currency = excluded.currency,
                       country = excluded.country,
                       updated_at = datetime('now')""",
                (
                    cfg["code"],
                    cfg.get("name", cfg["code"]),
                    cfg["base_url"],
                    cfg.get("adapter", "html"),
                    cfg.get("_config_file"),
                    cfg.get("currency", settings.get("app.currency", "CLP")),
                    1 if cfg.get("enabled", True) else 0,
                    cfg.get("country"),
                ),
            )
    return len(configs)


def store_config(store: Dict[str, Any]) -> Dict[str, Any]:
    for cfg in settings.load_store_configs():
        if cfg.get("code") == store["code"]:
            return cfg
    return {}


# ---------------------------------------------------------------------------
# Normalización de un producto crudo
# ---------------------------------------------------------------------------
_CODE_LABEL = re.compile(r"\D+")


def _clean_code(value: Optional[str]) -> Optional[str]:
    """Deja solo los dígitos de un código de barras.

    Muchas tiendas publican "EAN: 8206278424773" dentro del mismo elemento.
    Si tras limpiar no queda algo con pinta de GTIN, se conserva el original.
    """
    if not value:
        return None
    digits = _CODE_LABEL.sub("", str(value))
    return digits if len(digits) in (8, 12, 13, 14) else str(value).strip() or None


def _content_hash(raw: RawProduct) -> str:
    payload = "|".join(
        str(part)
        for part in (raw.name, raw.price, raw.stock_status, raw.image_url, raw.description)
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def load_excluded_keys() -> set:
    """Claves estables de las ofertas que el usuario mandó eliminar.

    Se lee una vez por tienda y no por ficha: son pocas y la consulta dentro
    del bucle costaría un viaje a la base por cada producto.
    """
    with get_connection() as conn:
        return {
            r["entity_key"]
            for r in conn.execute("SELECT entity_key FROM excluded_offers").fetchall()
        }


def load_manual_attributes() -> Dict[str, Dict[str, Any]]:
    """{clave_estable: {atributo: valor}} con las correcciones del usuario."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT entity_key, attribute, value FROM manual_attributes"
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        out.setdefault(row["entity_key"], {})[row["attribute"]] = row["value"]
    return out


def prepare_record(
    raw: RawProduct,
    store: Dict[str, Any],
    cfg: Dict[str, Any],
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    # Varias APIs devuelven el nombre con entidades HTML sin decodificar
    # ("Chaos Rising &#8211; Booster Bundle"). Sin esto, "8211" acabaría como
    # un token numérico —los números están protegidos— y ensuciaría el
    # matching además de verse mal en pantalla.
    raw.name = html.unescape(raw.name or "")
    if raw.description:
        raw.description = html.unescape(raw.description)

    normalized = normalize_name(raw.name)
    extracted = attrs_module.extract(
        normalized,
        description=raw.description,
        game_hint=cfg.get("default_game"),
        brand=raw.brand,
        language_hint=cfg.get("default_language"),
    )

    # Correcciones manuales: mandan sobre lo que se dedujo del nombre y se
    # reaplican en cada actualización, porque los atributos se recalculan.
    manual = (overrides or {}).get(
        stable_key(store["code"], raw.external_id, raw.url), {}
    )
    if "language" in manual:
        extracted.language = manual["language"] or None
    if manual.get("set_code"):
        extracted.set_code = manual["set_code"]
        extracted.set_name = attrs_module.set_name(extracted.game, manual["set_code"])
    if manual.get("product_type"):
        extracted.product_type = manual["product_type"]
        extracted.product_type_name = attrs_module.type_name(manual["product_type"])

    # Tokens usados por el matching: sin la información que ya viaja como
    # atributo (marca, serie, multiplicador, tipos secundarios).
    match_tokens = attrs_module.refine_tokens(normalized, extracted)

    gtins = {
        value
        for value in (
            normalize_gtin(raw.gtin),
            normalize_gtin(raw.ean),
            normalize_gtin(raw.upc),
        )
        if value
    }

    return {
        "store_id": store["id"],
        "external_id": raw.external_id,
        "url": raw.url,
        "name": raw.name,
        "normalized_name": normalized.canonical,
        "name_key": " ".join(sorted(set(match_tokens))),
        "tokens": json.dumps(match_tokens, ensure_ascii=False),
        "description": raw.description,
        "image_url": raw.image_url,
        "category": raw.category,
        "price": raw.price,
        "price_raw": raw.price_raw,
        "currency": raw.currency or store.get("currency") or "CLP",
        "stock_status": raw.stock_status,
        "stock_raw": raw.stock_raw,
        "sku": raw.sku,
        "mpn": normalize_mpn(raw.mpn),
        "upc": _clean_code(raw.upc),
        "ean": _clean_code(raw.ean),
        "gtin": _clean_code(raw.gtin),
        "brand": extracted.brand,
        "game": extracted.game,
        "set_code": extracted.set_code,
        "product_type": extracted.product_type,
        "quantity": extracted.multiplier,
        "quantity_confidence": extracted.quantity_confidence,
        "units_total": extracted.units_total,
        "language": extracted.language,
        "content_hash": _content_hash(raw),
        "_gtins": gtins,
        "_attributes": extracted,
    }


# ---------------------------------------------------------------------------
# Persistencia con detección de cambios
# ---------------------------------------------------------------------------
class IngestStats:
    def __init__(self) -> None:
        self.found = 0
        self.new = 0
        self.updated = 0
        self.unchanged = 0
        self.removed = 0
        self.price_changes = 0
        self.stock_changes = 0


def persist_products(
    store: Dict[str, Any],
    records: List[Dict[str, Any]],
    run_id: Optional[int],
    unchanged_urls: Optional[set] = None,
) -> IngestStats:
    """Guarda los productos de una tienda y detecta los cambios.

    `unchanged_urls` son fichas que respondieron 304 Not Modified: no se
    descargaron, pero siguen publicadas y no deben darse de baja.
    """
    unchanged_urls = unchanged_urls or set()
    stats = IngestStats()
    stats.found = len(records) + len(unchanged_urls)
    if not records and not unchanged_urls:
        return stats

    min_hours = float(settings.get("history.min_hours_between_snapshots", 24))
    seen_ids: List[int] = []
    comienzo = time.perf_counter()

    with transaction() as conn:
        existing_rows = conn.execute(
            """SELECT id, url, external_id, name, price, stock_status, content_hash,
                      is_active, last_price_change_at
               FROM store_products WHERE store_id = ?""",
            (store["id"],),
        ).fetchall()

        by_url = {row["url"]: dict(row) for row in existing_rows}
        by_external = {
            row["external_id"]: dict(row) for row in existing_rows if row["external_id"]
        }

        lote = Lote(conn)

        for record in records:
            existing = by_url.get(record["url"])
            if existing is None and record.get("external_id"):
                existing = by_external.get(record["external_id"])

            if existing is None:
                product_id = _insert_product(conn, record)
                stats.new += 1
                seen_ids.append(product_id)
                _record_price(conn, lote, product_id, record)
                _record_stock(lote, product_id, record)
                _event(
                    lote, "new_product", store["id"], product_id, None,
                    None, record["name"], None,
                    f"Nuevo producto en {store['name']}: {record['name']}",
                )
            else:
                product_id = existing["id"]
                seen_ids.append(product_id)
                changed = existing["content_hash"] != record["content_hash"]

                if changed:
                    stats.updated += 1
                    _detect_changes(conn, lote, store, existing, record, stats, min_hours)
                else:
                    stats.unchanged += 1

                _update_product(lote, product_id, record, changed)

            _sync_identifiers(lote, product_id, record)
            _sync_attributes(lote, product_id, record["_attributes"])

        lote.flush()

        # --- fichas sin cambios (304): solo se refresca "visto por última vez"
        for url in unchanged_urls:
            existing = by_url.get(url)
            if existing is None:
                continue
            seen_ids.append(existing["id"])
            stats.unchanged += 1
            lote.execute(
                "UPDATE store_products SET last_seen_at = datetime('now'), is_active = 1 WHERE id = ?",
                (existing["id"],),
            )
        lote.flush()

        # --- productos que ya no aparecen en la tienda --------------------
        if seen_ids:
            placeholders = ",".join("?" * len(seen_ids))
            missing = conn.execute(
                f"""SELECT id, name FROM store_products
                    WHERE store_id = ? AND is_active = 1 AND id NOT IN ({placeholders})""",
                (store["id"], *seen_ids),
            ).fetchall()
            for row in missing:
                lote.execute(
                    "UPDATE store_products SET is_active = 0, last_changed_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                stats.removed += 1
                _event(
                    lote, "removed_product", store["id"], row["id"], None,
                    row["name"], None, None,
                    f"Ya no aparece en {store['name']}: {row['name']}",
                )
            lote.flush()

    # Separado del tiempo total del scraping a propósito: así se ve de un
    # vistazo si el cuello de botella es descargar o es escribir en la base.
    log("info", store["code"],
        f"Guardado en {time.perf_counter() - comienzo:.1f}s "
        f"({stats.new} altas, {stats.updated} cambios, {stats.unchanged} sin cambios)")
    return stats


def _insert_product(conn, record: Dict[str, Any]) -> int:
    cur = conn.execute(
        """INSERT INTO store_products (
               store_id, external_id, url, name, normalized_name, name_key, tokens,
               description, image_url, category, price, price_raw, currency,
               stock_status, stock_raw, sku, mpn, upc, ean, gtin, brand,
               game, set_code, product_type, quantity, quantity_confidence,
               units_total, language, content_hash,
               first_seen_at, last_seen_at, last_changed_at, last_price_change_at, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?,
                   datetime('now'), datetime('now'), datetime('now'), datetime('now'), 1)""",
        (
            record["store_id"], record["external_id"], record["url"], record["name"],
            record["normalized_name"], record["name_key"], record["tokens"],
            record["description"], record["image_url"], record["category"],
            record["price"], record["price_raw"], record["currency"],
            record["stock_status"], record["stock_raw"], record["sku"], record["mpn"],
            record["upc"], record["ean"], record["gtin"], record["brand"],
            record["game"], record["set_code"], record["product_type"],
            record["quantity"], record["quantity_confidence"], record["units_total"],
            record["language"], record["content_hash"],
        ),
    )
    return int(cur.lastrowid)


def _update_product(lote, product_id: int, record: Dict[str, Any], changed: bool) -> None:
    lote.execute(
        """UPDATE store_products SET
               external_id = COALESCE(?, external_id),
               name = ?, normalized_name = ?, name_key = ?, tokens = ?,
               description = COALESCE(?, description),
               image_url = COALESCE(?, image_url),
               category = COALESCE(?, category),
               price = ?, price_raw = ?, currency = ?,
               stock_status = ?, stock_raw = ?,
               sku = COALESCE(?, sku), mpn = COALESCE(?, mpn),
               upc = COALESCE(?, upc), ean = COALESCE(?, ean), gtin = COALESCE(?, gtin),
               brand = COALESCE(?, brand),
               game = ?, set_code = ?, product_type = ?,
               quantity = ?, quantity_confidence = ?, units_total = ?, language = ?,
               content_hash = ?,
               last_seen_at = datetime('now'),
               -- El `= 1` no sobra: SQLite acepta un entero como condición,
               -- Postgres exige un booleano y rechaza CASE WHEN 1.
               last_changed_at = CASE WHEN ? = 1 THEN datetime('now') ELSE last_changed_at END,
               is_active = 1
           WHERE id = ?""",
        (
            record["external_id"], record["name"], record["normalized_name"],
            record["name_key"], record["tokens"], record["description"],
            record["image_url"], record["category"], record["price"], record["price_raw"],
            record["currency"], record["stock_status"], record["stock_raw"],
            record["sku"], record["mpn"], record["upc"], record["ean"], record["gtin"],
            record["brand"], record["game"], record["set_code"], record["product_type"],
            record["quantity"], record["quantity_confidence"], record["units_total"],
            record["language"], record["content_hash"], 1 if changed else 0, product_id,
        ),
    )


def _detect_changes(
    conn,
    lote,
    store: Dict[str, Any],
    existing: Dict[str, Any],
    record: Dict[str, Any],
    stats: IngestStats,
    min_hours: float,
) -> None:
    old_price = existing.get("price")
    new_price = record.get("price")

    if new_price is not None and old_price is not None and abs(new_price - old_price) > 0.001:
        stats.price_changes += 1
        pct = ((new_price - old_price) / old_price * 100.0) if old_price else None
        event_type = "price_drop" if new_price < old_price else "price_rise"
        _event(
            lote, event_type, store["id"], existing["id"], None,
            f"{old_price}", f"{new_price}", pct,
            f"{record['name']}: {old_price:,.0f} → {new_price:,.0f}".replace(",", "."),
        )
        _record_price(conn, lote, existing["id"], record)
        lote.execute(
            "UPDATE store_products SET last_price_change_at = datetime('now') WHERE id = ?",
            (existing["id"],),
        )
    elif new_price is not None:
        _record_price(conn, lote, existing["id"], record, min_hours=min_hours)

    old_stock = existing.get("stock_status")
    new_stock = record.get("stock_status")
    if old_stock != new_stock:
        stats.stock_changes += 1
        if new_stock == "in_stock" and old_stock in ("out_of_stock", "coming_soon"):
            event_type = "back_in_stock"
            message = f"Volvió a estar disponible: {record['name']}"
        elif new_stock == "out_of_stock":
            event_type = "out_of_stock"
            message = f"Se agotó: {record['name']}"
        else:
            event_type = "stock_change"
            message = f"Cambio de disponibilidad: {record['name']}"
        _event(lote, event_type, store["id"], existing["id"], None, old_stock, new_stock, None, message)
        _record_stock(lote, existing["id"], record)

    if existing.get("name") != record["name"]:
        _event(
            lote, "name_change", store["id"], existing["id"], None,
            existing.get("name"), record["name"], None,
            f"Cambio de nombre en {store['name']}",
        )


def _record_price(conn, lote, store_product_id: int, record: Dict[str, Any],
                  min_hours: float = 0.0) -> None:
    if record.get("price") is None:
        return
    if min_hours > 0:
        row = conn.execute(
            "SELECT recorded_at FROM price_history WHERE store_product_id = ? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (store_product_id,),
        ).fetchone()
        if row:
            try:
                last = datetime.fromisoformat(row["recorded_at"])
                if datetime.utcnow() - last < timedelta(hours=min_hours):
                    return
            except (TypeError, ValueError):
                pass
    lote.execute(
        "INSERT INTO price_history (store_product_id, price, currency) VALUES (?, ?, ?)",
        (store_product_id, record["price"], record.get("currency", "CLP")),
    )


def _record_stock(lote, store_product_id: int, record: Dict[str, Any]) -> None:
    lote.execute(
        "INSERT INTO stock_history (store_product_id, stock_status) VALUES (?, ?)",
        (store_product_id, record.get("stock_status", "unknown")),
    )


def _sync_identifiers(lote, store_product_id: int, record: Dict[str, Any]) -> None:
    rows: List[Tuple[str, str, str, int]] = []
    for gtin in record["_gtins"]:
        rows.append(("gtin", gtin, gtin, 1))
    if record.get("mpn"):
        rows.append(("mpn", record["mpn"], record["mpn"], 4))
    if record.get("sku"):
        rows.append(("sku", record["sku"], str(record["sku"]).strip().upper(), 6))
    if record.get("external_id"):
        rows.append(
            ("external_id", record["external_id"], str(record["external_id"]).strip().upper(), 7)
        )
    for kind, value, normalized, priority in rows:
        lote.execute(
            """INSERT INTO product_identifiers
                   (store_product_id, kind, value, normalized_value, priority)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(store_product_id, kind, normalized_value) DO NOTHING""",
            (store_product_id, kind, value, normalized, priority),
        )


def _sync_attributes(lote, store_product_id: int, extracted) -> None:
    pairs = {
        "game": (extracted.game, 1.0),
        "set": (extracted.set_code, 1.0),
        "product_type": (extracted.product_type, 1.0),
        "multiplier": (extracted.multiplier, 1.0),
        "units_total": (extracted.units_total, extracted.quantity_confidence),
        "language": (extracted.language, 1.0),
    }
    for key, (value, confidence) in pairs.items():
        if value is None:
            continue
        lote.execute(
            """INSERT INTO product_attributes
                   (entity_type, entity_id, key, value, confidence, source)
               VALUES ('store_product', ?, ?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id, key) DO UPDATE SET
                   value = excluded.value,
                   confidence = excluded.confidence,
                   source = excluded.source""",
            (
                store_product_id, key, str(value), float(confidence or 0.0),
                extracted.sources.get(key if key != "set" else "set_code", "name"),
            ),
        )


def _event(
    lote,
    event_type: str,
    store_id: Optional[int],
    store_product_id: Optional[int],
    product_id: Optional[int],
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    pct_change: Optional[float] = None,
    message: Optional[str] = None,
) -> None:
    lote.execute(
        """INSERT INTO events (type, store_id, store_product_id, product_id,
                               old_value, new_value, pct_change, message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_type, store_id, store_product_id, product_id, old_value, new_value,
         pct_change, message),
    )


# ---------------------------------------------------------------------------
# Ejecución del scraping de una tienda
# ---------------------------------------------------------------------------
async def scrape_store(store: Dict[str, Any], trigger: str = "manual") -> Dict[str, Any]:
    cfg = store_config(store)
    started = time.monotonic()

    with transaction() as conn:
        run_id = conn.execute(
            "INSERT INTO scrape_runs (store_id, trigger, status) VALUES (?, ?, 'running')",
            (store["id"], trigger),
        ).lastrowid

    errors: List[Tuple[str, Optional[str], str]] = []

    def report_error(stage: str, url: Optional[str], message: str) -> None:
        errors.append((stage, url, message))

    log("info", store["code"], f"Iniciando scraping de {store['name']}")

    records: List[Dict[str, Any]] = []
    overrides = load_manual_attributes()
    adapter = None
    client = HttpClient(
        store["code"],
        concurrency=cfg.get("concurrency"),
        min_delay=cfg.get("min_delay_seconds"),
        headers=cfg.get("headers"),
    )

    try:
        adapter = build_adapter(store, cfg, client, report_error)
        excluidas = load_excluded_keys()
        saltadas = 0
        async for raw in adapter.iter_products():
            # Lo que el usuario mandó eliminar no vuelve a entrar. Se comprueba
            # aquí, antes de normalizar, porque la decisión es sobre la ficha
            # de la tienda y no depende de nada que se deduzca después.
            if stable_key(store["code"], raw.external_id, raw.url) in excluidas:
                saltadas += 1
                continue
            try:
                records.append(prepare_record(raw, store, cfg, overrides))
            except Exception as exc:  # noqa: BLE001
                report_error("parse", raw.url, f"{type(exc).__name__}: {exc}")
        if saltadas:
            log("info", store["code"],
                f"{saltadas} ficha(s) omitidas por estar eliminadas a mano")
    except Exception as exc:  # noqa: BLE001
        report_error("network", store["base_url"], f"{type(exc).__name__}: {exc}")
    finally:
        close = getattr(adapter, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
        await client.close()

    # Deduplicar por URL dentro de la misma ejecución.
    unique: Dict[str, Dict[str, Any]] = {}
    for record in records:
        unique[record["url"]] = record
    records = list(unique.values())

    unchanged = getattr(adapter, "unchanged_urls", set()) if adapter is not None else set()
    stats = persist_products(store, records, run_id, unchanged)
    duration_ms = int((time.monotonic() - started) * 1000)

    with transaction() as conn:
        for stage, url, message in errors:
            conn.execute(
                """INSERT INTO scrape_errors (run_id, store_id, stage, url, message)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, store["id"], stage, url, message[:1000]),
            )
        status = "ok" if not errors else ("partial" if stats.found else "error")
        conn.execute(
            """UPDATE scrape_runs SET
                   status = ?, finished_at = datetime('now'), duration_ms = ?,
                   products_found = ?, products_new = ?, products_updated = ?,
                   products_removed = ?, requests_made = ?, requests_cached = ?,
                   error_count = ?, message = ?
               WHERE id = ?""",
            (
                status, duration_ms, stats.found, stats.new, stats.updated, stats.removed,
                client.requests_made, client.requests_cached, len(errors),
                (errors[0][2][:400] if errors else None), run_id,
            ),
        )

    log(
        "info" if not errors else "warn",
        store["code"],
        f"{store['name']} — {stats.found} productos "
        f"({stats.new} nuevos, {stats.updated} actualizados, {len(errors)} errores) "
        f"en {duration_ms / 1000:.1f}s",
    )

    return {
        "store": store["code"],
        "store_name": store["name"],
        "run_id": run_id,
        "found": stats.found,
        "new": stats.new,
        "updated": stats.updated,
        "removed": stats.removed,
        "price_changes": stats.price_changes,
        "stock_changes": stats.stock_changes,
        "errors": len(errors),
        "duration_ms": duration_ms,
        "status": status,
    }


def get_store(code_or_id: Any) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        if isinstance(code_or_id, int) or str(code_or_id).isdigit():
            row = conn.execute("SELECT * FROM stores WHERE id = ?", (int(code_or_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM stores WHERE code = ?", (code_or_id,)).fetchone()
    return dict(row) if row else None


def list_stores(only_enabled: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM stores"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql).fetchall()]
