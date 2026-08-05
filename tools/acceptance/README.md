# Acceptance tools

These commands exercise real external environments and are intentionally outside required CI.

- `uv run python -m tools.acceptance.device --help` — explicit-target device and control checks.
- `uv run python -m tools.acceptance.ocr --help` — local OCR provider and debug-output checks.
- `uv run python -m tools.acceptance.ocr_opsi_zone --help` — bounded, read-only Operation Siren zone OCR checks.
- `uv run python -m tools.acceptance.ocr_commission --help` — bounded, read-only Commission OCR checks.
- `uv run python -m tools.acceptance.webui_smoke --help` — local WebUI startup smoke.

Acceptance output is local diagnostic data. Do not commit generated reports, screenshots, device identifiers, paths, or external output.
