from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dev_tools.runtime_russianization_audit import (
    RuntimeIdentity,
    audit_source,
    classify_english_only,
    read_runtime_identity,
    run_audit,
    validate_runtime_identity,
)

ROOT = Path(__file__).resolve().parents[1]


def test_actual_repository_has_zero_actionable_runtime_residuals() -> None:
    result = run_audit(ROOT)

    assert result.blockers == []
    assert result.categories["TRANSLATE"] == 0
    assert result.consumer_sites > 0
    assert result.deferred_exception_text > 0


def test_cjk_operator_logger_message_fails() -> None:
    result = audit_source('logger.info("正在运行")\n', "module/sample.py")

    assert result.categories["TRANSLATE"] == 1
    assert "contains CJK operator text" in result.blockers[0]


def test_ordinary_english_operator_sentence_fails() -> None:
    result = audit_source(
        'logger.info("Starting task")\nlogger.info("Waiting")\n'
        'logger.info("Waiting_for_task")\n',
        "module/sample.py",
    )

    assert result.categories["TRANSLATE"] == 3
    assert "unclassified English operator text" in result.blockers[0]


def test_russian_context_and_exact_technical_tokens_pass() -> None:
    source = r'''
logger.info("Задача запущена")
logger.info("Ответ: %s", raw_payload)
logger.info("ADB")
logger.info("https://example.invalid/status")
logger.info(r"C:\runtime\log.txt")
logger.info("com.YoStarEN.AzurLane")
logger.info("SUBMARINE")
'''

    result = audit_source(source, "module/sample.py")

    assert result.blockers == []
    assert classify_english_only("OCR") == "PRESERVE_TECHNICAL"


def test_exception_text_is_deferred_and_not_reclassified_as_prose() -> None:
    result = audit_source(
        'raise RuntimeError("External backend failed")\n',
        "module/sample.py",
    )

    assert result.blockers == []
    assert result.consumer_sites == 0
    assert result.deferred_exception_text == 1


def test_legitimate_feature_structure_outside_display_sink_passes() -> None:
    source = """
def select_state(enabled):
    if enabled:
        return "Starting task"
    return "idle"
"""

    result = audit_source(source, "module/sample.py")

    assert result.blockers == []
    assert result.consumer_sites == 0


def test_current_runtime_identity_passes() -> None:
    identity = read_runtime_identity(ROOT)

    assert validate_runtime_identity(identity) == []


def test_foreign_runtime_identity_variants_fail_closed() -> None:
    good = RuntimeIdentity(
        ui_locale="ru-RU",
        build_time_locales=("en-US",),
        event_name_source="en",
        event_name_fallback_order=(),
        server="en",
        valid_servers=("en",),
        valid_packages=(("com.YoStarEN.AzurLane", "en"),),
        channel_packages=(),
        asset_roots=("en",),
        ocr_aliases=(("ONNX_MODEL_PARAMS", ("azur_lane",)),),
    )
    variants = (
        replace(good, ui_locale="en-US"),
        replace(good, server="cn"),
        replace(good, valid_packages=(("com.YoStarJP.AzurLane", "jp"),)),
        replace(good, event_name_fallback_order=("cn",)),
        replace(good, asset_roots=("cn", "en")),
        replace(good, ocr_aliases=(("ONNX_MODEL_PARAMS", ("azur_lane", "cnocr")),)),
    )

    for identity in variants:
        assert validate_runtime_identity(identity)
