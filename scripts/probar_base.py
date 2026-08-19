"""Ejecuta todas las consultas del proyecto contra la base y dice cuáles fallan.

Existe porque SQLite y PostgreSQL no son el mismo dialecto, y las diferencias
no se ven leyendo el código: aparecen al ejecutarlo. En vez de descubrirlas de
una en una —desplegar, fallar, arreglar, repetir— esto las saca todas juntas.

    python scripts/probar_base.py                  # contra la base local (SQLite)
    python scripts/probar_base.py --url "postgresql://…"   # contra Supabase

No escribe nada por defecto. Con `--escribir` prueba también las inserciones,
deshaciéndolas al terminar.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def pruebas(escribir: bool):
    """Cada prueba es (nombre, función). Se ejecutan en orden."""
    from app.services import queries

    def primer_producto():
        filas = queries.search_products(page_size=1)
        items = filas.get("items") if isinstance(filas, dict) else filas
        return items[0]["id"] if items else None

    pid = None

    def con_producto(fn):
        def envuelto():
            nonlocal pid
            if pid is None:
                pid = primer_producto()
            if pid is None:
                return "sin productos en la base — omitida"
            return fn(pid)
        return envuelto

    lista = [
        ("search_products (sin filtros)", lambda: queries.search_products(page_size=5)),
        ("search_products (texto)", lambda: queries.search_products("pokemon", page_size=5)),
        # En SQLite LIKE ignora mayúsculas; en Postgres no. Esta prueba y la
        # anterior deben devolver lo mismo, o el buscador se comporta distinto
        # en el servidor que en local.
        ("search_products (texto MAYÚSCULAS)",
         lambda: queries.search_products("POKEMON", page_size=5)),
        ("search_products (juego + tipo)",
         lambda: queries.search_products(game="pokemon", product_type="elite_trainer_box",
                                         page_size=5)),
        ("search_products (min_stores)", lambda: queries.search_products(min_stores=2, page_size=5)),
        ("search_products (solo con stock)",
         lambda: queries.search_products(only_in_stock=True, page_size=5)),
        ("search_products (rango de precio)",
         lambda: queries.search_products(min_price=1000, max_price=90000, page_size=5)),
        ("search_products (orden precio)",
         lambda: queries.search_products(sort="price_asc", page_size=5)),
        ("search_products (orden novedad)", lambda: queries.search_products(sort="new", page_size=5)),
        ("search_products (página 2)", lambda: queries.search_products(page=2, page_size=5)),
        ("facets", queries.facets),
        ("dashboard", queries.dashboard),
        ("home", lambda: queries.home(per_section=6)),
        ("home (pokemon)", lambda: queries.home(game="pokemon", per_section=6)),
        ("most_viewed", lambda: queries.most_viewed(limit=6)),
        ("daily_deals", lambda: queries.daily_deals(limit=6)),
        ("opportunities", lambda: queries.opportunities(limit=6)),
        ("unit_price_ranking", lambda: queries.unit_price_ranking(limit=6)),
        ("historic_lows", lambda: queries.historic_lows(limit=6)),
        ("suggest", lambda: queries.suggest("poke", limit=5)),
        ("events", lambda: queries.events(limit=10)),
        ("logs", lambda: queries.logs(limit=10)),
        ("store_overview", queries.store_overview),
        ("pending_reviews", lambda: queries.pending_reviews(limit=10)),
        ("manual_decisions", lambda: queries.manual_decisions(limit=10)),
        ("offer_overrides", queries.offer_overrides),
        ("admin_offers", lambda: queries.admin_offers(page_size=10)),
        ("admin_offers (búsqueda)", lambda: queries.admin_offers("Pokemon", page_size=10)),
        ("admin_offers (solo editadas)",
         lambda: queries.admin_offers(edited_only=True, page_size=10)),
        ("product_detail", con_producto(lambda p: queries.product_detail(p))),
        ("price_history", con_producto(lambda p: queries.price_history(p, days=30))),
        ("comments", con_producto(lambda p: queries.comments(p))),
        ("merge_candidates (parecidos)",
         con_producto(lambda p: queries.merge_candidates(p, limit=5))),
        ("merge_candidates (búsqueda)",
         con_producto(lambda p: queries.merge_candidates(p, q="pokemon", limit=5))),
    ]

    if escribir:
        lista += [
            ("register_view (escribe)", con_producto(lambda p: queries.register_view(p))),
            ("add_comment (escribe)",
             con_producto(lambda p: queries.add_comment(p, "prueba automática", "probar_base"))),
        ]

    # El planificador y el notificador también consultan la base.
    def scheduler_info():
        from app.services import scheduler
        return scheduler.last_update()

    def telegram_pendientes():
        from app.services import notify
        return notify.eventos_pendientes(limite=5)

    def telegram_destacado():
        from app.services import notify
        # Devuelve None si no hay ninguna oportunidad; lo que se prueba aquí
        # es que la consulta corra, no que encuentre algo.
        return notify.destacado() or "sin oportunidades ahora mismo — omitida"

    def telegram_ultimo_envio():
        from app.services import notify
        notify._horas_desde_ultimo_envio()
        return True

    def membresia_socios():
        from app.services import membership
        return membership.socios(None)

    def vigilancia_estado():
        # La escritura es un upsert sobre una tabla con clave de texto, que es
        # justo el tipo de consulta que se comporta distinto en cada motor.
        # Se prueba con una dirección de mentira y se borra después.
        from app.db.database import query, transaction
        from app.services import watch

        prueba = "https://ejemplo.invalido/probar-base"
        watch._guardar({"url": prueba, "etiqueta": "prueba"},
                       {"http": 404, "nombre": None, "precio": None,
                        "stock": None, "imagen": None}, avisado=False)
        filas = query("SELECT * FROM vigilancia WHERE url = ?", (prueba,))
        with transaction() as conn:
            conn.execute("DELETE FROM vigilancia WHERE url = ?", (prueba,))
        return f"{len(watch.enlaces())} enlace(s) en la lista, upsert {'ok' if filas else 'FALLÓ'}"

    lista += [
        ("scheduler.last_update", scheduler_info),
        ("notify.eventos_pendientes", telegram_pendientes),
        ("notify.destacado", telegram_destacado),
        ("notify.horas desde el último envío", telegram_ultimo_envio),
        ("membresia.socios", membresia_socios),
        ("vigilancia", vigilancia_estado),
    ]
    return lista


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="URL de Postgres (si no, la base local)")
    parser.add_argument("--escribir", action="store_true", help="prueba también las inserciones")
    parser.add_argument("--limpiar", action="store_true",
                        help="con --escribir, borra después los comentarios de prueba")
    args = parser.parse_args()

    if args.url:
        os.environ["DATABASE_URL"] = args.url

    from app.db.database import es_postgres

    motor = "PostgreSQL" if es_postgres() else "SQLite"
    print(f"\nMotor: {motor}\n" + "-" * 60)

    fallos = []
    for nombre, fn in pruebas(args.escribir):
        try:
            resultado = fn()
        except Exception as exc:  # noqa: BLE001 — aquí queremos verlo todo
            fallos.append((nombre, exc, traceback.format_exc()))
            print(f"  FALLA   {nombre}")
            print(f"          {type(exc).__name__}: {exc}")
            continue
        if isinstance(resultado, str):          # prueba omitida
            print(f"  omitida {nombre}  ({resultado})")
        else:
            print(f"  ok      {nombre}")

    if args.escribir and args.limpiar:
        from app.db.database import execute
        execute("DELETE FROM comments WHERE author = ?", ("probar_base",))
        print("\n  (comentarios de prueba borrados)")

    print("-" * 60)
    if fallos:
        print(f"\n{len(fallos)} fallo(s). Detalle:\n")
        for nombre, _exc, tb in fallos:
            print(f"=== {nombre} " + "=" * (56 - len(nombre)))
            print(tb)
        return 1

    print("\nTodo correcto.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
