from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
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
    HandoverNotificationOutcome,
    HandoverNotificationResult,
    NotificationAgentAck,
    NotificationAgentAckResult,
    NotificationAgentAckStatus,
    NotificationAgentDelivery,
    NotificationSensitivity,
    PublishResult,
    PublishStatus,
    ReceiptStrength,
)
from module.application.runtime_handover import NotificationOutcome
from module.application.runtime_state import RuntimePhase, RuntimeStateSnapshot
from module.notification_agent.client import (
    DesktopAgentClient,
    DesktopAgentClientConfig,
    DesktopAgentClientRuntime,
    DesktopAgentConfigurationError,
    DesktopAgentProtocolError,
    DesktopAgentRecoverableAckError,
    iter_sse_events,
    present_desktop_agent_notification,
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
    negative_length = asyncio.run(
        webui_api.api_notification_agent_ack(
            _api_request(
                "POST",
                "/api/notification-agent/ack",
                headers={**headers, "Content-Length": "-1"},
                body=b"{}",
            )
        )
    )

    assert malformed.status_code == 400
    assert oversized.status_code == 400
    assert negative_length.status_code == 400


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
    assert resumed[0].document == frame.document

    result = runtime.acknowledge_agent(principal, _ack_from_frame(frame))
    duplicate = runtime.acknowledge_agent(principal, _ack_from_frame(frame))
    assert result.status.value == "acknowledged"
    assert duplicate.status.value == "duplicate"


def test_agent_history_stops_at_the_first_unresolved_durable_frontier() -> None:
    runtime, repository = _runtime()
    first_event = _event(operation_id="frontier-1")
    second_event = _event(operation_id="frontier-2")
    for event in (first_event, second_event):
        result = runtime._publisher.publish_for_handover(
            event, NOW + timedelta(seconds=30)
        )
        assert result.outcome.value == "accepted"
    assert runtime._dispatcher.dispatch_once().updated == 2

    first_delivery = next(
        delivery
        for delivery in repository.deliveries.values()
        if delivery.event_id == first_event.id
    )
    repository.deliveries[first_delivery.id] = replace(
        first_delivery,
        state=DeliveryState.RETRY_WAIT,
        lease_owner=None,
        lease_token=None,
        lease_until=None,
    )
    principal = _principal(runtime)
    assert runtime.read_agent_batch(
        principal, profile_id="profile-1", cursor=None, limit=1
    ) == ()

    recovered_token = uuid4()
    current = repository.deliveries[first_delivery.id]
    repository.deliveries[first_delivery.id] = replace(
        current,
        state=DeliveryState.AWAITING_AGENT_ACK,
        lease_token=recovered_token,
        lease_until=NOW + timedelta(seconds=30),
    )
    repository.attempts[first_delivery.id][-1] = replace(
        repository.attempts[first_delivery.id][-1],
        lease_token=recovered_token,
        result_class=DeliveryResultClass.PROVIDER_ACCEPTED,
    )
    first_frame = runtime.read_agent_batch(
        principal, profile_id="profile-1", cursor=None, limit=1
    )[0]
    assert first_frame.document["event_id"] == str(first_event.id)
    assert runtime.acknowledge_agent(
        principal, _ack_from_frame(first_frame)
    ).status is NotificationAgentAckStatus.ACKNOWLEDGED

    second_frame = runtime.read_agent_batch(
        principal,
        profile_id="profile-1",
        cursor=first_frame.cursor.encode(),
        limit=1,
    )[0]
    assert second_frame.document["event_id"] == str(second_event.id)


def test_agent_cursor_is_validated_against_durable_history() -> None:
    runtime, _repository = _runtime()
    principal, frame = _stage_delivery(runtime, _event(operation_id="cursor-valid"))

    forged_future = NotificationCursor(
        "profile-1", frame.cursor.profile_sequence + 10_000, uuid4()
    ).encode()
    mismatched_pair = NotificationCursor(
        "profile-1", frame.cursor.profile_sequence, uuid4()
    ).encode()
    for cursor in (forged_future, mismatched_pair):
        with pytest.raises(NotificationCursorError):
            runtime.read_agent_batch(
                principal, profile_id="profile-1", cursor=cursor, limit=1
            )

    assert runtime.acknowledge_agent(
        principal, _ack_from_frame(frame)
    ).status is NotificationAgentAckStatus.ACKNOWLEDGED
    assert runtime.read_agent_batch(
        principal,
        profile_id="profile-1",
        cursor=frame.cursor.encode(),
        limit=1,
    ) == ()


def test_agent_session_is_server_issued_and_invalidates_old_connection() -> None:
    runtime, repository = _runtime()
    principal, frame = _stage_delivery(runtime, _event(operation_id="session-identity"))
    old_session = UUID(str(frame.document["session_epoch"]))
    assert old_session != UUID(str(frame.document["lease_token"]))
    new_session = runtime.open_agent_session(principal, profile_id="profile-1")
    assert new_session != old_session
    assert not runtime.is_agent_session_current(
        principal, profile_id="profile-1", session_epoch=old_session
    )

    stale = runtime.acknowledge_agent(principal, _ack_from_frame(frame))
    assert stale.status is NotificationAgentAckStatus.REJECTED
    assert stale.reason == "session_identity_mismatch"
    assert repository.deliveries[UUID(str(frame.document["delivery_id"]))].state is DeliveryState.AWAITING_AGENT_ACK

    current_frame = runtime.read_agent_batch(
        principal,
        profile_id="profile-1",
        cursor=None,
        limit=1,
        session_epoch=new_session,
    )[0]
    assert current_frame.document["session_epoch"] == str(new_session)
    assert runtime.acknowledge_agent(
        principal, _ack_from_frame(current_frame)
    ).status is NotificationAgentAckStatus.ACKNOWLEDGED


def test_agent_delivery_state_and_ack_are_object_scoped() -> None:
    runtime, repository = _runtime()
    principal, frame = _stage_delivery(runtime, _event(operation_id="object-scope"))

    forged_frame = replace(
        frame,
        document={**frame.document, "event_id": str(uuid4())},
    )
    assert runtime.delivery_state(principal, forged_frame) is None

    forged_ack = replace(_ack_from_frame(frame), event_id=uuid4())
    result = runtime.acknowledge_agent(principal, forged_ack)
    assert result.status is NotificationAgentAckStatus.REJECTED
    assert result.reason == "delivery_not_found"
    delivery = repository.deliveries[UUID(str(frame.document["delivery_id"]))]
    assert delivery.state is DeliveryState.AWAITING_AGENT_ACK


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
            ),
            session_epoch=uuid4(),
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

    stale_lease = uuid4()
    for candidate in (
        replace(ack, event_id=uuid4()),
        replace(ack, lease_token=stale_lease, session_epoch=stale_lease),
    ):
        result = runtime.acknowledge_agent(principal, candidate)
        assert result.status is NotificationAgentAckStatus.REJECTED

    stale_session_result = runtime.acknowledge_agent(
        principal, replace(ack, session_epoch=uuid4())
    )
    assert stale_session_result.status is NotificationAgentAckStatus.REJECTED
    assert stale_session_result.reason == "session_identity_mismatch"

    expired = repository.acknowledge_agent_delivery(
        ack,
        now=NOW + timedelta(seconds=31),
        channel_instance_id="desktop-agent",
    )
    assert expired.status is NotificationAgentAckStatus.REJECTED
    assert expired.reason == "ack_expired"
    assert repository.deliveries[ack.delivery_id].state is DeliveryState.AWAITING_AGENT_ACK


def test_backend_restart_preserves_unacknowledged_backlog_and_resumed_cursor() -> None:
    runtime, repository = _runtime()
    event = _event(operation_id="restart-1")
    _principal_value, frame = _stage_delivery(runtime, event)

    restarted, _same_repository = _runtime(repository)
    restarted_principal = _principal(restarted)
    replay = restarted.read_agent_batch(
        restarted_principal, profile_id="profile-1", cursor=None, limit=1
    )
    assert replay[0].document == {
        key: value
        for key, value in frame.document.items()
        if key != "session_epoch"
    } | {"session_epoch": replay[0].document["session_epoch"]}
    assert replay[0].document["session_epoch"] != frame.document["session_epoch"]

    assert (
        restarted.acknowledge_agent(restarted_principal, _ack_from_frame(replay[0])).status
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
                stored_event = repository.events[(delivery.event_source, delivery.event_id)]
                attempt = repository.attempts[delivery.id][-1]
                frame = notification_agent_document(
                    NotificationAgentDelivery(
                        event=stored_event,
                        delivery=delivery,
                        attempt=attempt,
                    ),
                    session_epoch=uuid4(),
                )
                ack = _ack_from_frame(frame, agent_id=principal.agent_id)
                repository.acknowledge_agent_delivery(
                    ack, now=NOW, channel_instance_id="desktop-agent"
                )
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


def test_handover_waiter_rechecks_deadline_after_durable_read() -> None:
    runtime, repository = _runtime()
    event = _event(operation_id="deadline-edge")
    _principal_value, frame = _stage_delivery(runtime, event)
    delivery_id = UUID(str(frame.document["delivery_id"]))
    stored_delivery = repository.deliveries[delivery_id]
    deadline = NOW + timedelta(seconds=1)
    published = HandoverNotificationResult(
        HandoverNotificationOutcome.ACCEPTED,
        PublishResult(PublishStatus.PERSISTED, event.id),
    )

    class AcceptedPublisher:
        def publish_for_handover(self, _event, _deadline):
            return published

    class IdleDispatcher:
        def recover_expired(self):
            return 0

        def dispatch_once(self):
            return None

    wall_clock = [NOW]
    monotonic_clock = [0.0]
    waiter = runtime._waiter
    waiter._publisher = AcceptedPublisher()
    waiter._dispatcher = IdleDispatcher()
    waiter._clock = lambda: wall_clock[0]
    waiter._monotonic = lambda: monotonic_clock[0]

    def read_after_deadline(_event):
        wall_clock[0] = deadline
        return (replace(stored_delivery, state=DeliveryState.DELIVERED, updated_at=NOW),)

    waiter._deliveries = read_after_deadline
    result = waiter.publish_and_wait(event, deadline=deadline)

    assert result.outcome is HandoverNotificationOutcome.FAILED
    assert result.is_proof is False


def test_fatal_dispatcher_failure_disables_agent_runtime() -> None:
    runtime, _repository = _runtime()

    class FailingDispatcher:
        def recover_expired(self):
            raise RuntimeError("synthetic dispatcher failure")

    runtime._dispatcher = FailingDispatcher()
    runtime._run_dispatcher()

    assert runtime.enabled is False
    assert runtime.fatal_stop_reason == "dispatcher_failed"
    runtime.start()
    assert runtime._worker is None


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


class _FakeContent:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def iterate():
            for chunk in self._chunks:
                yield chunk

        return iterate()


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        payload: object,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(
        self,
        document: dict[str, object],
        *,
        ack_status: int = 200,
        ack_payload: object | None = None,
        stream_headers: dict[str, str] | None = None,
    ) -> None:
        self.document = document
        self.ack_status = ack_status
        self.ack_payload = ack_payload or {"status": "acknowledged"}
        self.stream_headers = stream_headers or {"Content-Type": "text/event-stream"}
        self.get_headers: dict[str, str] = {}
        self.post_headers: dict[str, str] = {}

    def get(self, _url: str, *, headers: dict[str, str], **_kwargs: object):
        self.get_headers = headers
        cursor = NotificationCursor(
            str(self.document["profile_id"]),
            int(self.document["profile_sequence"]),
            UUID(str(self.document["event_id"])),
        ).encode()
        payload = json.dumps(self.document, ensure_ascii=False, separators=(",", ":"))
        return _FakeResponse(
            status=200,
            payload=None,
            headers=self.stream_headers,
            chunks=(
                f"id: {cursor}\nevent: notification\ndata: {payload}\n\n".encode(
                    "utf-8"
                ),
            ),
        )

    def post(self, _url: str, *, headers: dict[str, str], **_kwargs: object):
        self.post_headers = headers
        return _FakeResponse(
            status=self.ack_status,
            payload=self.ack_payload,
            headers={"Content-Type": "application/json"},
        )


def _client_config(tmp_path: Path, credential: DesktopAgentCredential | None = None):
    return DesktopAgentClientConfig(
        "https://agent.example",
        credential or _credential(),
        tmp_path / "cursor.json",
    )


def test_client_uses_identity_encoding_and_persists_logical_presentation(tmp_path: Path) -> None:
    runtime, _repository = _runtime()
    _principal_value, frame = _stage_delivery(
        runtime, _event(operation_id="client-logical-presentation")
    )
    presented: list[str] = []
    config = _client_config(tmp_path)
    client = DesktopAgentClient(
        config,
        on_notification=lambda document: presented.append(str(document["delivery_id"])),
    )
    first_session = _FakeSession(frame.document)
    assert asyncio.run(
        client.run_once("profile-1", session=first_session)
    ) == frame.cursor.encode()
    assert first_session.get_headers["Accept-Encoding"] == "identity"
    assert presented == [str(frame.document["delivery_id"])]

    retry_document = dict(frame.document)
    retry_document.update(
        {
            "attempt_ordinal": 2,
            "lease_token": str(uuid4()),
            "session_epoch": str(uuid4()),
        }
    )
    second_session = _FakeSession(retry_document, ack_payload={"status": "duplicate"})
    assert asyncio.run(
        client.run_once("profile-1", session=second_session)
    ) == frame.cursor.encode()
    assert presented == [str(frame.document["delivery_id"])]

    restarted_presented: list[str] = []
    restarted = DesktopAgentClient(
        config,
        on_notification=lambda document: restarted_presented.append(
            str(document["delivery_id"])
        ),
    )
    assert asyncio.run(
        restarted.run_once("profile-1", session=_FakeSession(retry_document))
    ) == frame.cursor.encode()
    assert restarted_presented == []
    stored = json.loads(config.cursor_file.read_text(encoding="utf-8"))
    assert stored["v"] == 2
    assert stored["presented"]["profile-1"] == str(frame.document["delivery_id"])


def test_client_emits_bounded_connection_ack_and_backlog_signals(tmp_path: Path) -> None:
    runtime, _repository = _runtime()
    _principal_value, frame = _stage_delivery(
        runtime, _event(operation_id="client-telemetry")
    )

    class Telemetry:
        def __init__(self) -> None:
            self.connection: list[str] = []
            self.acks: list[str] = []
            self.backlog: list[str] = []

        def record_agent_connection(self, *, status: str) -> None:
            self.connection.append(status)

        def record_agent_ack(self, *, status: str) -> None:
            self.acks.append(status)

        def record_agent_backlog(self, *, status: str) -> None:
            self.backlog.append(status)

    telemetry = Telemetry()
    client = DesktopAgentClient(
        _client_config(tmp_path),
        on_notification=lambda _document: None,
        telemetry=telemetry,
    )

    asyncio.run(client.run_once("profile-1", session=_FakeSession(frame.document)))

    assert telemetry.connection == ["started"]
    assert telemetry.acks == ["acknowledged"]
    assert telemetry.backlog == ["available"]


def test_client_rejects_compressed_agent_stream(tmp_path: Path) -> None:
    runtime, _repository = _runtime()
    _principal_value, frame = _stage_delivery(
        runtime, _event(operation_id="client-compression")
    )
    client = DesktopAgentClient(
        _client_config(tmp_path), on_notification=lambda _document: None
    )
    with pytest.raises(DesktopAgentProtocolError):
        asyncio.run(
            client.run_once(
                "profile-1",
                session=_FakeSession(
                    frame.document,
                    stream_headers={
                        "Content-Type": "text/event-stream",
                        "Content-Encoding": "gzip",
                    },
                ),
            )
        )


def test_client_treats_bounded_stale_ack_as_reconnectable(tmp_path: Path) -> None:
    runtime, _repository = _runtime()
    _principal_value, frame = _stage_delivery(
        runtime, _event(operation_id="client-stale-ack")
    )
    client = DesktopAgentClient(
        _client_config(tmp_path), on_notification=lambda _document: None
    )
    with pytest.raises(DesktopAgentRecoverableAckError) as error:
        asyncio.run(
            client.run_once(
                "profile-1",
                session=_FakeSession(
                    frame.document,
                    ack_status=409,
                    ack_payload={
                        "status": "rejected",
                        "reason": "session_identity_mismatch",
                    },
                ),
            )
        )
    assert error.value.reason == "session_identity_mismatch"
    stale_state = json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8"))
    assert stale_state["profiles"] == {}
    assert stale_state["presented"]["profile-1"] == str(frame.document["delivery_id"])

    with pytest.raises(DesktopAgentProtocolError):
        asyncio.run(
            client.run_once(
                "profile-1",
                session=_FakeSession(
                    frame.document,
                    ack_status=409,
                    ack_payload={
                        "status": "rejected",
                        "reason": "payload_digest_mismatch",
                    },
                ),
            )
        )


def test_client_cursor_rmw_keeps_concurrent_profiles(tmp_path: Path) -> None:
    credential = DesktopAgentCredential(
        agent_id="desktop-agent-1",
        profiles=frozenset({"profile-1", "profile-2"}),
        token="agent-token-0123456789abcdef",
    )
    client = DesktopAgentClient(
        _client_config(tmp_path, credential), on_notification=lambda _document: None
    )
    cursors = {
        profile: NotificationCursor(profile, index, uuid4()).encode()
        for index, profile in enumerate(sorted(credential.profiles), start=1)
    }

    def write(profile: str) -> None:
        for _ in range(10):
            client._write_cursor(profile, cursors[profile])

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(write, sorted(cursors)))
    stored = json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8"))
    assert stored["profiles"] == cursors


def test_client_recoverable_ack_does_not_cancel_other_profile_loop(tmp_path: Path) -> None:
    credential = DesktopAgentCredential(
        agent_id="desktop-agent-1",
        profiles=frozenset({"profile-1", "profile-2"}),
        token="agent-token-0123456789abcdef",
    )
    reconnects: list[str] = []
    client = DesktopAgentClient(
        _client_config(tmp_path, credential),
        on_notification=lambda _document: None,
    )
    stop_event = asyncio.Event()

    async def run_once(profile_id: str, *, session=None):
        del session
        if profile_id == "profile-1":
            reconnects.append(profile_id)
            raise DesktopAgentRecoverableAckError("session_identity_mismatch")
        stop_event.set()
        return None

    client.run_once = run_once
    asyncio.run(client.run_forever(stop_event=stop_event))
    assert reconnects == ["profile-1"]


def test_desktop_agent_client_runtime_has_start_stop_lifecycle() -> None:
    started = Event()

    class StubClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def run_forever(self, *, stop_event: asyncio.Event) -> None:
            started.set()
            await stop_event.wait()

    config = DesktopAgentClientConfig(
        "https://agent.example", _credential(), Path("cursor.json")
    )
    runtime = DesktopAgentClientRuntime(
        config,
        on_notification=lambda _document: None,
        client_factory=StubClient,
    )
    runtime.start()
    assert started.wait(timeout=1.0)
    runtime.stop()
    deadline = time.monotonic() + 1.0
    while runtime.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.running is False


def test_production_composition_wires_agent_client_to_shared_telemetry(monkeypatch) -> None:
    from module.notification_agent import client as client_module
    from module.persistence.runtime import build_runtime_desktop_agent_composition

    monkeypatch.setenv("AZURPILOT_NOTIFICATION_AGENT_URL", "https://agent.example")
    monkeypatch.setenv("AZURPILOT_NOTIFICATION_AGENT_ID", "desktop-agent-1")
    monkeypatch.setenv("AZURPILOT_NOTIFICATION_AGENT_PROFILES", "profile-1")
    monkeypatch.setenv(
        "AZURPILOT_NOTIFICATION_AGENT_TOKEN", "agent-token-0123456789abcdef"
    )
    monkeypatch.delenv("AZURPILOT_NOTIFICATION_AGENT_TOKEN_FILE", raising=False)

    telemetry = object()
    captured: dict[str, object] = {}

    class StubRuntime:
        def __init__(self, config, *, on_notification, telemetry):
            captured.update(
                config=config,
                on_notification=on_notification,
                telemetry=telemetry,
            )

    monkeypatch.setattr(client_module, "DesktopAgentClientRuntime", StubRuntime)

    runtime = build_runtime_desktop_agent_composition(telemetry=telemetry)

    assert isinstance(runtime, StubRuntime)
    assert captured["telemetry"] is telemetry
    assert captured["on_notification"] is present_desktop_agent_notification


def test_production_composition_ignores_inbound_only_agent_configuration(monkeypatch) -> None:
    from module.persistence.runtime import build_runtime_desktop_agent_composition

    monkeypatch.delenv("AZURPILOT_NOTIFICATION_AGENT_URL", raising=False)
    monkeypatch.setenv("AZURPILOT_NOTIFICATION_AGENT_ID", "desktop-agent-1")
    monkeypatch.setenv("AZURPILOT_NOTIFICATION_AGENT_PROFILES", "profile-1")
    monkeypatch.setenv(
        "AZURPILOT_NOTIFICATION_AGENT_TOKEN", "agent-token-0123456789abcdef"
    )

    assert build_runtime_desktop_agent_composition() is None


def test_production_presenter_uses_existing_local_presentation_callback(monkeypatch) -> None:
    from module.notify import notify as notify_module

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        notify_module,
        "notify_webui",
        lambda instance, title, content: calls.append((instance, title, content)) or True,
    )

    present_desktop_agent_notification(
        {"profile_id": "profile-1", "title": "Заголовок", "body": "Текст"}
    )

    assert calls == [("profile-1", "Заголовок", "Текст")]


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


def test_api_rejects_semantically_invalid_cursor_before_opening_sse_stream(monkeypatch) -> None:
    from module.webui import api as webui_api

    runtime, _repository = _runtime()
    _principal_value, frame = _stage_delivery(
        runtime, _event(operation_id="api-invalid-cursor")
    )
    monkeypatch.setattr(webui_api, "_notification_agent_runtime", lambda: runtime)
    forged = NotificationCursor(
        "profile-1", frame.cursor.profile_sequence + 1, uuid4()
    ).encode()
    request = _api_request(
        "GET",
        "/api/notification-agent/stream",
        headers={"Authorization": f"Bearer {_credential().token}"},
        query_string=f"profile=profile-1&cursor={forged}".encode("ascii"),
    )

    response = asyncio.run(webui_api.api_notification_agent_stream(request))

    assert response.status_code == 400
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
    lease_token = uuid4()
    ack_document = {
        "delivery_id": str(uuid4()),
        "event_id": str(uuid4()),
        "event_source": "runtime",
        "profile_id": "profile-1",
        "attempt_ordinal": 1,
        "lease_token": str(lease_token),
        "session_epoch": str(lease_token),
        "payload_digest": "a" * 64,
    }
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


def test_api_returns_bounded_recoverable_reason_for_stale_ack(monkeypatch) -> None:
    from module.webui import api as webui_api

    runtime, _repository = _runtime()
    principal, frame = _stage_delivery(runtime, _event(operation_id="api-stale-ack"))
    del principal
    monkeypatch.setattr(webui_api, "_notification_agent_runtime", lambda: runtime)
    ack_document = {
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
    }
    ack_document["session_epoch"] = str(uuid4())
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

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "status": "rejected",
        "reason": "session_identity_mismatch",
    }


__all__ = []
