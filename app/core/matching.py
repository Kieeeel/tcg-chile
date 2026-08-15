"""Motor de emparejamiento determinístico.

No hay modelos entrenados ni servicios externos. La decisión sale de:

    1. Identificadores objetivos (UPC / EAN / GTIN / SKU de fabricante)
    2. Rechazos duros por atributos incompatibles (set, tipo, juego)
    3. Un puntaje ponderado y configurable sobre atributos + similitud textual
    4. Decisiones manuales guardadas, que siempre mandan

Salida:
    >= auto_threshold      -> se agrupan automáticamente
    review..auto           -> par pendiente de revisión manual
    < review_threshold     -> no son el mismo producto
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app import settings
from app.core import similarity
from app.core.similarity import TfidfIndex

# Métodos de agrupación (se guardan en product_matches.method)
METHOD_EAN = "EAN_MATCH"
METHOD_SKU = "SKU_MATCH"
METHOD_NAME = "NAME_MATCH"
METHOD_ATTRIBUTE = "ATTRIBUTE_MATCH"
METHOD_FUZZY = "FUZZY_MATCH"
METHOD_MANUAL = "MANUAL_MATCH"
METHOD_SINGLETON = "SINGLETON"

_NON_DIGITS = re.compile(r"\D+")


# ---------------------------------------------------------------------------
# Identificadores
# ---------------------------------------------------------------------------
def normalize_gtin(value: Optional[str]) -> Optional[str]:
    """Lleva UPC-A/EAN-8/EAN-13/GTIN-14 a una forma común de 14 dígitos.

    Así un UPC de 12 dígitos y el mismo código publicado como EAN-13 con un
    cero delante se reconocen como el mismo identificador.
    """
    if not value:
        return None
    digits = _NON_DIGITS.sub("", str(value))
    if len(digits) not in (8, 12, 13, 14):
        return None
    if set(digits) == {"0"}:
        return None
    return digits.rjust(14, "0")


def normalize_mpn(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(value)).upper()
    return cleaned or None


def stable_key(store_code: str, external_id: Optional[str], url: str) -> str:
    """Clave estable de una oferta, usada por las decisiones manuales."""
    tail = (external_id or "").strip() or url.strip()
    return f"{store_code}::{tail}"


# ---------------------------------------------------------------------------
# Candidato
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    id: int
    store_id: int
    store_code: str
    key: str
    name: str
    tokens: List[str]
    name_key: str
    game: Optional[str] = None
    set_code: Optional[str] = None
    product_type: Optional[str] = None
    units_total: Optional[int] = None
    quantity_confidence: float = 0.0
    language: Optional[str] = None
    gtins: Set[str] = field(default_factory=set)
    mpns: Set[str] = field(default_factory=set)
    vector: Dict[str, float] = field(default_factory=dict)


@dataclass
class PairScore:
    a_id: int
    b_id: int
    score: float
    method: str
    breakdown: Dict[str, object]


@dataclass
class MatchResult:
    groups: List[List[int]]
    decisions: Dict[int, PairScore]          # store_product_id -> cómo entró al grupo
    reviews: List[PairScore]                 # pares 75..89 pendientes
    rejected: int = 0
    compared_pairs: int = 0
    #: Pares puntuados de esta pasada. Se pueden reutilizar mientras las
    #: ofertas no cambien: una decisión manual altera las restricciones, no
    #: los puntajes, así que no hace falta volver a comparar nada.
    scored: List[PairScore] = field(default_factory=list)
    from_cache: bool = False


# ---------------------------------------------------------------------------
# Configuración del scoring
# ---------------------------------------------------------------------------
@dataclass
class ScoringConfig:
    auto_threshold: float = 90.0
    review_threshold: float = 75.0
    w_identifier: float = 100.0
    w_manufacturer_sku: float = 60.0
    w_set: float = 20.0
    w_type: float = 25.0
    w_quantity: float = 15.0
    w_tokens: float = 20.0
    w_name: float = 20.0
    w_language: float = 5.0
    p_set: float = -100.0
    p_type: float = -100.0
    p_quantity: float = -30.0
    p_language: float = -30.0
    unknown_credit: float = 0.35
    unknown_attributes_cap: float = 88.0
    # Con True, dos idiomas conocidos y distintos descartan el par de plano,
    # igual que la expansión o el tipo. Con False vuelve a ser la penalización
    # `different_language`, y entonces los pares dudosos sí llegan a revisión.
    language_is_identity: bool = True
    # Un token presente en un nombre y ausente en el otro que además es raro
    # en el catálogo suele ser lo que distingue dos variantes del mismo set:
    # "Tin Mega Feraligatr ex" vs "Tin Mega Emboar ex".
    distinctive_token_ratio: float = 0.02
    p_distinctive: float = -14.0
    max_distinctive_penalty: float = -42.0
    min_quantity_confidence: float = 0.7
    max_candidates: int = 80
    max_token_ratio: float = 0.25

    @classmethod
    def from_settings(cls) -> "ScoringConfig":
        cfg = settings.get("matching", {}) or {}
        weights = cfg.get("weights", {}) or {}
        penalties = cfg.get("penalties", {}) or {}
        blocking = cfg.get("blocking", {}) or {}
        return cls(
            auto_threshold=float(cfg.get("auto_threshold", 90)),
            review_threshold=float(cfg.get("review_threshold", 75)),
            w_identifier=float(weights.get("identifier", 100)),
            w_manufacturer_sku=float(weights.get("manufacturer_sku", 60)),
            w_set=float(weights.get("same_set", 20)),
            w_type=float(weights.get("same_product_type", 25)),
            w_quantity=float(weights.get("same_quantity", 15)),
            w_tokens=float(weights.get("token_overlap", 20)),
            w_name=float(weights.get("name_similarity", 20)),
            w_language=float(weights.get("same_language", 5)),
            p_set=float(penalties.get("different_set", -100)),
            p_type=float(penalties.get("different_product_type", -100)),
            p_quantity=float(penalties.get("different_quantity", -30)),
            p_language=float(penalties.get("different_language", -30)),
            unknown_credit=float(cfg.get("unknown_attribute_credit", 0.35)),
            unknown_attributes_cap=float(cfg.get("unknown_attributes_cap", 88)),
            language_is_identity=bool(cfg.get("language_is_identity", True)),
            distinctive_token_ratio=float(cfg.get("distinctive_token_ratio", 0.02)),
            p_distinctive=float(penalties.get("distinctive_token", -14)),
            max_distinctive_penalty=float(penalties.get("max_distinctive_total", -42)),
            min_quantity_confidence=float(
                settings.get("unit_price.min_confidence", 0.7)
            ),
            max_candidates=int(blocking.get("max_candidates_per_product", 80)),
            max_token_ratio=float(blocking.get("max_token_document_ratio", 0.25)),
        )


# ---------------------------------------------------------------------------
# Scoring de un par
# ---------------------------------------------------------------------------
def score_pair(
    a: Candidate,
    b: Candidate,
    cfg: ScoringConfig,
    index: Optional[TfidfIndex] = None,
) -> PairScore:
    breakdown: Dict[str, object] = {}

    # --- 1. Identificadores objetivos ---------------------------------
    shared_gtin = a.gtins & b.gtins
    if shared_gtin:
        breakdown["identifier"] = sorted(shared_gtin)[0]
        breakdown["reason"] = "UPC/EAN/GTIN idéntico"
        return PairScore(a.id, b.id, cfg.w_identifier, METHOD_EAN, breakdown)

    shared_mpn = a.mpns & b.mpns
    mpn_bonus = 0.0
    if shared_mpn:
        breakdown["manufacturer_sku"] = sorted(shared_mpn)[0]
        mpn_bonus = cfg.w_manufacturer_sku

    # --- 2. Rechazos duros --------------------------------------------
    if a.game and b.game and a.game != b.game:
        breakdown["reason"] = f"Juegos distintos: {a.game} vs {b.game}"
        return PairScore(a.id, b.id, 0.0, METHOD_FUZZY, breakdown)

    if a.set_code and b.set_code and a.set_code != b.set_code:
        breakdown["reason"] = f"Expansiones distintas: {a.set_code} vs {b.set_code}"
        breakdown["penalty_set"] = cfg.p_set
        return PairScore(a.id, b.id, 0.0, METHOD_FUZZY, breakdown)

    if a.product_type and b.product_type and a.product_type != b.product_type:
        breakdown["reason"] = (
            f"Tipos de producto distintos: {a.product_type} vs {b.product_type}"
        )
        breakdown["penalty_type"] = cfg.p_type
        return PairScore(a.id, b.id, 0.0, METHOD_FUZZY, breakdown)

    # El idioma también es identidad: un ETB en español y uno en inglés son
    # productos distintos. Al ser rechazo duro —y no una simple penalización—
    # el par no vuelve a la cola de revisión: ya está respondido.
    if cfg.language_is_identity and a.language and b.language and a.language != b.language:
        breakdown["reason"] = f"Idiomas distintos: {a.language} vs {b.language}"
        return PairScore(a.id, b.id, 0.0, METHOD_FUZZY, breakdown)

    # --- 3. Puntaje ponderado -----------------------------------------
    earned = 0.0
    applicable = 0.0

    def attribute_component(
        value_a: Optional[object], value_b: Optional[object], weight: float, label: str
    ) -> bool:
        """Devuelve True si ambos valores eran conocidos e iguales."""
        nonlocal earned, applicable
        if value_a is not None and value_b is not None:
            applicable += weight
            earned += weight
            breakdown[label] = {"value": value_a, "points": round(weight, 2)}
            return True
        if value_a is None and value_b is None:
            return False  # el atributo no aporta información: fuera del denominador
        applicable += weight
        partial = weight * cfg.unknown_credit
        earned += partial
        breakdown[label] = {
            "value": value_a if value_a is not None else value_b,
            "points": round(partial, 2),
            "note": "solo una de las dos tiendas lo declara",
        }
        return False

    same_set = attribute_component(a.set_code, b.set_code, cfg.w_set, "set")
    same_type = attribute_component(
        a.product_type, b.product_type, cfg.w_type, "product_type"
    )

    # Cantidad: solo se compara si ambas se determinaron con confianza.
    quantity_penalty = 0.0
    same_quantity = False
    a_qty = a.units_total if a.quantity_confidence >= cfg.min_quantity_confidence else None
    b_qty = b.units_total if b.quantity_confidence >= cfg.min_quantity_confidence else None
    if a_qty is not None and b_qty is not None:
        applicable += cfg.w_quantity
        if a_qty == b_qty:
            earned += cfg.w_quantity
            same_quantity = True
            breakdown["quantity"] = {"value": a_qty, "points": round(cfg.w_quantity, 2)}
        else:
            quantity_penalty = cfg.p_quantity
            breakdown["quantity"] = {
                "value": f"{a_qty} vs {b_qty}",
                "points": round(cfg.p_quantity, 2),
            }
    elif a_qty is not None or b_qty is not None:
        attribute_component(a_qty, b_qty, cfg.w_quantity, "quantity")

    # Idioma
    language_penalty = 0.0
    if a.language and b.language:
        applicable += cfg.w_language
        if a.language == b.language:
            earned += cfg.w_language
            breakdown["language"] = {"value": a.language, "points": round(cfg.w_language, 2)}
        else:
            language_penalty = cfg.p_language
            breakdown["language"] = {
                "value": f"{a.language} vs {b.language}",
                "points": round(cfg.p_language, 2),
            }

    # --- corte temprano -------------------------------------------------
    # Jaccard son operaciones de conjuntos (microsegundos); las métricas de
    # texto hacen varios Levenshtein sobre cadenas largas (milisegundos).
    # Con el Jaccard ya calculado podemos acotar por arriba el puntaje final
    # suponiendo similitud de nombre perfecta: si ni así llega al umbral de
    # revisión, no hay razón para calcular el resto. La cota es admisible
    # (las penalizaciones que faltan solo restan), así que no pierde matches.
    token_jaccard = similarity.jaccard(a.tokens, b.tokens)
    if not shared_mpn:
        ceiling = 100.0 * (
            (earned + cfg.w_tokens * token_jaccard + cfg.w_name)
            / (applicable + cfg.w_tokens + cfg.w_name)
        ) + quantity_penalty + language_penalty
        if ceiling < cfg.review_threshold:
            breakdown["reason"] = "Descartado sin comparar el texto completo"
            breakdown["token_overlap"] = {"value": round(token_jaccard, 3)}
            return PairScore(a.id, b.id, round(max(0.0, ceiling), 2), METHOD_FUZZY, breakdown)

    # Similitud textual (siempre aplica)
    metrics = similarity.name_similarity(a.tokens, b.tokens, a.vector, b.vector)
    applicable += cfg.w_tokens + cfg.w_name
    token_points = cfg.w_tokens * metrics["jaccard"]
    name_points = cfg.w_name * metrics["combined"]
    earned += token_points + name_points
    breakdown["token_overlap"] = {
        "value": round(metrics["jaccard"], 3),
        "points": round(token_points, 2),
    }
    breakdown["name_similarity"] = {
        "value": round(metrics["combined"], 3),
        "points": round(name_points, 2),
        "detail": {key: round(val, 3) for key, val in metrics.items() if key != "combined"},
    }

    # Tokens distintivos: presentes en un nombre y ausentes en el otro, y
    # raros en el catálogo. Es la señal que separa dos variantes del mismo
    # set y tipo ("Tin Mega Feraligatr ex" vs "Tin Mega Emboar ex"): sin
    # esto ambas suman set + tipo + cantidad y el nombre se parece demasiado.
    # Las palabras frecuentes ("ingles", "espanol", "pokemon") no penalizan.
    distinctive_penalty = 0.0
    if index is not None:
        exclusive = set(a.tokens) ^ set(b.tokens)
        distinctive = sorted(
            token
            for token in exclusive
            # Los tokens con "_" salen del diccionario de equivalencias: son
            # conceptos (tipos de producto), nunca palabras incidentales. Que
            # uno los tenga y el otro no es una diferencia estructural, por
            # frecuente que sea el token:
            #   "Luminose City Mini Tin"  vs  "Display Sellado ... Mini Tin"
            #
            # Una letra suelta que aparece en un nombre y no en el otro es,
            # por definición, lo único que los separa, aunque sea frecuente
            # en el catálogo:
            #   "Mega Charizard X Tin"  vs  "Mega Charizard Y Tin"
            if "_" in token
            or (len(token) == 1 and token.isalpha())
            or index.document_ratio(token) <= cfg.distinctive_token_ratio
        )
        if distinctive:
            distinctive_penalty = max(
                cfg.max_distinctive_penalty, cfg.p_distinctive * len(distinctive)
            )
            breakdown["distinctive_tokens"] = {
                "value": distinctive,
                "points": round(distinctive_penalty, 2),
                "note": "aparecen en un nombre y no en el otro, y son poco frecuentes",
            }

    score = 100.0 * (earned / applicable) if applicable else 0.0
    score += quantity_penalty + language_penalty + distinctive_penalty
    if mpn_bonus:
        score = max(score, cfg.auto_threshold + 5.0)

    # Seguridad: si ninguno de los dos tiene set ni tipo identificados, el
    # puntaje se apoya solo en el texto -> no permitimos agrupar en automático.
    attributes_known = bool(
        (a.set_code or b.set_code) or (a.product_type or b.product_type)
    )
    if not attributes_known and a.name_key != b.name_key:
        score = min(score, cfg.unknown_attributes_cap)
        breakdown["cap"] = "sin atributos identificados: requiere revisión manual"

    score = max(0.0, min(100.0, score))

    # --- 4. Método ------------------------------------------------------
    if shared_mpn:
        method = METHOD_SKU
    elif a.name_key and a.name_key == b.name_key:
        method = METHOD_NAME
        score = max(score, 95.0)
        breakdown["reason"] = "Nombre normalizado idéntico"
    elif same_set and same_type and (same_quantity or a_qty == b_qty):
        method = METHOD_ATTRIBUTE
    else:
        method = METHOD_FUZZY

    return PairScore(a.id, b.id, round(score, 2), method, breakdown)


# ---------------------------------------------------------------------------
# Union-Find que respeta las separaciones manuales
# ---------------------------------------------------------------------------
class BlockedUnionFind:
    """Union-Find con dos restricciones que se aplican también en cadena.

    El scoring compara pares, pero los grupos se forman por cierre transitivo:
    si A≈B y B≈C, A y C acaban juntos aunque nunca se comparen. Eso permitía
    que un producto sin idioma declarado uniera la versión en español con la
    inglesa. Por eso la coherencia se comprueba a nivel de GRUPO:

      · separaciones manuales del usuario
      · atributos de identidad incompatibles (set, tipo, idioma)
    """

    #: Atributos que dos ofertas del mismo grupo no pueden tener distintos.
    DEFAULT_IDENTITY = ("set_code", "product_type", "language")

    def __init__(
        self,
        ids: Iterable[int],
        blocked: Dict[int, Set[int]],
        attributes: Optional[Dict[int, Dict[str, object]]] = None,
        identity: Optional[Sequence[str]] = None,
    ):
        self.IDENTITY = tuple(identity if identity is not None else self.DEFAULT_IDENTITY)
        self.parent: Dict[int, int] = {i: i for i in ids}
        self.members: Dict[int, Set[int]] = {i: {i} for i in self.parent}
        self.blocked = blocked
        self.attributes = attributes or {}
        # Valores conocidos de cada atributo de identidad por grupo.
        self.known: Dict[int, Dict[str, Set[object]]] = {
            i: {
                key: ({value} if (value := self.attributes.get(i, {}).get(key)) else set())
                for key in self.IDENTITY
            }
            for i in self.parent
        }

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # compresión de caminos
            self.parent[item], item = root, self.parent[item]
        return root

    def can_union(self, a: int, b: int) -> bool:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return True

        # Una separación manual entre cualquier par de miembros lo impide.
        small, large = sorted((root_a, root_b), key=lambda r: len(self.members[r]))
        target = self.members[large]
        for member in self.members[small]:
            if self.blocked.get(member, set()) & target:
                return False

        # Ningún atributo de identidad puede quedar con dos valores distintos.
        for key in self.IDENTITY:
            if len(self.known[root_a][key] | self.known[root_b][key]) > 1:
                return False
        return True

    def union(self, a: int, b: int) -> bool:
        if not self.can_union(a, b):
            return False
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return True
        if len(self.members[root_a]) < len(self.members[root_b]):
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        self.members[root_a] |= self.members.pop(root_b)
        merged = self.known.pop(root_b)
        for key in self.IDENTITY:
            self.known[root_a][key] |= merged[key]
        return True

    def groups(self) -> List[List[int]]:
        buckets: Dict[int, List[int]] = defaultdict(list)
        for item in self.parent:
            buckets[self.find(item)].append(item)
        return [sorted(group) for group in buckets.values()]


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------
class MatchEngine:
    def __init__(self, cfg: Optional[ScoringConfig] = None) -> None:
        self.cfg = cfg or ScoringConfig.from_settings()
        self.index = TfidfIndex()

    # -- preparación -----------------------------------------------------
    def prepare(self, candidates: Sequence[Candidate]) -> None:
        self.index.fit([c.tokens for c in candidates])
        for candidate in candidates:
            candidate.vector = self.index.vector(candidate.tokens)

    # -- generación de pares candidatos (blocking) ------------------------
    #
    # Comparar todo contra todo sería O(n²). En su lugar se generan pares a
    # partir de tres fuentes complementarias:
    #
    #   1. Bloques por atributos: (juego, set, tipo) y (juego, set)
    #   2. Clave de nombre normalizado idéntica
    #   3. Índice invertido de tokens poco frecuentes
    #
    # La 1 es la que más aporta cuando los atributos se detectaron bien; la 3
    # cubre los productos cuyos atributos no se pudieron extraer.
    MAX_BUCKET = 300

    def _compatible(self, a: Candidate, b: Candidate) -> bool:
        """Descarte barato antes de gastar en el scoring completo."""
        if a.game and b.game and a.game != b.game:
            return False
        if a.set_code and b.set_code and a.set_code != b.set_code:
            return False
        if a.product_type and b.product_type and a.product_type != b.product_type:
            return False
        return True

    def candidate_pairs(self, candidates: Sequence[Candidate]) -> Set[Tuple[int, int]]:
        by_id = {c.id: c for c in candidates}
        pairs: Set[Tuple[int, int]] = set()

        def add(a_id: int, b_id: int) -> None:
            if a_id == b_id:
                return
            if not self._compatible(by_id[a_id], by_id[b_id]):
                return
            pairs.add((min(a_id, b_id), max(a_id, b_id)))

        # --- 1 y 2: bloques exactos -----------------------------------
        buckets: Dict[Tuple, List[int]] = defaultdict(list)
        for candidate in candidates:
            if candidate.set_code:
                buckets[("set_type", candidate.game, candidate.set_code,
                         candidate.product_type)].append(candidate.id)
                buckets[("set", candidate.game, candidate.set_code)].append(candidate.id)
            if candidate.name_key:
                buckets[("name", candidate.name_key)].append(candidate.id)

        for members in buckets.values():
            if len(members) < 2 or len(members) > self.MAX_BUCKET:
                continue
            for index, a_id in enumerate(members):
                for b_id in members[index + 1 :]:
                    add(a_id, b_id)

        # --- 3: tokens poco frecuentes --------------------------------
        postings: Dict[str, List[int]] = defaultdict(list)
        for candidate in candidates:
            for token in set(candidate.tokens):
                postings[token].append(candidate.id)

        # Un token sirve para bloquear si es raro en proporción o si su lista
        # es corta en términos absolutos (importante con catálogos pequeños,
        # donde casi todos los tokens superarían el umbral proporcional).
        usable = {
            token: bucket
            for token, bucket in postings.items()
            if len(bucket) <= 60 or self.index.document_ratio(token) <= self.cfg.max_token_ratio
        }

        for candidate in candidates:
            shared: Dict[int, float] = defaultdict(float)
            for token in set(candidate.tokens):
                bucket = usable.get(token)
                if not bucket or len(bucket) > 400:
                    continue
                weight = self.index.rarity(token)
                for other_id in bucket:
                    if other_id != candidate.id:
                        shared[other_id] += weight

            best = sorted(shared.items(), key=lambda item: item[1], reverse=True)
            for other_id, _weight in best[: self.cfg.max_candidates]:
                add(candidate.id, other_id)

        return pairs

    # -- ejecución completa ------------------------------------------------
    def run(
        self,
        candidates: Sequence[Candidate],
        manual_same: Sequence[Tuple[str, str]] = (),
        manual_different: Sequence[Tuple[str, str]] = (),
        precomputed: Optional[Sequence[PairScore]] = None,
    ) -> MatchResult:
        """Agrupa las ofertas.

        `precomputed` permite reutilizar los puntajes de una pasada anterior.
        Sirve cuando lo único que cambió son las decisiones manuales: esas
        alteran qué uniones se permiten, no cuánto se parecen dos productos,
        así que volver a comparar decenas de miles de pares sería tiempo
        tirado. Es lo que hace instantánea la revisión manual.
        """
        if not candidates:
            return MatchResult(groups=[], decisions={}, reviews=[])

        if precomputed is None:
            self.prepare(candidates)
        by_key = {c.key: c.id for c in candidates}
        ids = [c.id for c in candidates]

        # Separaciones manuales -> pares prohibidos
        blocked: Dict[int, Set[int]] = defaultdict(set)
        for a_key, b_key in manual_different:
            a_id, b_id = by_key.get(a_key), by_key.get(b_key)
            if a_id is not None and b_id is not None:
                blocked[a_id].add(b_id)
                blocked[b_id].add(a_id)

        dsu = BlockedUnionFind(
            ids,
            blocked,
            {
                c.id: {
                    "set_code": c.set_code,
                    "product_type": c.product_type,
                    "language": c.language,
                }
                for c in candidates
            },
            identity=(
                BlockedUnionFind.DEFAULT_IDENTITY
                if self.cfg.language_is_identity
                else ("set_code", "product_type")
            ),
        )
        decisions: Dict[int, PairScore] = {}
        reviews: List[PairScore] = []
        by_id = {c.id: c for c in candidates}

        def record(pair: PairScore) -> None:
            for member in (pair.a_id, pair.b_id):
                current = decisions.get(member)
                if current is None or pair.score > current.score:
                    decisions[member] = pair

        # --- 1. Identificadores objetivos: máxima prioridad --------------
        by_gtin: Dict[str, List[int]] = defaultdict(list)
        for candidate in candidates:
            for gtin in candidate.gtins:
                by_gtin[gtin].append(candidate.id)
        for gtin, bucket in by_gtin.items():
            if len(bucket) < 2:
                continue
            anchor = bucket[0]
            for other in bucket[1:]:
                pair = PairScore(
                    anchor, other, 100.0, METHOD_EAN,
                    {"identifier": gtin, "reason": "UPC/EAN/GTIN idéntico"},
                )
                if dsu.union(anchor, other):
                    record(pair)

        # --- 2. Decisiones manuales de "es el mismo producto" -------------
        self.conflicts: List[Tuple[str, str]] = []
        for a_key, b_key in manual_same:
            a_id, b_id = by_key.get(a_key), by_key.get(b_key)
            if a_id is None or b_id is None:
                continue
            if dsu.union(a_id, b_id):
                pair = PairScore(
                    a_id, b_id, 100.0, METHOD_MANUAL,
                    {"reason": "Confirmado manualmente por el usuario"},
                )
                decisions[a_id] = pair
                decisions[b_id] = pair
            else:
                # Une dos grupos que otra decisión manual mandó separar.
                # Gana la separación; lo registramos para que sea visible.
                self.conflicts.append((a_key, b_key))

        # --- 3. Scoring de los pares candidatos ---------------------------
        rejected = 0
        if precomputed is not None:
            scored = list(precomputed)
        else:
            scored = []
            for a_id, b_id in self.candidate_pairs(candidates):
                pair = score_pair(by_id[a_id], by_id[b_id], self.cfg, self.index)
                if pair.score >= self.cfg.review_threshold:
                    scored.append(pair)
                else:
                    rejected += 1
            # De mayor a menor puntaje: las uniones más seguras primero.
            scored.sort(key=lambda p: p.score, reverse=True)

        compared = len(scored) + rejected

        for pair in scored:
            # Las separaciones manuales se aplican aquí y no al puntuar, para
            # que los puntajes se puedan cachear entre reagrupaciones.
            if pair.b_id in blocked.get(pair.a_id, set()):
                continue
            if pair.score >= self.cfg.auto_threshold:
                if dsu.find(pair.a_id) == dsu.find(pair.b_id):
                    continue
                if dsu.union(pair.a_id, pair.b_id):
                    record(pair)
                else:
                    reviews.append(pair)  # bloqueado por una separación manual
            else:
                if dsu.find(pair.a_id) != dsu.find(pair.b_id):
                    reviews.append(pair)

        # Ofertas que quedaron solas
        for candidate in candidates:
            decisions.setdefault(
                candidate.id,
                PairScore(
                    candidate.id, candidate.id, 100.0, METHOD_SINGLETON,
                    {"reason": "Única oferta encontrada para este producto"},
                ),
            )

        return MatchResult(
            groups=dsu.groups(),
            decisions=decisions,
            reviews=reviews,
            rejected=rejected,
            compared_pairs=compared,
            scored=scored,
            from_cache=precomputed is not None,
        )
