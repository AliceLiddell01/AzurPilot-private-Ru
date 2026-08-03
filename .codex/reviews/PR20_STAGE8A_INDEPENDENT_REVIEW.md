# Independent review — PR #20 / Stage 8A

- Review date: 2026-08-03
- Initial reviewed head: `27940215645422c6bc2efaccc44ae8d02bf29332`
- Remediated implementation head: `a97d4e943f8424c4444f544625b8edfa7d1b82a2`
- Source: independent reviewer report supplied for PR #20 plus remediation re-review
- Initial verdict: **BLOCKED**
- Merge authorization: **not granted**

## Findings

| ID | Severity | Finding | Remediation evidence | Status |
|---|---|---|---|---|
| IR-01 | BLOCKER | Bare exception logger calls have no Russian first-party context | Dedicated fail-closed AST audit; `stage8a_bare_exception_context_findings=0`; 83 reviewed wrappers preserve exception payload and logger method | REMEDIATED |
| IR-02 | BLOCKER | Dynamic logger expressions are classified too broadly as raw external payload | Exception expressions have a separate classifier and negative fixtures; unrelated raw external expressions remain outside this allowance | REMEDIATED |
| IR-03 | BLOCKER | Required backend/scenario matrix is not machine-readable and complete | `scenario-evidence.json`: 87/87 requirements with executable or explicitly external evidence, status `PASS` | REMEDIATED |
| IR-04 | BLOCKER | Independent review was asserted without a review artifact | This versioned artifact records initial findings, failed attempts and remediation evidence | REMEDIATED |
| IR-05 | BLOCKER | Security review was not recorded as a separate verdict | `.codex/reviews/PR20_STAGE8A_SECURITY_REVIEW.md` plus `security-review.json`, 19 checks, status `PASS` | REMEDIATED |
| IR-06 | HIGH | Backend coverage matrix contradicts external real acceptance | `backend-coverage.json` separates `CI_EVIDENCE_ONLY` from external acceptance and does not assert a real backend from CI | REMEDIATED |
| IR-07 | HIGH | Binary-log audit checks only the first positional logger argument | All positional and keyword arguments, lazy formatting, `logger.attr` and nested containers are audited; blocking metric is `0` | REMEDIATED |
| IR-08 | MEDIUM | Pinned external dependency contracts are not evidenced | Exact `adbutils==0.11.0`, `uiautomator2==2.16.17` and bundled scrcpy-server 1.20 contracts are documented and tested; status `PASS` | REMEDIATED |
| IR-09 | LOW | Superseded Stage 8A branch remains | Cleanup remains deferred until after merge and unique-data verification; no branch was deleted during remediation | DEFERRED |

## Remediation history

- Run `30841001060`: fail-closed validation stopped before commit because the new bundled scrcpy contract test inspected `module/config/argument/args.json`, which is not the source of the bundled JAR path.
- Retry configuration `0f68a807…`: no remediation run was created because the temporary workflow contained invalid YAML indentation inside an embedded multiline Python literal; no payload was applied.
- Run `30843237705`: all 100 Stage 8A tests passed; the semantic verifier remained fail-closed with 100 unresolved candidates and one sequence/control-flow mismatch.
- Run `30843676649`: diagnostics proved that all unresolved candidates were 50 removed plus 50 added stable IDs in `module/webui/api.py`, caused by inserting security helpers before existing message-bearing runtime nodes.
- Run `30844916018`: all patch digests, Stage 8A tests, semantic verifier, diagnostic upload, fail-closed enforcement and clean-tree publication passed. Temporary payload directories and the one-shot workflow were removed before commit.

## Remediation evidence

Artifact `stage8a-remediation-30844916018-1`:

- artifact digest: `sha256:a856df7e98ac5c73ed7b7d61a6d7cc39942204ceb8647984112ab20e237de747`;
- verifier head: `c437aa66a74dc9aee9bc9c219c225d2b339e3fb7`;
- candidates: `647`;
- translation required: `501`;
- translated: `501`;
- unresolved: `0`;
- CJK/English remaining: `0/0`;
- placeholder, severity, sequence and control-flow mismatches: `0`;
- raw/binary payload findings: `0/0`;
- bare exception context findings: `0`;
- secret and mojibake findings: `0/0`;
- approved metadata expression changes: `11`;
- approved exception context wrappers: `83`;
- approved security control-flow changes: `1`;
- scenario/security/external-contract statuses: `PASS/PASS/PASS`.

The clean implementation head is `a97d4e943f8424c4444f544625b8edfa7d1b82a2`. It contains no `.stage8a-remediation-*` payload and no one-shot remediation workflow.

## Re-review verdict

**REMEDIATION PASS — exact-head CI and new real-device acceptance are still required.**

PR remains Draft. This verdict does not authorize Ready or merge. A final PASS applies only to the exact post-evidence head after all five required CI jobs and a new relevant MuMu acceptance succeed on that same head.
