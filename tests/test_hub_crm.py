from unittest.mock import AsyncMock

from app.hub.crm import (
    HubCRMPort,
    INTEGRATION_RECORD_BATCH_SIZE,
    INTEGRATION_RECORD_MAX_PAYLOAD_BYTES,
)


async def test_integration_records_use_payload_safe_batches() -> None:
    port = HubCRMPort(base_url="https://crm.test", api_key="test")
    port._request = AsyncMock()  # type: ignore[method-assign]
    records = [{"external_id": str(index)} for index in range(61)]

    await port.upsert_integration_records(
        provider="facto",
        resource="documents",
        records=records,
    )

    requests = port._request.await_args_list
    assert len(requests) == 3
    assert [len(request.kwargs["payload"]["records"]) for request in requests] == [
        INTEGRATION_RECORD_BATCH_SIZE,
        INTEGRATION_RECORD_BATCH_SIZE,
        11,
    ]
    assert all(
        request.args == ("POST", "hub/integrations/records/batch")
        for request in requests
    )


async def test_integration_records_split_large_documents_by_encoded_size() -> None:
    port = HubCRMPort(base_url="https://crm.test", api_key="test")
    port._request = AsyncMock()  # type: ignore[method-assign]
    large_value = "x" * (INTEGRATION_RECORD_MAX_PAYLOAD_BYTES // 2 + 1)

    await port.upsert_integration_records(
        provider="facto",
        resource="document_details",
        records=[
            {"external_id": "one", "payload": large_value},
            {"external_id": "two", "payload": large_value},
        ],
    )

    requests = port._request.await_args_list
    assert len(requests) == 2
    assert [
        request.kwargs["payload"]["records"][0]["external_id"]
        for request in requests
    ] == ["one", "two"]
