"""Algoritmos de similitud de texto, implementados en Python puro.

Ninguno de estos métodos usa aprendizaje automático ni servicios externos:
son métricas clásicas de comparación de cadenas y de conjuntos de tokens.

    - Levenshtein (distancia de edición) y su ratio
    - Jaro y Jaro-Winkler
    - Jaccard sobre conjuntos de tokens
    - token_sort_ratio / token_set_ratio (estilo fuzzywuzzy)
    - TF-IDF + similitud coseno construido sobre el propio catálogo local
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Dict, Iterable, List, Sequence


# ---------------------------------------------------------------------------
# Levenshtein
# ---------------------------------------------------------------------------
def levenshtein(a: str, b: str, max_distance: int | None = None) -> int:
    """Distancia de edición con dos filas (memoria O(min(n, m)))."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)

    if max_distance is not None and abs(len(a) - len(b)) > max_distance:
        return max_distance + 1

    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        best_in_row = i
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            value = min(
                previous[j] + 1,        # borrado
                current[j - 1] + 1,     # inserción
                previous[j - 1] + cost, # sustitución
            )
            current.append(value)
            if value < best_in_row:
                best_in_row = value
        if max_distance is not None and best_in_row > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def levenshtein_ratio(a: str, b: str) -> float:
    """0..1 — 1 significa cadenas idénticas."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - (levenshtein(a, b) / longest)


# ---------------------------------------------------------------------------
# Jaro / Jaro-Winkler
# ---------------------------------------------------------------------------
def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    len_a, len_b = len(a), len(b)
    if len_a == 0 or len_b == 0:
        return 0.0

    window = max(len_a, len_b) // 2 - 1
    if window < 0:
        window = 0

    flags_a = [False] * len_a
    flags_b = [False] * len_b
    matches = 0

    for i, char in enumerate(a):
        start = max(0, i - window)
        end = min(i + window + 1, len_b)
        for j in range(start, end):
            if flags_b[j] or b[j] != char:
                continue
            flags_a[i] = flags_b[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i in range(len_a):
        if not flags_a[i]:
            continue
        while not flags_b[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    return (
        matches / len_a + matches / len_b + (matches - transpositions) / matches
    ) / 3.0


def jaro_winkler(a: str, b: str, prefix_weight: float = 0.1) -> float:
    score = jaro(a, b)
    if score <= 0.7:
        return score
    prefix = 0
    for char_a, char_b in zip(a[:4], b[:4]):
        if char_a != char_b:
            break
        prefix += 1
    return score + prefix * prefix_weight * (1.0 - score)


# ---------------------------------------------------------------------------
# Conjuntos de tokens
# ---------------------------------------------------------------------------
def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def containment(a: Iterable[str], b: Iterable[str]) -> float:
    """Proporción del conjunto más pequeño contenida en el más grande."""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))


def token_sort_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """Compara los tokens ordenados alfabéticamente (ignora el orden original)."""
    return levenshtein_ratio(" ".join(sorted(a)), " ".join(sorted(b)))


def token_set_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """Ignora los tokens comunes y compara el resto; tolera texto extra."""
    set_a, set_b = set(a), set(b)
    intersection = sorted(set_a & set_b)
    rest_a = sorted(set_a - set_b)
    rest_b = sorted(set_b - set_a)

    combined_a = " ".join(intersection + rest_a).strip()
    combined_b = " ".join(intersection + rest_b).strip()
    only_common = " ".join(intersection).strip()

    return max(
        levenshtein_ratio(only_common, combined_a),
        levenshtein_ratio(only_common, combined_b),
        levenshtein_ratio(combined_a, combined_b),
    )


# ---------------------------------------------------------------------------
# TF-IDF + coseno
# ---------------------------------------------------------------------------
class TfidfIndex:
    """Índice TF-IDF construido sobre el catálogo local.

    Es un cálculo estadístico clásico sobre frecuencias de palabras: no es un
    embedding ni un modelo entrenado, y se reconstruye en memoria en cada
    ejecución del matching.
    """

    def __init__(self) -> None:
        self.idf: Dict[str, float] = {}
        self.document_count = 0
        self.document_frequency: Counter = Counter()

    def fit(self, documents: Iterable[Sequence[str]]) -> "TfidfIndex":
        self.document_frequency = Counter()
        self.document_count = 0
        for tokens in documents:
            self.document_count += 1
            for token in set(tokens):
                self.document_frequency[token] += 1

        total = max(self.document_count, 1)
        self.idf = {
            token: math.log((total + 1) / (freq + 1)) + 1.0
            for token, freq in self.document_frequency.items()
        }
        return self

    def vector(self, tokens: Sequence[str]) -> Dict[str, float]:
        if not tokens:
            return {}
        counts = Counter(tokens)
        length = len(tokens)
        vec = {
            token: (count / length) * self.idf.get(token, math.log(self.document_count + 1) + 1.0)
            for token, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in vec.values()))
        if norm == 0:
            return {}
        return {token: value / norm for token, value in vec.items()}

    @staticmethod
    def cosine(vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        if not vec_a or not vec_b:
            return 0.0
        if len(vec_a) > len(vec_b):
            vec_a, vec_b = vec_b, vec_a
        return sum(value * vec_b.get(token, 0.0) for token, value in vec_a.items())

    def rarity(self, token: str) -> float:
        """IDF del token: alto = poco frecuente = mejor para bloquear."""
        return self.idf.get(token, math.log(self.document_count + 1) + 1.0)

    def document_ratio(self, token: str) -> float:
        if not self.document_count:
            return 1.0
        return self.document_frequency.get(token, 0) / self.document_count


# ---------------------------------------------------------------------------
# Métrica combinada de nombre (0..1)
# ---------------------------------------------------------------------------
def name_similarity(
    tokens_a: Sequence[str],
    tokens_b: Sequence[str],
    vec_a: Dict[str, float] | None = None,
    vec_b: Dict[str, float] | None = None,
) -> Dict[str, float]:
    """Devuelve cada métrica por separado más una combinación ponderada."""
    text_a = " ".join(tokens_a)
    text_b = " ".join(tokens_b)

    metrics = {
        "jaccard": jaccard(tokens_a, tokens_b),
        "containment": containment(tokens_a, tokens_b),
        "token_sort": token_sort_ratio(tokens_a, tokens_b),
        "token_set": token_set_ratio(tokens_a, tokens_b),
        "levenshtein": levenshtein_ratio(text_a, text_b),
        "jaro_winkler": jaro_winkler(text_a, text_b),
        "tfidf_cosine": TfidfIndex.cosine(vec_a or {}, vec_b or {}),
    }

    # Pesos fijos internos de la métrica combinada: privilegian las medidas
    # robustas al orden y a las palabras sobrantes.
    metrics["combined"] = (
        0.30 * metrics["token_set"]
        + 0.25 * metrics["tfidf_cosine"]
        + 0.20 * metrics["token_sort"]
        + 0.15 * metrics["jaro_winkler"]
        + 0.10 * metrics["levenshtein"]
    )
    return metrics


# ---------------------------------------------------------------------------
# Búsqueda difusa para el buscador de la interfaz
# ---------------------------------------------------------------------------
def search_score(query_tokens: Sequence[str], target_tokens: Sequence[str]) -> float:
    """Puntaje 0..1 pensado para autocompletar/buscar, no para agrupar."""
    if not query_tokens:
        return 0.0

    hits = 0.0
    for q in query_tokens:
        best = 0.0
        for t in target_tokens:
            if t == q:
                best = 1.0
                break
            if t.startswith(q) or q.startswith(t):
                best = max(best, 0.9)
            elif q in t:
                best = max(best, 0.75)
            else:
                ratio = jaro_winkler(q, t)
                if ratio > 0.88:
                    best = max(best, ratio * 0.7)
        hits += best
    return hits / len(query_tokens)
