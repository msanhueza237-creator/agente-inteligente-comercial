"""Throwaway spike to reverse-engineer the Latin Chile CRM's API.

There is no formal API documentation for this CRM. The plan (see
docs/crm_api_notes.md) is:
  1. Log in to the CRM's web panel with browser DevTools open (Network tab).
  2. Exercise search / create / edit flows and capture the requests the
     CRM's own frontend makes: base URL, auth mechanism (cookie vs bearer
     token vs API key), endpoint paths, request/response shapes.
  3. Check for an unlinked /api/docs or Swagger/OpenAPI endpoint.
  4. Fill in CRM_BASE_URL / CRM_USERNAME / CRM_PASSWORD / CRM_API_KEY in
     .env, then use this script to validate one auth + search + create
     round-trip before writing app/crm/latin_chile.py for real.

Nothing in here is wired into the app -- it is a standalone script, not
imported anywhere else.

Usage: python -m scripts.crm_discovery_spike
"""

import httpx

from app.config import get_settings


def main() -> None:
    settings = get_settings()

    if not settings.crm_base_url:
        print(
            "CRM_BASE_URL no esta configurado en .env todavia.\n"
            "Ver docs/crm_api_notes.md para el proceso de descubrimiento."
        )
        return

    # --- Step 1: try a plain unauthenticated request to see what we get
    # back (redirect to login? JSON 401? HTML?). Adjust once real
    # endpoints are known from DevTools inspection.
    with httpx.Client(base_url=settings.crm_base_url, timeout=15) as client:
        resp = client.get("/")
        print(f"GET / -> {resp.status_code}, content-type={resp.headers.get('content-type')}")

        # TODO once endpoints are known from DevTools:
        # - auth: POST /api/login or similar with crm_username/crm_password,
        #   capture cookie/token
        # - search: GET/POST a search endpoint by RUT or name
        # - create: POST a prospect and confirm it appears in the CRM UI


if __name__ == "__main__":
    main()
