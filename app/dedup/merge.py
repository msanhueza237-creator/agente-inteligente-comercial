"""Field-conflict resolution when merging a newly ingested record into an
existing prospect. See docs plan section 4 (Deduplicacion) for the rationale.
"""

# Highest-trust source wins on a tie; index = priority (lower is more trusted).
SOURCE_PRIORITY = [
    "manual_edit",
    "crm",
    "excel_import",
    "google_places",
    "paginas_amarillas",
    "website_scrape",
    "llm_enrichment",
]

# Fields considered "volatile" -- prefer the most recently fetched value
# rather than sticking with whatever is already stored.
VOLATILE_FIELDS = {"google_rating", "google_ratings_total", "phone", "email", "website"}

# Once a prospect reaches these statuses, a merge must not silently overwrite
# fields -- conflicting incoming data should be surfaced for review instead.
PROTECTED_STATUSES = {"approved", "synced"}


def _source_rank(source_type: str | None) -> int:
    if source_type in SOURCE_PRIORITY:
        return SOURCE_PRIORITY.index(source_type)
    return len(SOURCE_PRIORITY)


def merge_fields(
    existing: dict,
    incoming: dict,
    *,
    existing_source: str | None = None,
    incoming_source: str | None = None,
) -> tuple[dict, list[str]]:
    """Merge `incoming` field values into `existing`, returning
    (merged_dict, conflicting_fields). `conflicting_fields` lists fields
    where existing and incoming disagreed and existing was protected
    (status in PROTECTED_STATUSES) -- callers should route these to a
    review queue instead of silently applying `merged_dict`.
    """
    merged = dict(existing)
    conflicts: list[str] = []
    protected = existing.get("status") in PROTECTED_STATUSES

    for field, new_value in incoming.items():
        if field in ("id", "status", "created_at"):
            continue
        old_value = existing.get(field)

        if new_value is None or new_value == "":
            continue

        if old_value is None or old_value == "":
            merged[field] = new_value
            continue

        if old_value == new_value:
            continue

        # Both sides have a non-null, differing value -- a real conflict.
        if protected:
            conflicts.append(field)
            continue

        if field in VOLATILE_FIELDS:
            merged[field] = new_value
        elif _source_rank(incoming_source) < _source_rank(existing_source):
            merged[field] = new_value
        # else: keep existing (more trusted or equal-priority source wins ties)

    return merged, conflicts
