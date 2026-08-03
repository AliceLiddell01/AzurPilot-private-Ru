# Security review — PR #20 / Stage 8A

- Review status: **PENDING FINAL EXACT-HEAD CI**
- Review scope: Stage 8A runtime log changes, verifier/audit code, acceptance runner,
  device live preview/control boundary and generated diagnostics.
- Initial security finding: `SR-01` — live screenshot/control WebSocket routes accepted
  non-loopback clients before a dedicated authorization check.

## Remediation

`module/webui/api.py` now accepts `/ws/live_screenshot` and `/ws/live_control` only
for loopback clients (`127.0.0.0/8`, `::1`, or `localhost`). A non-local client is
accepted only long enough to receive a JSON error, then closed with code `4403`
before device/session initialization.

Remote live preview/control remains intentionally unavailable until a separate
authenticated transport contract is designed. This avoids treating the PyWebIO
session password as implicit authorization for directly mounted Starlette routes.

## Checklist

| Area | Evidence | Current result |
|---|---|---|
| command injection | acceptance subprocess AST audit; no `shell=True` | PASS |
| shell quoting | argv-list acceptance runner; no shell expansion | PASS |
| serial injection | strict serial regex and explicit `-s`/endpoint selection | PASS |
| unsafe subprocess logging | sanitized bounded evidence | PASS |
| SSH credentials / URL credentials | sanitizer fixtures | PASS |
| device serial / local paths | sanitizer fixtures and `<serial>` report | PASS |
| clipboard / typed text leakage | acceptance forbids clipboard and user text; live text logs only operation type | PASS |
| screenshot / binary leakage | byte-count-only evidence and all-argument binary audit | PASS |
| HTML/WebSocket injection | JSON encoding and loopback live-route guard | PASS |
| ANSI/control/newline forging | sanitizer strips/bounds external output | PASS |
| exception local leakage | bounded sanitized failure report | PASS |
| temporary paths | screenshot is unlinked in normal and `finally` paths | PASS |
| port exposure | minitouch forward removed; live WebSockets loopback-only | PASS |
| live preview authorization | functional IPv4/IPv6/local/remote guard tests | PASS |

## Residual limitations

- Existing macOS emulator helpers use `shell=True` for legacy command execution. PR #20
  does not change those command strings or trust boundaries; this is documented legacy
  behavior, not a new Stage 8A regression.
- Generic non-device API routes retain their existing product authorization model and
  are outside Stage 8A.
- Remote live preview/control is disabled, not newly authenticated.

## Final verdict rule

The final verdict becomes **PASS** only if security tests, secret audits, semantic
gates and exact-head CI all succeed on the same head, followed by relevant user
acceptance for the changed observable behavior.
