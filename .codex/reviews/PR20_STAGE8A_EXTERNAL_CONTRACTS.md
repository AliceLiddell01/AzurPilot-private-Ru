# Stage 8A — pinned external contract review

- Review status: **PASS WITH DOCUMENTED LIMITATIONS**
- Scope: behavior used by PR #20 only
- Rule: current upstream documentation is never substituted for a pinned legacy API/protocol.

## adbutils `0.11.0`

- Pinned by `pyproject.toml` and `uv.lock`.
- Checked source: `openatx/adbutils` tag `0.11.0`.
- Relevant contract: selecting one concrete device by serial, ADB server/client transport,
  `device`, `offline`, `unauthorized`, timeout and connection errors.
- Fork evidence: `module/device/connection.py` resolves `self.adb_client.device(self.serial)`;
  acceptance commands use explicit `adb -s <serial>`.
- Current upstream difference: newer transport-id and API documentation is not used to infer
  behavior of `0.11.0`.

## uiautomator2 `2.16.17`

- Pinned by `pyproject.toml` and `uv.lock`.
- Checked source: `openatx/uiautomator2` tag `2.16.17`.
- Relevant contract:
  - HTTP requests default to a connect/read timeout pair;
  - implicit wait is an element-search timeout and is separate from HTTP timeout;
  - XPath wait/get has its own explicit timeout contract;
  - device/service recovery is separate from element wait behavior;
  - shell, click/long-click, swipe/drag, text input and XPath waits have distinct APIs.
- Fork evidence:
  - `connection_attr.py` creates the pinned client and sets the Android new-command timeout;
  - `uiautomator_2.py` keeps operation-specific retry and timeout call sites.
- Current upstream difference: the project is pinned to 2.x; current 3.x documentation and
  transport behavior are not treated as authoritative for this fork.

## scrcpy-server `1.20`

- Bundled artifact: `bin/scrcpy/scrcpy-server-v1.20.jar`.
- Checked source: `Genymobile/scrcpy` tag `v1.20`, `DEVELOP.md`, commit
  `ffe0417228fb78ab45b7ee4e202fc06fc8875bf3`.
- Relevant contract:
  - the host pushes and starts the server;
  - v1.20 uses separate video and control sockets;
  - the server sends device name and initial dimensions before video frames;
  - video is raw H.264;
  - frames may be absent at startup while the surface is unchanged;
  - controls and device messages use version-specific binary framing.
- Fork evidence:
  - `ScrcpyOptions.command_v120`;
  - `ScrcpyCore._scrcpy_server_start`, `_scrcpy_stream_loop`, and control sender serialization;
  - live preview preserves the v1.20 handshake and falls back to screenshot mode.
- Current upstream difference: current scrcpy protocol documentation describes a newer
  multi-stream protocol and explicitly states that the protocol is internal and version-specific.
  It is useful only as a warning against cross-version assumptions, not as the v1.20 wire spec.

## Result

The pinned tags/source and the fork implementation agree for the contracts used by Stage 8A.
Residual limitations are represented in `scenario-evidence.json` and
`security-review.json`; real device behavior remains a separate sanitized acceptance artifact.
