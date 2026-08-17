"""Marca eventos como ya publicados, para que el bot no los anuncie.

Sirve cuando un arreglo del scraper genera cambios de precio que no son
rebajas de verdad. Al corregir una configuración que guardaba precios
equivocados, TODAS las fichas de esa tienda cambian de precio a la vez y cada
corrección parece una oferta.

No borra nada: solo apunta los eventos en `telegram_sent`, que es la tabla que
el bot consulta para no repetirse.

    python scripts/silenciar_eventos.py --tienda samuraitcg --url "postgres..."
    python scripts/silenciar_eventos.py --tienda samuraitcg --horas 12
    python scripts/silenciar_eventos.py --tienda samuraitcg --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tienda", required=True, help="código de la tienda")
    parser.add_argument("--url", default=None, help="URL de Postgres (si no, la base local)")
    parser.add_argument("--horas", type=int, default=48,
                        help="cuántas horas atrás mirar")
    parser.add_argument("--tipo", default="price_drop",
                        help="tipo de evento: price_drop, back_in_stock…")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.url:
        import os

        os.environ["DATABASE_URL"] = args.url

    from app.db.database import es_postgres, query, transaction

    pendientes = query(
        f"""SELECT e.id, sp.name, e.old_value, e.new_value, e.pct_change
            FROM events e
            JOIN store_products sp ON sp.id = e.store_product_id
            JOIN stores s ON s.id = sp.store_id
            LEFT JOIN telegram_sent t ON t.event_id = e.id
            WHERE s.code = ? AND e.type = ?
              AND t.event_id IS NULL
              AND e.created_at >= datetime('now', '-{int(args.horas)} hours')
            ORDER BY e.id""",
        (args.tienda, args.tipo),
    )

    print(f"\nMotor: {'PostgreSQL' if es_postgres() else 'SQLite'}")
    print(f"{len(pendientes)} evento(s) de «{args.tienda}» sin anunciar:\n")
    for p in pendientes:
        pct = f"{abs(p['pct_change'] or 0):.0f}%"
        print(f"  −{pct:<5} {str(p['old_value'])[:9]:>10} → {str(p['new_value'])[:9]:<10} "
              f"{str(p['name'])[:44]}")

    if not pendientes:
        return 0
    if args.dry_run:
        print("\n--dry-run: no se ha tocado nada.\n")
        return 0

    with transaction() as conn:
        conn.executemany(
            "INSERT INTO telegram_sent (event_id) VALUES (?) ON CONFLICT DO NOTHING",
            [(p["id"],) for p in pendientes],
        )
    print(f"\n{len(pendientes)} evento(s) marcados como publicados. El bot ya no los anunciará.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
