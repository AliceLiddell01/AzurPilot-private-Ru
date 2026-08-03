# Independent review — PR #21 / executable Stage 8A scenario matrix

- Initial finding: the PR #20 scenario matrix primarily asserted source text and did not execute the required runtime behaviors.
- Stable CI finding: the Stage 7 → Stage 8A bridge compared `origin/personal/stable..HEAD`, which becomes an empty self-diff on a push to `personal/stable`.
- Initial verdict: **BLOCKED**.

## Required remediation

1. Every Stage 8A scenario requirement must map to one unique executable `unittest` fixture ID.
2. Evidence generation must fail closed when a fixture is missing, duplicated or not importable.
3. Static source assertions may remain supplemental, but must not be represented as regression evidence.
4. The matrix must cover ADB state/retry/final exception, device readiness, package detection, emulator lifecycle, screenshot backends and image contract, input serialization, scrcpy v1.20 lifecycle/framing, uiautomator2 operations/timeouts, NemuIpc/LDOpenGL and WebUI live control/cleanup.
5. The stable bridge test must compare against the immutable Stage 8A baseline rather than the current stable ref.
6. Final acceptance applies only to the exact clean head after all required CI jobs and a new MuMu acceptance pass.

## Remediation implementation

- Added `tests/test_stage8a_runtime_scenario_matrix.py` with scenario-specific executable fixtures.
- Expanded `SCENARIO_REQUIREMENTS`; each row owns a unique `fixture_test`.
- `scenario_evidence()` emits `CI_FIXTURE` only after fixture resolution succeeds.
- Added Android boot-readiness verification through target-explicit `adb -s <serial> shell getprop sys.boot_completed` to the acceptance runner.
- Fixed the Stage 7 policy bridge to use the immutable Stage 8A baseline.
- Cross-platform fixtures isolate Windows-only `winreg`, `CREATE_NO_WINDOW` and `ctypes.WinDLL` dependencies without changing production behavior.
- One-shot remediation run `30852739690` executed the full `test_stage8a_*.py` scope, Stage 8A verifier and `git diff --check`, then removed its payload/workflow and published clean implementation commit `54e2a7c9e6793e7256f80b67f6ffab2d9a8b0d91`.

## Self-review correction

A manual post-CI review found three rows that still did not execute the represented behavior:

- `device_readiness/adb_state_device` inspected a local tuple;
- `uiautomator2/implicit_wait` inspected the external-contract Markdown;
- `uiautomator2/xpath_wait_get` inspected the external-contract Markdown.

They were replaced by direct runtime fixtures:

- `_wait_for_target_device()` is called with a target-explicit mocked ADB transport and its result, attempts and argv are asserted;
- the actual pinned `uiautomator2==2.16.17` `Device.implicitly_wait` implementation is called for setter and getter behavior;
- the actual pinned 2.16.17 `XPath(device) → XPathSelector.wait/get` implementation parses a synthetic hierarchy and returns the matched element.

The pinned XPath API was captured from the installed 2.16.17 distribution rather than inferred from current 3.x documentation. Focused and full validation run `30854531613` passed, then removed its temporary payload/workflow and published clean implementation commit `07ba788a2e609acce7d18b03c75b3324f6d9d774`.

## Final verdict rule

This review becomes **PASS** only when all five required jobs complete successfully on the exact post-evidence head, the generated scenario evidence reports full executable coverage, no unresolved review thread remains, and the user repeats real MuMu acceptance on that same head.
