"""Precio por unidad (por booster, por carta, por mazo...).

Solo se calcula cuando la cantidad de unidades se pudo determinar con
suficiente confianza; en caso contrario se devuelve None y la interfaz
simplemente no muestra el dato.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from app import settings


def unit_price(
    price: Optional[float],
    units_total: Optional[int],
    quantity_confidence: float,
) -> Optional[float]:
    if not settings.get("unit_price.enabled", True):
        return None
    if price is None or not units_total or units_total <= 0:
        return None
    if quantity_confidence < float(settings.get("unit_price.min_confidence", 0.7)):
        return None
    return price / units_total


def annotate_offers(offers: Iterable[Dict]) -> List[Dict]:
    """Agrega `unit_price` y `unit_name` a una lista de ofertas."""
    out: List[Dict] = []
    for offer in offers:
        enriched = dict(offer)
        enriched["unit_price"] = unit_price(
            offer.get("price"),
            offer.get("units_total"),
            float(offer.get("quantity_confidence") or 0.0),
        )
        enriched["unit_name"] = offer.get("unit_name") or settings.get(
            "unit_price.base_unit", "booster"
        )
        out.append(enriched)
    return out


def savings(best: Optional[float], other: Optional[float]) -> Dict[str, Optional[float]]:
    """Diferencia absoluta y porcentual entre el mejor precio y otro precio."""
    if best is None or other is None or other <= 0:
        return {"amount": None, "percent": None}
    return {
        "amount": round(other - best, 2),
        "percent": round((other - best) / other * 100.0, 2),
    }


def format_currency(value: Optional[float]) -> str:
    """Formatea según config (por defecto CLP: $36.990)."""
    if value is None:
        return "—"
    decimals = int(settings.get("app.currency_decimals", 0))
    thousands = settings.get("app.currency_thousands_sep", ".")
    decimal_sep = settings.get("app.currency_decimal_sep", ",")
    symbol = settings.get("app.currency_symbol", "$")

    formatted = f"{value:,.{decimals}f}"
    # Cambiamos los separadores de la convención inglesa a la configurada.
    formatted = formatted.replace(",", "\x00").replace(".", decimal_sep).replace("\x00", thousands)
    return f"{symbol}{formatted}"
