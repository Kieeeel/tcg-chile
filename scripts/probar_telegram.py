"""Manda un mensaje de prueba al grupo de Telegram.

Sirve para ver cómo queda el formato de verdad —en el móvil, con los enlaces
y la negrita— antes de dejar el bot publicando solo.

    python scripts/probar_telegram.py                 # solo lo muestra aquí
    python scripts/probar_telegram.py --enviar        # lo manda al grupo
    python scripts/probar_telegram.py --enviar --sueltos   # un mensaje por oferta

Usa productos reales de la base, pero inventa la bajada de precio: así se ve
el formato completo aunque todavía no haya habido ninguna rebaja de verdad.

NO marca nada como publicado. Las ofertas auténticas que estén esperando se
anunciarán igual en la siguiente actualización.

El token y el grupo se leen del entorno, nunca de un archivo:

    $env:TELEGRAM_BOT_TOKEN = "..."
    $env:TELEGRAM_CHAT_ID   = "-5363195083"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def ejemplos(cuantos: int):
    """Arma ofertas de muestra a partir de productos que existen de verdad."""
    from app.db.database import query

    # Una oferta por producto: si no, salen tres veces la misma caja en tres
    # tiendas distintas y la prueba no enseña nada.
    filas = query(
        """SELECT p.display_name, p.id AS product_id, sp.name AS offer_name,
                  sp.url, sp.price, s.name AS store_name,
                  COALESCE(sp.image_url, p.image_url) AS image_url
           FROM products p
           JOIN store_products sp ON sp.id = (
                   SELECT sp2.id FROM store_products sp2
                   WHERE sp2.product_id = p.id AND sp2.is_active = 1
                     AND sp2.price IS NOT NULL AND sp2.stock_status = 'in_stock'
                   ORDER BY sp2.price ASC LIMIT 1)
           JOIN stores s ON s.id = sp.store_id
           WHERE sp.price > 5000
           ORDER BY p.stores_count DESC, p.id
           LIMIT ?""",
        (cuantos,),
    )
    if not filas:
        raise SystemExit("No hay productos con precio en la base. ¿Está vacía?")

    muestras = []
    for indice, fila in enumerate(filas):
        fila = dict(fila)
        precio = float(fila["price"])
        if indice % 3 == 2:
            # Una de cada tres, como vuelta a stock.
            muestras.append({
                "id": -1 - indice,
                "type": "back_in_stock",
                "old_value": None,
                "new_value": "in_stock",
                "pct_change": None,
                "current_price": precio,
                **fila,
            })
        else:
            antes = round(precio * 1.25)
            muestras.append({
                "id": -1 - indice,
                "type": "price_drop",
                "old_value": str(antes),
                "new_value": str(precio),
                "pct_change": -round((antes - precio) / antes * 100, 1),
                "current_price": precio,
                "_bajada": antes - precio,
                **fila,
            })
    return muestras


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enviar", action="store_true", help="mandarlo de verdad al grupo")
    parser.add_argument("--sueltos", action="store_true", help="un mensaje por oferta")
    parser.add_argument("--cuantos", type=int, default=3, help="cuántas ofertas de muestra")
    args = parser.parse_args()

    from app.services import notify

    muestras = ejemplos(args.cuantos)
    mensajes = (
        notify.componer_sueltos(muestras) if args.sueltos else [notify.componer(muestras)]
    )

    print(f"\n{len(muestras)} ofertas de muestra en {len(mensajes)} mensaje(s):\n")
    for indice, mensaje in enumerate(mensajes):
        print("─" * 60)
        print(mensaje)
        if args.sueltos:
            foto = muestras[indice].get("image_url")
            print(f"   [foto] {foto or 'sin imagen — se manda solo el texto'}")
    print("─" * 60)

    if not args.enviar:
        print("\n(no se ha enviado nada — añade --enviar para mandarlo al grupo)\n")
        return 0

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("\nFalta TELEGRAM_BOT_TOKEN en el entorno.\n")
    if not notify.chat_id():
        raise SystemExit("\nFalta TELEGRAM_CHAT_ID en el entorno.\n")

    for indice, mensaje in enumerate(mensajes):
        if indice:
            await asyncio.sleep(1.5)
        # La foto solo tiene sentido con un producto por mensaje.
        foto = muestras[indice].get("image_url") if args.sueltos else None
        await notify.enviar(mensaje, foto)
    print(f"\nEnviado a {notify.chat_id()}. Míralo en el grupo.\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
