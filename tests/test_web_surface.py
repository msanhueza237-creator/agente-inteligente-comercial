import pytest
from fastapi import HTTPException

from app.web.routes_search import deprecated_search_form


@pytest.mark.asyncio
async def test_legacy_manual_search_is_gone() -> None:
    with pytest.raises(HTTPException) as error:
        await deprecated_search_form()
    assert error.value.status_code == 410
    assert "CRM" in error.value.detail
