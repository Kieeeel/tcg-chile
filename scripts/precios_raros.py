"""Busca precios que se desvían tanto del resto que huelen a error.

Un comparador que enseña un precio equivocado es peor que uno que no enseña
nada: manda a alguien a una tienda con una expectativa falsa. Este script
compara cada oferta con la mediana de lo que piden las demás tiendas por ese
mismo producto y saca las que se salen.

    python scripts/precios_raros.py
    python scripts/precios_raros.py --tienda samuraitcg
    python scripts/precios_raros.py --minimo 0.3 --maximo 3

Lo que aparece aquí es de dos clases, y conviene distinguirlas:

  · Fallo al recoger — el scraper cogió el precio de otro sitio de la página.
    Se arregla en la configuración de la tienda. Señal: la ficha real dice
    otra cosa.

  · Fallo al agrupar — el precio está bien, pero el producto está metido con
    otros que no son el mismo, así que la mediana no significa nada. Señal:
    los nombres del grupo no coinciden entre sí. Se arregla separándolos.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tienda", default=None, help="limitar a una tienda")
    parser.add_argument("--minimo", type=float, default=0.45,
                        help="por debajo de esta fracción de la mediana, sospechoso")
    parser.add_argument("--maximo", type=float, default=2.5,
                        help="por encima de este múltiplo, sospechoso")
    parser.add_argument("--min-tiendas", type=int, default=3,
                        help="cuántas ofertas necesita un producto para comparar")
    args = parser.parse_args()

    from app.db.database import query

    filas = query(
        """SELECT sp.id, sp.name, sp.price, sp.product_id, sp.url, s.code AS tienda
           FROM store_products sp JOIN stores s ON s.id = sp.store_id
           WHERE sp.is_active = 1 AND sp.price IS NOT NULL
             AND sp.product_id IS NOT NULL""")

    por_producto = defaultdict(list)
    for r in filas:
        por_producto[r["product_id"]].append(r)

    raros = []
    for ofertas in por_producto.values():
        if len(ofertas) < args.min_tiendas:
            continue
        precios = [o["price"] for o in ofertas]
        for o in ofertas:
            if args.tienda and o["tienda"] != args.tienda:
                continue
            # La mediana SIN esta oferta: si no, una oferta muy rara se
            # arrastra a sí misma hacia el centro y deja de destacar.
            otros = [p for p in precios if p is not o["price"]] or precios
            mediana = statistics.median(otros)
            if not mediana:
                continue
            ratio = o["price"] / mediana
            if ratio < args.minimo or ratio > args.maximo:
                raros.append((ratio, o, mediana))

    raros.sort(key=lambda x: x[0])
    print(f"\n{len(raros)} precio(s) fuera de rango "
          f"(menos del {args.minimo:.0%} o más del {args.maximo:.0%} de la mediana)\n")

    por_tienda: dict = defaultdict(int)
    for ratio, o, mediana in raros:
        por_tienda[o["tienda"]] += 1
        señal = "BARATO" if ratio < 1 else "CARO  "
        print(f"  {señal} {ratio:>6.0%}  ${o['price']:>10,.0f} vs ${mediana:>10,.0f}  "
              f"{o['tienda']:<15} {o['name'][:44]}".replace(",", "."))
        print(f"          {o['url'][:96]}")

    if por_tienda:
        print("\nPor tienda:")
        for t, n in sorted(por_tienda.items(), key=lambda x: -x[1]):
            print(f"  {t:<16} {n}")
        print("\nAbre un par de enlaces: si la ficha dice otro precio es fallo al")
        print("recoger; si dice el mismo, el producto está mal agrupado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
