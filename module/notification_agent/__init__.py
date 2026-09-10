"""Outbound-only Desktop Agent SSE client."""

from module.notification_agent.client import (
    DesktopAgentClient,
    DesktopAgentClientConfig,
    DesktopAgentClientError,
    DesktopAgentClientRuntime,
    DesktopAgentRecoverableAckError,
    DesktopAgentProtocolError,
    DesktopAgentTransportError,
    SSEEvent,
    iter_sse_events,
    present_desktop_agent_notification,
)

__all__ = [
    "DesktopAgentClient",
    "DesktopAgentClientConfig",
    "DesktopAgentClientError",
    "DesktopAgentClientRuntime",
    "DesktopAgentRecoverableAckError",
    "DesktopAgentProtocolError",
    "DesktopAgentTransportError",
    "SSEEvent",
    "iter_sse_events",
    "present_desktop_agent_notification",
]
