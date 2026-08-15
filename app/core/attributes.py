"""Extracción de atributos del producto a partir del nombre y la descripción.

Nada de esto es inferencia estadística: son catálogos declarados en YAML,
expresiones regulares y reglas explícitas.

    "Pokemon 151 Booster Bundle - 6 Booster Packs"
        game          = pokemon
        set_code      = sv035  (151)
        product_type  = booster_bundle
        multiplier    = 1
        units_total   = 6 boosters
"""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app import settings
from app.core.normalize import NormalizedName, basic_normalize, detect_language

_lock = threading.RLock()
_catalog: Dict[str, Any] = {}


@dataclass
class ProductAttributes:
    game: Optional[str] = None
    game_name: Optional[str] = None
    set_code: Optional[str] = None
    set_name: Optional[str] = None
    product_type: Optional[str] = None
    product_type_name: Optional[str] = None
    multiplier: int = 1
    units_total: Optional[int] = None
    unit_name: Optional[str] = None
    quantity_confidence: float = 0.0
    language: Optional[str] = None
    brand: Optional[str] = None
    sources: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Catálogo compilado
# ---------------------------------------------------------------------------
def _build_catalog() -> Dict[str, Any]:
    with _lock:
        if _catalog:
            return _catalog

        games = settings.load_games()
        game_tokens: Dict[str, Tuple[str, str]] = {}
        for game in games:
            for token in game.get("tokens", []) or []:
                game_tokens[token] = (game["code"], game.get("name", game["code"]))

        # --- Expansiones: (regex, game, code, name, palabras) ------------
        set_patterns: List[Tuple[re.Pattern, str, str, str, int, bool]] = []
        set_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for game_code, catalog in settings.load_sets().items():
            for entry in catalog.get("sets", []):
                code = str(entry["code"])
                name = str(entry["name"])
                set_index[(game_code, code)] = {
                    "code": code,
                    "name": name,
                    "game": game_code,
                    "released": entry.get("released"),
                }
                aliases = list(entry.get("aliases", []) or [])
                # Los nombres de serie (ej. "Scarlet & Violet") aparecen en
                # casi todos los productos: no sirven para identificar el set.
                if entry.get("use_name_alias", True):
                    aliases.append(name)
                for alias in aliases:
                    norm = basic_normalize(str(alias))
                    if not norm:
                        continue
                    pattern = re.compile(r"(?<!\w)" + re.escape(norm) + r"(?!\w)")
                    set_patterns.append(
                        (pattern, game_code, code, name, len(norm.split()),
                         bool(entry.get("series_level")))
                    )
        # Orden de preferencia al buscar la expansión en un nombre:
        #   1. las expansiones concretas antes que los nombres de serie
        #      ("Mega Evolution - Pitch Black" -> Pitch Black, no Mega Evolution)
        #   2. el alias con más palabras ("paradox rift" antes que "rift")
        #   3. a igualdad, el alias más largo
        set_patterns.sort(
            key=lambda item: (not item[5], item[4], len(item[0].pattern)), reverse=True
        )

        # --- Tipos de producto -------------------------------------------
        types_cfg = settings.load_product_types()
        type_by_token: Dict[str, Dict[str, Any]] = {}
        type_by_code: Dict[str, Dict[str, Any]] = {}
        for entry in types_cfg.get("types", []) or []:
            type_by_code[entry["code"]] = entry
            for token in entry.get("tokens", []) or []:
                type_by_token[token] = entry

        # --- Patrones de cantidad -----------------------------------------
        quantity_patterns = [
            (
                re.compile(item["pattern"]),
                item.get("kind", "multiplier"),
                float(item.get("confidence", 0.5)),
            )
            for item in types_cfg.get("quantity_patterns", []) or []
        ]

        _catalog.update(
            {
                "game_tokens": game_tokens,
                "games_by_code": {g["code"]: g for g in games},
                "set_patterns": set_patterns,
                "set_index": set_index,
                "type_by_token": type_by_token,
                "type_by_code": type_by_code,
                "quantity_patterns": quantity_patterns,
                "quantity_blacklist": set(types_cfg.get("quantity_blacklist", []) or []),
                "quantity_min": int(types_cfg.get("quantity_min", 1)),
                "quantity_max": int(types_cfg.get("quantity_max", 500)),
            }
        )
        return _catalog


def invalidate() -> None:
    with _lock:
        _catalog.clear()


def set_name(game: Optional[str], code: Optional[str]) -> Optional[str]:
    if not game or not code:
        return None
    entry = _build_catalog()["set_index"].get((game, code))
    return entry["name"] if entry else code


def type_name(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    entry = _build_catalog()["type_by_code"].get(code)
    return entry["name"] if entry else code


def game_name(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    entry = _build_catalog()["games_by_code"].get(code)
    return entry.get("name", code) if entry else code


def all_sets() -> List[Dict[str, Any]]:
    return list(_build_catalog()["set_index"].values())


def all_types() -> List[Dict[str, Any]]:
    return list(_build_catalog()["type_by_code"].values())


# ---------------------------------------------------------------------------
# Detectores individuales
# ---------------------------------------------------------------------------
def detect_game(normalized: NormalizedName, hint: Optional[str] = None) -> Tuple[Optional[str], str]:
    game_tokens: Dict[str, Tuple[str, str]] = _build_catalog()["game_tokens"]
    for token in normalized.tokens:
        if token in game_tokens:
            return game_tokens[token][0], "name"
    if hint:
        return hint, "store_default"
    return None, "unknown"


def detect_set(
    normalized: NormalizedName, game: Optional[str]
) -> Tuple[Optional[str], Optional[str], str]:
    """Busca la expansión sobre el texto básico. Devuelve (code, name, fuente)."""
    text = normalized.basic
    for pattern, set_game, code, name, _words, _series in _build_catalog()["set_patterns"]:
        if game and set_game != game:
            continue
        if pattern.search(text):
            return code, name, "name"
    # Sin juego conocido probamos contra todos los catálogos.
    if not game:
        for pattern, set_game, code, name, _words, _series in _build_catalog()["set_patterns"]:
            if pattern.search(text):
                return code, name, "name"
    return None, None, "unknown"


def detect_product_type(normalized: NormalizedName) -> Tuple[Optional[str], Optional[str], str]:
    """Elige el tipo más específico presente entre los tokens canónicos."""
    type_by_token: Dict[str, Dict[str, Any]] = _build_catalog()["type_by_token"]
    found: List[Dict[str, Any]] = []
    for token in normalized.tokens:
        entry = type_by_token.get(token)
        if entry is not None:
            found.append(entry)
    if not found:
        return None, None, "unknown"
    # Cuando el nombre menciona varios tipos gana el más específico según
    # `specificity` en product_types.yaml:
    #   "Blister Pack"        -> blister, no booster_pack
    #   "Bundle x6 Boosters"  -> booster_bundle, no booster_pack
    # El resto de las menciones describen el contenido, no el producto.
    best = max(found, key=lambda entry: int(entry.get("specificity", 50)))
    return best["code"], best["name"], "name"


def detect_quantity(
    normalized: NormalizedName,
    product_type: Optional[str],
    description: Optional[str] = None,
) -> Tuple[int, Optional[int], float, str]:
    """Devuelve (multiplicador, unidades_totales, confianza, fuente)."""
    catalog = _build_catalog()
    type_entry = catalog["type_by_code"].get(product_type or "")
    base_units = int(type_entry.get("units", 0)) if type_entry else 0
    base_confidence = float(type_entry.get("units_confidence", 0.0)) if type_entry else 0.0

    for text, source in ((normalized.basic, "name"), (basic_normalize(description or ""), "description")):
        if not text:
            continue
        for pattern, kind, confidence in catalog["quantity_patterns"]:
            match = pattern.search(text)
            if not match:
                continue
            try:
                value = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if value in catalog["quantity_blacklist"]:
                continue
            if not (catalog["quantity_min"] <= value <= catalog["quantity_max"]):
                continue

            if kind == "contents":
                return 1, value, confidence, source

            # multiplicador: "Booster Bundle x6" describe el contenido, no
            # seis bundles, así que lo tratamos como contenido.
            if base_units > 1 and value == base_units:
                return 1, base_units, max(confidence, base_confidence), source

            units = value * base_units if base_units > 0 else value
            # La confianza del total es la del eslabón MÁS DÉBIL. Multiplicar
            # un "x5" seguro por unas unidades base dudosas no da un total
            # seguro: "5 x Protector Caja Plastica Booster Box" acababa
            # contando 180 sobres con plena confianza, y no trae ninguno.
            confianza_total = min(confidence, base_confidence) if base_units > 0 else confidence
            return value, units or None, confianza_total, source

    if base_units > 0:
        return 1, base_units, base_confidence, "product_type_default"
    return 1, None, 0.0, "unknown"


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------
def language_name(code: Optional[str]) -> Optional[str]:
    from app.core.normalize import language_name as _language_name

    return _language_name(code)


def extract(
    normalized: NormalizedName,
    description: Optional[str] = None,
    game_hint: Optional[str] = None,
    brand: Optional[str] = None,
    language_hint: Optional[str] = None,
) -> ProductAttributes:
    game, game_source = detect_game(normalized, game_hint)
    set_code, set_label, set_source = detect_set(normalized, game)

    # Si el juego no aparecía en el nombre pero sí una expansión conocida,
    # el juego queda determinado por el catálogo de esa expansión.
    if not game and set_code:
        for (catalog_game, code), entry in _build_catalog()["set_index"].items():
            if code == set_code:
                game, game_source = catalog_game, "set_catalog"
                break

    type_code, type_label, type_source = detect_product_type(normalized)
    multiplier, units_total, quantity_confidence, quantity_source = detect_quantity(
        normalized, type_code, description
    )
    # Si la tienda no lo escribe en el nombre pero solo vende un idioma, se
    # usa el declarado en su YAML (`default_language`).
    language = detect_language(normalized) or language_hint

    type_entry = _build_catalog()["type_by_code"].get(type_code or "")
    unit_name = type_entry.get("unit_name") if type_entry else None

    return ProductAttributes(
        game=game,
        game_name=game_name(game),
        set_code=set_code,
        set_name=set_label,
        product_type=type_code,
        product_type_name=type_label,
        multiplier=multiplier,
        units_total=units_total,
        unit_name=unit_name,
        quantity_confidence=quantity_confidence,
        language=language,
        brand=brand or game_name(game),
        sources={
            "game": game_source,
            "set_code": set_source,
            "product_type": type_source,
            "quantity": quantity_source,
        },
    )


_MULTIPLIER_TOKEN = re.compile(r"^(x\d{1,3}|\d{1,3}x)$")


def refine_tokens(normalized: NormalizedName, attrs: ProductAttributes) -> List[str]:
    """Quita del texto los tokens que los atributos ya explican.

    El puntaje compara los atributos por separado y con su propio peso. Si el
    texto vuelve a incluir esa misma información, dos ofertas del mismo
    producto se penalizan solo por escribirlo distinto:

        "Pokémon TCG: Scarlet & Violet—151 Booster Bundle"
        "Pokemon 151 Booster Bundle"

    Ambas tienen set=151, tipo=Booster Bundle y 6 sobres. Lo que queda tras
    descontar marca, serie y marcadores de cantidad es idéntico, que es
    justamente lo que debe compararse.

    Nunca se descarta información que no esté ya capturada como atributo: si
    el set no se detectó, los nombres de serie se conservan.
    """
    catalog = _build_catalog()
    normalization = settings.load_normalization()

    drop: set = set()

    # 1. Marca / juego: ya está en el atributo `game`.
    if attrs.game:
        drop.update(
            token for token, (code, _name) in catalog["game_tokens"].items() if code == attrs.game
        )

    # 2. Nombre de la serie, solo si la expansión concreta ya se identificó.
    if attrs.set_code:
        drop.update(normalization.get("series_tokens", []) or [])

    # 3. Palabras de idioma, si el idioma ya se detectó: "ESPAÑOL", "Español"
    #    y "esp" son el mismo dato y ya viaja en el atributo `language`.
    if attrs.language:
        drop.update(normalization.get("language_tokens", []) or [])

    # 4. Marcadores de multiplicador ("x6", "10x"): ya están en `multiplier`.
    # 5. Tipos de producto secundarios ("... Bundle x6 Boosters"): el tipo
    #    principal ya está en `product_type` y el resto describe el contenido.
    kept: List[str] = []
    for token in normalized.core_tokens:
        if token in drop:
            continue
        if _MULTIPLIER_TOKEN.match(token):
            continue
        entry = catalog["type_by_token"].get(token)
        if entry is not None and attrs.product_type and entry["code"] != attrs.product_type:
            # Un tipo secundario que contiene MENOS unidades que el principal
            # describe el contenido ("Blister Pack", "Bundle x6 Boosters") y
            # sobra. Si contiene MÁS, es un envase que agrupa varias unidades
            # ("Display Sellado ... Mini Tin") y hay que conservarlo: es
            # justamente lo que distingue una lata de una caja de latas.
            chosen = catalog["type_by_code"].get(attrs.product_type, {})
            if int(entry.get("units", 0)) <= int(chosen.get("units", 0)):
                continue
        kept.append(token)

    # Nunca devolvemos una lista vacía: sin tokens no hay nada que comparar.
    return kept or list(normalized.core_tokens)


def build_canonical_name(attrs: ProductAttributes, fallback: str) -> str:
    """Nombre legible del producto maestro a partir de sus atributos."""
    # El nombre derivado de atributos solo sirve si identifica el producto.
    # Con la expansión o el tipo sin detectar queda demasiado genérico —tres
    # productos distintos se llamarían "Pokémon 30th Anniversary Celebration"—
    # así que en ese caso vale más el nombre real más corto de las tiendas.
    if not (attrs.set_name and attrs.product_type_name):
        return fallback.strip() or "Producto sin nombre"

    parts: List[str] = []
    if attrs.game_name:
        parts.append(attrs.game_name)
    parts.append(attrs.set_name)
    parts.append(attrs.product_type_name)

    name = " ".join(parts)
    if attrs.multiplier and attrs.multiplier > 1:
        name = f"{name} x{attrs.multiplier}"
    if attrs.language:
        # El idioma va en el nombre porque son productos distintos:
        # "Chaos Rising Booster Bundle (Español)" ≠ "(Inglés)".
        name = f"{name} ({language_name(attrs.language)})"
    return name
