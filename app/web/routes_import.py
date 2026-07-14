from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import ResearchJob
from app.ingestion.pipeline import ingest_excel
from app.ingestion.historical import normalize_historical_file
from app.web.deps import templates

router = APIRouter()
MAX_HISTORICAL_FILE_BYTES = 25 * 1024 * 1024


@router.get("/import")
async def import_form(request: Request):
    return templates.TemplateResponse(request, "import.html", {"result": None})


@router.post("/import")
async def import_file(
    request: Request, file: UploadFile, db: AsyncSession = Depends(get_db)
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
async def preview_historical_import(file: UploadFile, relationship_date: date | None = None):
    """Normalize an old customer workbook without writing contacts or companies."""
    filename = file.filename or "archivo"
    if not filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=415, detail="Usa un archivo CSV, XLSX o XLS.")
    content = await file.read(MAX_HISTORICAL_FILE_BYTES + 1)
    if len(content) > MAX_HISTORICAL_FILE_BYTES:
        raise HTTPException(status_code=413, detail="El archivo supera el limite de 25 MB.")
    try:
        result = normalize_historical_file(content, filename, relationship_date)
    except (ValueError, ImportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(result.rows) > 10_000:
        raise HTTPException(status_code=422, detail="El archivo supera el limite de 10.000 empresas.")
    return result.as_dict()
