"""Copia a Supabase las decisiones manuales de la base local.

Todo lo demás —tiendas, ofertas, precios— lo vuelve a construir el scraping en
la primera pasada. Estas tres tablas no: son trabajo tuyo, revisando productos
a mano, y si no se copian se pierden.

  · manual_matches    — «estos dos son el mismo producto», y lo contrario
  · manual_attributes — idiomas, expansiones y enlaces corregidos
  · excluded_offers   — lo que mandaste eliminar y no debe volver

    python scripts/migrar_decisiones.py --dry-run     # solo mira y cuenta
    python scripts/migrar_decisiones.py               # copia de verdad

La URL de Postgres se lee de DATABASE_URL, o se pasa con --url.

Se puede ejecutar las veces que haga falta: las filas se identifican por su
clave estable (`a_key`/`b_key`, `entity_key`/`attribute`), no por el `id`, así
que una segunda pasada actualiza en vez de duplicar.

Qué NO se copia, y por qué: `favorites` y `alerts` apuntan a `products.id`, un
número que genera el agrupador en cada base. En Supabase esos identificadores
serán otros, así que copiarlos apuntaría a productos equivocados.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# Cada tabla con las columnas que se copian y las que forman su clave única.
TABLAS = (
    {
        "nombre": "manual_matches",
        "columnas": ("a_key", "b_key", "decision", "note"),
        "clave": ("a_key", "b_key"),
        "actualiza": ("decision", "note"),
    },
    {
        "nombre": "manual_attributes",
        "columnas": ("entity_key", "attribute", "value", "note"),
        "clave": ("entity_key", "attribute"),
        "actualiza": ("value", "note"),
    },
    {
        # Lo eliminado a mano. Sin esto, el bot seguiría publicando ofertas de
        # productos que ya borraste: la exclusión vive en la base, y son dos
        # bases distintas.
        "nombre": "excluded_offers",
        "columnas": ("entity_key", "store_code", "name", "url", "reason"),
        "clave": ("entity_key",),
        "actualiza": ("store_code", "name", "url", "reason"),
    },
)


def leer_sqlite(ruta: Path) -> dict:
    if not ruta.exists():
        raise SystemExit(f"No encuentro la base local en {ruta}")
    conn = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    datos = {}
    for tabla in TABLAS:
        columnas = ", ".join(tabla["columnas"])
        filas = conn.execute(f"SELECT {columnas} FROM {tabla['nombre']}").fetchall()
        datos[tabla["nombre"]] = [tuple(fila) for fila in filas]
    conn.close()
    return datos


def escribir_postgres(url: str, datos: dict) -> None:
    try:
        import psycopg
    except ModuleNotFoundError:
        raise SystemExit(
            "Falta psycopg. Instálalo con:  pip install \"psycopg[binary]==3.2.3\""
        )

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for tabla in TABLAS:
                filas = datos[tabla["nombre"]]
                if not filas:
                    print(f"  · {tabla['nombre']}: nada que copiar")
                    continue

                columnas = ", ".join(tabla["columnas"])
                marcadores = ", ".join(["%s"] * len(tabla["columnas"]))
                conflicto = ", ".join(tabla["clave"])
                actualizacion = ", ".join(
                    f"{c} = EXCLUDED.{c}" for c in tabla["actualiza"]
                )
                cur.executemany(
                    f"INSERT INTO {tabla['nombre']} ({columnas}) "
                    f"VALUES ({marcadores}) "
                    f"ON CONFLICT ({conflicto}) DO UPDATE SET {actualizacion}",
                    filas,
                )
                cur.execute(f"SELECT COUNT(*) FROM {tabla['nombre']}")
                total = cur.fetchone()[0]
                print(f"  · {tabla['nombre']}: {len(filas)} enviadas, {total} en destino")
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="URL de Postgres (si no, DATABASE_URL)")
    parser.add_argument("--db", default=None, help="ruta del SQLite (si no, data/tcg.db)")
    parser.add_argument("--dry-run", action="store_true", help="cuenta sin escribir")
    args = parser.parse_args()

    ruta = Path(args.db) if args.db else RAIZ / "data" / "tcg.db"
    datos = leer_sqlite(ruta)

    print(f"\nBase local: {ruta}")
    for tabla in TABLAS:
        print(f"  · {tabla['nombre']}: {len(datos[tabla['nombre']])} filas")

    if args.dry_run:
        print("\n--dry-run: no se ha escrito nada.\n")
        return 0

    url = args.url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit(
            "\nFalta la URL de Postgres. Pásala con --url o define DATABASE_URL."
        )

    print("\nCopiando a Postgres…")
    escribir_postgres(url, datos)
    print("\nListo.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
