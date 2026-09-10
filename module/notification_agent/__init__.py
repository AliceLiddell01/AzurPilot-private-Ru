"""Outbound-only Desktop Agent SSE client."""

from module.notification_agent.client import (
    DesktopAgentClient,
    DesktopAgentClientConfig,
    DesktopAgentClientError,
    DesktopAgentProtocolError,
    DesktopAgentTransportError,
    SSEEvent,
    iter_sse_events,
)

__all__ = [
    "DesktopAgentClient",
    "DesktopAgentClientConfig",
    "DesktopAgentClientError",
    "DesktopAgentProtocolError",
    "DesktopAgentTransportError",
    "SSEEvent",
    "iter_sse_events",
]
