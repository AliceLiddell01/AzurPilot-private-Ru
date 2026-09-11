"""Контракты policy resolution и immutable snapshot."""

from __future__ import annotations

from dataclasses import replace

import pytest

from module.application.notifications import (
    NotificationPolicy,
    NotificationPolicyResolver,
    NotificationRule,
    NotificationRuleMatcher,
    NotificationSeverity,
    PolicyAction,
    PolicyState,
)
from module.application.notifications.encoding import canonical_value
from tests.notification_test_support import _event, _policy


def test_policy_is_first_matching_rule_and_snapshot_is_stable() -> None:
    event = _event()
    resolver = NotificationPolicyResolver(_policy())
    decision = resolver.resolve(event)
    assert decision.state is PolicyState.ROUTED
    assert decision.matched_rule_id == "handover"
    assert resolver.resolve(event).snapshot_hash == decision.snapshot_hash


def test_canonical_value_materializes_string_enums_as_plain_strings() -> None:
    value = canonical_value(NotificationSeverity.CRITICAL)

    assert value == "CRITICAL"
    assert type(value) is str


def test_policy_rejects_duplicate_rule_ids() -> None:
    rule = NotificationRule(
        rule_id="duplicate",
        priority=1,
        matcher=NotificationRuleMatcher(exact_type="runtime.handover.preemption_requested"),
        action=PolicyAction(channel_instance_ids=("agent",)),
    )
    policy = NotificationPolicy(
        version=1,
        rules=(rule, rule),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )

    with pytest.raises(ValueError, match="повторяющиеся rule id"):
        NotificationPolicyResolver(policy)


def test_policy_rejects_channels_with_suppression_reason() -> None:
    policy = NotificationPolicy(
        version=1,
        rules=(),
        default_action=PolicyAction(
            channel_instance_ids=("agent",),
            suppression_reason="maintenance",
        ),
    )

    with pytest.raises(ValueError, match="channels вместе"):
        NotificationPolicyResolver(policy)


def test_policy_severity_matcher_is_typed_and_safe() -> None:
    policy = NotificationPolicy(
        version=1,
        rules=(
            NotificationRule(
                rule_id="critical-only",
                priority=1,
                matcher=NotificationRuleMatcher(
                    minimum_severity=NotificationSeverity.ERROR
                ),
                action=PolicyAction(channel_instance_ids=("agent",)),
            ),
        ),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )
    resolver = NotificationPolicyResolver(policy)

    assert resolver.resolve(_event()).matched_rule_id == "critical-only"
    assert not NotificationRuleMatcher(
        minimum_severity=NotificationSeverity.ERROR
    ).matches(replace(_event(), severity="CRITICAL"))
