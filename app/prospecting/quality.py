from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from unidecode import unidecode

from app.prospecting.contracts import ProspectCandidate, SourceName

TARGET_TYPES = {
    "distribuidor",
    "tienda comercial",
    "tecnico",
    "instalador grande",
    "competencia",
}

QUALITY_REJECTION_REASONS = (
    "not_hvac_related",
    "excluded_business_type",
    "target_type_unconfirmed",
    "outside_target_types",
    "outside_requested_territory",
    "missing_business_contact",
    "missing_required_evidence",
)


def _normalize(value: str | None) -> str:
    text = unidecode(value or "").casefold()
    text = re.sub(r"[_/|,-]+", " ", text)
    return " ".join(text.split())


_STRONG_HVAC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aire_acondicionado", re.compile(r"\baire(?:s)? acondicionado(?:s)?\b")),
    ("hvac_r", re.compile(r"\bhvac(?:\s*r)?\b")),
    (
        "google_hvac_business_type",
        re.compile(
            r"\b(?:air conditioning|hvac) contractor\b|"
            r"\brefrigeration equipment (?:supplier|store)\b|"
            r"\bair conditioning (?:equipment )?(?:supplier|store)\b"
        ),
    ),
    (
        "refrigeracion_especializada",
        re.compile(
            r"\b(?:refrigeracion|frio) (?:comercial|industrial)\b|"
            r"\bsistemas? de refrigeracion\b"
        ),
    ),
    (
        "camaras_frigorificas",
        re.compile(r"\bcamaras? (?:frigorificas?|de frio|refrigeradas?)\b"),
    ),
    ("climatizacion", re.compile(r"\bclimatiz(?:acion|adores?|ador|ar)\b")),
    ("chillers", re.compile(r"\bchillers?\b")),
    ("bombas_calor", re.compile(r"\bbombas? de calor\b")),
    ("vrf_vrv", re.compile(r"\b(?:vrf|vrv)\b")),
    (
        "contratista_hvac",
        re.compile(r"\b(?:contratista|contractor) (?:de )?(?:hvac|climatizacion)\b"),
    ),
    (
        "productos_hvac",
        re.compile(
            r"\b(?:equipos?|repuestos?|herramientas?|insumos?|suministros?) (?:de |para )?"
            r"(?:hvac|climatizacion|aire acondicionado|refrigeracion)\b"
        ),
    ),
    (
        "servicios_hvac",
        re.compile(
            r"\b(?:instalacion|mantencion|mantenimiento|reparacion|servicio tecnico) (?:de |para )?"
            r"(?:sistemas? de )?(?:hvac|climatizacion|aire acondicionado|refrigeracion)\b"
        ),
    ),
)

_HVAC_CONTEXT = re.compile(
    r"\b(?:hvac|aire acondicionado|climatizacion|refrigeracion (?:comercial|industrial)|"
    r"frio industrial|camaras? (?:frigorificas?|de frio)|chillers?|vrf|vrv)\b"
)
_WEAK_ACTIVITY = re.compile(
    r"\b(?:refrigeracion|ventilacion|calefaccion|mantencion|mantenimiento|instalacion|"
    r"servicio tecnico|tecnico|repuestos?|ingenieria|proyectos?)\b"
)

_DISTRIBUTOR = re.compile(
    r"\b(?:distribuidor|distribucion|importador|mayorista|distributor|importer|wholesaler|supplier)\b"
)
_STORE = re.compile(
    r"\b(?:tienda|local comercial|venta|comercializ|repuestos?|herramientas?|insumos?|"
    r"suministros?|equipos?|store|shop|retail)\b"
)
_TECHNICIAN = re.compile(
    r"\b(?:tecnico|servicio tecnico|instalacion|instalador|mantencion|mantenimiento|reparacion|"
    r"technician|service|installation|installer|maintenance|repair|contractor)\b"
)
_LARGE_INSTALLER = re.compile(
    r"\b(?:ingenieria|contratista|proyectos? (?:hvac|de climatizacion|de refrigeracion)|"
    r"instalaciones? (?:comerciales?|industriales?)|climatizacion (?:comercial|industrial)|"
    r"refrigeracion industrial|frio industrial)\b"
)

_FULL_TEXT_EXCLUSIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "automotive_air_conditioning",
        re.compile(r"\b(?:aire acondicionado|climatizacion) automotriz\b|\bauto clima\b"),
    ),
    (
        "refrigerated_transport",
        re.compile(r"\btransporte refrigerado\b|\bcamiones? frigorificos?\b"),
    ),
    (
        "appliance_repair_only",
        re.compile(
            r"\b(?:reparacion|servicio tecnico) (?:de )?(?:lavadoras?|refrigeradores?|"
            r"electrodomesticos?|linea blanca)\b"
        ),
    ),
    (
        "employment_or_training",
        re.compile(
            r"\b(?:oferta de empleo|bolsa de trabajo|vacante|trabaja con nosotros|curso|"
            r"capacitacion|instituto|universidad)\b"
        ),
    ),
    (
        "content_or_directory",
        re.compile(
            r"\b(?:blog|noticias|directorio de empresas|guia de empresas|"
            r"manual educativo|documento educativo|tesis|archivo pdf)\b|\.pdf\b"
        ),
    ),
)

_NAME_TYPE_EXCLUSIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "refrigeration_end_user",
        re.compile(
            r"\b(?:supermercado|restaurant(?:e)?|hotel|pesquera|bodega frigorifica|"
            r"centro de distribucion|frigorifico de alimentos)\b"
        ),
    ),
    (
        "general_trade_without_hvac",
        re.compile(
            r"\b(?:ferreteria|constructora|electricista|electricidad|gasfiter|gasfiteria|"
            r"mantencion general)\b"
        ),
    ),
)

_BLOCKED_HOST_PARTS = (
    "amarillas.",
    "chileguia.",
    "guiadeempresas.",
    "chileinform.",
    "hotfrog.",
    "mercantil.",
    "mercadolibre.",
    "yapo.",
    "facebook.",
    "instagram.",
    "linkedin.",
    "twitter.",
    "x.com",
    "starofservice.",
    "tripadvisor.",
    "wikipedia.",
)


@dataclass(frozen=True)
class HVACQualityAssessment:
    eligible: bool
    hvac_confidence: float
    positive_signals: tuple[str, ...]
    negative_signals: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]
    inferred_target_type: str | None


@dataclass(frozen=True)
class DiscoveryAssessment:
    allow_details: bool
    priority: int
    positive_signals: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]


def _strong_signals(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in _STRONG_HVAC_PATTERNS if pattern.search(text))


def _provider_text(candidate: ProspectCandidate, provider: SourceName) -> str:
    values = [
        evidence.value
        for evidence in candidate.evidence
        if evidence.provider == provider
        and evidence.field
        in {
            "name",
            "trade_name",
            "description",
            "category",
            "specialties",
            "business_status",
        }
    ]
    return _normalize(" ".join(values))


def _candidate_text(candidate: ProspectCandidate) -> str:
    return _normalize(
        " ".join(
            value
            for value in (
                candidate.name,
                candidate.trade_name,
                candidate.description,
                candidate.category,
                *candidate.specialties,
                *candidate.brands,
            )
            if value
        )
    )


def _critical_exclusions(candidate: ProspectCandidate, text: str) -> tuple[str, ...]:
    reasons = [name for name, pattern in _FULL_TEXT_EXCLUSIONS if pattern.search(text)]
    name_type_text = _normalize(
        " ".join(
            value
            for value in (candidate.name, candidate.trade_name, candidate.category)
            if value
        )
    )
    for name, pattern in _NAME_TYPE_EXCLUSIONS:
        if not pattern.search(name_type_text):
            continue
        if name == "general_trade_without_hvac" and _strong_signals(name_type_text):
            continue
        reasons.append(name)

    host = urlparse(candidate.website or "").netloc.casefold()
    if host and any(blocked in host for blocked in _BLOCKED_HOST_PARTS):
        reasons.append("directory_marketplace_or_social_profile")
    if "permanently_closed" in candidate.review_flags or "cerrado permanentemente" in text:
        reasons.append("permanently_closed")
    return tuple(dict.fromkeys(reasons))


def infer_hvac_target_type(text: str, *, radar_mode: bool = False) -> str | None:
    normalized = _normalize(text)
    if _DISTRIBUTOR.search(normalized):
        return "distribuidor"
    if _LARGE_INSTALLER.search(normalized):
        return "instalador grande"
    if _TECHNICIAN.search(normalized):
        return "tecnico"
    if _STORE.search(normalized):
        return "tienda comercial"
    if radar_mode and _strong_signals(normalized):
        return "competencia"
    return None


def evaluate_hvac_quality(
    candidate: ProspectCandidate,
    *,
    allowed_target_types: tuple[str, ...] | list[str] | set[str] = (),
    radar_mode: bool = False,
) -> HVACQualityAssessment:
    """Return the mandatory HVAC-R admission gate before commercial scoring.

    Search queries and review flags are deliberately ignored. Brave snippets
    may discover a company but only an official website (or coherent Google
    Places fields) can confirm the trade.
    """

    text = _candidate_text(candidate)
    strong = _strong_signals(text)
    weak_context = bool(_WEAK_ACTIVITY.search(text) and _HVAC_CONTEXT.search(text))
    positives = list(strong)
    if weak_context:
        positives.append("weak_signal_with_explicit_hvac_context")

    critical = _critical_exclusions(candidate, text)
    official_text = _provider_text(candidate, SourceName.official_website)
    google_text = _provider_text(candidate, SourceName.google_places)
    official_signals = _strong_signals(official_text)
    google_signals = _strong_signals(google_text)

    google_fields = {
        evidence.field
        for evidence in candidate.evidence
        if evidence.provider == SourceName.google_places
        and evidence.field in {"name", "description", "category", "specialties"}
        and _strong_signals(_normalize(evidence.value))
    }
    google_role = bool(
        _DISTRIBUTOR.search(google_text)
        or _STORE.search(google_text)
        or _TECHNICIAN.search(google_text)
        or _LARGE_INSTALLER.search(google_text)
        or any(
            token in google_text
            for token in (
                "hvac contractor",
                "air conditioning contractor",
                "refrigeration equipment supplier",
                "air conditioning store",
            )
        )
    )
    official_confirmed = bool(official_signals)
    google_confirmed = bool(google_signals) and (len(google_fields) >= 2 or google_role)
    evidence_confirmed = official_confirmed or google_confirmed

    if official_confirmed:
        positives.append("official_website_hvac_evidence")
    if google_confirmed:
        positives.append("coherent_google_places_hvac_evidence")

    inferred = infer_hvac_target_type(text, radar_mode=radar_mode) if evidence_confirmed else None
    reasons: list[str] = []
    negatives = list(critical)
    if critical:
        reasons.append("excluded_business_type")
    if not strong or not evidence_confirmed:
        reasons.append("not_hvac_related")
    if evidence_confirmed and inferred is None:
        reasons.append("target_type_unconfirmed")

    allowed = set(allowed_target_types)
    if inferred == "competencia" and not radar_mode:
        reasons.append("outside_target_types")
    elif allowed and inferred and inferred not in allowed:
        reasons.append("outside_target_types")
    if inferred == "otro":
        reasons.append("target_type_unconfirmed")

    confidence = 0.0
    if official_confirmed:
        confidence = 0.95
    elif google_confirmed:
        confidence = 0.85
    elif strong:
        confidence = 0.45
    if critical:
        confidence = min(confidence, 0.1)

    reasons = list(dict.fromkeys(reasons))
    return HVACQualityAssessment(
        eligible=not reasons,
        hvac_confidence=confidence,
        positive_signals=tuple(dict.fromkeys(positives)),
        negative_signals=tuple(dict.fromkeys(negatives)),
        exclusion_reasons=tuple(reasons),
        inferred_target_type=inferred,
    )


def evaluate_google_discovery(
    *,
    name: str,
    place_types: tuple[str, ...] | list[str],
    independent_query_hits: int,
) -> DiscoveryAssessment:
    """Decide whether a Text Search result deserves a paid Details request."""

    text = _normalize(" ".join((name, *place_types)))
    signals = _strong_signals(text)
    critical: list[str] = []
    for reason, pattern in (*_FULL_TEXT_EXCLUSIONS, *_NAME_TYPE_EXCLUSIONS):
        if pattern.search(text):
            critical.append(reason)

    generic_place = any(
        token in text
        for token in (
            "store",
            "general contractor",
            "home goods store",
            "point of interest",
            "establishment",
        )
    )
    explicit = bool(signals)
    ambiguous_supported = independent_query_hits >= 2 and bool(
        _WEAK_ACTIVITY.search(text) or generic_place
    )
    allow = not critical and (explicit or ambiguous_supported)
    priority = 2 if explicit else (1 if ambiguous_supported else 0)
    reasons = () if allow else tuple(dict.fromkeys(critical or ["not_hvac_related"]))
    return DiscoveryAssessment(
        allow_details=allow,
        priority=priority,
        positive_signals=signals,
        exclusion_reasons=reasons,
    )
