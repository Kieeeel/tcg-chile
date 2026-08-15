"""Alertas de precio, evaluadas localmente después de cada actualización."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app import settings
from app.db.database import get_connection, transaction


def list_alerts() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT a.*, p.display_name, p.best_price, p.best_available_price,
                      p.image_url, p.set_name, p.product_type_name
               FROM alerts a
               JOIN products p ON p.id = a.product_id
               ORDER BY a.active DESC, a.created_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def create_alert(product_id: int, target_price: float, only_in_stock: bool = True) -> int:
    with transaction() as conn:
        return conn.execute(
            """INSERT INTO alerts (product_id, target_price, only_in_stock)
               VALUES (?, ?, ?)""",
            (product_id, float(target_price), 1 if only_in_stock else 0),
        ).lastrowid


def delete_alert(alert_id: int) -> bool:
    with transaction() as conn:
        return conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,)).rowcount > 0


def set_active(alert_id: int, active: bool) -> bool:
    with transaction() as conn:
        return conn.execute(
            "UPDATE alerts SET active = ? WHERE id = ?", (1 if active else 0, alert_id)
        ).rowcount > 0


def evaluate_alerts() -> List[Dict[str, Any]]:
    """Devuelve las alertas que se cumplieron en esta pasada."""
    if not settings.get("alerts.enabled", True):
        return []

    triggered: List[Dict[str, Any]] = []
    with transaction() as conn:
        rows = conn.execute(
            """SELECT a.id, a.product_id, a.target_price, a.only_in_stock,
                      a.last_triggered_price,
                      p.display_name, p.best_price, p.best_available_price,
                      p.best_store_id, p.best_available_store_id
               FROM alerts a
               JOIN products p ON p.id = a.product_id
               WHERE a.active = 1"""
        ).fetchall()

        for row in rows:
            price = row["best_available_price"] if row["only_in_stock"] else row["best_price"]
            store_id = (
                row["best_available_store_id"] if row["only_in_stock"] else row["best_store_id"]
            )
            if price is None or price > row["target_price"]:
                continue
            # No repetimos el aviso mientras el precio no baje aún más.
            if row["last_triggered_price"] is not None and price >= row["last_triggered_price"]:
                continue

            conn.execute(
                """INSERT INTO alert_hits (alert_id, product_id, price, store_id)
                   VALUES (?, ?, ?, ?)""",
                (row["id"], row["product_id"], price, store_id),
            )
            conn.execute(
                """UPDATE alerts SET last_triggered_at = datetime('now'),
                                     last_triggered_price = ?
                   WHERE id = ?""",
                (price, row["id"]),
            )
            conn.execute(
                """INSERT INTO events (type, product_id, new_value, message)
                   VALUES ('alert', ?, ?, ?)""",
                (
                    row["product_id"],
                    str(price),
                    f"Alerta: {row['display_name']} bajó a {price:,.0f}".replace(",", "."),
                ),
            )
            triggered.append(
                {
                    "alert_id": row["id"],
                    "product_id": row["product_id"],
                    "product": row["display_name"],
                    "price": price,
                    "target_price": row["target_price"],
                }
            )
    return triggered


def pending_hits(limit: int = 50) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT h.*, p.display_name, s.name AS store_name
               FROM alert_hits h
               JOIN products p ON p.id = h.product_id
               LEFT JOIN stores s ON s.id = h.store_id
               WHERE h.seen = 0
               ORDER BY h.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_hits_seen(hit_ids: Optional[List[int]] = None) -> int:
    with transaction() as conn:
        if hit_ids:
            placeholders = ",".join("?" * len(hit_ids))
            cur = conn.execute(
                f"UPDATE alert_hits SET seen = 1 WHERE id IN ({placeholders})", tuple(hit_ids)
            )
        else:
            cur = conn.execute("UPDATE alert_hits SET seen = 1 WHERE seen = 0")
        return cur.rowcount
