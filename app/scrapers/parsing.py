"""Utilidades de parseo compartidas por todos los adaptadores de tienda."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Precios
# ---------------------------------------------------------------------------
_PRICE_RE = re.compile(r"[-+]?\d[\d.,\s]*")


def parse_price(raw: Optional[str]) -> Optional[float]:
    """Convierte texto de precio a float, tolerando formatos mixtos.

        "$36.990"      -> 36990.0     (punto = miles)
        "36.990,50"    -> 36990.5
        "$39.99"       -> 39.99       (punto = decimal)
        "1,234.56"     -> 1234.56
        "CLP 45.990"   -> 45990.0
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return None

    match = _PRICE_RE.search(text)
    if not match:
        return None

    number = match.group(0).replace(" ", "").replace("\xa0", "")
    if not number:
        return None

    has_dot = "." in number
    has_comma = "," in number

    if has_dot and has_comma:
        # El separador decimal es el que aparece más a la derecha.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif has_comma:
        decimals = len(number.split(",")[-1])
        number = number.replace(",", "." if decimals in (1, 2) else "")
    elif has_dot:
        decimals = len(number.split(".")[-1])
        # Un punto seguido de exactamente 3 dígitos y más de un grupo es
        # separador de miles ("36.990"); con 1 o 2 dígitos es decimal.
        if decimals == 3 or number.count(".") > 1:
            number = number.replace(".", "")

    try:
        return float(number)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Disponibilidad
# ---------------------------------------------------------------------------
STOCK_IN = "in_stock"
STOCK_OUT = "out_of_stock"
STOCK_PREORDER = "preorder"
STOCK_SOON = "coming_soon"
STOCK_UNKNOWN = "unknown"

_STOCK_KEYWORDS = [
    (STOCK_PREORDER, ("preventa", "pre-venta", "preorder", "pre-order", "pre order", "reserva")),
    (STOCK_SOON, ("proximamente", "próximamente", "coming soon", "disponible pronto")),
    (
        STOCK_OUT,
        (
            "agotado", "sin stock", "no disponible", "out of stock", "sold out",
            "outofstock", "unavailable", "descontinuado",
        ),
    ),
    (
        STOCK_IN,
        (
            "en stock", "disponible", "in stock", "instock", "add to cart",
            "agregar al carro", "añadir al carrito", "comprar", "hay stock",
        ),
    ),
]


def parse_stock(raw: Optional[str], fallback: str = STOCK_UNKNOWN) -> str:
    if raw is None:
        return fallback
    if isinstance(raw, bool):
        return STOCK_IN if raw else STOCK_OUT

    text = str(raw).strip().lower()
    if not text:
        return fallback
    for status, keywords in _STOCK_KEYWORDS:
        for keyword in keywords:
            if keyword in text:
                return status
    return fallback


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def absolute_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "#")):
        return None
    return urljoin(base, href)


def clean_url(url: str) -> str:
    """Quita query y fragmento para que la misma ficha no se guarde dos veces."""
    parts = urlparse(url)
    return urlunparse((parts.scheme, parts.netloc, parts.path.rstrip("/") or "/", "", "", ""))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def select_text(node: Any, selector: Optional[str]) -> Optional[str]:
    if not node or not selector:
        return None
    found = node.select_one(selector)
    if found is None:
        return None
    return " ".join(found.get_text(" ", strip=True).split())


def select_attr(node: Any, selector: Optional[str], attribute: str) -> Optional[str]:
    if not node or not selector:
        return None
    found = node.select_one(selector)
    if found is None:
        return None
    value = found.get(attribute)
    if isinstance(value, list):
        value = value[0] if value else None
    return value.strip() if isinstance(value, str) else None


def select_all_attr(node: Any, selector: Optional[str], attribute: str) -> List[str]:
    if not node or not selector:
        return []
    out: List[str] = []
    for found in node.select(selector):
        value = found.get(attribute)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


# ---------------------------------------------------------------------------
# JSON-LD (schema.org/Product) — muchas tiendas lo publican y trae GTIN/SKU
# ---------------------------------------------------------------------------
def extract_json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        blocks.extend(_flatten_json_ld(data))
    return blocks


def _flatten_json_ld(data: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            out.extend(_flatten_json_ld(item))
    elif isinstance(data, dict):
        out.append(data)
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in data:
                out.extend(_flatten_json_ld(data[key]))
    return out


def product_from_json_ld(blocks: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Extrae los campos útiles del primer bloque de tipo Product."""
    for block in blocks:
        types = block.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if not any(str(t).lower() == "product" for t in types):
            continue

        offers = block.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        offers = offers if isinstance(offers, dict) else {}

        image = block.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")

        brand = block.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        return {
            "name": block.get("name"),
            "description": block.get("description"),
            "image_url": image if isinstance(image, str) else None,
            "sku": block.get("sku"),
            "mpn": block.get("mpn"),
            "gtin": block.get("gtin") or block.get("gtin14"),
            "gtin13": block.get("gtin13"),
            "gtin12": block.get("gtin12"),
            "gtin8": block.get("gtin8"),
            "ean": block.get("gtin13"),
            "upc": block.get("gtin12"),
            "brand": brand if isinstance(brand, str) else None,
            "price": offers.get("price") or offers.get("lowPrice"),
            "currency": offers.get("priceCurrency"),
            "availability": offers.get("availability"),
        }
    return {}


def dig(data: Any, path: Optional[str], default: Any = None) -> Any:
    """Accede a un JSON con una ruta con puntos: 'variants.0.price'."""
    if not path:
        return default
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return default
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return node if node is not None else default


def strip_html(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", " ", str(text))
    return " ".join(cleaned.split()) or None
