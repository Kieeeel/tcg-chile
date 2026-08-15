"""Contrato que debe cumplir el adaptador de cada tienda.

Para añadir una tienda nueva NO hay que tocar el resto del sistema: basta con
un YAML en config/stores/ (adaptadores genéricos) o una subclase de
StoreAdapter registrada en el registry (casos especiales).
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from app.scrapers.http_client import HttpClient


@dataclass
class Category:
    name: str
    url: str
    game: Optional[str] = None


@dataclass
class RawProduct:
    """Producto tal como lo publica la tienda, antes de normalizar."""

    url: str
    name: str
    price: Optional[float] = None
    price_raw: Optional[str] = None
    currency: Optional[str] = None
    external_id: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    stock_status: str = "unknown"
    stock_raw: Optional[str] = None
    sku: Optional[str] = None
    mpn: Optional[str] = None
    upc: Optional[str] = None
    ean: Optional[str] = None
    gtin: Optional[str] = None
    brand: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScrapeStats:
    products: int = 0
    errors: int = 0
    requests: int = 0
    cached: int = 0


ErrorReporter = Callable[[str, Optional[str], str], None]


class StoreAdapter(ABC):
    """Interfaz de una tienda.

    Métodos que puede implementar un adaptador concreto:
        get_categories()    -> categorías/colecciones a recorrer
        get_product_urls()  -> URLs de ficha dentro de una categoría
        parse_product()     -> convierte una ficha en RawProduct
        get_price() / get_stock() / get_product_id()  -> extractores puntuales

    `iter_products()` ya trae una implementación por defecto que encadena los
    tres primeros; un adaptador basado en API puede sobrescribirla.
    """

    adapter_name: str = "base"

    def __init__(
        self,
        store: Dict[str, Any],
        config: Dict[str, Any],
        client: HttpClient,
        report_error: Optional[ErrorReporter] = None,
    ) -> None:
        self.store = store
        self.config = config or {}
        self.client = client
        self.code: str = store["code"]
        self.name: str = store["name"]
        self.base_url: str = store["base_url"].rstrip("/")
        self.currency: str = store.get("currency") or "CLP"
        self.default_game: Optional[str] = self.config.get("default_game")
        self._report_error = report_error
        self.stats = ScrapeStats()
        # Fichas que el servidor respondió con 304 Not Modified: no se
        # descargaron ni se parsearon, pero siguen existiendo en la tienda.
        # El pipeline las marca como vistas para no darlas de baja.
        self.unchanged_urls: set[str] = set()

    # -- utilidades ---------------------------------------------------------
    def report_error(self, stage: str, url: Optional[str], message: str) -> None:
        self.stats.errors += 1
        if self._report_error:
            self._report_error(stage, url, message)

    def limit(self, key: str, default: int) -> int:
        from app import settings

        return int(self.config.get(key, settings.get(f"scraping.{key}", default)))

    # -- contrato -----------------------------------------------------------
    @abstractmethod
    async def get_categories(self) -> List[Category]:
        """Categorías/colecciones que hay que recorrer."""

    @abstractmethod
    async def get_product_urls(self, category: Category) -> List[str]:
        """URLs de ficha de producto dentro de una categoría (con paginación)."""

    @abstractmethod
    async def parse_product(self, url: str, category: Optional[Category] = None) -> Optional[RawProduct]:
        """Descarga y convierte una ficha de producto."""

    # -- extractores puntuales (sobrescribibles) -----------------------------
    def get_price(self, node: Any) -> Optional[float]:
        from app.scrapers.parsing import parse_price, select_text

        selectors = self.config.get("selectors", {}) or {}
        for selector in _as_list(selectors.get("price")):
            value = parse_price(select_text(node, selector))
            if value is not None:
                return value
        return None

    def get_stock(self, node: Any) -> str:
        from app.scrapers.parsing import STOCK_UNKNOWN, parse_stock, select_text

        selectors = self.config.get("selectors", {}) or {}
        for selector in _as_list(selectors.get("stock")):
            status = parse_stock(select_text(node, selector), STOCK_UNKNOWN)
            if status != STOCK_UNKNOWN:
                return status
        return STOCK_UNKNOWN

    def get_product_id(self, node: Any, url: str) -> Optional[str]:
        from app.scrapers.parsing import select_attr, select_text

        selectors = self.config.get("selectors", {}) or {}
        for selector in _as_list(selectors.get("external_id")):
            value = select_text(node, selector) or select_attr(node, selector, "content")
            if value:
                return value
        return None

    # -- recorrido por defecto ----------------------------------------------
    async def iter_products(self) -> AsyncIterator[RawProduct]:
        max_products = self.limit("max_products_per_store", 5000)
        seen: set[str] = set()
        emitted = 0

        try:
            categories = await self.get_categories()
        except Exception as exc:  # noqa: BLE001 - un fallo aquí no debe matar el run
            self.report_error("categories", self.base_url, f"{type(exc).__name__}: {exc}")
            return

        for category in categories:
            if emitted >= max_products:
                break
            try:
                urls = await self.get_product_urls(category)
            except Exception as exc:  # noqa: BLE001
                self.report_error("listing", category.url, f"{type(exc).__name__}: {exc}")
                continue

            fresh = [u for u in urls if u not in seen]
            seen.update(fresh)
            fresh = fresh[: max(0, max_products - emitted)]

            # Las fichas se piden en paralelo, pero el HttpClient impone el
            # ritmo y la concurrencia máxima configurada para esta tienda.
            tasks = [self._safe_parse(url, category) for url in fresh]
            for coro in asyncio.as_completed(tasks):
                for product in await coro:
                    if emitted >= max_products:
                        break
                    emitted += 1
                    self.stats.products += 1
                    yield product

    async def parse_products(
        self, url: str, category: Optional[Category] = None
    ) -> List[RawProduct]:
        """Una URL puede esconder varias ofertas.

        Lo normal es que sea una sola. Las tiendas cuyas variantes viven en la
        misma ficha —idioma en Bsale, por ejemplo— sobrescriben este método
        para devolver una oferta por variante.
        """
        product = await self.parse_product(url, category)
        return [product] if product is not None else []

    async def _safe_parse(self, url: str, category: Category) -> List[RawProduct]:
        try:
            return await self.parse_products(url, category)
        except Exception as exc:  # noqa: BLE001
            self.report_error("product", url, f"{type(exc).__name__}: {exc}")
            return []


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []
