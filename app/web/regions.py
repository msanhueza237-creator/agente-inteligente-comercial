from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RegionComuna


async def load_region_comuna_map(db: AsyncSession) -> dict[str, list[str]]:
    """Region -> sorted list of comunas, from the regions_comunas reference
    table (seeded by scripts/seed_regions_comunas.py). Used to drive the
    region -> comuna cascading dropdowns in /prospects and /search.
    """
    rows = (await db.execute(select(RegionComuna.region, RegionComuna.comuna))).all()
    mapping: dict[str, list[str]] = {}
    for region, comuna in rows:
        mapping.setdefault(region, []).append(comuna)
    for comunas in mapping.values():
        comunas.sort()
    return dict(sorted(mapping.items()))
