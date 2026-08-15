"""Adaptadores para tiendas que exponen sus productos en JSON.

Si una tienda tiene un endpoint público accesible sin autenticación es
preferible usarlo antes que raspar el HTML: es más rápido, más estable y
carga menos al servidor de la tienda.

    GenericJsonAdapter   -> cualquier API descrita por YAML
    ShopifyAdapter       -> /collections/<handle>/products.json
    WooCommerceAdapter   -> /wp-json/wc/store/v1/products
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, AsyncIterator, Dict, List, Optional

from app.scrapers.base import Category, RawProduct, StoreAdapter
from app.scrapers.parsing import (
    STOCK_IN,
    STOCK_OUT,
    STOCK_UNKNOWN,
    absolute_url,
    clean_url,
    dig,
    parse_price,
    parse_stock,
    strip_html,
)


class GenericJsonAdapter(StoreAdapter):
    """Recorre endpoints JSON descritos en el YAML de la tienda."""

    adapter_name = "json"

    # Los endpoints hacen de "categorías".
    async def get_categories(self) -> List[Category]:
        endpoints = self.config.get("endpoints") or []
        return [
            Category(
                name=item.get("name") or item["url"],
                url=absolute_url(self.base_url + "/", item["url"]) or item["url"],
                game=item.get("game") or self.default_game,
            )
            for item in endpoints
            if item.get("url")
        ]

    async def get_product_urls(self, category: Category) -> List[str]:
        # No aplica: este adaptador produce los productos directamente.
        return []

    async def parse_product(
        self, url: str, category: Optional[Category] = None
    ) -> Optional[RawProduct]:
        return None

    def _page_urls(self, category: Category) -> List[str]:
        pagination = self.config.get("pagination") or {}
        if pagination.get("mode") != "query":
            return [category.url]
        param = pagination.get("param", "page")
        start = int(pagination.get("start", 1))
        max_pages = int(pagination.get("max_pages", self.limit("max_pages_per_category", 50)))
        separator = "&" if "?" in category.url else "?"
        return [
            f"{category.url}{separator}{param}={page}"
            for page in range(start, start + max_pages)
        ]

    def extract_items(self, payload: Any) -> List[Dict[str, Any]]:
        path = self.config.get("list_path")
        items = dig(payload, path, payload) if path else payload
        if isinstance(items, dict):
            items = items.get("items") or items.get("results") or []
        return items if isinstance(items, list) else []

    def build_product(self, item: Dict[str, Any], category: Optional[Category]) -> Optional[RawProduct]:
        fields: Dict[str, str] = self.config.get("fields", {}) or {}

        name = dig(item, fields.get("name", "name"))
        if not name:
            return None

        raw_url = dig(item, fields.get("url", "url"))
        template = self.config.get("url_template")
        if template and raw_url is not None:
            raw_url = template.replace("{value}", str(raw_url))
        url = absolute_url(self.base_url + "/", str(raw_url)) if raw_url else None
        if not url:
            return None

        price = parse_price(dig(item, fields.get("price", "price")))
        divisor = float(self.config.get("price_divisor", 1) or 1)
        if price is not None and divisor != 1:
            price = price / divisor

        stock_value = dig(item, fields.get("stock", "available"))
        if isinstance(stock_value, bool):
            stock = STOCK_IN if stock_value else STOCK_OUT
        else:
            stock = parse_stock(stock_value, STOCK_UNKNOWN)

        image = dig(item, fields.get("image", "image"))
        if isinstance(image, dict):
            image = image.get("src") or image.get("url")
        if isinstance(image, list):
            image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("src") or image.get("url")

        external_id = dig(item, fields.get("external_id", "id"))

        return RawProduct(
            url=clean_url(url),
            name=str(name),
            price=price,
            price_raw=str(dig(item, fields.get("price", "price")) or ""),
            currency=str(dig(item, fields.get("currency", "")) or self.currency),
            external_id=str(external_id) if external_id is not None else None,
            image_url=absolute_url(url, str(image)) if image else None,
            description=strip_html(dig(item, fields.get("description", "description"))),
            category=(category.name if category else None),
            stock_status=stock,
            stock_raw=str(stock_value) if stock_value is not None else None,
            sku=_as_text(dig(item, fields.get("sku", "sku"))),
            mpn=_as_text(dig(item, fields.get("mpn", "mpn"))),
            upc=_as_text(dig(item, fields.get("upc", "upc"))),
            ean=_as_text(dig(item, fields.get("ean", "ean"))),
            gtin=_as_text(dig(item, fields.get("gtin", "gtin"))),
            brand=_as_text(dig(item, fields.get("brand", "brand"))),
        )

    def build_products(
        self, item: Dict[str, Any], category: Optional[Category]
    ) -> List[RawProduct]:
        """Un registro del JSON puede dar lugar a varias ofertas.

        Por defecto es una sola; los adaptadores que manejan variantes
        (Shopify) devuelven una por variante.
        """
        product = self.build_product(item, category)
        return [product] if product is not None else []

    async def iter_products(self) -> AsyncIterator[RawProduct]:
        max_products = self.limit("max_products_per_store", 5000)
        emitted = 0
        seen: set[str] = set()

        for category in await self.get_categories():
            for page_url in self._page_urls(category):
                if emitted >= max_products:
                    return
                # Sin caché condicional: de un 304 no se puede extraer el
                # catálogo, y estos endpoints devuelven todos los productos.
                result, payload = await self.client.get_json(page_url, use_cache=False)
                if not result.ok:
                    self.report_error(
                        "listing", page_url, result.error or f"HTTP {result.status_code}"
                    )
                    break
                items = self.extract_items(payload)
                if not items:
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        productos = self.build_products(item, category)
                    except Exception as exc:  # noqa: BLE001
                        self.report_error("parse", page_url, f"{type(exc).__name__}: {exc}")
                        continue
                    for product in productos:
                        if product is None or product.url in seen:
                            continue
                        seen.add(product.url)
                        emitted += 1
                        self.stats.products += 1
                        yield product


class ShopifyAdapter(GenericJsonAdapter):
    """Shopify publica /products.json y /collections/<handle>/products.json."""

    adapter_name = "shopify"

    async def get_categories(self) -> List[Category]:
        collections = self.config.get("collections") or []
        if not collections:
            return [Category(name="Todo el catálogo", url=f"{self.base_url}/products.json",
                             game=self.default_game)]
        categories: List[Category] = []
        for entry in collections:
            handle = entry if isinstance(entry, str) else entry.get("handle")
            if not handle:
                continue
            name = handle if isinstance(entry, str) else entry.get("name", handle)
            game = self.default_game if isinstance(entry, str) else entry.get("game", self.default_game)
            categories.append(
                Category(
                    name=name,
                    url=f"{self.base_url}/collections/{handle}/products.json",
                    game=game,
                )
            )
        return categories

    def _page_urls(self, category: Category) -> List[str]:
        limit = int(self.config.get("page_size", 250))
        max_pages = int(self.config.get("max_pages", self.limit("max_pages_per_category", 50)))
        separator = "&" if "?" in category.url else "?"
        return [
            f"{category.url}{separator}limit={limit}&page={page}"
            for page in range(1, max_pages + 1)
        ]

    def extract_items(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            return payload.get("products", []) or []
        return []

    # -- variantes --------------------------------------------------------
    #
    # En Shopify una "variante" puede ser una talla... o un producto distinto.
    # En las tiendas de TCG chilenas se usa sobre todo para el IDIOMA y para
    # el personaje, y cada variante tiene su propio precio, su propio stock y
    # a menudo su propio código de barras:
    #
    #   Pokemon: Mega Evolution - Pitch Black: "Elite Trainer Box"
    #       Elite Trainer Box - Español   $64.990   disponible
    #       Elite Trainer Box - Ingles    $68.990   agotado
    #
    # Quedarse con la primera variante (o con el precio mínimo) daría un
    # precio y un stock que no corresponden a ningún producto real. Por eso
    # cada variante se publica como una oferta propia, con su URL
    # ?variant=<id>, que es la que el usuario necesita para comprarla.
    def build_products(
        self, item: Dict[str, Any], category: Optional[Category]
    ) -> List[RawProduct]:
        variants = item.get("variants") or []
        if len(variants) <= 1 or not self.config.get("expand_variants", True):
            return super().build_products(item, category)

        title = item.get("title")
        handle = item.get("handle")
        if not title or not handle:
            return []

        images = item.get("images") or []
        imagen_producto = None
        if images:
            imagen_producto = images[0].get("src") if isinstance(images[0], dict) else str(images[0])

        salida: List[RawProduct] = []
        for variante in variants:
            if not isinstance(variante, dict) or variante.get("id") is None:
                continue

            sufijo = _variant_suffix(str(title), str(variante.get("title") or ""))
            nombre = f"{title} - {sufijo}" if sufijo else str(title)

            barcode = _as_text(variante.get("barcode"))
            imagen = variante.get("featured_image")
            if isinstance(imagen, dict):
                imagen = imagen.get("src")

            salida.append(
                RawProduct(
                    # Sin clean_url: aquí la query ES la identidad de la oferta.
                    url=f"{self.base_url}/products/{handle}?variant={variante['id']}",
                    name=nombre,
                    price=parse_price(variante.get("price")),
                    price_raw=str(variante.get("price") or ""),
                    currency=self.currency,
                    external_id=str(variante["id"]),
                    image_url=imagen if isinstance(imagen, str) else imagen_producto,
                    description=strip_html(item.get("body_html")),
                    category=(category.name if category else item.get("product_type")),
                    stock_status=STOCK_IN if variante.get("available") else STOCK_OUT,
                    stock_raw=str(variante.get("available")),
                    sku=_as_text(variante.get("sku")),
                    ean=barcode if barcode and len(barcode) == 13 else None,
                    upc=barcode if barcode and len(barcode) == 12 else None,
                    gtin=barcode,
                    brand=_as_text(item.get("vendor")),
                    extra={
                        "product_type": item.get("product_type"),
                        "variant_title": variante.get("title"),
                        "variant_of": handle,
                    },
                )
            )
        return salida

    def build_product(self, item: Dict[str, Any], category: Optional[Category]) -> Optional[RawProduct]:
        title = item.get("title")
        handle = item.get("handle")
        if not title or not handle:
            return None

        variants = item.get("variants") or []
        first = variants[0] if variants else {}
        prices = [parse_price(v.get("price")) for v in variants if v.get("price") is not None]
        prices = [p for p in prices if p is not None]
        price = min(prices) if prices else parse_price(first.get("price"))

        available = any(v.get("available") for v in variants) if variants else None
        stock = STOCK_UNKNOWN if available is None else (STOCK_IN if available else STOCK_OUT)

        images = item.get("images") or []
        image = None
        if images:
            image = images[0].get("src") if isinstance(images[0], dict) else str(images[0])

        barcode = first.get("barcode")

        return RawProduct(
            url=clean_url(f"{self.base_url}/products/{handle}"),
            name=str(title),
            price=price,
            price_raw=str(first.get("price") or ""),
            currency=self.currency,
            external_id=str(item.get("id")) if item.get("id") is not None else None,
            image_url=image,
            description=strip_html(item.get("body_html")),
            category=(category.name if category else item.get("product_type")),
            stock_status=stock,
            stock_raw=str(available) if available is not None else None,
            sku=_as_text(first.get("sku")),
            ean=_as_text(barcode) if barcode and len(str(barcode)) == 13 else None,
            upc=_as_text(barcode) if barcode and len(str(barcode)) == 12 else None,
            gtin=_as_text(barcode),
            brand=_as_text(item.get("vendor")),
            extra={"product_type": item.get("product_type"), "tags": item.get("tags")},
        )


class WooCommerceAdapter(GenericJsonAdapter):
    """WooCommerce Store API pública: /wp-json/wc/store/v1/products."""

    adapter_name = "woocommerce"

    async def get_categories(self) -> List[Category]:
        categories = self.config.get("categories") or []
        endpoint = f"{self.base_url}/wp-json/wc/store/v1/products"
        if not categories:
            return [Category(name="Todo el catálogo", url=endpoint, game=self.default_game)]
        out: List[Category] = []
        for entry in categories:
            slug = entry if isinstance(entry, str) else entry.get("slug")
            name = slug if isinstance(entry, str) else entry.get("name", slug)
            game = self.default_game if isinstance(entry, str) else entry.get("game", self.default_game)
            out.append(Category(name=name, url=f"{endpoint}?category={slug}", game=game))
        return out

    def _page_urls(self, category: Category) -> List[str]:
        per_page = int(self.config.get("page_size", 100))
        max_pages = int(self.config.get("max_pages", self.limit("max_pages_per_category", 50)))
        separator = "&" if "?" in category.url else "?"
        return [
            f"{category.url}{separator}per_page={per_page}&page={page}"
            for page in range(1, max_pages + 1)
        ]

    def extract_items(self, payload: Any) -> List[Dict[str, Any]]:
        return payload if isinstance(payload, list) else []

    def build_product(self, item: Dict[str, Any], category: Optional[Category]) -> Optional[RawProduct]:
        name = item.get("name")
        permalink = item.get("permalink")
        if not name or not permalink:
            return None

        prices = item.get("prices") or {}
        # La Store API devuelve enteros en la unidad mínima (minor units).
        minor = int(prices.get("currency_minor_unit", 0) or 0)
        raw_price = prices.get("price")
        price = None
        if raw_price is not None:
            try:
                price = float(raw_price) / (10 ** minor)
            except (TypeError, ValueError):
                price = parse_price(raw_price)

        images = item.get("images") or []
        image = images[0].get("src") if images and isinstance(images[0], dict) else None

        return RawProduct(
            url=clean_url(str(permalink)),
            name=str(name),
            price=price,
            price_raw=str(raw_price or ""),
            currency=str(prices.get("currency_code") or self.currency),
            external_id=str(item.get("id")) if item.get("id") is not None else None,
            image_url=image,
            description=strip_html(item.get("description") or item.get("short_description")),
            category=(category.name if category else None),
            stock_status=STOCK_IN if item.get("is_in_stock") else STOCK_OUT,
            stock_raw=str(item.get("is_in_stock")),
            sku=_as_text(item.get("sku")),
        )


def _variant_suffix(titulo: str, titulo_variante: str) -> str:
    """Devuelve solo lo que la variante añade al nombre del producto.

    Las tiendas suelen repetir el nombre del producto dentro del de la
    variante. Concatenarlos tal cual daría nombres absurdos:

        'Pitch Black: "Elite Trainer Box"' + 'Elite Trainer Box - Español'

    Nos quedamos con las palabras que de verdad son nuevas -> 'Español'.
    """
    if not titulo_variante or titulo_variante.strip().lower() == "default title":
        return ""

    import unicodedata

    def plano(texto: str) -> str:
        sin_tildes = "".join(
            c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
        )
        return sin_tildes.lower()

    ya_esta = set(re.findall(r"[a-z0-9]+", plano(titulo)))
    # Excepción: el idioma nunca es redundante. Hay tiendas que anuncian los
    # dos en el título del producto y luego los separan en las variantes:
    #
    #   'Rival Battle deck Steven - Ingles y Español'
    #       variante 'Español'   ← "español" ya salía en el título…
    #       variante 'Ingles'    ← …e "ingles" también
    #
    # Descartándolos por repetidos, las dos variantes quedaban con el mismo
    # nombre y ambas se etiquetaban igual. Es justo lo contrario: el idioma
    # es lo único que las distingue.
    idiomas = _language_words()
    nuevas = [
        palabra
        for palabra in re.split(r"[^\w]+", titulo_variante.strip())
        if palabra and (plano(palabra) not in ya_esta or plano(palabra) in idiomas)
    ]
    return " ".join(nuevas)


@lru_cache(maxsize=1)
def _language_words() -> frozenset:
    """Marcas de idioma configuradas en normalization.yaml."""
    from app import settings

    datos = settings.load_normalization() or {}
    return frozenset(str(t).strip().lower() for t in (datos.get("language_tokens") or []) if t)


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
