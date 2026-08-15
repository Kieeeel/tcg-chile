"""Adaptador genérico para tiendas con HTML estático.

Toda la tienda se describe en un YAML con selectores CSS; no hace falta
escribir código para añadirla. Ver config/stores/*.yaml para ejemplos.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from app.scrapers.base import Category, RawProduct, StoreAdapter, _as_list
from app.scrapers.parsing import (
    STOCK_UNKNOWN,
    absolute_url,
    clean_url,
    extract_json_ld,
    parse_price,
    parse_stock,
    product_from_json_ld,
    select_attr,
    select_text,
    soup_of,
    strip_html,
)


class HtmlStoreAdapter(StoreAdapter):
    adapter_name = "html"

    # -- categorías ---------------------------------------------------------
    async def get_categories(self) -> List[Category]:
        declared = self.config.get("categories") or []
        categories = [
            Category(
                name=item.get("name") or item["url"],
                url=absolute_url(self.base_url + "/", item["url"]) or item["url"],
                game=item.get("game") or self.default_game,
            )
            for item in declared
            if item.get("url")
        ]

        discovery = self.config.get("category_discovery") or {}
        if discovery.get("url") and discovery.get("selector"):
            url = absolute_url(self.base_url + "/", discovery["url"]) or discovery["url"]
            result = await self.client.get(url, use_cache=False)
            if result.ok and result.text:
                soup = soup_of(result.text)
                for anchor in soup.select(discovery["selector"]):
                    href = absolute_url(url, anchor.get("href"))
                    if not href:
                        continue
                    categories.append(
                        Category(
                            name=" ".join(anchor.get_text(" ", strip=True).split()) or href,
                            url=href,
                            game=discovery.get("game") or self.default_game,
                        )
                    )
            else:
                self.report_error("categories", url, result.error or "sin contenido")

        # Deduplicar conservando el orden declarado.
        seen: set[str] = set()
        unique: List[Category] = []
        for category in categories:
            key = clean_url(category.url)
            if key in seen:
                continue
            seen.add(key)
            unique.append(category)
        return unique

    # -- páginas de listado --------------------------------------------------
    def _page_urls(self, category: Category) -> List[str]:
        pagination = self.config.get("pagination") or {}
        mode = pagination.get("mode", "none")
        max_pages = int(pagination.get("max_pages", self.limit("max_pages_per_category", 50)))

        if mode != "query":
            return [category.url]

        param = pagination.get("param", "page")
        start = int(pagination.get("start", 1))
        separator = "&" if "?" in category.url else "?"
        return [
            f"{category.url}{separator}{param}={page}"
            for page in range(start, start + max_pages)
        ]

    async def _listing_items(self, url: str) -> tuple[List[Any], Optional[str], str]:
        """Devuelve (nodos de producto, URL de la página siguiente, base para URLs)."""
        # Las páginas de listado se piden siempre completas: de un 304 no se
        # pueden extraer las URLs de los productos.
        result = await self.client.get(url, use_cache=False)
        if not result.ok:
            self.report_error("listing", url, result.error or f"HTTP {result.status_code}")
            return [], None, url
        if not result.text:
            return [], None, url

        listing = self.config.get("listing") or {}
        soup = soup_of(result.text)
        items = soup.select(listing.get("item", "")) if listing.get("item") else []

        next_url = None
        pagination = self.config.get("pagination") or {}
        if pagination.get("mode") == "link" and pagination.get("next_selector"):
            next_url = absolute_url(url, select_attr(soup, pagination["next_selector"], "href"))
        return items, next_url, url

    async def get_product_urls(self, category: Category) -> List[str]:
        listing = self.config.get("listing") or {}
        link_selector = listing.get("link")
        pagination = self.config.get("pagination") or {}
        mode = pagination.get("mode", "none")
        max_pages = int(pagination.get("max_pages", self.limit("max_pages_per_category", 50)))

        urls: List[str] = []
        seen: set[str] = set()

        if mode == "link":
            next_url: Optional[str] = category.url
            pages = 0
            while next_url and pages < max_pages:
                items, next_url, base = await self._listing_items(next_url)
                if not items:
                    break
                urls.extend(self._links_from(items, link_selector, base, seen))
                pages += 1
        else:
            for page_url in self._page_urls(category):
                items, _next, base = await self._listing_items(page_url)
                if not items:
                    break  # página vacía = fin de la paginación
                urls.extend(self._links_from(items, link_selector, base, seen))
        return urls

    def _links_from(
        self, items: List[Any], link_selector: Optional[str], base: str, seen: set[str]
    ) -> List[str]:
        out: List[str] = []
        for item in items:
            href = None
            if link_selector:
                href = select_attr(item, link_selector, "href")
            if not href and item.name == "a":
                href = item.get("href")
            if not href:
                anchor = item.select_one("a[href]")
                href = anchor.get("href") if anchor else None
            url = absolute_url(base, href)
            if not url:
                continue
            url = clean_url(url)
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out

    # -- ficha de producto ---------------------------------------------------
    async def parse_product(
        self, url: str, category: Optional[Category] = None
    ) -> Optional[RawProduct]:
        result = await self.client.get(url)
        if result.not_modified:
            # La tienda confirma que la ficha no cambió: nos ahorramos la
            # descarga y el parseo, pero el producto sigue vigente.
            self.unchanged_urls.add(clean_url(url))
            return None
        if not result.ok:
            self.report_error("product", url, result.error or f"HTTP {result.status_code}")
            return None
        if not result.text:
            return None
        return self.build_product(soup_of(result.text), url, category)

    # -- variantes dentro de una misma ficha ---------------------------------
    #
    # Bsale (Third Impact, Game of Magic) vende el español y el inglés en la
    # misma URL, y los distingue con un selector que se resuelve en el
    # navegador. Mirando solo el h1 y el precio visible se pierde la mitad del
    # catálogo y encima con los datos del que esté seleccionado:
    #
    #   Pokémon: Stellar Crown Booster Pack
    #       ESPAÑOL  $5.500  disponible     <- lo único que se veía
    #       INGLES   $6.500  agotado        <- se perdía
    #
    # El JSON-LD sí publica los dos, cada uno con su nombre, su SKU, su precio
    # y su disponibilidad. Con `expand_json_ld_variants: true` se emite una
    # oferta por variante.
    #
    # Solo cuentan los bloques cuyo `offers.url` es esta misma ficha: hay
    # tiendas (Tiendanube) que meten en el JSON-LD los productos del carrusel
    # de recomendados, y esos son otras fichas, no variantes de esta.
    async def parse_products(
        self, url: str, category: Optional[Category] = None
    ) -> List[RawProduct]:
        if not self.config.get("expand_json_ld_variants", False):
            return await super().parse_products(url, category)

        result = await self.client.get(url)
        if result.not_modified:
            self.unchanged_urls.add(clean_url(url))
            return []
        if not result.ok:
            self.report_error("product", url, result.error or f"HTTP {result.status_code}")
            return []
        if not result.text:
            return []

        soup = soup_of(result.text)
        propios = [
            block
            for block in extract_json_ld(soup)
            if _is_product(block) and _same_page(block, url)
        ]
        if len(propios) < 2:
            product = self.build_product(soup, url, category)
            return [product] if product is not None else []

        base = self.build_product(soup, url, category)
        limpia = clean_url(url)
        salida: List[RawProduct] = []
        for block in propios:
            datos = product_from_json_ld([block])
            nombre = datos.get("name")
            sku = datos.get("sku")
            if not nombre or not sku:
                continue
            # Bsale no tiene una URL por variante: el selector es de navegador.
            # El ancla mantiene única la identidad de cada oferta y el enlace
            # sigue abriendo la ficha correcta.
            salida.append(
                RawProduct(
                    url=f"{limpia}#{sku}",
                    name=nombre,
                    price=parse_price(datos.get("price")),
                    price_raw=str(datos.get("price") or ""),
                    currency=self.currency,
                    external_id=str(sku),
                    image_url=datos.get("image_url") or (base.image_url if base else None),
                    description=(base.description if base else None),
                    category=(category.name if category else None),
                    stock_status=parse_stock(
                        str(datos.get("availability") or "").rsplit("/", 1)[-1], STOCK_UNKNOWN
                    ),
                    stock_raw=str(datos.get("availability") or "") or None,
                    sku=str(sku),
                    brand=datos.get("brand"),
                )
            )
        return salida or ([base] if base is not None else [])

    def build_product(
        self, node: Any, url: str, category: Optional[Category] = None
    ) -> Optional[RawProduct]:
        selectors: Dict[str, Any] = self.config.get("selectors", {}) or {}
        attrs: Dict[str, Any] = self.config.get("attributes", {}) or {}

        # 1) schema.org/Product suele traer SKU, GTIN y disponibilidad limpios.
        ld: Dict[str, Any] = {}
        if self.config.get("use_json_ld", True):
            ld = product_from_json_ld(extract_json_ld(node))

        name = self._first_text(node, selectors.get("name")) or ld.get("name")
        if not name:
            return None

        price = self.get_price(node)
        if price is None:
            price = parse_price(ld.get("price"))
        price_raw = self._first_text(node, selectors.get("price"))

        stock = self.get_stock(node)
        if stock == STOCK_UNKNOWN and ld.get("availability"):
            stock = parse_stock(str(ld["availability"]).rsplit("/", 1)[-1], STOCK_UNKNOWN)
        stock_raw = self._first_text(node, selectors.get("stock")) or ld.get("availability")

        image = None
        for selector in _as_list(selectors.get("image")):
            image = select_attr(node, selector, attrs.get("image_attr", "src")) or select_attr(
                node, selector, "data-src"
            )
            if image:
                break
        if not image:
            image = ld.get("image_url")
        image = absolute_url(url, image) if image else None

        description = self._first_text(node, selectors.get("description")) or strip_html(
            ld.get("description")
        )

        # El SKU sirve de identidad solo si la tienda lo usa como tal. Hay
        # catálogos (Tiendanube) donde el mismo SKU se repite en productos
        # distintos —el de español y el de inglés comparten código—, y ahí
        # tomarlo como `external_id` hace que una ficha pise a la otra en cada
        # scraping. Con `id_from_sku: false` la identidad vuelve a ser la URL.
        external_id = self.get_product_id(node, url)
        if not external_id and self.config.get("id_from_sku", True):
            external_id = ld.get("sku")

        return RawProduct(
            url=clean_url(url),
            name=name,
            price=price,
            price_raw=price_raw,
            currency=self.currency,
            external_id=str(external_id) if external_id else None,
            image_url=image,
            description=description,
            category=(category.name if category else None)
            or self._first_text(node, selectors.get("category")),
            stock_status=stock,
            stock_raw=str(stock_raw) if stock_raw else None,
            sku=self._first_text(node, selectors.get("sku")) or ld.get("sku"),
            mpn=self._first_text(node, selectors.get("mpn")) or ld.get("mpn"),
            upc=self._first_text(node, selectors.get("upc")) or ld.get("upc"),
            ean=self._first_text(node, selectors.get("ean")) or ld.get("ean"),
            gtin=self._first_text(node, selectors.get("gtin")) or ld.get("gtin"),
            brand=self._first_text(node, selectors.get("brand")) or ld.get("brand"),
        )

    def _first_text(self, node: Any, selector: Any) -> Optional[str]:
        for candidate in _as_list(selector):
            value = select_text(node, candidate)
            if value:
                return value
            # Algunos datos viven en atributos (meta[content], data-*).
            for attribute in ("content", "value", "data-value"):
                value = select_attr(node, candidate, attribute)
                if value:
                    return value
        return None

    # -- modo "todo desde el listado" ----------------------------------------
    async def iter_products(self) -> AsyncIterator[RawProduct]:
        listing = self.config.get("listing") or {}
        if not listing.get("parse_inline"):
            async for product in super().iter_products():
                yield product
            return

        max_products = self.limit("max_products_per_store", 5000)
        emitted = 0
        seen: set[str] = set()

        try:
            categories = await self.get_categories()
        except Exception as exc:  # noqa: BLE001
            self.report_error("categories", self.base_url, f"{type(exc).__name__}: {exc}")
            return

        for category in categories:
            for page_url in self._page_urls(category):
                if emitted >= max_products:
                    return
                items, _next, base = await self._listing_items(page_url)
                if not items:
                    break
                for item in items:
                    href = select_attr(item, listing.get("link", "a"), "href") or (
                        item.select_one("a[href]").get("href")
                        if item.select_one("a[href]")
                        else None
                    )
                    product_url = absolute_url(base, href)
                    if not product_url:
                        continue
                    product_url = clean_url(product_url)
                    if product_url in seen:
                        continue
                    seen.add(product_url)
                    product = self.build_product(item, product_url, category)
                    if product is not None:
                        emitted += 1
                        self.stats.products += 1
                        yield product


def _is_product(block: Dict[str, Any]) -> bool:
    types = block.get("@type")
    types = [types] if isinstance(types, str) else (types or [])
    return any(str(t).lower() == "product" for t in types)


def _same_page(block: Dict[str, Any], url: str) -> bool:
    """¿Este bloque describe la ficha que estamos leyendo, o es un recomendado?"""
    offers = block.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    candidatas = [
        (offers or {}).get("url") if isinstance(offers, dict) else None,
        block.get("url"),
        block.get("mainEntityOfPage"),
        block.get("@id"),
    ]
    objetivo = clean_url(url).rstrip("/")
    for candidata in candidatas:
        if isinstance(candidata, dict):
            candidata = candidata.get("@id") or candidata.get("url")
        if isinstance(candidata, str) and clean_url(candidata).rstrip("/") == objetivo:
            return True
    return False
