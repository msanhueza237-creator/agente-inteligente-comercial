from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

CATEGORY_LABELS: dict[str, str] = {
    "distributor": "Distribuidor",
    "retailer": "Minorista",
    "installer_large": "Instalador grande",
    "installer_independent": "Instalador independiente",
    "maintenance": "Mantención",
    "refrigeration": "Refrigeración",
    "competitor": "Competidor",
    "other": "Otro",
}

LEVEL_LABELS: dict[str, str] = {
    "low": "Bajo",
    "medium": "Medio",
    "high": "Alto",
    "very_high": "Muy alto",
}

STATUS_LABELS: dict[str, str] = {
    "pending": "Pendiente",
    "running": "En proceso",
    "partial": "Parcial",
    "completed": "Completada",
    "failed": "Fallida",
    "cancel_requested": "Cancelación solicitada",
    "cancelled": "Cancelada",
}

templates.env.globals["category_labels"] = CATEGORY_LABELS
templates.env.globals["level_labels"] = LEVEL_LABELS
templates.env.globals["status_labels"] = STATUS_LABELS
