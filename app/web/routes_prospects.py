from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import DedupStatus, Prospect, ProspectCategory, ProspectStatus
from app.web.deps import templates
from app.web.regions import load_region_comuna_map

router = APIRouter()


@router.get("/")
async def home(request: Request, db: AsyncSession = Depends(get_db)):
    total = (await db.execute(select(func.count(Prospect.id)))).scalar_one()
    needs_review = (
        await db.execute(
            select(func.count(Prospect.id)).where(Prospect.dedup_status == DedupStatus.needs_review)
        )
    ).scalar_one()
    new_count = (
        await db.execute(select(func.count(Prospect.id)).where(Prospect.status == ProspectStatus.new))
    ).scalar_one()
    approved = (
        await db.execute(
            select(func.count(Prospect.id)).where(Prospect.status == ProspectStatus.approved)
        )
    ).scalar_one()

    by_category_rows = (
        await db.execute(
            select(Prospect.category, func.count(Prospect.id)).group_by(Prospect.category)
        )
    ).all()
    by_category = [(cat.value if cat else None, count) for cat, count in by_category_rows]

    stats = {
        "total": total,
        "needs_review": needs_review,
        "new": new_count,
        "approved": approved,
        "by_category": by_category,
    }
    return templates.TemplateResponse(request, "home.html", {"stats": stats})


@router.get("/prospects")
async def list_prospects(
    request: Request,
    region: str | None = None,
    comuna: str | None = None,
    category: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Prospect).order_by(Prospect.created_at.desc()).limit(200)
    if region:
        query = query.where(Prospect.region == region)
    if comuna:
        query = query.where(Prospect.comuna == comuna)
    if category:
        query = query.where(Prospect.category == category)
    if status:
        query = query.where(Prospect.status == status)

    prospects = (await db.execute(query)).scalars().all()
    total = len(prospects)

    region_comuna_map = await load_region_comuna_map(db)

    return templates.TemplateResponse(
        request,
        "prospects_list.html",
        {
            "prospects": prospects,
            "total": total,
            "filters": {"region": region, "comuna": comuna, "category": category, "status": status},
            "filter_options": {
                "regions": list(region_comuna_map.keys()),
                "categories": [c.value for c in ProspectCategory],
                "statuses": [s.value for s in ProspectStatus],
            },
            "region_comuna_map": region_comuna_map,
        },
    )
