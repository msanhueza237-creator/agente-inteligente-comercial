from fastapi import APIRouter

from app.config import get_settings
from app.hub.agents import AgentRegistry
from app.integrations.facto import FactoClient
from app.integrations.tiendanube import TiendanubeClient

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/integrations")
async def integration_health() -> dict:
    settings = get_settings()
    facto, tiendanube = await FactoClient(settings).health(), await TiendanubeClient(settings).health()
    return {
        "status": "ok" if facto.connected or tiendanube.connected else "pending_configuration",
        "integrations": {
            "facto": {
                "configured": facto.configured,
                "connected": facto.connected,
                "read_only": True,
                "message": facto.message,
            },
            "tiendanube": {
                "configured": tiendanube.configured,
                "connected": tiendanube.connected,
                "read_only": True,
                "message": tiendanube.message,
            },
        },
        "agents": AgentRegistry().names(),
    }
