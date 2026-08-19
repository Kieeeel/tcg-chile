"""Vigilancia de enlaces concretos, uno a uno.

El scraping normal recorre categorías enteras cada cuatro horas y compara unas
tiendas con otras. Para una preventa anunciada eso llega tarde: lo que importa
no es el precio medio del mercado, sino el minuto en que una ficha concreta se
puede comprar.

Aquí se lee una lista corta de direcciones (config/vigilancia.yaml), se mira
cada una tal cual, y se avisa SOLO cuando algo cambia respecto a la última vez.
Mirar cada hora no es hablar cada hora: mientras la ficha siga igual, calla.

No hace falta que la tienda esté configurada en config/stores/. El precio y la
disponibilidad salen de donde los declara la propia tienda —la API de
WooCommerce, el JSON-LD, las etiquetas Open Graph—, que es la misma fuente que
usa el resto del proyecto.
"""
from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app import settings
from app.db.database import log, query, transaction
from app.scrapers.parsing import (
    STOCK_IN,
    STOCK_OUT,
    STOCK_PREORDER,
    STOCK_UNKNOWN,
    extract_json_ld,
    parse_price,
    product_from_json_ld,
    soup_of,
)

# Estados en los que la ficha se puede comprar. `preorder` cuenta: en una
# preventa es justo el estado que se está esperando.
COMPRABLE = (STOCK_IN, STOCK_PREORDER)


def _cabeceras() -> Dict[str, str]:
    """Las mismas que manda el scraper.

    Con solo el User-Agent hay tiendas que contestan 403: miran el conjunto de
    cabeceras, no una suelta.
    """
    return {
        "User-Agent": settings.get(
            "scraping.user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    }


# ---------------------------------------------------------------------------
# La lista
# ---------------------------------------------------------------------------
def enlaces() -> List[Dict[str, Any]]:
    """Lee config/vigilancia.yaml y normaliza las dos formas admitidas.

    Se acepta tanto una dirección suelta como un par url/etiqueta, para que
    añadir una a mano no obligue a recordar la estructura.
    """
    import yaml

    ruta = settings.CONFIG_DIR / "vigilancia.yaml"
    if not ruta.exists():
        return []
    with ruta.open("r", encoding="utf-8") as fh:
        datos = yaml.safe_load(fh) or {}

    if not datos.get("enabled", True):
        return []

    salida: List[Dict[str, Any]] = []
    vistas = set()
    for entrada in datos.get("enlaces") or []:
        if isinstance(entrada, str):
            url, etiqueta = entrada.strip(), None
        elif isinstance(entrada, dict):
            url = str(entrada.get("url") or "").strip()
            etiqueta = entrada.get("etiqueta") or entrada.get("nombre")
        else:
            continue
        if not url.startswith("http") or url in vistas:
            continue
        vistas.add(url)
        salida.append({"url": url, "etiqueta": etiqueta})
    return salida


# ---------------------------------------------------------------------------
# Leer una ficha
# ---------------------------------------------------------------------------
# El orden importa: «PreOrder» se comprueba antes que «InStock» porque hay
# tiendas que declaran las dos cosas en la misma ficha.
_DISPONIBILIDAD = (
    (STOCK_PREORDER, ("preorder", "presale", "preventa", "backorder")),
    (STOCK_OUT, ("outofstock", "soldout", "discontinued")),
    (STOCK_IN, ("instock", "limitedavailability", "onlineonly", "instoreonly")),
)


def _disponibilidad(valor: Any) -> str:
    """Traduce el `availability` de schema.org a los estados del proyecto.

    Llega de muchas formas —«https://schema.org/InStock», «InStock», a veces en
    minúsculas—, así que se compara sobre el texto reducido a letras.
    """
    texto = re.sub(r"[^a-z]", "", str(valor or "").lower())
    if not texto:
        return STOCK_UNKNOWN
    for estado, claves in _DISPONIBILIDAD:
        if any(clave in texto for clave in claves):
            return estado
    return STOCK_UNKNOWN


# Las etiquetas de la propia página van antes que el JSON-LD: muchas fichas
# llevan debajo un carrusel de productos relacionados y cada tarjeta trae su
# propio bloque JSON-LD. Ahí, quedarse con el primero es leer el precio de otro
# artículo. Ya pasó con Samurai TCG y con Pokestop.
_META = (
    r'property="tiendanube:price"\s+content="([^"]+)"',
    r'property="(?:og:)?price:amount"\s+content="([^"]+)"',
    r'content="([^"]+)"\s+property="(?:og:)?price:amount"',
    r'itemprop="price"[^>]*content="([^"]+)"',
)


def _precio_de_meta(texto: str) -> Optional[float]:
    for patron in _META:
        encaje = re.search(patron, texto)
        if encaje:
            valor = parse_price(encaje.group(1))
            if valor is not None:
                return valor
    return None


async def _leer_woo(cliente: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    """WooCommerce por su API pública, que es la fuente más fiable.

    La ficha en HTML depende del tema que use cada tienda; `/wp-json/wc/store/v1/`
    devuelve el precio y `is_in_stock` sin tener que interpretar nada. Solo se
    usa si la dirección tiene forma de ficha de producto y la API contesta con
    UNA: con varias no hay forma de saber cuál es.
    """
    partes = urlsplit(url)
    encaje = re.search(r"/(?:producto|product)/([^/?#]+)", partes.path)
    if not encaje:
        return None
    endpoint = (f"{partes.scheme}://{partes.netloc}"
                f"/wp-json/wc/store/v1/products?slug={encaje.group(1)}")
    try:
        respuesta = await cliente.get(endpoint, headers=_cabeceras(), timeout=25)
    except httpx.HTTPError:
        return None
    if respuesta.status_code >= 400:
        return None
    try:
        datos = respuesta.json()
    except ValueError:
        return None
    if not isinstance(datos, list) or len(datos) != 1:
        return None

    item = datos[0]
    precios = item.get("prices") or {}
    # La Store API devuelve enteros en la unidad mínima (minor units).
    decimales = int(precios.get("currency_minor_unit", 0) or 0)
    crudo = precios.get("price")
    precio = None
    if crudo is not None:
        try:
            precio = float(crudo) / (10 ** decimales)
        except (TypeError, ValueError):
            precio = parse_price(str(crudo))

    imagenes = item.get("images") or []
    return {
        "http": respuesta.status_code,
        "nombre": item.get("name"),
        "precio": precio,
        "stock": STOCK_IN if item.get("is_in_stock") else STOCK_OUT,
        "imagen": (imagenes[0].get("src")
                   if imagenes and isinstance(imagenes[0], dict) else None),
    }


async def _pedir_json(cliente: httpx.AsyncClient, url: str) -> Optional[Any]:
    try:
        respuesta = await cliente.get(url, headers=_cabeceras(), timeout=25)
    except httpx.HTTPError:
        return None
    if respuesta.status_code >= 400:
        return None
    try:
        return respuesta.json()
    except ValueError:
        return None


async def _leer_shopify(cliente: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    """Shopify por el JSON de la ficha, que es lo único que sabe de variantes.

    `?variant=123` cambia lo que ves en pantalla pero NO cambia el
    `og:price:amount` de la página, que sigue siendo el de la variante por
    defecto. Leer la etiqueta daba el precio de otra versión del producto —el
    inglés por el español—, y además el HTML no suele declarar si hay stock.

    Se pide primero `.js`, que trae `available` variante a variante; es el
    único sitio donde está. El `.json` de la misma ficha NO lo incluye —cosa de
    Shopify, no un fallo—, así que sirve de reserva para el precio y el nombre
    y la disponibilidad se completa luego con el HTML.
    """
    partes = urlsplit(url)
    if "/products/" not in partes.path:
        return None
    base = f"{partes.scheme}://{partes.netloc}{partes.path.rstrip('/')}"

    # En `.js` el precio va en céntimos: 3899000 son $38.990. En `.json` va
    # como texto decimal. Cada uno se interpreta a su manera.
    datos = await _pedir_json(cliente, f"{base}.js")
    if isinstance(datos, dict) and datos.get("variants"):
        producto, en_centimos = datos, True
    else:
        envoltorio = await _pedir_json(cliente, f"{base}.json")
        producto = (envoltorio or {}).get("product") if isinstance(envoltorio, dict) else None
        en_centimos = False
    if not isinstance(producto, dict):
        return None

    variantes = [v for v in (producto.get("variants") or []) if isinstance(v, dict)]
    if not variantes:
        return None

    # La que pida la dirección; si no pide ninguna —o pide una que ya no
    # existe— la primera, que es la que la tienda enseña por defecto.
    elegida = variantes[0]
    encaje = re.search(r"[?&]variant=(\d+)", url)
    if encaje:
        pedida = encaje.group(1)
        elegida = next((v for v in variantes if str(v.get("id")) == pedida), elegida)

    crudo = elegida.get("price")
    precio = None
    if crudo is not None:
        precio = float(crudo) / 100 if en_centimos else parse_price(str(crudo))

    nombre = producto.get("title")
    sufijo = str(elegida.get("title") or "").strip()
    if sufijo and sufijo.lower() != "default title":
        nombre = f"{nombre} — {sufijo}"

    imagenes = producto.get("images") or []
    imagen = imagenes[0] if imagenes else None
    if isinstance(imagen, dict):
        imagen = imagen.get("src")

    # Sin `available` no se afirma nada: darlo por falso sería anunciar «se
    # agotó» de algo que está a la venta.
    disponible = elegida.get("available")
    return {
        "http": 200,
        "nombre": nombre,
        "precio": precio,
        "stock": (STOCK_UNKNOWN if disponible is None
                  else (STOCK_IN if disponible else STOCK_OUT)),
        "imagen": imagen if isinstance(imagen, str) else None,
    }


async def _leer_html(cliente: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    try:
        respuesta = await cliente.get(url, headers=_cabeceras(), timeout=25)
    except httpx.HTTPError as exc:
        return {"http": None, "error": str(exc)}

    if respuesta.status_code >= 400:
        # 404 no es un fallo: es la respuesta esperada mientras la ficha de una
        # preventa todavía no está publicada.
        return {"http": respuesta.status_code}

    texto = respuesta.text
    sopa = soup_of(texto)
    ficha = product_from_json_ld(extract_json_ld(sopa)) or {}

    precio = _precio_de_meta(texto)
    if precio is None:
        precio = parse_price(str(ficha.get("price") or ""))

    nombre = ficha.get("name")
    if not nombre:
        etiqueta = sopa.find("meta", attrs={"property": "og:title"})
        nombre = (etiqueta.get("content") if etiqueta
                  else (sopa.title.get_text(strip=True) if sopa.title else None))

    imagen = ficha.get("image_url")
    if not imagen:
        etiqueta = sopa.find("meta", attrs={"property": "og:image"})
        imagen = etiqueta.get("content") if etiqueta else None

    return {
        "http": respuesta.status_code,
        "nombre": (str(nombre or "").strip() or None),
        "precio": precio,
        "stock": _disponibilidad(ficha.get("availability")),
        "imagen": imagen,
    }


async def leer(cliente: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    """Estado actual de una ficha: nombre, precio y disponibilidad.

    Se prueban primero las dos plataformas que publican un JSON propio, porque
    ahí el dato viene sin interpretar. El HTML es el último recurso: sirve para
    cualquier tienda, pero es donde más fácil es leer el precio de la ficha de
    al lado.
    """
    por_woo = await _leer_woo(cliente, url)
    if por_woo:
        return _limpiar(por_woo)

    por_json = await _leer_shopify(cliente, url)
    if por_json and por_json.get("stock") != STOCK_UNKNOWN:
        return _limpiar(por_json)

    por_html = await _leer_html(cliente, url)
    if por_json and not por_html.get("error"):
        # El precio del JSON manda —es el de la variante que pide la
        # dirección, y la etiqueta og: de la página es siempre el de la
        # variante por defecto—, pero la disponibilidad la sabe el HTML.
        por_json["stock"] = por_html.get("stock") or STOCK_UNKNOWN
        por_json["imagen"] = por_json.get("imagen") or por_html.get("imagen")
        return _limpiar(por_json)
    return _limpiar(por_html)


def _limpiar(estado: Dict[str, Any]) -> Dict[str, Any]:
    """Deshace las entidades HTML del nombre.

    Varias tiendas lo devuelven escapado —«TRAINER&#39;S TOOLKIT»—, y al
    componer el mensaje se volvería a escapar: el grupo leería «&amp;#39;».
    """
    nombre = estado.get("nombre")
    if nombre:
        estado["nombre"] = html.unescape(str(nombre)).strip() or None
    return estado


# ---------------------------------------------------------------------------
# Qué merece un mensaje
# ---------------------------------------------------------------------------
def _cambio(antes: Optional[Dict[str, Any]],
            ahora: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compara con la última vez y decide si hay algo que contar.

    Devuelve None cuando no lo hay, que es el caso normal: se mira cada hora y
    casi siempre está todo igual.

    Un enlace recién puesto en la lista NO se anuncia. Añadir algo que lleva
    meses a la venta no es noticia, y si no fuera así, pegar una dirección
    provocaría un mensaje al grupo por el simple hecho de pegarla.
    """
    if antes is None:
        return None
    if not ahora.get("http") or int(ahora["http"]) >= 400:
        return None

    stock = ahora.get("stock") or STOCK_UNKNOWN
    precio = ahora.get("precio")
    comprable = stock in COMPRABLE

    antes_existia = bool(antes.get("http_status")) and int(antes["http_status"]) < 400
    antes_stock = antes.get("stock_status") or STOCK_UNKNOWN
    antes_precio = antes.get("price")

    # La ficha no existía y ahora sí: la preventa acaba de publicarse.
    if not antes_existia:
        return {"icono": "🚨",
                "titular": "YA ESTÁ PUBLICADO" if comprable else "YA APARECIÓ LA FICHA"}

    # De no poder comprarse a poder comprarse. Este es el aviso que justifica
    # todo lo demás.
    if comprable and antes_stock not in COMPRABLE:
        return {"icono": "🚨",
                "titular": ("YA SE PUEDE RESERVAR" if stock == STOCK_PREORDER
                            else "YA SE PUEDE COMPRAR")}

    if not comprable and antes_stock in COMPRABLE:
        return {"icono": "⛔", "titular": "SE AGOTÓ"}

    # Cambio de precio con la ficha ya publicada. Un peso de holgura, por si la
    # tienda redondea distinto en la API que en el HTML.
    if precio is not None and antes_precio is not None:
        anterior = float(antes_precio)
        if abs(precio - anterior) > 1:
            subida = precio > anterior
            return {"icono": "📈" if subida else "📉",
                    "titular": "SUBIÓ DE PRECIO" if subida else "BAJÓ DE PRECIO",
                    "antes": anterior}
    return None


def _mensaje(enlace: Dict[str, Any], ahora: Dict[str, Any],
             cambio: Dict[str, Any]) -> str:
    from app.services.notify import _pesos

    nombre = enlace.get("etiqueta") or ahora.get("nombre") or "Producto vigilado"
    filas = [
        f"{cambio['icono']} <b>{cambio['titular']}</b>",
        f"<b>{html.escape(str(nombre))}</b>",
    ]

    precio = ahora.get("precio")
    anterior = cambio.get("antes")
    if precio is not None and anterior:
        pct = abs(precio - anterior) / anterior * 100
        filas.append(f"Precio · {_pesos(precio)} "
                     f"(<s>{_pesos(anterior)}</s> / {pct:.0f} %)")
    elif precio is not None:
        filas.append(f"Precio · {_pesos(precio)}")

    if ahora.get("stock") == STOCK_PREORDER:
        filas.append("En preventa")

    filas.append(f'<a href="{html.escape(str(enlace["url"]), quote=True)}">Ver en la tienda</a>')
    return "\n".join(filas)


# ---------------------------------------------------------------------------
# La pasada
# ---------------------------------------------------------------------------
async def revisar() -> Dict[str, Any]:
    """Mira todos los enlaces vigilados y anuncia lo que haya cambiado."""
    from app.services import notify

    lista = enlaces()
    if not lista:
        return {"enlaces": 0, "avisos": 0}

    previos = {fila["url"]: dict(fila) for fila in query("SELECT * FROM vigilancia")}
    cfg = notify.config()
    encendido = bool(cfg.get("enabled"))
    simulacion = bool(cfg.get("dry_run", True))
    con_imagen = bool(cfg.get("include_image", True))
    pausa = float(cfg.get("delay_between_messages", 3) or 0)

    # De una en una y no en paralelo: son pocas, y así no se golpea a la misma
    # tienda con varias peticiones justo el día que más carga tiene.
    lecturas: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    async with httpx.AsyncClient(follow_redirects=True) as cliente:
        for enlace in lista:
            estado = await leer(cliente, enlace["url"])
            lecturas.append((enlace, estado))
            if estado.get("error"):
                log("warn", "vigilancia",
                    f"No se pudo mirar {enlace['url']}: {estado['error']}")

    avisos = 0
    for enlace, estado in lecturas:
        cambio = _cambio(previos.get(enlace["url"]), estado)
        es_nuevo = enlace["url"] not in previos
        _guardar(enlace, estado, avisado=bool(cambio))

        if es_nuevo:
            log("info", "vigilancia",
                f"Enlace nuevo en la lista: {enlace['url']} "
                f"(estado inicial: {estado.get('stock') or 'sin ficha'}). "
                f"A partir de la próxima pasada se avisa de lo que cambie.")
        if not cambio:
            continue

        texto = _mensaje(enlace, estado, cambio)
        if simulacion or not encendido:
            log("info", "vigilancia", f"[simulación] {texto}")
            continue
        try:
            await notify.enviar(texto, estado.get("imagen") if con_imagen else None)
        except notify.TelegramRechazo as exc:
            log("warn", "vigilancia", f"No se pudo anunciar {enlace['url']}: {exc}")
            continue
        avisos += 1
        log("info", "vigilancia", f"{cambio['titular']}: {enlace['url']}")
        if pausa:
            await asyncio.sleep(pausa)

    return {"enlaces": len(lista), "avisos": avisos}


def _guardar(enlace: Dict[str, Any], estado: Dict[str, Any], avisado: bool) -> None:
    """Deja apuntado lo leído, para poder comparar en la próxima pasada.

    Una lectura que falló por red NO se guarda. Si se guardara, un corte de un
    minuto se leería después como «la ficha desapareció y volvió a aparecer», y
    el grupo recibiría un aviso por algo que nunca ocurrió.
    """
    if estado.get("error"):
        return

    with transaction() as conn:
        conn.execute(
            """INSERT INTO vigilancia
                   (url, etiqueta, nombre, price, stock_status, http_status,
                    image_url, visto_at, avisado_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'),
                       CASE WHEN ? = 1 THEN datetime('now') ELSE NULL END)
               ON CONFLICT(url) DO UPDATE SET
                   etiqueta     = excluded.etiqueta,
                   nombre       = COALESCE(excluded.nombre, vigilancia.nombre),
                   price        = excluded.price,
                   stock_status = excluded.stock_status,
                   http_status  = excluded.http_status,
                   image_url    = COALESCE(excluded.image_url, vigilancia.image_url),
                   visto_at     = excluded.visto_at,
                   avisado_at   = COALESCE(excluded.avisado_at, vigilancia.avisado_at)""",
            (enlace["url"], enlace.get("etiqueta"), estado.get("nombre"),
             estado.get("precio"), estado.get("stock"), estado.get("http"),
             estado.get("imagen"), 1 if avisado else 0),
        )
