from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
Rule = dict[str, Any]
Dependency = dict[str, str]


def _load_config() -> dict[str, Any]:
    return json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))


def _matches(rule: Rule, dependency: Dependency) -> bool:
    match_fields = (
        ("matchPackageNames", "packageName"),
        ("matchManagers", "manager"),
        ("matchDatasources", "datasource"),
        ("matchDepTypes", "depType"),
        ("matchUpdateTypes", "updateType"),
    )
    return all(
        not rule.get(rule_key)
        or dependency.get(dependency_key) in rule[rule_key]
        for rule_key, dependency_key in match_fields
    )


def _matching_rules(config: dict[str, Any], dependency: Dependency) -> list[Rule]:
    return [
        rule
        for rule in config["packageRules"]
        if _matches(rule, dependency)
    ]


def _resolved_priority(config: dict[str, Any], dependency: Dependency) -> int:
    priorities = [
        rule["prPriority"]
        for rule in _matching_rules(config, dependency)
        if "prPriority" in rule
    ]
    return max(priorities, default=0)


def _resolved_dashboard_approval(
    config: dict[str, Any], dependency: Dependency
) -> bool:
    matching_rules = _matching_rules(config, dependency)
    if any(rule.get("dependencyDashboardApproval") is True for rule in matching_rules):
        return True
    return config["dependencyDashboardApproval"] is True


def test_uv_coupling_identities_remain_dashboard_approved() -> None:
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
        assert _resolved_dashboard_approval(config, dependency) is True, package_name
        assert _resolved_priority(config, dependency) == 0, package_name

    uv_rule = next(
        rule
        for rule in config["packageRules"]
        if "uv" in rule.get("matchPackageNames", [])
    )
    assert set(uv_rule["matchPackageNames"]) == {
        "uv",
        "astral-sh/uv",
        "ghcr.io/astral-sh/uv",
    }
    assert "astral-sh/setup-uv" not in uv_rule["matchPackageNames"]


def test_first_wave_priority_is_explicit_and_non_major() -> None:
    config = _load_config()
    assert config["dependencyDashboardApproval"] is False
    assert config["major"]["dependencyDashboardApproval"] is True
    assert config["automerge"] is False
    assert config["vulnerabilityAlerts"]["dependencyDashboardApproval"] is False

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
    assert _resolved_dashboard_approval(config, protected_uv) is True
    assert _resolved_priority(config, protected_uv) == 0

    priority_rules = [
        rule for rule in config["packageRules"] if rule.get("prPriority") == 10
    ]
    assert len(priority_rules) == 2
    assert all("major" not in rule["matchUpdateTypes"] for rule in priority_rules)
    assert all(
        set(rule["matchPackageNames"]).isdisjoint(
            {"uv", "astral-sh/uv", "ghcr.io/astral-sh/uv"}
        )
        for rule in priority_rules
    )
