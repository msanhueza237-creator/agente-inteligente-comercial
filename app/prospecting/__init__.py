"""Domain and worker primitives for CRM-controlled prospecting."""

from app.prospecting.contracts import (
    ProspectCandidate,
    ProspectingCampaign,
    ProspectingRunSnapshot,
    RunEvent,
    SourceEvidence,
)

__all__ = [
    "ProspectCandidate",
    "ProspectingCampaign",
    "ProspectingRunSnapshot",
    "RunEvent",
    "SourceEvidence",
]
