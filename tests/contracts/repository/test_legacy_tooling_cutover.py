from __future__ import annotations

from dev_tools.legacy_tooling_inventory import classify, inventory
from tests.support.contracts import assert_no_legacy_operator_surfaces
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_legacy_tooling_inventory_is_machine_readable_and_exhaustive():
    surfaces = inventory(ROOT)
    assert surfaces
    assert all(item.category != "UNCLASSIFIED_SHELL" for item in surfaces)
    assert all(item.owner for item in surfaces)
    assert not any(
        item.category
        in {
            "PROJECT_OPERATIONAL_LEGACY",
            "PROJECT_ACCEPTANCE_LEGACY",
            "PROJECT_DOCKER_ORCHESTRATOR_LEGACY",
            "PROJECT_LAUNCHER_LEGACY",
        }
        for item in surfaces
    )


def test_external_native_hooks_are_not_mistaken_for_operator_tooling():
    retained = classify("infrastructure/observability/postgres/init/01-bootstrap.sh")
    assert retained is not None
    assert retained.category == "EXTERNAL_NATIVE_HOOK"
    assert retained.disposition == "RETAIN"


def test_unlisted_observability_shell_is_not_auto_retained():
    candidate = classify("infrastructure/observability/example/operator.sh")
    assert candidate is not None
    assert candidate.category == "UNCLASSIFIED_SHELL"
    assert candidate.disposition == "REVIEW"


def test_removed_operator_directories_stay_removed():
    assert_no_legacy_operator_surfaces(ROOT)
