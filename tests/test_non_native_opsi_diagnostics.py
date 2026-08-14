from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import module.base.non_native_diagnostics as diagnostics
import module.base.utils as base_utils
from module.base.button import Button


def _match_kwargs(button_name: str, *, similarity: float, matched: bool) -> dict:
    return {
        'button_name': button_name,
        'image': np.zeros((720, 1280, 3), dtype=np.uint8),
        'requested_threshold': 0.85,
        'effective_threshold': 0.75,
        'similarity': similarity,
        'matched': matched,
        'search_area': (1122, 610, 1212, 698),
        'match_point': (20, 20),
        'match_offset': (0, 0),
        'base_button': (1142, 630, 1192, 678),
        'resolved_button': (1142, 630, 1192, 678),
    }


class Stage1NonNativeDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_root = diagnostics._DIAGNOSTIC_ROOT
        self._original_enabled = base_utils.TEMPLATE_MATCH_NON_NATIVE_720P
        self._original_resolution = base_utils.TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION
        diagnostics._session_id = None
        diagnostics._session_dir = None
        diagnostics._transition_active = False
        diagnostics._first_globe_observation_saved = False
        diagnostics._best_globe_similarity = None

    def tearDown(self) -> None:
        diagnostics._DIAGNOSTIC_ROOT = self._original_root
        base_utils.set_template_match_non_native_720p(
            self._original_enabled,
            self._original_resolution,
        )
        diagnostics._session_id = None
        diagnostics._session_dir = None
        diagnostics._transition_active = False
        diagnostics._first_globe_observation_saved = False
        diagnostics._best_globe_similarity = None

    def test_native_720p_does_not_create_diagnostic_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'diagnostics'
            diagnostics._DIAGNOSTIC_ROOT = root
            base_utils.set_template_match_non_native_720p(False, (1280, 720))

            diagnostics.record_stage1_non_native_match(
                **_match_kwargs(
                    'MAP_GOTO_GLOBE',
                    similarity=0.90,
                    matched=True,
                )
            )

            self.assertFalse(root.exists())

    def test_transition_session_records_thresholds_scores_and_limited_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'diagnostics'
            diagnostics._DIAGNOSTIC_ROOT = root
            base_utils.set_template_match_non_native_720p(True, (3840, 2160))

            diagnostics.record_stage1_non_native_match(
                **_match_kwargs(
                    'MAP_GOTO_GLOBE',
                    similarity=0.91,
                    matched=True,
                )
            )
            diagnostics.record_stage1_non_native_match(
                **_match_kwargs(
                    'GLOBE_GOTO_MAP',
                    similarity=0.71,
                    matched=False,
                )
            )
            diagnostics.record_stage1_non_native_match(
                **_match_kwargs(
                    'GLOBE_GOTO_MAP',
                    similarity=0.82,
                    matched=True,
                )
            )

            sessions = [path for path in root.iterdir() if path.is_dir()]
            self.assertEqual(len(sessions), 1)
            session = sessions[0]

            events = [
                json.loads(line)
                for line in (session / 'events.jsonl').read_text(
                    encoding='utf-8'
                ).splitlines()
            ]
            match_events = [
                event for event in events
                if event['event'] == 'template_match'
            ]
            self.assertEqual(
                [event['button'] for event in match_events],
                ['MAP_GOTO_GLOBE', 'GLOBE_GOTO_MAP', 'GLOBE_GOTO_MAP'],
            )
            self.assertEqual(match_events[0]['source_resolution'], [3840, 2160])
            self.assertEqual(match_events[0]['working_resolution'], [1280, 720])
            self.assertEqual(match_events[0]['requested_threshold'], 0.85)
            self.assertEqual(match_events[0]['effective_threshold'], 0.75)
            self.assertEqual(match_events[1]['actual_similarity'], 0.71)
            self.assertFalse(match_events[1]['matched'])
            self.assertEqual(match_events[1]['phase'], 'post_click')
            self.assertTrue(
                any(event['event'] == 'transition_result' for event in events)
            )

            self.assertTrue(
                (session / 'before_map_goto_globe_normalized.png').is_file()
            )
            self.assertTrue(
                (session / 'after_click_first_normalized.png').is_file()
            )
            self.assertTrue(
                (session / 'globe_best_normalized.png').is_file()
            )
            self.assertTrue(
                (session / 'globe_confirmed_normalized.png').is_file()
            )
            self.assertLessEqual(len(list(session.glob('*.png'))), 4)

    def test_observer_failure_does_not_escape_into_button_match(self) -> None:
        with patch.object(
            diagnostics,
            '_record_stage1_non_native_match',
            side_effect=OSError('diagnostic write failed'),
        ):
            diagnostics.record_stage1_non_native_match(
                **_match_kwargs(
                    'MAP_GOTO_GLOBE',
                    similarity=0.90,
                    matched=True,
                )
            )

    def test_button_match_keeps_existing_native_and_non_native_decisions(self) -> None:
        button = Button(
            area=(10, 10, 20, 20),
            color=(),
            button=(10, 10, 20, 20),
            name='MAP_GOTO_GLOBE',
        )
        button._match_init = True
        button.image = np.zeros((10, 10, 3), dtype=np.uint8)
        image = np.zeros((720, 1280, 3), dtype=np.uint8)

        with (
            patch('module.base.button.cv2.matchTemplate', return_value=np.zeros((1, 1))),
            patch(
                'module.base.button.cv2.minMaxLoc',
                return_value=(0.0, 0.80, (0, 0), (0, 0)),
            ),
            patch('module.base.button.record_stage1_non_native_match') as observer,
        ):
            base_utils.set_template_match_non_native_720p(False, (1280, 720))
            self.assertFalse(button.match(image, offset=(20, 20), similarity=0.85))

            base_utils.set_template_match_non_native_720p(True, (3840, 2160))
            self.assertTrue(button.match(image, offset=(20, 20), similarity=0.85))

        self.assertEqual(observer.call_count, 2)
        native_call = observer.call_args_list[0].kwargs
        non_native_call = observer.call_args_list[1].kwargs
        self.assertEqual(native_call['requested_threshold'], 0.85)
        self.assertEqual(native_call['effective_threshold'], 0.85)
        self.assertEqual(non_native_call['requested_threshold'], 0.85)
        self.assertEqual(non_native_call['effective_threshold'], 0.75)


if __name__ == '__main__':
    unittest.main()
