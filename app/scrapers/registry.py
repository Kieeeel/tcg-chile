"""Registro de adaptadores.

Añadir una tienda nueva:

  A) Plataforma conocida o HTML simple -> solo un YAML en config/stores/
     indicando `adapter: html | json | shopify | woocommerce | playwright`.

  B) Caso especial -> subclase de StoreAdapter y `register(MiAdaptador)`
     desde app/scrapers/custom/.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Dict, Optional, Type

from app.scrapers.base import StoreAdapter
from app.scrapers.html_adapter import HtmlStoreAdapter
from app.scrapers.http_client import HttpClient
from app.scrapers.json_adapter import (
    GenericJsonAdapter,
    ShopifyAdapter,
    WooCommerceAdapter,
)
from app.scrapers.playwright_adapter import PlaywrightAdapter

_REGISTRY: Dict[str, Type[StoreAdapter]] = {}


def register(adapter_class: Type[StoreAdapter], name: Optional[str] = None) -> Type[StoreAdapter]:
    _REGISTRY[(name or adapter_class.adapter_name).lower()] = adapter_class
    return adapter_class


def available() -> Dict[str, str]:
    return {name: cls.__name__ for name, cls in sorted(_REGISTRY.items())}


def get(name: str) -> Optional[Type[StoreAdapter]]:
    return _REGISTRY.get((name or "").lower())


def build_adapter(
    store: Dict[str, Any],
    config: Dict[str, Any],
    client: HttpClient,
    report_error: Any = None,
) -> StoreAdapter:
    name = (store.get("adapter") or config.get("adapter") or "html").lower()
    adapter_class = get(name)
    if adapter_class is None:
        raise ValueError(
            f"Adaptador desconocido '{name}'. Disponibles: {', '.join(sorted(_REGISTRY))}"
        )
    return adapter_class(store, config, client, report_error)


def load_custom_adapters() -> None:
    """Importa app/scrapers/custom/*.py para que se auto-registren."""
    try:
        package = importlib.import_module("app.scrapers.custom")
    except ImportError:
        return
    for module in pkgutil.iter_modules(package.__path__):
        if module.name.startswith("_"):
            continue
        importlib.import_module(f"app.scrapers.custom.{module.name}")


# Adaptadores incluidos
register(HtmlStoreAdapter)
register(GenericJsonAdapter)
register(ShopifyAdapter)
register(WooCommerceAdapter)
register(PlaywrightAdapter)
load_custom_adapters()
