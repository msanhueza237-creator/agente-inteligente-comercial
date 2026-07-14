from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import (
    CRMOutboxMessage,
    ProspectingCandidateRecord,
    ProspectingEventRecord,
    ProspectingRun,
    ProspectingTask,
)
from app.web.deps import templates

router = APIRouter()


@router.get("/monitor")
async def monitor(
    request: Request,
    run_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    runs = (
        (
            await db.execute(
                select(ProspectingRun).order_by(ProspectingRun.created_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    selected = None
    tasks = []
    events = []
    outbox_pending = 0
    candidate_count = 0
    if run_id:
        try:
            selected_id = uuid.UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Ejecución no encontrada") from exc
        selected = await db.get(ProspectingRun, selected_id)
        if selected is None:
            raise HTTPException(status_code=404, detail="Ejecución no encontrada")
    elif runs:
        selected = runs[0]

    if selected:
        tasks = (
            (
                await db.execute(
                    select(ProspectingTask)
                    .where(ProspectingTask.run_id == selected.id)
                    .order_by(ProspectingTask.created_at)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await db.execute(
                    select(ProspectingEventRecord)
                    .where(ProspectingEventRecord.run_id == selected.id)
                    .order_by(ProspectingEventRecord.occurred_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        outbox_pending = len(
            (
                await db.execute(
                    select(CRMOutboxMessage.id).where(
                        CRMOutboxMessage.run_id == selected.id,
                        CRMOutboxMessage.status == "queued",
                    )
                )
            ).all()
        )
        candidate_count = (
            await db.scalar(
                select(func.count(ProspectingCandidateRecord.id)).where(
                    ProspectingCandidateRecord.run_id == selected.id
                )
            )
        ) or 0

    counts = {
        "total": len(tasks),
        "completed": sum(task.status == "completed" for task in tasks),
        "failed": sum(task.status == "failed" for task in tasks),
        "pending": sum(task.status in {"pending", "running"} for task in tasks),
        "candidates": candidate_count,
    }
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "runs": runs,
            "selected": selected,
            "tasks": tasks,
            "events": events,
            "counts": counts,
            "outbox_pending": outbox_pending,
        },
    )
