"""Stable runtime namespace for generated Event campaign packages."""

from pathlib import Path


# Generated packages physically live under ``campaign/generated_event``.  This
# alias intentionally starts with ``event`` so the established CampaignRun
# event path keeps its normal stage semantics while individual event ids remain
# data-driven by Event artifact metadata.
__path__ = [str(Path(__file__).resolve().parents[1] / "generated_event")]
