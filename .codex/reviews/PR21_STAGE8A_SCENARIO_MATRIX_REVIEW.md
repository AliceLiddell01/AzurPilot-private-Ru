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
- Added Android boot-readiness verification through `sys.boot_completed` to the acceptance runner.
- Fixed the Stage 7 policy bridge to use the immutable Stage 8A baseline.
- Cross-platform fixtures isolate Windows-only `winreg`, `CREATE_NO_WINDOW` and `ctypes.WinDLL` dependencies without changing production behavior.
- One-shot remediation run `30852739690` executed the full `test_stage8a_*.py` scope, Stage 8A verifier and `git diff --check`, then removed its payload/workflow and published clean implementation commit `54e2a7c9e6793e7256f80b67f6ffab2d9a8b0d91`.

## Final verdict rule

This review becomes **PASS** only when all five required jobs complete successfully on the exact post-evidence head, the generated scenario evidence reports full executable coverage, no unresolved review thread remains, and the user repeats real MuMu acceptance on that same head.
