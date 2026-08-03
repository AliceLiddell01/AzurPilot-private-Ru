# Independent review — PR #20 / Stage 8A

- Review date: 2026-08-03
- Reviewed head: `27940215645422c6bc2efaccc44ae8d02bf29332`
- Source: independent reviewer report supplied for PR #20
- Initial verdict: **BLOCKED**
- Merge authorization: **not granted**

## Findings

| ID | Severity | Finding | Required exit criterion |
|---|---|---|---|
| IR-01 | BLOCKER | Bare exception logger calls have no Russian first-party context | Dedicated fail-closed AST audit reports zero findings; exception payload, severity, traceback and call order remain preserved |
| IR-02 | BLOCKER | Dynamic logger expressions are classified too broadly as raw external payload | Bare exception expressions are independently classified and blocked; positive and negative fixtures pass |
| IR-03 | BLOCKER | Required backend/scenario matrix is not machine-readable and complete | Every required scenario has explicit CI fixture, semantic or external real-acceptance evidence with limitations |
| IR-04 | BLOCKER | Independent review was asserted without a review artifact | This artifact and PR review comment are present; remediation verdict is recorded on the final exact head |
| IR-05 | BLOCKER | Security review was not recorded as a separate verdict | A separate machine-readable security checklist and PR verdict exist on the final exact head |
| IR-06 | HIGH | Backend coverage matrix contradicts external real acceptance | CI evidence and external user-acceptance evidence are represented as separate channels; no hard-coded false `actual_user_backend` flag |
| IR-07 | HIGH | Binary-log audit checks only the first positional logger argument | All positional and keyword arguments, lazy formatting, `logger.attr` and nested containers are audited |
| IR-08 | MEDIUM | Pinned external dependency contracts are not evidenced | Pinned versions, checked source tags, relevant contracts and upstream differences are recorded and validated |
| IR-09 | LOW | Superseded Stage 8A branch remains | Cleanup is deferred until after merge and only after unique-data verification |

## Remediation rules

1. PR remains Draft.
2. Previous user acceptance cannot authorize a changed runtime head.
3. No finding may be closed by broad allowlists, `continue-on-error`, skipped tests or prose-only assertions.
4. Runtime log changes must preserve severity, traceback, retry, timeout, fallback, protocol and call order.
5. Final remediation requires a fresh exact-head CI cycle and a new relevant user acceptance on the same head.

## Remediation attempts

- Run `30841001060`: fail-closed validation stopped before commit because the new bundled scrcpy contract test inspected `module/config/argument/args.json`, which is not the source of the bundled JAR path.
- The retry verifies the bundled path through the dedicated external-contract artifact, the actual binary when present, `ScrcpyOptions.command_v120` and the WebUI 1.20 integration.

## Remediation verdict

**PENDING — keep PR BLOCKED.**

This section may be changed to PASS only after all finding-specific evidence is attached to the exact final head.
