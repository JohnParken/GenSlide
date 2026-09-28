import pytest

from frontend.assistant_client import AssistantClient


class Transport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"action_id": "action", "status": "active"}


def test_retry_preserves_action_key_and_excludes_service_identity():
    transport = Transport()
    client = AssistantClient(transport)
    for _ in range(2):
        client.turn("session", "直接写项目总结", idempotency_key="stable",
                    expected_session_version=0, expected_lifecycle_version=1,
                    current_file_ids=["attachment"])
    assert transport.calls[0] == transport.calls[1]
    method, path, kwargs = transport.calls[0]
    assert method == "POST" and path == "/api/v1/sessions/session/actions"
    assert kwargs["headers"] == {"Idempotency-Key": "stable"}
    assert not {"user_id", "tenant_id", "authorization", "snapshot", "operation"} & kwargs["json"].keys()


def test_invalid_turn_is_not_sent():
    transport = Transport()
    with pytest.raises(ValueError):
        AssistantClient(transport).turn("session", "", idempotency_key="stable",
                                       expected_session_version=0, expected_lifecycle_version=1)
    assert not transport.calls
