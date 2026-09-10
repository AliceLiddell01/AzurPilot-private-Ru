from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from module.application.notifications.agent import (
    MAX_AGENT_ACK_BODY_BYTES,
    DesktopAgentAuthenticator,
    DesktopAgentAuthorizationError,
    DesktopAgentChannel,
    DesktopAgentCredential,
    DesktopAgentNotificationRuntime,
    DesktopAgentRequestError,
    DesktopAgentUnavailableError,
    NotificationCursor,
    NotificationCursorError,
    build_handover_preemption_event,
    notification_agent_document,
    parse_agent_ack_document,
    validate_agent_delivery_document,
)
from module.application.notifications.models import (
    DeliveryResultClass,
    DeliveryState,
    NotificationAgentAck,
    NotificationAgentAckResult,
    NotificationAgentAckStatus,
    NotificationAgentDelivery,
    NotificationSensitivity,
    ReceiptStrength,
)
from module.application.runtime_handover import NotificationOutcome
from module.application.runtime_state import RuntimePhase, RuntimeStateSnapshot
from module.notification_agent.client import (
    DesktopAgentClient,
    DesktopAgentClientConfig,
    DesktopAgentConfigurationError,
    iter_sse_events,
)
from tests.notification_test_support import NOW, _event, _MemoryRepository, _MemoryUow


def _credential() -> DesktopAgentCredential:
    return DesktopAgentCredential(
        agent_id="desktop-agent-1",
        profiles=frozenset({"profile-1"}),
        token="agent-token-0123456789abcdef",
    )


def _principal(runtime: DesktopAgentNotificationRuntime):
    principal = runtime.authenticator.authenticate(
        {"Authorization": "Bearer agent-token-0123456789abcdef"}
    )
    assert principal is not None
    return principal


def _snapshot() -> RuntimeStateSnapshot:
    return RuntimeStateSnapshot(
        profile="profile-1",
        phase=RuntimePhase.USER_PROFILE_BUSY,
        worker_running=True,
        busy=True,
        current_task="DailyTask",
        operation_id="handover-1",
        session_id="session-1",
        handover_requested=False,
        draining=False,
        stop_requested=False,
        terminal_state=None,
        worker_pid=123,
        worker_created_at=456.0,
        updated_at=NOW.isoformat(),
        freshness="fresh",
        provenance="test",
    )


def _runtime(repository: _MemoryRepository | None = None):
    repository = repository or _MemoryRepository()
    runtime = DesktopAgentNotificationRuntime(
        lambda: _MemoryUow(repository),
        credential=_credential(),
        clock=lambda: NOW,
    )
    return runtime, repository


def _stage_delivery(runtime: DesktopAgentNotificationRuntime, event):
    published = runtime._publisher.publish_for_handover(event, NOW + timedelta(seconds=30))
    assert published.outcome.value == "accepted"
    report = runtime._dispatcher.dispatch_once()
    assert report.updated == 1
    principal = _principal(runtime)
    frames = runtime.read_agent_batch(
        principal, profile_id=event.profile_id, cursor=None, limit=1
    )
    assert len(frames) == 1
    return principal, frames[0]


def _ack_from_frame(frame, *, agent_id: str = "desktop-agent-1"):
    return parse_agent_ack_document(
        {
            key: frame.document[key]
            for key in (
                "delivery_id",
                "event_id",
                "event_source",
                "profile_id",
                "attempt_ordinal",
                "lease_token",
                "session_epoch",
                "payload_digest",
            )
        },
        agent_id=agent_id,
    )


def test_credential_and_authenticator_never_repr_token_and_enforce_scope() -> None:
    credential = _credential()
    assert credential.token not in repr(credential)
    authenticator = DesktopAgentAuthenticator(credential)
    principal = authenticator.authenticate(
        {"authorization": f"Bearer {credential.token}"}
    )
    assert principal is not None
    assert principal.agent_id == credential.agent_id
    assert authenticator.authenticate({"Authorization": "Bearer wrong-token"}) is None
    assert authenticator.authenticate({"Authorization": f"Bearer {credential.token} "}) is None


def test_partial_agent_configuration_fails_closed_instead_of_disabling_silently() -> None:
    with pytest.raises(DesktopAgentConfigurationError):
        DesktopAgentNotificationRuntime.from_environment(
            lambda: _MemoryUow(_MemoryRepository()),
            environment={"AZURPILOT_NOTIFICATION_AGENT_ID": "desktop-agent-1"},
        )
    disabled = DesktopAgentNotificationRuntime.from_environment(
        lambda: _MemoryUow(_MemoryRepository()), environment={}
    )
    assert disabled.enabled is False


def test_api_requires_authentication_for_stream_and_ack(monkeypatch) -> None:
    from module.webui import api as webui_api

    runtime, _repository = _runtime()
    monkeypatch.setattr(webui_api, "_notification_agent_runtime", lambda: runtime)

    stream_response = asyncio.run(
        webui_api.api_notification_agent_stream(
            _api_request("GET", "/api/notification-agent/stream")
        )
    )
    ack_response = asyncio.run(
        webui_api.api_notification_agent_ack(
            _api_request("POST", "/api/notification-agent/ack")
        )
    )

    assert stream_response.status_code == 401
    assert ack_response.status_code == 401


def test_api_rejects_malformed_and_oversized_ack_body(monkeypatch) -> None:
    from module.webui import api as webui_api

    runtime, _repository = _runtime()
    monkeypatch.setattr(webui_api, "_notification_agent_runtime", lambda: runtime)
    headers = {"Authorization": f"Bearer {_credential().token}"}

    malformed = asyncio.run(
        webui_api.api_notification_agent_ack(
            _api_request(
                "POST",
                "/api/notification-agent/ack",
                headers=headers,
                body=b"not-json",
            )
        )
    )
    oversized = asyncio.run(
        webui_api.api_notification_agent_ack(
            _api_request(
                "POST",
                "/api/notification-agent/ack",
                headers=headers,
                body=b"x" * (MAX_AGENT_ACK_BODY_BYTES + 1),
            )
        )
    )

    assert malformed.status_code == 400
    assert oversized.status_code == 400


def test_opaque_cursor_rejects_foreign_and_malformed_values() -> None:
    cursor = NotificationCursor("profile-1", 7, uuid4()).encode()
    assert NotificationCursor.decode(cursor, expected_profile_id="profile-1") is not None
    with pytest.raises(NotificationCursorError):
        NotificationCursor.decode(cursor, expected_profile_id="profile-2")
    with pytest.raises(NotificationCursorError):
        NotificationCursor.decode("na1.not-base64!", expected_profile_id="profile-1")


def test_desktop_agent_channel_only_returns_provider_acceptance() -> None:
    result = DesktopAgentChannel().send(object())
    assert result.result_class is DeliveryResultClass.PROVIDER_ACCEPTED
    assert result.result_class is not DeliveryResultClass.DELIVERED
    assert DesktopAgentChannel.capabilities.receipt_strength is ReceiptStrength.AGENT_ACK


def test_durable_agent_projection_is_resumable_and_two_reads_are_independent() -> None:
    runtime, _repository = _runtime()
    event = _event(operation_id="handover-1")
    principal, frame = _stage_delivery(runtime, event)

    second_read = runtime.read_agent_batch(
        principal, profile_id="profile-1", cursor=None, limit=1
    )
    assert second_read[0].document["delivery_id"] == frame.document["delivery_id"]
    resumed = runtime.read_agent_batch(
        principal,
        profile_id="profile-1",
        cursor=frame.cursor.encode(),
        limit=1,
    )
    assert resumed == ()

    result = runtime.acknowledge_agent(principal, _ack_from_frame(frame))
    duplicate = runtime.acknowledge_agent(principal, _ack_from_frame(frame))
    assert result.status.value == "acknowledged"
    assert duplicate.status.value == "duplicate"


def test_agent_projection_rejects_mismatched_current_attempt_lease() -> None:
    runtime, repository = _runtime()
    event = _event(operation_id="projection-lease-1")
    _principal_value, frame = _stage_delivery(runtime, event)
    delivery = repository.deliveries[UUID(str(frame.document["delivery_id"]))]
    attempt = repository.attempts[delivery.id][-1]
    repository.attempts[delivery.id][-1] = replace(attempt, lease_token=uuid4())

    stored_event = repository.events[(event.source, event.id)]
    with pytest.raises(DesktopAgentUnavailableError):
        notification_agent_document(
            NotificationAgentDelivery(
                stored_event, delivery, repository.attempts[delivery.id][-1]
            )
        )


def test_sensitive_projection_does_not_expose_rendered_content() -> None:
    runtime, repository = _runtime()
    event = replace(
        _event(operation_id="sensitive-1"), sensitivity=NotificationSensitivity.SENSITIVE
    )
    _principal_value, frame = _stage_delivery(runtime, event)
    delivery = repository.deliveries[UUID(str(frame.document["delivery_id"]))]
    rendered = delivery.rendered_snapshot
    wire = json.dumps(frame.document, ensure_ascii=False)
    assert frame.document["sensitivity"] == "SENSITIVE"
    assert rendered.title not in wire
    assert rendered.body not in wire
    assert "sensitive-1" not in wire


def test_ack_rejects_wrong_profile_before_persistence_mutation() -> None:
    runtime, repository = _runtime()
    event = _event(operation_id="wrong-profile-1")
    principal, frame = _stage_delivery(runtime, event)
    ack = replace(_ack_from_frame(frame), profile_id="profile-2")
    with pytest.raises(DesktopAgentAuthorizationError):
        runtime.acknowledge_agent(principal, ack)
    delivery = repository.deliveries[UUID(str(frame.document["delivery_id"]))]
    assert delivery.state is DeliveryState.AWAITING_AGENT_ACK


def test_ack_rejects_mismatched_attempt_identity_and_expiry() -> None:
    runtime, repository = _runtime()
    event = _event(operation_id="ack-identity-1")
    principal, frame = _stage_delivery(runtime, event)
    ack = _ack_from_frame(frame)

    for candidate in (
        replace(ack, event_id=uuid4()),
        replace(ack, lease_token=uuid4(), session_epoch=ack.session_epoch),
        replace(ack, session_epoch=uuid4()),
    ):
        result = runtime.acknowledge_agent(principal, candidate)
        assert result.status is NotificationAgentAckStatus.REJECTED

    expired = repository.acknowledge_agent_delivery(
        ack, now=NOW + timedelta(seconds=31)
    )
    assert expired.status is NotificationAgentAckStatus.REJECTED
    assert expired.reason == "ack_expired"
    assert repository.deliveries[ack.delivery_id].state is DeliveryState.AWAITING_AGENT_ACK


def test_backend_restart_preserves_unacknowledged_backlog_and_resumed_cursor() -> None:
    runtime, repository = _runtime()
    event = _event(operation_id="restart-1")
    principal, frame = _stage_delivery(runtime, event)

    restarted, _same_repository = _runtime(repository)
    restarted_principal = _principal(restarted)
    replay = restarted.read_agent_batch(
        restarted_principal, profile_id="profile-1", cursor=None, limit=1
    )
    assert replay[0].document == frame.document

    assert (
        restarted.acknowledge_agent(restarted_principal, _ack_from_frame(frame)).status
        is NotificationAgentAckStatus.ACKNOWLEDGED
    )
    assert restarted.read_agent_batch(
        restarted_principal,
        profile_id="profile-1",
        cursor=frame.cursor.encode(),
        limit=1,
    ) == ()


def test_handover_waiter_returns_delivered_only_after_agent_ack() -> None:
    runtime, repository = _runtime()
    principal = _principal(runtime)

    class AutoAckDispatcher:
        def recover_expired(self):
            return runtime._dispatcher.recover_expired()

        def dispatch_once(self):
            report = runtime._dispatcher.dispatch_once()
            for delivery in tuple(repository.deliveries.values()):
                if delivery.state is not DeliveryState.AWAITING_AGENT_ACK:
                    continue
                event = repository.events[(delivery.event_source, delivery.event_id)].event
                attempt = repository.attempts[delivery.id][-1]
                ack = NotificationAgentAck(
                    delivery_id=delivery.id,
                    event_id=event.id,
                    event_source=event.source,
                    profile_id=event.profile_id,
                    attempt_ordinal=attempt.attempt_ordinal,
                    lease_token=attempt.lease_token,
                    session_epoch=attempt.lease_token,
                    payload_digest=event.payload_digest,
                    agent_id=principal.agent_id,
                )
                repository.acknowledge_agent_delivery(ack, now=NOW)
            return report

    runtime._waiter._dispatcher = AutoAckDispatcher()
    outcome = runtime.notify_preemption(
        "profile-1",
        "handover-1",
        "session-1",
        deadline=NOW + timedelta(seconds=5),
        runtime_state=_snapshot(),
    )
    assert outcome is NotificationOutcome.DELIVERED


def test_handover_waiter_times_out_provider_acceptance_without_ack() -> None:
    runtime, _repository = _runtime()
    ticks = [0.0]
    waiter = runtime._waiter
    waiter._monotonic = lambda: ticks[0]
    waiter._sleep = lambda seconds: ticks.__setitem__(0, ticks[0] + seconds)
    waiter._poll_seconds = 0.5
    outcome = runtime.notify_preemption(
        "profile-1",
        "handover-timeout-1",
        "session-1",
        deadline=NOW + timedelta(seconds=1.0),
        runtime_state=replace(_snapshot(), operation_id="handover-timeout-1"),
    )
    assert outcome is NotificationOutcome.FAILED


def test_event_builder_preserves_runtime_identity() -> None:
    event = build_handover_preemption_event(
        "profile-1",
        "handover-1",
        "session-1",
        deadline=NOW + timedelta(seconds=30),
        runtime_state=_snapshot(),
        clock=lambda: NOW,
    )
    assert event.profile_id == "profile-1"
    assert event.data.operation_id == "handover-1"
    assert event.data.session_id == "session-1"
    assert event.correlation.runtime_session_id == "session-1"


def test_sse_parser_handles_id_event_and_multiline_data() -> None:
    async def chunks():
        yield "id: na1.cursor\nevent: notification\ndata: {\"a\":\n"
        yield "data: 1}\n\n: keepalive\n\n"

    async def collect():
        return [event async for event in iter_sse_events(chunks())]

    events = asyncio.run(collect())
    assert len(events) == 1
    assert events[0].event_id == "na1.cursor"
    assert events[0].data == '{"a":\n1}'


def test_sse_parser_discards_unterminated_frame_on_disconnect() -> None:
    async def chunks():
        yield "event: notification\ndata: {\"partial\":true}\n"

    async def collect():
        return [event async for event in iter_sse_events(chunks())]

    assert asyncio.run(collect()) == []


def test_client_requires_verified_https_and_persists_only_valid_cursor(tmp_path: Path) -> None:
    with pytest.raises(DesktopAgentConfigurationError):
        DesktopAgentClientConfig("http://localhost", _credential(), tmp_path / "cursor.json")
    config = DesktopAgentClientConfig(
        "https://agent.example",
        _credential(),
        tmp_path / "cursor.json",
    )
    client = DesktopAgentClient(config, on_notification=lambda _document: None)
    cursor = NotificationCursor("profile-1", 1, uuid4()).encode()
    client._write_cursor("profile-1", cursor)
    assert client._read_cursor("profile-1") == cursor
    assert "agent-token" not in repr(config)


def test_client_rejects_unknown_notification_fields() -> None:
    runtime, _repository = _runtime()
    _principal_value, frame = _stage_delivery(
        runtime, _event(operation_id="unknown-field-1")
    )
    validate_agent_delivery_document(frame.document)
    with pytest.raises(DesktopAgentRequestError):
        validate_agent_delivery_document(dict(frame.document, unexpected=True))


def _api_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query_string: bytes = b"",
) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "headers": [
            (key.lower().encode("ascii"), value.encode("utf-8"))
            for key, value in (headers or {}).items()
        ],
        "scheme": "https",
        "client": ("test", 443),
        "server": ("test", 443),
        "http_version": "1.1",
    }
    return Request(scope, receive)


def test_api_rejects_foreign_profile_before_opening_sse_stream(monkeypatch) -> None:
    from module.webui import api as webui_api

    runtime, _repository = _runtime()
    monkeypatch.setattr(webui_api, "_notification_agent_runtime", lambda: runtime)
    request = _api_request(
        "GET",
        "/api/notification-agent/stream",
        headers={"Authorization": f"Bearer {_credential().token}"},
        query_string=b"profile=profile-2",
    )

    response = asyncio.run(webui_api.api_notification_agent_stream(request))

    assert response.status_code == 403
    assert response.__class__.__name__ == "JSONResponse"


def test_api_ack_is_authenticated_and_profile_scoped(monkeypatch) -> None:
    from module.webui import api as webui_api

    runtime, _repository = _runtime()
    acknowledged: list[NotificationAgentAck] = []

    def acknowledge(principal, ack):
        acknowledged.append(ack)
        assert principal.agent_id == "desktop-agent-1"
        return NotificationAgentAckResult(NotificationAgentAckStatus.ACKNOWLEDGED)

    runtime.acknowledge_agent = acknowledge
    monkeypatch.setattr(webui_api, "_notification_agent_runtime", lambda: runtime)
    ack_document = {
        "delivery_id": str(uuid4()),
        "event_id": str(uuid4()),
        "event_source": "runtime",
        "profile_id": "profile-1",
        "attempt_ordinal": 1,
        "lease_token": str(uuid4()),
        "session_epoch": "",
        "payload_digest": "a" * 64,
    }
    ack_document["session_epoch"] = ack_document["lease_token"]
    request = _api_request(
        "POST",
        "/api/notification-agent/ack",
        headers={
            "Authorization": f"Bearer {_credential().token}",
            "Content-Type": "application/json",
        },
        body=json.dumps(ack_document).encode("utf-8"),
    )

    response = asyncio.run(webui_api.api_notification_agent_ack(request))

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "acknowledged"}
    assert len(acknowledged) == 1

    foreign_document = dict(ack_document, profile_id="profile-2")
    foreign_request = _api_request(
        "POST",
        "/api/notification-agent/ack",
        headers={"Authorization": f"Bearer {_credential().token}"},
        body=json.dumps(foreign_document).encode("utf-8"),
    )
    foreign_response = asyncio.run(webui_api.api_notification_agent_ack(foreign_request))

    assert foreign_response.status_code == 403
    assert len(acknowledged) == 1


__all__ = []
