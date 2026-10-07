from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.db.base import get_db
from app.db.models import ResearchJob
from app.ingestion.historical import normalize_historical_file
from app.ingestion.pipeline import ingest_excel
from app.web.deps import templates
from app.web.import_auth import require_historical_import_user

router = APIRouter()
MAX_HISTORICAL_FILE_BYTES = 25 * 1024 * 1024


@router.get("/import")
async def import_form(request: Request):
    return templates.TemplateResponse(request, "import.html", {"result": None})


@router.post("/import")
async def import_file(
    request: Request, file: UploadFile, db: Annotated[AsyncSession, Depends(get_db)]
):
    content = await file.read()
    batch = await ingest_excel(db, content=content, filename=file.filename or "archivo")

    # The created/merged/needs_review/errors breakdown lives on the
    # research_jobs row created for this batch, not on the batch itself.
    job = (
        await db.execute(
            select(ResearchJob)
            .where(ResearchJob.parameters["import_batch_id"].astext == str(batch.id))
            .order_by(ResearchJob.created_at.desc())
        )
    ).scalars().first()

    result = {
        "filename": batch.filename,
        "row_count": batch.row_count,
        "stats": job.stats if job else {"created": 0, "merged": 0, "needs_review": 0, "errors": 0},
    }

    return templates.TemplateResponse(request, "import.html", {"result": result})


@router.post("/api/historical-imports/preview")
async def preview_historical_import(
    request: Request,
    user_id: Annotated[str, Depends(require_historical_import_user)],
    relationship_date: date | None = None,
):
    """Normalize an old customer workbook without writing contacts or companies."""
    # Authenticate before parsing multipart uploads. Bound the whole stream even
    # when Content-Length is absent, plus the file itself below.
    received = 0

    async def bounded_receive():
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > MAX_HISTORICAL_FILE_BYTES + 64 * 1024:
            raise HTTPException(413, "El archivo supera el limite de 25 MB.")
        return message

    bounded_request = Request(request.scope, bounded_receive)
    async with bounded_request.form(max_files=1, max_fields=0) as form:
        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            raise HTTPException(422, "Selecciona un archivo CSV o Excel.")
        filename = file.filename or "archivo"
        content = await file.read(MAX_HISTORICAL_FILE_BYTES + 1)
    if not filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=415, detail="Usa un archivo CSV, XLSX o XLS.")
    if len(content) > MAX_HISTORICAL_FILE_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera el limite de 25 MB.")
    try:
        result = await run_in_threadpool(
            normalize_historical_file, content, filename, relationship_date
        )
    except (ValueError, ImportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(result.rows) > 10_000:
        raise HTTPException(status_code=422, detail="El archivo supera el limite de 10.000 empresas.")
    return result.as_dict()
