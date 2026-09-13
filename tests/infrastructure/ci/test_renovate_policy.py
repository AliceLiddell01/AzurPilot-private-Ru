from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
Rule = dict[str, Any]
Dependency = dict[str, str]


def _load_config() -> dict[str, Any]:
    return json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))


def _raw_pattern_matches(pattern: str, value: str) -> bool:
    if pattern.startswith("/") and pattern.rfind("/") > 0:
        end = pattern.rfind("/")
        expression = pattern[1:end]
        flags = re.IGNORECASE if pattern[end + 1 :] == "i" else 0
        return re.search(expression, value, flags) is not None
    return fnmatch.fnmatchcase(value, pattern)


def _matches_patterns(value: str | None, patterns: object) -> bool:
    if not isinstance(value, str):
        return False
    if isinstance(patterns, str):
        patterns = (patterns,)
    if not isinstance(patterns, (list, tuple)) or not patterns:
        return False

    positive_patterns = []
    has_string_pattern = False
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        has_string_pattern = True
        negated = pattern.startswith("!")
        raw_pattern = pattern[1:] if negated else pattern
        if negated and _raw_pattern_matches(raw_pattern, value):
            return False
        if not negated:
            positive_patterns.append(raw_pattern)

    return has_string_pattern and (
        not positive_patterns
        or any(
            _raw_pattern_matches(pattern, value) for pattern in positive_patterns
        )
    )


def _matches(rule: Rule, dependency: Dependency) -> bool:
    match_fields = (
        ("matchPackageNames", "packageName"),
        ("matchManagers", "manager"),
        ("matchDatasources", "datasource"),
        ("matchDepTypes", "depType"),
        ("matchUpdateTypes", "updateType"),
    )
    return all(
        rule_key not in rule
        or _matches_patterns(dependency.get(dependency_key), rule[rule_key])
        for rule_key, dependency_key in match_fields
    )


def _matching_rules(config: dict[str, Any], dependency: Dependency) -> list[Rule]:
    return [
        rule
        for rule in config["packageRules"]
        if _matches(rule, dependency)
    ]


def _resolved_priority(config: dict[str, Any], dependency: Dependency) -> int:
    priority = 0
    for rule in _matching_rules(config, dependency):
        if "prPriority" in rule:
            priority = rule["prPriority"]
    return priority


def _resolved_dashboard_approval(
    config: dict[str, Any], dependency: Dependency
) -> bool:
    approval = config["dependencyDashboardApproval"] is True
    matching_rules = _matching_rules(config, dependency)
    for rule in matching_rules:
        if "dependencyDashboardApproval" in rule:
            approval = rule["dependencyDashboardApproval"] is True
    return approval


def test_renovate_matching_supports_glob_regex_and_negation() -> None:
    dependency = {
        "packageName": "docker/setup-buildx-action",
        "manager": "github-actions",
        "datasource": "github-tags",
        "depType": "action",
        "updateType": "minor",
    }

    assert _matches(
        {"matchPackageNames": ["docker/*"]},
        dependency,
    )
    assert _matches(
        {"matchPackageNames": ["/^docker\\//"]},
        dependency,
    )
    assert not _matches(
        {"matchPackageNames": ["docker/*", "!docker/setup-buildx-action"]},
        dependency,
    )
    assert not _matches({"matchPackageNames": []}, dependency)
    assert not _matches({"matchPackageNames": [None]}, dependency)


def test_renovate_priority_uses_last_matching_rule_value() -> None:
    config = {
        "packageRules": [
            {"matchPackageNames": ["example"], "prPriority": 10},
            {"matchPackageNames": ["example"], "prPriority": 0},
        ]
    }

    assert _resolved_priority(
        config,
        {
            "packageName": "example",
            "manager": "pep621",
            "datasource": "pypi",
            "depType": "project.dependencies",
            "updateType": "patch",
        },
    ) == 0


def test_uv_coupling_identities_follow_normal_dashboard_policy() -> None:
    config = _load_config()
    identities = (
        {
            "packageName": "uv",
            "manager": "pep621",
            "datasource": "pypi",
            "depType": "project.dependencies",
            "updateType": "minor",
        },
        {
            "packageName": "astral-sh/uv",
            "manager": "github-actions",
            "datasource": "github-releases",
            "depType": "uses-with",
            "updateType": "minor",
        },
        {
            "packageName": "ghcr.io/astral-sh/uv",
            "manager": "dockerfile",
            "datasource": "docker",
            "depType": "final",
            "updateType": "digest",
        },
    )

    for dependency in identities:
        package_name = dependency["packageName"]
        assert _resolved_dashboard_approval(config, dependency) is False, package_name
        assert _resolved_priority(config, dependency) == 0, package_name

    assert not any(
        dependency["packageName"] in rule.get("matchPackageNames", [])
        and rule.get("dependencyDashboardApproval") is True
        for rule in config["packageRules"]
        for dependency in identities
    )


def test_first_wave_priority_is_explicit_and_non_major() -> None:
    config = _load_config()
    assert config["dependencyDashboardApproval"] is False
    assert config["dependencyDashboard"] is True
    assert config["prConcurrentLimit"] == 4
    assert config["prHourlyLimit"] == 2
    assert config["commitHourlyLimit"] == 2
    assert config["separateMultipleMajor"] is True
    assert config["automerge"] is False
    assert config["vulnerabilityAlerts"]["dependencyDashboardApproval"] is False
    assert not any(
        rule.get("dependencyDashboardApproval") is True
        for rule in config["packageRules"]
    )

    first_wave = (
        {
            "packageName": "playwright",
            "manager": "pep621",
            "datasource": "pypi",
            "depType": "dependency-groups",
            "updateType": "minor",
        },
        {
            "packageName": "actions/checkout",
            "manager": "github-actions",
            "datasource": "github-tags",
            "depType": "action",
            "updateType": "digest",
        },
        {
            "packageName": "docker/setup-buildx-action",
            "manager": "github-actions",
            "datasource": "github-tags",
            "depType": "action",
            "updateType": "digest",
        },
    )

    for dependency in first_wave:
        package_name = dependency["packageName"]
        assert _resolved_dashboard_approval(config, dependency) is False, package_name
        assert _resolved_priority(config, dependency) == 10, package_name

    ordinary_update = {
        "packageName": "matplotlib",
        "manager": "pep621",
        "datasource": "pypi",
        "depType": "project.dependencies",
        "updateType": "patch",
    }
    assert _resolved_dashboard_approval(config, ordinary_update) is False
    assert _resolved_priority(config, ordinary_update) == 0

    protected_uv = {
        "packageName": "uv",
        "manager": "pep621",
        "datasource": "pypi",
        "depType": "project.dependencies",
        "updateType": "minor",
    }
    assert _resolved_dashboard_approval(config, protected_uv) is False
    assert _resolved_priority(config, protected_uv) == 0

    priority_rules = [
        rule for rule in config["packageRules"] if rule.get("prPriority") == 10
    ]
    assert len(priority_rules) == 2
    for rule in priority_rules:
        update_types = rule.get("matchUpdateTypes")
        assert update_types, rule
        assert "major" not in update_types, rule
    assert all(
        set(rule["matchPackageNames"]).isdisjoint(
            {"uv", "astral-sh/uv", "ghcr.io/astral-sh/uv"}
        )
        for rule in priority_rules
    )
