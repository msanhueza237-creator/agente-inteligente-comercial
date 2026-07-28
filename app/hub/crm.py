from __future__ import annotations

import hashlib
import json
import uuid

import httpx

from app.hub.contracts import AgentResult, ClaimedHubTask


class HubCRMError(RuntimeError):
    pass


class HubCRMPort:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 15) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout = timeout

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                trust_env=False,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Climactiva-Api-Key": self.api_key,
                },
            ) as client:
                response = await client.request(
                    method,
                    path.lstrip("/"),
                    json=payload,
                    headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HubCRMError(f"CRM transport failed ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise HubCRMError(f"CRM returned status {response.status_code}")
        return response

    @staticmethod
    def _data(response: httpx.Response) -> dict:
        payload = response.json() if response.content else {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload if isinstance(payload, dict) else {}

    async def claim(
        self, worker_id: str, *, lease_seconds: int
    ) -> ClaimedHubTask | None:
        response = await self._request(
            "POST",
            "hub/tasks/claim",
            payload={"worker_id": worker_id, "lease_seconds": lease_seconds},
        )
        data = self._data(response)
        task = data.get("task")
        if not isinstance(task, dict):
            return None
        return ClaimedHubTask.model_validate(
            {
                **task,
                "lease_token": data["lease_token"],
                "lease_expires_at": data["lease_expires_at"],
            }
        )

    async def heartbeat(
        self,
        task: ClaimedHubTask,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> None:
        await self._request(
            "POST",
            f"hub/tasks/{task.id}/heartbeat",
            payload={
                "worker_id": worker_id,
                "lease_token": task.lease_token,
                "lease_seconds": lease_seconds,
            },
        )

    async def complete(
        self, task: ClaimedHubTask, worker_id: str, result: AgentResult
    ) -> None:
        result_payload = result.model_dump(mode="json")
        await self._request(
            "POST",
            f"hub/tasks/{task.id}/complete",
            payload={
                "worker_id": worker_id,
                "lease_token": task.lease_token,
                "result": result_payload,
            },
            idempotency_key=self._stable_key(task.id, "complete", result_payload),
        )

    async def fail(self, task: ClaimedHubTask, worker_id: str, error_code: str) -> None:
        await self._request(
            "POST",
            f"hub/tasks/{task.id}/fail",
            payload={
                "worker_id": worker_id,
                "lease_token": task.lease_token,
                "error": error_code[:200],
            },
            idempotency_key=self._stable_key(task.id, "fail", {"error": error_code[:200]}),
        )

    async def report_integration(
        self,
        *,
        provider: str,
        enabled: bool,
        read_only: bool,
        status: str,
        message: str,
    ) -> None:
        payload = {
            "provider": provider,
            "enabled": enabled,
            "read_only": read_only,
            "status": status,
            "message": message[:500],
        }
        await self._request(
            "POST",
            "hub/integrations/status",
            payload=payload,
            idempotency_key=self._stable_key(provider, "status", payload),
        )

    async def upsert_integration_records(
        self,
        *,
        provider: str,
        resource: str,
        records: list[dict],
    ) -> None:
        if not records:
            return
        payload = {
            "provider": provider,
            "resource": resource,
            "records": records[:100],
        }
        await self._request(
            "POST",
            "hub/integrations/records/batch",
            payload=payload,
            idempotency_key=self._stable_key(provider, resource, payload),
        )

    @staticmethod
    def _stable_key(resource_id: str, action: str, payload: dict) -> str:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"climactiva:{resource_id}:{action}:{digest}"))
