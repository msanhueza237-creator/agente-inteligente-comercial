"""Amarillas connector placeholder.

The former HTML scraper is intentionally removed. This source remains
non-executable until Clima Activa has written authorization and an official
API/feed implementation replaces this module.
"""

from __future__ import annotations


class AmarillasDisabledError(RuntimeError):
    """Raised before any network request can be made to Amarillas."""


def _disabled() -> AmarillasDisabledError:
    return AmarillasDisabledError(
        "Amarillas is disabled until an authorized official API or feed is implemented"
    )


async def search(
    keyword: str, *, comuna: str | None = None, max_results: int = 15
) -> list[dict]:
    del keyword, comuna, max_results
    raise _disabled()


async def get_detail(detail_url: str) -> dict | None:
    del detail_url
    raise _disabled()
