"""Normalización de nombres de producto.

Pipeline:
    "Pokémon TCG: Scarlet & Violet—151 Booster Bundle"
      -> minúsculas, sin acentos, sin puntuación
         "pokemon tcg scarlet and violet 151 booster bundle"
      -> equivalencias del diccionario (frase larga primero)
         "pokemon scarlet violet 151 booster_bundle"
      -> tokens sin stopwords (respetando los protegidos)
         ["pokemon", "scarlet", "violet", "151", "booster_bundle"]

Todo el comportamiento se controla desde config/normalization.yaml.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set, Tuple

from app import settings

_lock = threading.RLock()
_compiled: Dict[str, object] = {}

# Un token es "protegido" si contiene "_" (viene de una equivalencia) o si es
# numérico: esos nunca se descartan aunque estén en la lista de stopwords.
_NUMERIC_RE = re.compile(r"^\d+(\.\d+)?$")


@dataclass
class NormalizedName:
    raw: str
    basic: str                       # minúsculas, sin acentos ni puntuación
    punctuated: str                  # igual, pero conservando ( ) [ ] y guiones
    canonical: str                   # tras aplicar equivalencias
    tokens: List[str] = field(default_factory=list)        # canónicos completos
    core_tokens: List[str] = field(default_factory=list)   # sin stopwords
    name_key: str = ""               # tokens core ordenados -> clave exacta

    @property
    def core_set(self) -> frozenset:
        return frozenset(self.core_tokens)


# ---------------------------------------------------------------------------
# Compilación del diccionario (se cachea; invalidate() la recarga)
# ---------------------------------------------------------------------------
def _rules() -> Dict[str, object]:
    with _lock:
        if _compiled:
            return _compiled

        cfg = settings.load_normalization()

        # Equivalencias ordenadas de más palabras a menos, para que
        # "elite trainer box" gane sobre "box".
        raw_equiv: Dict[str, str] = cfg.get("equivalences", {}) or {}
        equivalences: List[Tuple[Tuple[str, ...], str]] = []
        for alias, canonical in raw_equiv.items():
            words = tuple(_basic_normalize(str(alias)).split())
            if not words:
                continue
            equivalences.append((words, str(canonical).strip()))
        equivalences.sort(key=lambda item: len(item[0]), reverse=True)

        max_phrase = max((len(words) for words, _ in equivalences), default=1)

        _compiled.update(
            {
                "equivalences": equivalences,
                "equiv_index": {words[0] for words, _ in equivalences},
                "max_phrase": max_phrase,
                "stopwords": set(cfg.get("stopwords", []) or []),
                "protected": set(cfg.get("protected_tokens", []) or []),
                "char_replacements": cfg.get("char_replacements", {}) or {},
                "languages": _compile_languages(cfg.get("languages", []) or []),
                "language_names": {
                    entry["code"]: entry.get("name", entry["code"])
                    for entry in (cfg.get("languages", []) or [])
                    if isinstance(entry, dict) and entry.get("code")
                },
                "language_tokens": set(cfg.get("language_tokens", []) or []),
                "noise_patterns": [
                    re.compile(pattern)
                    for pattern in (cfg.get("noise_patterns", []) or [])
                ],
            }
        )
        return _compiled


def invalidate() -> None:
    with _lock:
        _compiled.clear()


# ---------------------------------------------------------------------------
# Paso 1: limpieza básica
# ---------------------------------------------------------------------------
def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _prepare(
    text: str,
    replacements: Dict[str, str] | None = None,
    noise: Sequence[re.Pattern] | None = None,
) -> str:
    """Minúsculas, sin acentos y sin ruido, pero CONSERVANDO la puntuación.

    Los corchetes y paréntesis se mantienen porque hay información que solo
    es interpretable con ellos: "Pokemon TCG [EN] Chaos Rising" — un "en"
    suelto sería la preposición castellana, pero "[EN]" solo puede ser el
    idioma.
    """
    if not text:
        return ""

    out = str(text)
    for source, target in (replacements or {}).items():
        out = out.replace(source, target)

    out = strip_accents(out).lower()

    # Ruido configurable: se aplica aquí, cuando el texto ya está en
    # minúsculas y sin acentos pero todavía conserva los paréntesis.
    #   "(Preventa 1 - 04/12/26) 30th Celebration Booster Bundle Inglés"
    #     -> "30th celebration booster bundle ingles"
    for pattern in noise or ():
        out = pattern.sub(" ", out)

    return re.sub(r"\s+", " ", out).strip()


def _strip_punctuation(text: str) -> str:
    # Todo lo que no sea letra/número/punto pasa a ser espacio.
    out = re.sub(r"[^a-z0-9.]+", " ", text)
    # Los puntos solo sobreviven entre dígitos (códigos tipo "sv3.5").
    out = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _basic_normalize(
    text: str,
    replacements: Dict[str, str] | None = None,
    noise: Sequence[re.Pattern] | None = None,
) -> str:
    return _strip_punctuation(_prepare(text, replacements, noise))


def basic_normalize(text: str) -> str:
    rules = _rules()
    return _basic_normalize(
        text,
        rules["char_replacements"],  # type: ignore[arg-type]
        rules["noise_patterns"],  # type: ignore[arg-type]
    )


def punctuated_normalize(text: str) -> str:
    rules = _rules()
    return _prepare(
        text,
        rules["char_replacements"],  # type: ignore[arg-type]
        rules["noise_patterns"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Paso 2: equivalencias
# ---------------------------------------------------------------------------
def apply_equivalences(tokens: Sequence[str]) -> List[str]:
    """Sustituye frases del diccionario por su token canónico (greedy, más larga primero)."""
    rules = _rules()
    equivalences: List[Tuple[Tuple[str, ...], str]] = rules["equivalences"]  # type: ignore[assignment]
    starts: set = rules["equiv_index"]  # type: ignore[assignment]
    max_phrase: int = rules["max_phrase"]  # type: ignore[assignment]

    # Índice por longitud para no recorrer todo el diccionario en cada posición.
    by_length: Dict[int, Dict[Tuple[str, ...], str]] = {}
    cached = rules.get("_by_length")
    if cached is None:
        for words, canonical in equivalences:
            by_length.setdefault(len(words), {})[words] = canonical
        rules["_by_length"] = by_length
    else:
        by_length = cached  # type: ignore[assignment]

    out: List[str] = []
    index = 0
    total = len(tokens)
    while index < total:
        matched = False
        if tokens[index] in starts:
            upper = min(max_phrase, total - index)
            for size in range(upper, 0, -1):
                phrase = tuple(tokens[index : index + size])
                canonical = by_length.get(size, {}).get(phrase)
                if canonical is not None:
                    if canonical:  # "" significa "eliminar la frase"
                        out.append(canonical)
                    index += size
                    matched = True
                    break
        if not matched:
            out.append(tokens[index])
            index += 1
    return out


# ---------------------------------------------------------------------------
# Paso 3: stopwords
# ---------------------------------------------------------------------------
def is_protected(token: str) -> bool:
    rules = _rules()
    if "_" in token:
        return True
    if _NUMERIC_RE.match(token):
        return True
    return token in rules["protected"]  # type: ignore[operator]


def remove_stopwords(tokens: Sequence[str]) -> List[str]:
    rules = _rules()
    stopwords: set = rules["stopwords"]  # type: ignore[assignment]
    return [tok for tok in tokens if is_protected(tok) or tok not in stopwords]


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------
def normalize_name(raw: str) -> NormalizedName:
    punctuated = punctuated_normalize(raw or "")
    basic = _strip_punctuation(punctuated)
    tokens = apply_equivalences(basic.split())
    canonical = " ".join(tokens)
    core = remove_stopwords(tokens)

    # Sin tokens relevantes preferimos quedarnos con algo antes que con nada.
    if not core:
        core = list(tokens)

    return NormalizedName(
        raw=raw or "",
        basic=basic,
        punctuated=punctuated,
        canonical=canonical,
        tokens=tokens,
        core_tokens=core,
        name_key=" ".join(sorted(set(core))),
    )


def _compile_languages(entries: Sequence[Dict[str, object]]) -> List[Tuple[str, str, re.Pattern]]:
    """Devuelve [(nivel, código, regex)] con las `strict` antes que las `loose`."""
    compiled: List[Tuple[str, str, re.Pattern]] = []
    # Orden de las pasadas:
    #   strict    -> palabras completas ("español", "english"), sin ambigüedad
    #   delimited -> códigos de dos letras entre corchetes o paréntesis
    #   loose     -> abreviaturas sueltas, las más frágiles
    for level in ("strict", "delimited", "loose"):
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("code"):
                continue
            for pattern in entry.get(level, []) or []:  # type: ignore[union-attr]
                compiled.append((level, str(entry["code"]), re.compile(str(pattern))))
    return compiled


def detect_language(normalized: NormalizedName) -> str | None:
    """Idioma declarado en el nombre, o None si no lo dice.

    Las reglas `delimited` se buscan sobre el texto con puntuación, porque
    ahí está justamente la señal: "[EN]" es inequívoco, un "en" suelto no.

    Si en el mismo nivel coinciden DOS idiomas, el nombre no está diciendo
    cuál es: está diciendo los dos.

        'Koraidon ex Deluxe Battle Deck - ESPAÑOL - INGLES'
        'Rival Battle deck Steven - Ingles y Español'

    Antes ganaba el que estuviera primero en normalization.yaml —siempre el
    español—, lo que etiquetaba mal el producto y lo dejaba fuera de la
    comparación con los ingleses. Devolver None es más honesto: sin idioma,
    la oferta puede compararse con ambos.
    """
    reglas = _rules()["languages"]  # type: ignore[union-attr]
    for nivel in ("strict", "delimited", "loose"):
        encontrados: list[str] = []
        for level, code, pattern in reglas:
            if level != nivel or code in encontrados:
                continue
            objetivo = normalized.punctuated if level == "delimited" else normalized.basic
            if pattern.search(objetivo):
                encontrados.append(code)
        if len(encontrados) == 1:
            return encontrados[0]
        if encontrados:
            return None  # el nombre nombra varios idiomas: no elegimos por él
    return None


def language_name(code: str | None) -> str | None:
    if not code:
        return None
    return _rules()["language_names"].get(code, code.upper())  # type: ignore[union-attr]


def language_tokens() -> set:
    return _rules()["language_tokens"]  # type: ignore[return-value]
