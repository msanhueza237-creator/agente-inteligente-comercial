from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CRMOutboxMessage, ProspectEvidenceRecord, ProspectingCandidateRecord
from app.prospecting.contracts import ProspectCandidate, ProspectLocation, SourceEvidence
from app.prospecting.store import assess_import_eligibility, index_candidate_location_evidence
from app.prospecting.validation import has_business_contact, normalize_evidence_value


_INDEXED_LOCATION_FIELD = re.compile(r"^locations\[(\d+)]\.(region_code|region_name|comuna_code|comuna_name|address)$")


def _supported_value(
    candidate: ProspectCandidate,
    evidence: list[SourceEvidence],
    field: str,
) -> str | None:
    current = getattr(candidate, field)
    matches = [item for item in evidence if item.field == field]
    if current and any(
        normalize_evidence_value(field, item.value)
        == normalize_evidence_value(field, current)
        for item in matches
    ):
        return current
    return matches[0].value if matches else None


def rehydrate_candidate_from_evidence(
    candidate: ProspectCandidate, retained: list[SourceEvidence]
) -> ProspectCandidate | None:
    """Rebuild every external field solely from retained source evidence."""

    name = _supported_value(candidate, retained, "name")
    if not name:
        return None

    indexed: dict[int, dict[str, str]] = {}
    for item in retained:
        match = _INDEXED_LOCATION_FIELD.fullmatch(item.field)
        if match:
            indexed.setdefault(int(match.group(1)), {}).setdefault(match.group(2), item.value)

    locations: list[ProspectLocation] = []
    for index in sorted(indexed):
        values = indexed[index]
        if not values.get("region_code") or not values.get("comuna_code"):
            continue
        try:
            locations.append(ProspectLocation(**values))
        except ValueError:
            continue
    if not locations:
        legacy = {
            item.field.removeprefix("location."): item.value
            for item in retained
            if item.field.startswith("location.")
        }
        if legacy.get("region_code") and legacy.get("comuna_code"):
            try:
                locations.append(ProspectLocation(**legacy))
            except ValueError:
                pass
    if not locations:
        return None

    provider_ids: dict[str, str] = {}
    for item in sorted(
        retained,
        key=lambda value: (
            value.provider.value,
            value.observed_at,
            value.provider_record_id or "",
        ),
    ):
        if item.provider_record_id:
            provider_ids.setdefault(item.provider.value, item.provider_record_id)

    updates = {
        "name": name,
        "trade_name": _supported_value(candidate, retained, "trade_name"),
        "rut": _supported_value(candidate, retained, "rut"),
        "phone": _supported_value(candidate, retained, "phone"),
        "email": _supported_value(candidate, retained, "email"),
        "website": _supported_value(candidate, retained, "website"),
        "description": _supported_value(candidate, retained, "description"),
        "provider_ids": provider_ids,
        "location": locations[0],
        "locations": locations,
        "evidence": retained,
        "import_eligible": False,
        "importable_location_indexes": (),
        "review_flags": (),
    }
    rebuilt = ProspectCandidate.model_validate(
        {**candidate.model_dump(mode="python"), **updates}
    )
    if not has_business_contact(rebuilt):
        return None
    return assess_import_eligibility(index_candidate_location_evidence(rebuilt))


def rehydrate_candidate_batch_payload(payload: list[dict], *, at: datetime) -> list[dict]:
    """Scrub a not-yet-sent candidate outbox batch without changing its shape."""

    safe_candidates: list[dict] = []
    for raw_candidate in payload:
        prospect = ProspectCandidate.model_validate(raw_candidate)
        safe_evidence = [
            item
            for item in prospect.evidence
            if not item.retention_until or item.retention_until > at
        ]
        rebuilt = rehydrate_candidate_from_evidence(prospect, safe_evidence)
        if rebuilt is not None:
            safe_candidates.append(rebuilt.model_dump(mode="json"))
    return safe_candidates


async def purge_expired_source_data(
    session: AsyncSession, *, at: datetime | None = None
) -> dict[str, int]:
    """Remove expired licensed-source evidence from rows and JSON mirrors.

    Delivered outbox payloads are transport artifacts, so they are removed
    after 30 days regardless of source. Stable provider IDs remain only when
    another non-expired source still supports the candidate.
    """
    now = at or datetime.now(timezone.utc)
    expired = (
        (
            await session.execute(
                select(ProspectEvidenceRecord).where(
                    ProspectEvidenceRecord.retention_until.is_not(None),
                    ProspectEvidenceRecord.retention_until <= now,
                )
            )
        )
        .scalars()
        .all()
    )
    expired_ids = {row.id for row in expired}
    if expired_ids:
        await session.execute(
            delete(ProspectEvidenceRecord).where(ProspectEvidenceRecord.id.in_(expired_ids))
        )

    candidates = (
        (await session.execute(select(ProspectingCandidateRecord).with_for_update()))
        .scalars()
        .all()
    )
    json_evidence_removed = 0
    candidates_deleted = 0
    for candidate in candidates:
        payload = dict(candidate.payload)
        retained: list[dict] = []
        for raw_evidence in payload.get("evidence", []):
            evidence = SourceEvidence.model_validate(raw_evidence)
            if evidence.retention_until and evidence.retention_until <= now:
                json_evidence_removed += 1
            else:
                retained.append(evidence.model_dump(mode="json"))
        if len(retained) != len(payload.get("evidence", [])):
            rebuilt = rehydrate_candidate_from_evidence(
                ProspectCandidate.model_validate(payload),
                [SourceEvidence.model_validate(item) for item in retained],
            )
            if rebuilt is None:
                await session.delete(candidate)
                candidates_deleted += 1
                continue
            candidate.payload = rebuilt.model_dump(mode="json")

    queued_candidate_messages = (
        (
            await session.execute(
                select(CRMOutboxMessage)
                .where(
                    CRMOutboxMessage.status == "queued",
                    CRMOutboxMessage.kind == "candidates",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for message in queued_candidate_messages:
        raw_candidates = message.payload if isinstance(message.payload, list) else []
        if not any(
            (evidence := SourceEvidence.model_validate(raw)).retention_until
            and evidence.retention_until <= now
            for candidate_payload in raw_candidates
            for raw in candidate_payload.get("evidence", [])
        ):
            continue
        if message.attempt_count:
            # The key may already have committed remotely; changing its body
            # would turn a safe replay into an idempotency conflict.
            message.status = "dead"
            message.last_error = "expired evidence after an ambiguous delivery attempt"
            message.payload = []
            continue
        safe_candidates = rehydrate_candidate_batch_payload(raw_candidates, at=now)
        if safe_candidates:
            message.payload = safe_candidates
        else:
            message.status = "dead"
            message.last_error = "all candidate evidence expired before first delivery"
            message.payload = []

    outbox_result = await session.execute(
        delete(CRMOutboxMessage).where(
            CRMOutboxMessage.created_at <= now - timedelta(days=30),
            CRMOutboxMessage.status == "delivered",
        )
    )
    return {
        "evidence_rows": len(expired_ids),
        "candidate_evidence": json_evidence_removed,
        "candidates": candidates_deleted,
        "outbox_messages": outbox_result.rowcount or 0,
    }
