"""Task snapshots and the editor must agree without relaxing real-gap checks."""
import copy
import unittest

from backend.service import recording_layout
from backend.tasks.recording import collection_recording_layout, with_recording_layout


class TaskRecordingTests(unittest.TestCase):
    def collection(self):
        return {'videos': [
            {'id': 'left', 'video_name': 'A09999_20261014140608_0002.mp4',
             'start_ms': 1200267, 'end_ms': 1799934, 'duration_ms': 599667,
             'recording_start': '2026-10-14T14:06:08.267+08:00'},
            {'id': 'right', 'video_name': 'A09999_20261014141608_0003.mp4',
             'start_ms': 1800000, 'end_ms': 2399833, 'duration_ms': 599833,
             'recording_start': '2026-10-14T14:16:08.000+08:00'}]}

    def test_legacy_snapshot_recovers_camera_offsets_without_changing_coordinates(self):
        capture = self.collection()
        original = copy.deepcopy(capture)
        runs, bridges, gaps = collection_recording_layout(capture)
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in runs], [(1200267, 2399833)])
        self.assertEqual([(b['start_ms'], b['end_ms']) for b in bridges], [(1799934, 1800000)])
        self.assertEqual(gaps, [])
        editor = [{**v, 'name': v['video_name'], 'alignment_offset_ms': offset}
                  for v, offset in zip(capture['videos'], [267, 0])]
        self.assertEqual((runs, bridges, gaps), recording_layout(editor))
        self.assertEqual(capture, original)
        task = {'collection': {**capture, 'recording_runs': [], 'gaps': [{'old': True}]}}
        fixed = with_recording_layout(task)
        self.assertEqual(fixed['collection']['recording_runs'], runs)
        self.assertEqual(task['collection']['recording_runs'], [])

    def test_unknown_or_unqualified_seams_remain_gaps(self):
        changes = [
            {'video_name': 'B09999_20261014141608_0003.mp4'},
            {'video_name': 'A09999_20261014141608_0004.mp4'},
            {'alignment_offset_ms': 3001},
            {'recording_start': '2026-10-14T14:16:12.000+08:00'},
            {'recording_start': 'invalid'},
            {'recording_start': '2026-10-14T14:16:08'},
            {'start_ms': 1800934},  # A full second is a real gap.
        ]
        for change in changes:
            with self.subTest(change=change):
                capture = self.collection()
                capture['videos'][1].update(change)
                runs, bridges, gaps = collection_recording_layout(capture)
                self.assertEqual(len(runs), 2)
                self.assertEqual(bridges, [])
                self.assertEqual(len(gaps), 1)


if __name__ == '__main__':
    unittest.main()
