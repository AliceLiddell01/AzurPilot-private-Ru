"""Наблюдательная телеметрия Stage 1 для non-native 720p OpSi.

Модуль намеренно не меняет thresholds, match result, click target или control flow.
Он активен только для двух исследуемых OpSi-кнопок при включённом
TEMPLATE_MATCH_NON_NATIVE_720P и пишет артефакты в уже ignored-каталог log/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import module.base.utils as base_utils
from module.logger import logger


_STAGE1_TARGETS = frozenset({
    'MAP_GOTO_GLOBE',
    'GLOBE_GOTO_MAP',
})
_DIAGNOSTIC_ROOT = Path('log') / 'stage1_non_native_720p'
_session_id: str | None = None
_session_dir: Path | None = None
_transition_active = False
_first_globe_observation_saved = False
_best_globe_similarity: float | None = None


def _as_int_tuple(value) -> tuple[int, ...]:
    return tuple(int(item) for item in value)


def _enabled(button_name: str) -> bool:
    return (
        bool(base_utils.TEMPLATE_MATCH_NON_NATIVE_720P)
        and button_name in _STAGE1_TARGETS
    )


def _new_session() -> tuple[str, Path]:
    global _session_id, _session_dir
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    _session_id = stamp
    _session_dir = _DIAGNOSTIC_ROOT / stamp
    _session_dir.mkdir(parents=True, exist_ok=True)
    return _session_id, _session_dir


def _ensure_session() -> tuple[str, Path]:
    if _session_id is None or _session_dir is None:
        return _new_session()
    return _session_id, _session_dir


def _save_normalized_frame(image: np.ndarray, filename: str) -> str | None:
    _, directory = _ensure_session()
    path = directory / filename
    try:
        success = bool(cv2.imwrite(str(path), image))
    except (cv2.error, OSError) as exc:
        logger.warning(
            f'[Stage1 4K diagnostics] Не удалось сохранить {path}: {exc}'
        )
        return None
    if not success:
        logger.warning(
            f'[Stage1 4K diagnostics] OpenCV не сохранил кадр: {path}'
        )
        return None
    return str(path)


def _append_event(payload: dict[str, Any]) -> None:
    _, directory = _ensure_session()
    path = directory / 'events.jsonl'
    payload = {
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'session_id': _session_id,
        **payload,
    }
    try:
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            stream.write('\n')
    except OSError as exc:
        logger.warning(
            f'[Stage1 4K diagnostics] Не удалось записать {path}: {exc}'
        )


def _record_stage1_non_native_match(
    *,
    button_name: str,
    image: np.ndarray,
    requested_threshold: float,
    effective_threshold: float,
    similarity: float,
    matched: bool,
    search_area,
    match_point,
    match_offset,
    base_button,
    resolved_button,
) -> None:
    """Записать фактический результат уже выполненного Button.match().

    Функция не выполняет повторный template match и не изменяет Button.
    """
    global _transition_active
    global _first_globe_observation_saved
    global _best_globe_similarity

    if not _enabled(button_name):
        return
    if button_name == 'MAP_GOTO_GLOBE' and not matched:
        return
    if button_name == 'GLOBE_GOTO_MAP' and not _transition_active:
        return

    source_resolution = _as_int_tuple(
        base_utils.TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION
    )
    working_resolution = (int(image.shape[1]), int(image.shape[0]))
    search_area = _as_int_tuple(search_area)
    local_point = _as_int_tuple(match_point)
    absolute_point = (
        int(search_area[0] + local_point[0]),
        int(search_area[1] + local_point[1]),
    )
    match_offset = _as_int_tuple(match_offset)
    base_button = _as_int_tuple(base_button)
    resolved_button = _as_int_tuple(resolved_button)

    if button_name == 'MAP_GOTO_GLOBE' and matched:
        _new_session()
        _transition_active = True
        _first_globe_observation_saved = False
        _best_globe_similarity = None
        _save_normalized_frame(image, 'before_map_goto_globe_normalized.png')

    if button_name == 'GLOBE_GOTO_MAP' and _transition_active:
        if not _first_globe_observation_saved:
            _save_normalized_frame(image, 'after_click_first_normalized.png')
            _first_globe_observation_saved = True
        if _best_globe_similarity is None or similarity > _best_globe_similarity:
            _best_globe_similarity = float(similarity)
            _save_normalized_frame(image, 'globe_best_normalized.png')
        if matched:
            _save_normalized_frame(image, 'globe_confirmed_normalized.png')

    phase = 'pre_click'
    if button_name == 'GLOBE_GOTO_MAP' and _transition_active:
        phase = 'post_click'

    payload = {
        'event': 'template_match',
        'phase': phase,
        'button': button_name,
        'source_resolution': source_resolution,
        'working_resolution': working_resolution,
        'non_native_720p': True,
        'requested_threshold': float(requested_threshold),
        'effective_threshold': float(effective_threshold),
        'actual_similarity': float(similarity),
        'matched': bool(matched),
        'search_area': search_area,
        'best_match_point_local': local_point,
        'best_match_point_canonical': absolute_point,
        'match_offset': match_offset,
        'base_button': base_button,
        'resolved_button': resolved_button,
    }

    logger.info(
        '[Stage1 4K diagnostics] '
        f'button={button_name} phase={phase} '
        f'source={source_resolution[0]}x{source_resolution[1]} '
        f'working={working_resolution[0]}x{working_resolution[1]} '
        'non_native_720p=true '
        f'requested={float(requested_threshold):.4f} '
        f'effective={float(effective_threshold):.4f} '
        f'actual={float(similarity):.6f} matched={bool(matched)} '
        f'best={absolute_point} search={search_area} '
        f'match_offset={match_offset} resolved_button={resolved_button}'
    )
    _append_event(payload)

    if button_name == 'GLOBE_GOTO_MAP' and matched and _transition_active:
        _append_event({
            'event': 'transition_result',
            'state': 'Globe',
            'confirmed_by': 'GLOBE_GOTO_MAP',
            'actual_similarity': float(similarity),
            'effective_threshold': float(effective_threshold),
        })
        _transition_active = False


def record_stage1_non_native_match(**kwargs) -> None:
    """Безопасный observational hook: ошибки телеметрии не меняют match result."""
    try:
        _record_stage1_non_native_match(**kwargs)
    except Exception as exc:
        logger.warning(
            f'[Stage1 4K diagnostics] Ошибка наблюдателя: '
            f'{type(exc).__name__}: {exc}'
        )
