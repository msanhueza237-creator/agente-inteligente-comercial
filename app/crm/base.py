"""Decoupling boundary between the enrichment pipeline and whichever CRM API
turns out to back "Latin Chile" once the discovery spike (see
scripts/crm_discovery_spike.py and docs/crm_api_notes.md) is complete.

The rest of the pipeline (ingestion, enrichment, dedup, scoring) programs
against this interface only -- app/crm/latin_chile.py is the sole file that
needs to be filled in once the real API is understood.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProspectDTO:
    name: str
    rut: str | None
    phone: str | None
    email: str | None
    website: str | None
    address: str | None
    region: str | None
    comuna: str | None
    category: str | None
    commercial_potential_level: str | None
    notes: str | None = None


@dataclass
class CRMProspect:
    crm_id: str
    raw: dict


@dataclass
class CRMUpsertResult:
    crm_id: str
    created: bool
    raw_response: dict


class CRMClient(Protocol):
    def test_connection(self) -> bool:
        """Verify credentials/connectivity without mutating any data."""
        ...

    def search_prospect(
        self, rut: str | None = None, name: str | None = None, phone: str | None = None
    ) -> CRMProspect | None:
        """Look up an existing CRM record, preferring RUT, then name+phone."""
        ...

    def upsert_prospect(self, prospect: ProspectDTO) -> CRMUpsertResult:
        """Create or update a prospect in the CRM. Must be idempotent: calling
        it twice with the same RUT must not create duplicate CRM records."""
        ...
