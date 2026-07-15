from __future__ import annotations

import hashlib
import json
import uuid

import httpx

from app.crm.port import (
    CRMIdempotencyConflict,
    CRMLeaseLostError,
    CRMPermanentError,
    CRMPortError,
    CRMRetryableError,
    HeartbeatResult,
)
from app.prospecting.contracts import (
    CandidateBatchAck,
    ClaimedRun,
    CompletionReport,
    ProspectCandidate,
    RunEvent,
)


CRMTransportError = CRMPortError


def business_payload_hash(payload: dict) -> str:
    """Hash the immutable business body exactly as the CRM idempotency layer does.

    Lease ownership changes between retries and is deliberately excluded;
    reusing a key with different candidates/events/report data still conflicts.
    """

    business_payload = {
        key: value for key, value in payload.items() if key not in {"worker_id", "lease_token"}
    }
    encoded = json.dumps(
        business_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HttpCRMPort:
    """Inactive-by-default adapter for the CRM Edge Function contract.

    Credentials are constructor-only: this class never reads settings and is
    not selected by the worker entrypoint until the final integration milestone.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://") and transport is None:
            raise ValueError("CRM base_url must use HTTPS")
        if not api_key.strip():
            raise ValueError("CRM API key is required")
        normalized_base = base_url.rstrip("/")
        if not normalized_base.endswith("/crm-agent"):
            normalized_base = f"{normalized_base}/crm-agent"
        self.base_url = f"{normalized_base}/"
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport
        self._worker_id: str | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            trust_env=False,
            transport=self.transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Climactiva-Api-Key": self.api_key,
            },
        )

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    @staticmethod
    def _ensure_success(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        # Never include the body: upstream errors may echo contacts or secrets.
        message = f"CRM returned status {status}"
        if status in {401, 403, 404, 410, 423}:
            raise CRMLeaseLostError(message)
        if status == 409:
            raise CRMIdempotencyConflict(message)
        if status in {425, 429} or status >= 500:
            raise CRMRetryableError(message)
        raise CRMPermanentError(message)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with self._client() as client:
                return await client.request(method, path.lstrip("/"), **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CRMRetryableError(f"CRM transport failed: {type(exc).__name__}") from exc

    async def claim_run(self, worker_id: str, lease_seconds: int = 120) -> ClaimedRun | None:
        self._worker_id = worker_id
        response = await self._request(
            "POST",
            "prospecting-runs/claim",
            json={"worker_id": worker_id, "lease_seconds": lease_seconds},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if response.status_code == 204:
            return None
        self._ensure_success(response)
        payload = self._unwrap(response.json())
        if payload.get("run") is None and "run" in payload:
            return None
        return ClaimedRun.model_validate(payload)

    async def claim_integration_check(self, worker_id: str) -> dict | None:
        response = await self._request(
            "POST",
            "prospecting-integrations/checks/claim",
            json={"worker_id": worker_id},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self._ensure_success(response)
        payload = self._unwrap(response.json())
        check = payload.get("check")
        return check if isinstance(check, dict) else None

    async def report_integration_status(
        self,
        *,
        worker_id: str,
        check_id: str,
        provider: str,
        configured: bool,
        status: str,
        message: str,
        error_code: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        await self._write(
            "/prospecting-integrations/status",
            {
                "worker_id": worker_id,
                "check_id": check_id,
                "provider": provider,
                "configured": configured,
                "status": status,
                "message": message,
                "error_code": error_code,
                "metadata": metadata or {},
            },
            str(uuid.uuid4()),
        )

    async def heartbeat(
        self, run_id: str, lease_token: str, lease_seconds: int = 120
    ) -> HeartbeatResult:
        response = await self._request(
            "POST",
            f"prospecting-runs/{run_id}/heartbeat",
            json={
                "worker_id": self._require_worker_id(),
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if response.status_code in {401, 403, 404, 410, 423}:
            return HeartbeatResult(lease_valid=False)
        self._ensure_success(response)
        if not response.content:
            return HeartbeatResult(lease_valid=True)
        raw_payload = response.json()
        payload = self._unwrap(raw_payload) if isinstance(raw_payload, dict) else {}
        run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
        lease_valid = bool(payload.get("lease_valid", payload.get("ok", True)))
        cancel_requested = bool(
            payload.get("cancel_requested")
            or run.get("cancel_requested")
            or run.get("status") in {"cancel_requested", "cancelled"}
        )
        return HeartbeatResult(
            lease_valid=lease_valid,
            cancel_requested=cancel_requested,
        )

    async def is_cancel_requested(self, run_id: str, lease_token: str) -> bool:
        # Authentication is via the API header. Never put a lease token in a
        # URL where reverse proxies commonly retain it in access logs.
        del lease_token
        response = await self._request("GET", f"prospecting-runs/{run_id}")
        if response.status_code in {401, 403, 404, 410, 423}:
            return True
        self._ensure_success(response)
        payload = self._unwrap(response.json())
        run = payload.get("run") if isinstance(payload.get("run"), dict) else payload
        return run.get("status") in {"cancel_requested", "cancelled"}

    async def append_events(
        self,
        run_id: str,
        lease_token: str,
        events: list[RunEvent],
        idempotency_key: str,
    ) -> None:
        if not 1 <= len(events) <= 100:
            raise ValueError("event batches must contain 1..100 items")
        await self._write(
            f"/prospecting-runs/{run_id}/events/batch",
            {
                "worker_id": self._require_worker_id(),
                "lease_token": lease_token,
                "events": [e.model_dump(mode="json") for e in events],
            },
            idempotency_key,
        )

    async def upsert_candidates(
        self,
        run_id: str,
        lease_token: str,
        candidates: list[ProspectCandidate],
        idempotency_key: str,
    ) -> CandidateBatchAck:
        if not 1 <= len(candidates) <= 100:
            raise ValueError("candidate batches must contain 1..100 items")
        response = await self._write(
            f"/prospecting-runs/{run_id}/candidates/batch",
            {
                "worker_id": self._require_worker_id(),
                "lease_token": lease_token,
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            },
            idempotency_key,
        )
        if not response.content:
            return CandidateBatchAck()
        payload = self._unwrap(response.json())
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        return CandidateBatchAck.model_validate(result)

    async def complete_run(
        self,
        run_id: str,
        lease_token: str,
        report: CompletionReport,
        idempotency_key: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        await self._write(
            f"/prospecting-runs/{run_id}/complete",
            {
                "worker_id": worker_id or self._require_worker_id(),
                "lease_token": lease_token,
                **report.model_dump(mode="json"),
            },
            idempotency_key,
        )

    async def fail_run(
        self,
        run_id: str,
        lease_token: str,
        error: str,
        idempotency_key: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        await self._write(
            f"/prospecting-runs/{run_id}/fail",
            {
                "worker_id": worker_id or self._require_worker_id(),
                "lease_token": lease_token,
                "error": error,
            },
            idempotency_key,
        )

    async def _write(self, path: str, payload: dict, idempotency_key: str) -> httpx.Response:
        response = await self._request(
            "POST",
            path,
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        self._ensure_success(response)
        return response

    def _require_worker_id(self) -> str:
        if self._worker_id is None:
            raise CRMTransportError("claim_run must be called before run operations")
        return self._worker_id
