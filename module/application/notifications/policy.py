"""Детерминированное first-match разрешение notification policy."""

from __future__ import annotations

from re import fullmatch

from module.application.notifications.encoding import (
    MAX_SNAPSHOT_BYTES,
    canonical_digest,
    policy_snapshot_document,
)
from module.application.notifications.models import (
    DOTTED_TYPE_RE,
    NotificationEvent,
    NotificationPolicy,
    NotificationPolicySnapshot,
    NotificationRule,
    NotificationRuleMatcher,
    NotificationSeverity,
    PolicyAction,
    PolicyDecision,
    PolicyState,
)


def _bounded_token(value: object, pattern: str) -> bool:
    return isinstance(value, str) and fullmatch(pattern, value) is not None


def _validate_action(action: PolicyAction) -> None:
    if not isinstance(action, PolicyAction):
        raise TypeError("Policy action должен быть PolicyAction.")
    if not isinstance(action.channel_instance_ids, tuple):
        raise TypeError("Policy channels должны быть immutable tuple.")
    if not _bounded_token(action.locale, r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?"):
        raise ValueError("Policy locale имеет неверный формат.")
    if not _bounded_token(
        action.presentation_profile, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
    ):
        raise ValueError("Policy presentation profile имеет неверный формат.")
    if len(action.channel_instance_ids) > 32:
        raise ValueError("Policy не может выбрать больше 32 channel instances.")
    if len(set(action.channel_instance_ids)) != len(action.channel_instance_ids):
        raise ValueError("Policy не может содержать повторяющиеся channels.")
    for channel_id in action.channel_instance_ids:
        if not _bounded_token(channel_id, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"):
            raise ValueError("Policy channel instance имеет неверный формат.")
    if action.suppression_reason is not None and not _bounded_token(
        action.suppression_reason, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
    ):
        raise ValueError("Policy suppression reason имеет неверный формат.")
    if action.suppression_reason is not None and action.channel_instance_ids:
        raise ValueError(
            "Policy action не может задавать channels вместе с suppression reason."
        )


def _validate_matcher(matcher: object) -> None:
    if not isinstance(matcher, NotificationRuleMatcher):
        raise TypeError("Policy matcher должен быть NotificationRuleMatcher.")
    if matcher.exact_type is not None and not _bounded_token(
        matcher.exact_type, DOTTED_TYPE_RE
    ):
        raise ValueError("Policy exact type имеет неверный формат.")
    if matcher.type_prefix is not None and not _bounded_token(
        matcher.type_prefix, DOTTED_TYPE_RE
    ):
        raise ValueError("Policy type prefix имеет неверный формат.")
    for severity in (matcher.exact_severity, matcher.minimum_severity):
        if severity is not None and not isinstance(severity, NotificationSeverity):
            raise TypeError("Policy severity matcher должен быть NotificationSeverity.")
    if matcher.profile_id is not None and not _bounded_token(
        matcher.profile_id, r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
    ):
        raise ValueError("Policy profile selector имеет неверный формат.")
    for value, name in (
        (matcher.subject_kind, "subject kind"),
        (matcher.subject_id, "subject id"),
    ):
        if value is not None and not _bounded_token(
            value, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
        ):
            raise ValueError(f"Policy {name} selector имеет неверный формат.")
    if matcher.source is not None and not _bounded_token(
        matcher.source, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
    ):
        raise ValueError("Policy source selector имеет неверный формат.")


def _validate_rule(rule: NotificationRule) -> None:
    if not isinstance(rule, NotificationRule):
        raise TypeError("Policy rule должен быть NotificationRule.")
    if not _bounded_token(rule.rule_id, r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"):
        raise ValueError("Policy rule id имеет неверный формат.")
    if not isinstance(rule.priority, int) or isinstance(rule.priority, bool) or not 0 <= rule.priority <= 1_000_000:
        raise ValueError("Policy rule priority имеет неверный диапазон.")
    matcher = rule.matcher
    _validate_matcher(matcher)
    if matcher.exact_type is not None and matcher.type_prefix is not None:
        raise ValueError("Policy matcher не может иметь exact type и prefix одновременно.")
    _validate_action(rule.action)


class NotificationPolicyResolver:
    """Разрешает policy без зависимости от transport или persistence."""

    def __init__(self, policy: NotificationPolicy) -> None:
        if not isinstance(policy, NotificationPolicy):
            raise TypeError("Policy должен быть NotificationPolicy.")
        if not isinstance(policy.version, int) or isinstance(policy.version, bool) or policy.version <= 0:
            raise ValueError("Policy version должен быть положительным.")
        if not isinstance(policy.global_enabled, bool):
            raise TypeError("Policy global_enabled должен быть bool.")
        if not isinstance(policy.rules, tuple):
            raise TypeError("Policy rules должны быть immutable tuple.")
        for rule in policy.rules:
            _validate_rule(rule)
        rule_ids = tuple(rule.rule_id for rule in policy.rules)
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("Policy не может содержать повторяющиеся rule id.")
        _validate_action(policy.default_action)
        self._policy = policy
        # Меньшее значение priority выигрывает; ties сохраняют порядок объявления.
        self._ordered_rules = tuple(
            sorted(enumerate(policy.rules), key=lambda item: (item[1].priority, item[0]))
        )

    @property
    def policy(self) -> NotificationPolicy:
        return self._policy

    def resolve(self, event: NotificationEvent) -> PolicyDecision:
        selected_rule: NotificationRule | None = None
        if not self._policy.global_enabled:
            action = PolicyAction(suppression_reason="global_disabled")
            reason = "global_disabled"
        else:
            for _, rule in self._ordered_rules:
                if rule.matcher.matches(event):
                    selected_rule = rule
                    break
            if selected_rule is None:
                action = self._policy.default_action
                reason = action.suppression_reason or (
                    "default_routed"
                    if action.channel_instance_ids
                    else "default_suppressed"
                )
            else:
                action = selected_rule.action
                reason = action.suppression_reason or (
                    "rule_routed" if action.channel_instance_ids else "rule_suppressed"
                )
        snapshot = NotificationPolicySnapshot(
            policy_version=self._policy.version,
            rule_id=selected_rule.rule_id if selected_rule else None,
            action=action,
        )
        state = PolicyState.SUPPRESSED if action.suppressed else PolicyState.ROUTED
        return PolicyDecision(
            state=state,
            policy_version=self._policy.version,
            matched_rule_id=selected_rule.rule_id if selected_rule else None,
            reason=reason,
            snapshot=snapshot,
            snapshot_hash=canonical_digest(
                policy_snapshot_document(snapshot), max_bytes=MAX_SNAPSHOT_BYTES
            ),
        )


def default_notification_policy() -> NotificationPolicy:
    """Policy Stage 2: durable publication включена, transport выбирается явно."""
    return NotificationPolicy(
        version=1,
        rules=(),
        default_action=PolicyAction(suppression_reason="no_channel_registered"),
        global_enabled=True,
    )


__all__ = [
    "NotificationPolicyResolver",
    "default_notification_policy",
]
