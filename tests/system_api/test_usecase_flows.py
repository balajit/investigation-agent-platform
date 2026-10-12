"""Stateful use-case flows (design §8): investigation + chat by default.

``batch-intake`` is opt-in via ``TEST_API_FLOWS``. All mutation uses the
scratch tenant; every creation call carries a fresh ``X-Idempotency-Key``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from .conftest import ApiConfig, needs_live

pytestmark = needs_live

_FLOWS = frozenset(
    f.strip().lower() for f in os.environ.get("TEST_API_FLOWS", "investigation,chat").split(",")
)
needs_batch_intake = pytest.mark.skipif(
    "batch-intake" not in _FLOWS, reason="batch-intake flow not enabled"
)


def _post(client: Any, url: str, expected: set[int], **kwargs: Any) -> Any:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Idempotency-Key", uuid.uuid4().hex)
    response = client.post(url, headers=headers, **kwargs)
    assert response.status_code in expected, (
        f"POST {url} → {response.status_code}: {response.text[:500]}"
    )
    return response


def test_investigation_lifecycle(http_client: Any, api_config: ApiConfig) -> None:
    create = _post(
        http_client,
        "/api/v1/investigations",
        {201},
        json={
            "application_id": api_config.application_id,
            "problem_description": "system-api investigation flow",
        },
    ).json()
    investigation_id = create["id"]

    read = http_client.get(f"/api/v1/investigations/{investigation_id}")
    assert read.status_code == 200
    assert read.json()["investigation"]["id"] == investigation_id

    cancel = _post(http_client, f"/api/v1/investigations/{investigation_id}/cancel", {202})
    assert cancel.json()["status"] == "CANCELLING"

    verify = http_client.get(f"/api/v1/investigations/{investigation_id}")
    assert verify.status_code == 200
    assert verify.json()["investigation"]["status"] == "CANCELLED"


def test_chat_session_lifecycle(http_client: Any) -> None:
    create = _post(http_client, "/api/v1/chat/sessions", {201}, json={}).json()
    session_id = create["session"]["id"]

    read = http_client.get(f"/api/v1/chat/sessions/{session_id}/messages")
    assert read.status_code == 200

    delete = http_client.delete(f"/api/v1/chat/sessions/{session_id}")
    assert delete.status_code in (200, 409), f"DELETE → {delete.status_code}: {delete.text[:300]}"
    if delete.status_code == 409:
        pytest.skip("chat session under legal hold; delete refused")

    verify = http_client.get(f"/api/v1/chat/sessions/{session_id}/messages")
    assert verify.status_code == 404


@needs_batch_intake
def test_batch_intake_lifecycle(http_client: Any, api_config: ApiConfig) -> None:
    create = _post(
        http_client,
        "/api/v1/batch-intake",
        {202},
        json={
            "records": [
                {
                    "external_key": "system-api-flow-1",
                    "application_id": api_config.application_id,
                    "problem_description": "system-api batch flow",
                }
            ]
        },
    ).json()
    job_id = create["job_id"]

    read = http_client.get(f"/api/v1/batch-intake/{job_id}")
    assert read.status_code == 200

    cancel = _post(http_client, f"/api/v1/batch-intake/{job_id}/cancel", {202})
    assert cancel.json()["status"] == "CANCELLING"

    purge = http_client.delete(f"/api/v1/batch-intake/{job_id}")
    assert purge.status_code in (200, 409), f"DELETE → {purge.status_code}: {purge.text[:300]}"
    if purge.status_code == 200:
        assert purge.json()["status"] == "PURGED"
        verify = http_client.get(f"/api/v1/batch-intake/{job_id}")
        assert verify.status_code == 404
    # 409: batch still active or within retention — the row legitimately remains.
