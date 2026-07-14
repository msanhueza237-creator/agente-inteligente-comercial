import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import get_db
from app.db.models import DedupCandidate, DedupCandidateStatus, DedupStatus, Prospect
from app.dedup.merge import merge_fields
from app.web.deps import templates

router = APIRouter()


@router.get("/dedup")
async def dedup_queue(request: Request, db: AsyncSession = Depends(get_db)):
    candidates = (
        (
            await db.execute(
                select(DedupCandidate)
                .where(DedupCandidate.status == DedupCandidateStatus.pending)
                .options(selectinload(DedupCandidate.prospect_a), selectinload(DedupCandidate.prospect_b))
                .order_by(DedupCandidate.match_score.desc())
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(request, "dedup_queue.html", {"candidates": candidates})


@router.post("/dedup/{candidate_id}/merge")
async def merge_candidate(candidate_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    candidate = await db.get(DedupCandidate, candidate_id)
    if candidate is None:
        return RedirectResponse("/dedup?msg=Par+no+encontrado", status_code=303)

    winner = await db.get(Prospect, candidate.prospect_a_id)
    loser = await db.get(Prospect, candidate.prospect_b_id)

    winner_dict = {c.name: getattr(winner, c.name) for c in Prospect.__table__.columns}
    loser_dict = {c.name: getattr(loser, c.name) for c in Prospect.__table__.columns}
    merged, _conflicts = merge_fields(winner_dict, loser_dict, existing_source=None, incoming_source=None)
    for field, value in merged.items():
        if field not in ("id", "created_at", "status"):
            setattr(winner, field, value)

    loser.dedup_status = DedupStatus.merged
    loser.merged_into_id = winner.id
    winner.dedup_status = DedupStatus.unique

    candidate.status = DedupCandidateStatus.merged
    candidate.reviewed_at = datetime.now(timezone.utc)

    await db.commit()
    return RedirectResponse("/dedup?msg=Prospectos+fusionados", status_code=303)


@router.post("/dedup/{candidate_id}/reject")
async def reject_candidate(candidate_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    candidate = await db.get(DedupCandidate, candidate_id)
    if candidate is None:
        return RedirectResponse("/dedup?msg=Par+no+encontrado", status_code=303)

    candidate.status = DedupCandidateStatus.rejected_not_duplicate
    candidate.reviewed_at = datetime.now(timezone.utc)

    for prospect_id in (candidate.prospect_a_id, candidate.prospect_b_id):
        prospect = await db.get(Prospect, prospect_id)
        still_pending = (
            await db.execute(
                select(DedupCandidate).where(
                    DedupCandidate.status == DedupCandidateStatus.pending,
                    (DedupCandidate.prospect_a_id == prospect_id)
                    | (DedupCandidate.prospect_b_id == prospect_id),
                )
            )
        ).first()
        if not still_pending:
            prospect.dedup_status = DedupStatus.unique

    await db.commit()
    return RedirectResponse("/dedup?msg=Marcado+como+no+duplicado", status_code=303)
