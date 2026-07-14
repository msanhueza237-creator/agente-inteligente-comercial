from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/search")
async def deprecated_search_form() -> None:
    raise HTTPException(
        status_code=410,
        detail="La búsqueda manual fue retirada. Las campañas se crean exclusivamente en el CRM.",
    )


@router.post("/search")
async def disabled_manual_search() -> None:
    raise HTTPException(
        status_code=410,
        detail="La creación de búsquedas pertenece al CRM. Este agente es sólo un monitor técnico.",
    )
