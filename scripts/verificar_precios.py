"""Contrasta los precios guardados contra la ficha real de cada tienda.

`precios_raros.py` compara unas tiendas con otras y solo ve lo que se sale del
grupo. Eso no basta: si una tienda entera está mal recogida —como pasó con
Samurai TCG, que cogía el precio de un producto relacionado— sus precios son
coherentes entre sí y no destacan.

Esto va a la fuente: descarga la ficha y saca el precio del JSON-LD o de las
etiquetas Open Graph, que es lo que declara la propia tienda.

    python scripts/verificar_precios.py                 # 3 por tienda
    python scripts/verificar_precios.py --por-tienda 8
    python scripts/verificar_precios.py --tienda samuraitcg

Lo que no consigue leer se marca «sin comprobar», no como error: hay tiendas
que no publican el precio de forma legible sin ejecutar su JavaScript.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Las mismas cabeceras que manda el scraper. Con solo el User-Agent, algunas
# tiendas contestan 403: miran el conjunto, no una cabecera suelta.
CABECERAS = {
    "User-Agent": NAVEGADOR,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}


def _precio_de_json_ld(html: str) -> Optional[float]:
    """Precio del JSON-LD, SOLO si la página declara un único producto.

    Muchas fichas incluyen debajo un carrusel de productos relacionados, y cada
    tarjeta trae su propio bloque JSON-LD. Quedarse con el primero es coger el
    precio de otro artículo: Pokestop declara ocho bloques y ninguno es el
    producto que estás mirando.

    Con más de uno no hay forma fiable de saber cuál es el bueno sin repetir la
    lógica del scraper, así que se prefiere no responder a responder mal.
    """
    from app.scrapers.parsing import parse_price

    precios = []
    for m in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            datos = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        for bloque in (datos if isinstance(datos, list) else [datos]):
            if not isinstance(bloque, dict) or bloque.get("@type") != "Product":
                continue
            ofertas = bloque.get("offers") or {}
            if isinstance(ofertas, list):
                ofertas = ofertas[0] if ofertas else {}
            valor = parse_price(str((ofertas or {}).get("price") or ""))
            if valor is not None:
                precios.append(valor)
    return precios[0] if len(precios) == 1 else None


def _precio_de_meta(html: str) -> Optional[float]:
    """Precio de las etiquetas Open Graph o microdatos.

    El número se interpreta con `parse_price`, el mismo lector que usan los
    scrapers. Escribirlo a mano fue un error: en Chile el punto separa los
    miles, y algunas tiendas publican «299,990» con coma. Mi primera versión
    leía eso como 299,99 y daba por equivocados precios que estaban bien.
    """
    from app.scrapers.parsing import parse_price

    for patron in (
        # `tiendanube:price` y `og:price` son de la página, no de las tarjetas
        # de alrededor: por eso se miran ANTES que el JSON-LD.
        r'property="tiendanube:price"\s+content="([^"]+)"',
        r'property="(?:og:)?price:amount"\s+content="([^"]+)"',
        r'content="([^"]+)"\s+property="(?:og:)?price:amount"',
        r'itemprop="price"[^>]*content="([^"]+)"',
    ):
        m = re.search(patron, html)
        if m:
            valor = parse_price(m.group(1))
            if valor is not None:
                return valor
    return None


async def _precio_de_variante_shopify(cliente, url: str) -> Optional[float]:
    """Precio de la variante concreta que apunta la dirección.

    En Shopify, `?variant=123` cambia lo que ves pero NO cambia el
    `og:price:amount` de la página, que sigue siendo el de la variante por
    defecto. Comparar contra esa etiqueta daba por equivocados precios que
    estaban bien: un ETB en inglés a 3.000.000 frente al español a 1.500.000.

    El JSON de la ficha sí trae todas las variantes con su precio.
    """
    m = re.search(r"[?&]variant=(\d+)", url)
    if not m:
        return None
    variante = m.group(1)
    try:
        r = await cliente.get(url.split("?")[0] + ".json",
                              headers=CABECERAS, timeout=25)
        if r.status_code >= 400:
            return None
        for v in (r.json().get("product") or {}).get("variants", []):
            if str(v.get("id")) == variante:
                from app.scrapers.parsing import parse_price

                return parse_price(str(v.get("price")))
    except Exception:  # noqa: BLE001
        return None
    return None


async def _precio_de_woocommerce(cliente, url: str) -> Optional[float]:
    """Precio desde la Store API de WooCommerce, buscando por su slug.

    Estas tiendas no publican el precio en etiquetas legibles —lo pintan con
    JavaScript—, así que sin esto se quedaban sin comprobar. La API sí lo da,
    y en la unidad mínima de la moneda: en pesos chilenos no hay decimales,
    pero `currency_minor_unit` lo dice y conviene respetarlo.
    """
    # Cada tienda WooCommerce elige el prefijo de sus fichas: /producto/,
    # /productos/, /product/… y algunas lo cambian por el de la categoría.
    # Lo que importa es el último tramo, que es el slug.
    # Cada tienda elige el prefijo de sus fichas y algunas usan el de la
    # categoría —Deckswap publica en /pitch-black/<slug>—, así que lo que se
    # toma es el último tramo, que es siempre el slug.
    m = re.match(r"https?://([^/]+)/(.+)", url.rstrip("/"))
    if not m:
        return None
    dominio = m.group(1)
    slug = m.group(2).split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return None
    try:
        r = await cliente.get(
            f"https://{dominio}/wp-json/wc/store/v1/products?slug={slug}",
            headers=CABECERAS, timeout=25)
        if r.status_code >= 400:
            return None
        datos = r.json()
        if not datos:
            return None
        precios = datos[0].get("prices") or {}
        crudo = precios.get("price")
        if crudo in (None, ""):
            return None
        return float(crudo) / (10 ** int(precios.get("currency_minor_unit", 0) or 0))
    except Exception:  # noqa: BLE001
        return None


async def comprobar(cliente, oferta) -> dict:
    url = str(oferta["url"]).split("#")[0]

    # Si la dirección señala una variante, ese es el precio a comparar.
    real = await _precio_de_variante_shopify(cliente, url)
    if real is None:
        real = await _precio_de_woocommerce(cliente, url)
    if real is not None:
        nuestro = float(oferta["price"])
        if abs(real - nuestro) <= max(1.0, nuestro * 0.02):
            return {**dict(oferta), "estado": "correcto", "real": real}
        return {**dict(oferta), "estado": "DISTINTO", "real": real}
    try:
        r = await cliente.get(url, headers=CABECERAS, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return {**dict(oferta), "estado": "sin comprobar", "detalle": type(exc).__name__}

    if r.status_code >= 400:
        return {**dict(oferta), "estado": "sin comprobar", "detalle": f"HTTP {r.status_code}"}

    # Primero las etiquetas de la página, después el JSON-LD: al revés se
    # cuela el precio de los productos relacionados.
    real = _precio_de_meta(r.text) or _precio_de_json_ld(r.text)
    if real is None:
        return {**dict(oferta), "estado": "sin comprobar", "detalle": "la ficha no publica el precio"}

    nuestro = float(oferta["price"])
    # Un 2 % de holgura: hay tiendas que redondean o aplican descuento al pagar.
    if abs(real - nuestro) <= max(1.0, nuestro * 0.02):
        return {**dict(oferta), "estado": "correcto", "real": real}
    return {**dict(oferta), "estado": "DISTINTO", "real": real}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--por-tienda", type=int, default=3)
    parser.add_argument("--tienda", default=None)
    parser.add_argument("--concurrencia", type=int, default=4)
    args = parser.parse_args()

    import httpx

    from app.db.database import query

    condicion = "AND s.code = ?" if args.tienda else ""
    parametros = (args.tienda,) if args.tienda else ()
    tiendas = [
        r["code"] for r in query(
            f"""SELECT DISTINCT s.code FROM stores s
                JOIN store_products sp ON sp.store_id = s.id AND sp.is_active = 1
                WHERE s.enabled = 1 {condicion} ORDER BY s.code""", parametros)
    ]

    muestra = []
    for code in tiendas:
        muestra += [
            dict(r) for r in query(
                """SELECT sp.name, sp.price, sp.url, s.code AS tienda
                   FROM store_products sp JOIN stores s ON s.id = sp.store_id
                   WHERE s.code = ? AND sp.is_active = 1 AND sp.price IS NOT NULL
                   ORDER BY sp.price DESC LIMIT ?""", (code, args.por_tienda))
        ]

    print(f"\nComprobando {len(muestra)} fichas de {len(tiendas)} tiendas…\n")

    limite = asyncio.Semaphore(args.concurrencia)

    async with httpx.AsyncClient(follow_redirects=True) as cliente:
        async def una(o):
            async with limite:
                return await comprobar(cliente, o)

        resultados = await asyncio.gather(*(una(o) for o in muestra))

    distintos = [r for r in resultados if r["estado"] == "DISTINTO"]
    sin = [r for r in resultados if r["estado"] == "sin comprobar"]
    ok = [r for r in resultados if r["estado"] == "correcto"]

    por_tienda: dict = {}
    for r in resultados:
        d = por_tienda.setdefault(r["tienda"], {"correcto": 0, "DISTINTO": 0, "sin comprobar": 0})
        d[r["estado"]] += 1

    print(f"{'tienda':<16} {'correctos':>9} {'distintos':>10} {'sin comprobar':>14}")
    print("-" * 52)
    for code in sorted(por_tienda):
        d = por_tienda[code]
        marca = "  <-- revisar" if d["DISTINTO"] else ""
        print(f"{code:<16} {d['correcto']:>9} {d['DISTINTO']:>10} {d['sin comprobar']:>14}{marca}")

    if distintos:
        print(f"\n{len(distintos)} precio(s) que no coinciden con la ficha:\n")
        for r in distintos:
            print(f"  {r['tienda']:<15} guardado ${r['price']:>10,.0f}  "
                  f"ficha ${r['real']:>10,.0f}   {r['name'][:40]}".replace(",", "."))
            print(f"      {r['url'][:100]}")

    print(f"\nTotal: {len(ok)} correctos · {len(distintos)} distintos · "
          f"{len(sin)} sin comprobar")
    return 1 if distintos else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
