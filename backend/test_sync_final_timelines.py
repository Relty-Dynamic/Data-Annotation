"""NAS sync contracts with isolated S3 fixtures; no real network or NAS writes."""
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deploy.sync_final_timelines import (
    APPLICATION, MARKER, PENDING, SyncError, discover, main, run_lock, sync_capture,
)


CAPTURE = 'daily/20261021/1021张三'
DIRECTORY = CAPTURE + '/timeline/finally/'


class ReadOnlyObjects:
    def __init__(self):
        self.objects = {}
        self.reads = []

    def get(self, key):
        self.reads.append(key)
        return self.objects[key]

    def list(self, prefix):
        # Multiple pages exercise discoveries after the first listing page.
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        for key in keys:
            yield {'Contents': [{'Key': key}]}

    def publish(self, capture=CAPTURE, *, version='v1', accepted_at='2026-10-21T01:00:00Z', axes=('posture',), directory='finally'):
        metadata = {'task_id': 'task-fixture', 'final_version_id': version,
                    'accepted_by': 'admin-fixture', 'accepted_at': accepted_at}
        files = {}
        directory = capture + '/timeline/' + directory + '/'
        for axis in axes:
            name = axis + '.timeline.json'
            data = json.dumps({'schema_version': 3, 'axis': axis, 'segments': [],
                               'task_result': metadata}, ensure_ascii=False).encode()
            files[name] = hashlib.sha256(data).hexdigest()
            self.objects[directory + name] = data
        self.objects[directory + MARKER] = json.dumps({
            'application': APPLICATION, 'metadata': metadata, 'files': files,
        }, ensure_ascii=False).encode()


class FinalTimelineSyncTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / '.tmp'
        base.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.nas = self.root / 'nas' / 'daily'
        self.nas.mkdir(parents=True)
        self.collection = self.nas / '20261021' / '1021张三'
        self.collection.mkdir(parents=True)
        self.target = self.collection / 'timeline' / 'finally'
        self.store = ReadOnlyObjects()
        self.store.publish(axes=('posture', 'scene'))

    def sync(self, **kwargs):
        return sync_capture(self.store, self.nas, CAPTURE, **kwargs)

    def test_dry_run_and_repeat_keep_layout_and_unrelated_files(self):
        self.store.objects[CAPTURE + '/timeline/标注员/v1/posture.timeline.json'] = b'not-approved'
        before = copy.deepcopy(self.store.objects)
        self.assertEqual(self.sync()['copy'], 2)
        self.assertFalse((self.collection / 'timeline').exists())
        self.assertEqual(self.sync(apply=True)['copy'], 2)
        self.assertEqual((self.target / 'posture.timeline.json').read_bytes(),
                         self.store.objects[DIRECTORY + 'posture.timeline.json'])
        unrelated = self.collection / 'timeline' / 'user.txt'
        unrelated.write_text('keep')
        self.store.reads.clear()
        self.assertEqual(self.sync(apply=True)['copy'], 0)
        self.assertEqual(self.store.reads, [DIRECTORY + MARKER, DIRECTORY + MARKER])
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertFalse((self.collection / 'timeline' / '标注员').exists())
        self.assertFalse((self.collection / 'timeline' / '.final-sync-backups').exists())
        self.assertEqual(self.store.objects, before)

    def test_discover_paginates_and_ignores_unapproved_json(self):
        self.store.publish('daily/20261020/1020test')
        self.store.publish('daily/legacy_person')
        self.store.objects['daily/20261019/test/timeline/finally/scene.timeline.json'] = b'partial'
        self.store.objects['daily/20261019/test/timeline/person/v1/' + MARKER] = b'ignore'
        self.assertEqual(discover(self.store, 'daily'),
                         ['daily/20261020/1020test', CAPTURE, 'daily/legacy_person'])
        self.assertEqual(discover(self.store, 'daily/20261021'), [CAPTURE])
        self.assertEqual(discover(self.store, CAPTURE), [CAPTURE])

    def test_current_final_is_preferred_over_legacy_finally(self):
        self.store.publish(directory='final', version='current', accepted_at='2026-10-21T02:00:00Z')
        current = CAPTURE + '/timeline/final/'
        self.store.objects[current + 'delivery-fixture.csv'] = b'fixture CSV excluded from JSON sync'
        directories = {}
        self.assertEqual(discover(self.store, 'daily', directories=directories), [CAPTURE])
        self.assertEqual(directories, {CAPTURE: 'final'})
        args = ['--config', str(self.root / 'not-read.json'), '--nas-root', str(self.nas), '--apply',
                '--state-dir', str(self.root / 'state')]
        with patch('deploy.sync_final_timelines.R2Store', return_value=self.store), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        final = self.collection / 'timeline' / 'final'
        self.assertEqual((final / 'posture.timeline.json').read_bytes(),
                         self.store.objects[current + 'posture.timeline.json'])
        self.assertTrue((final / MARKER).is_file())
        self.assertFalse((final / 'delivery-fixture.csv').exists())
        self.assertFalse(self.target.exists())

    def test_invalid_current_final_does_not_fall_back_to_old_finally(self):
        self.store.publish(directory='final', version='current')
        self.store.objects[CAPTURE + '/timeline/final/' + MARKER] = b'not a valid completion marker'
        args = ['--config', str(self.root / 'not-read.json'), '--nas-root', str(self.nas), '--apply',
                '--state-dir', str(self.root / 'state')]
        with patch('deploy.sync_final_timelines.R2Store', return_value=self.store), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 1)
        self.assertFalse((self.collection / 'timeline').exists())

    def test_update_backs_up_previous_bytes_and_preserves_extra_files(self):
        self.sync(apply=True)
        old = (self.target / 'posture.timeline.json').read_bytes()
        old_marker = (self.target / MARKER).read_bytes()
        extra = self.target / 'notes.json'
        extra.write_text('{"keep":true}')
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))
        result = self.sync(apply=True)
        self.assertEqual(result['copy'], 2)
        backup = Path(result['backup'])
        self.assertEqual((backup / 'posture.timeline.json').read_bytes(), old)
        self.assertEqual((backup / MARKER).read_bytes(), old_marker)
        self.assertEqual(extra.read_text(), '{"keep":true}')
        self.assertEqual((self.target / MARKER).read_bytes(), self.store.objects[DIRECTORY + MARKER])

    def test_manual_changes_and_unknown_collisions_stop_before_writing(self):
        self.sync(apply=True)
        path = self.target / 'posture.timeline.json'
        path.write_text('{"manual":true}')
        old_marker = (self.target / MARKER).read_bytes()
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z')
        with self.assertRaisesRegex(SyncError, '人工修改'):
            self.sync(apply=True)
        self.assertEqual(path.read_text(), '{"manual":true}')
        self.assertEqual((self.target / MARKER).read_bytes(), old_marker)
        (self.target / MARKER).unlink()
        with self.assertRaisesRegex(SyncError, '未知文件'):
            self.sync(apply=True)

    def test_bad_checksum_and_incomplete_source_leave_no_files(self):
        self.store.objects[DIRECTORY + 'scene.timeline.json'] = b'broken'
        with self.assertRaisesRegex(SyncError, '摘要不一致'):
            self.sync(apply=True)
        self.assertFalse(self.target.exists())

    def test_missing_acceptance_marker_and_invalid_json_are_not_copied(self):
        marker = self.store.objects.pop(DIRECTORY + MARKER)
        self.assertEqual(discover(self.store, 'daily'), [])
        self.store.objects[DIRECTORY + MARKER] = marker
        payload = b'{"task_result":{"submission_id":"not-accepted"}}'
        value = json.loads(marker)
        value['files'] = {'posture.timeline.json': hashlib.sha256(payload).hexdigest()}
        self.store.objects[DIRECTORY + MARKER] = json.dumps(value).encode()
        self.store.objects[DIRECTORY + 'posture.timeline.json'] = payload
        with self.assertRaisesRegex(SyncError, '缺少验收结果信息'):
            self.sync(apply=True)
        self.assertFalse(self.target.exists())

    def test_nas_edit_during_download_is_detected_before_writes(self):
        self.sync(apply=True)
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))
        old_marker = (self.target / MARKER).read_bytes()
        get = self.store.get

        def editing(key):
            if key.endswith('/scene.timeline.json'):
                (self.target / 'posture.timeline.json').write_text('{"concurrent":"edit"}')
            return get(key)

        with patch.object(self.store, 'get', side_effect=editing):
            with self.assertRaisesRegex(SyncError, 'NAS 文件在同步期间变化'):
                self.sync(apply=True)
        self.assertEqual((self.target / 'posture.timeline.json').read_text(), '{"concurrent":"edit"}')
        self.assertEqual((self.target / MARKER).read_bytes(), old_marker)

    def test_failed_nas_readback_does_not_publish_success_marker(self):
        from deploy.sync_final_timelines import guarded_write

        def corrupt(path, data, expected, backup=None):
            guarded_write(path, data, expected, backup)
            if path.name.endswith('.timeline.json'):
                path.write_bytes(b'fixture storage corruption')

        with patch('deploy.sync_final_timelines.guarded_write', side_effect=corrupt):
            with self.assertRaisesRegex(SyncError, '整批读回校验失败'):
                self.sync(apply=True)
        self.assertFalse((self.target / MARKER).exists())

    def test_source_marker_change_before_writes_is_retryable(self):
        get = self.store.get
        count = 0

        def changing(key):
            nonlocal count
            if key == DIRECTORY + MARKER:
                count += 1
                if count == 2:
                    self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z')
            return get(key)

        with patch.object(self.store, 'get', side_effect=changing):
            with self.assertRaisesRegex(SyncError, '读取期间变化'):
                self.sync(apply=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.sync(apply=True)['copy'], 1)

    def test_symlink_and_path_traversal_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.collection / 'timeline').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(SyncError, '符号链接'):
            self.sync(apply=True)
        self.assertEqual(list(outside.iterdir()), [])
        (self.collection / 'timeline').unlink()
        marker = json.loads(self.store.objects[DIRECTORY + MARKER])
        marker['files']['../outside.json'] = 'a' * 64
        self.store.objects[DIRECTORY + MARKER] = json.dumps(marker).encode()
        with self.assertRaisesRegex(SyncError, '清单无效'):
            self.sync(apply=True)
        self.assertFalse(self.target.exists())

    def test_interrupted_copy_does_not_publish_marker_and_retry_finishes(self):
        from deploy.sync_final_timelines import guarded_write
        calls = 0

        def interrupt(path, data, expected, backup=None):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('fixture NAS interrupted')
            guarded_write(path, data, expected, backup)

        with patch('deploy.sync_final_timelines.guarded_write', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        self.assertFalse((self.target / MARKER).exists())
        self.assertEqual(self.sync(apply=True)['copy'], 1)
        self.assertTrue((self.target / MARKER).is_file())

    def test_late_manual_edit_is_captured_and_restored_without_loss(self):
        self.sync(apply=True)
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))
        target = self.target / 'posture.timeline.json'
        manual = b'{"fixture":"manual edit after preflight"}'
        rename = os.rename

        def editing(source, destination):
            if source == target:
                target.write_bytes(manual)
            rename(source, destination)

        with patch('deploy.sync_final_timelines.os.rename', side_effect=editing):
            with self.assertRaisesRegex(SyncError, '实际写入时变化'):
                self.sync(apply=True)
        self.assertEqual(target.read_bytes(), manual)
        backups = list((self.collection / 'timeline' / '.final-sync-backups').glob('*/posture.timeline.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), manual)
        self.assertEqual(json.loads((self.target / MARKER).read_bytes())['metadata']['final_version_id'], 'v1')

    def test_concurrent_creation_during_replacement_is_never_overwritten(self):
        self.sync(apply=True)
        target = self.target / 'posture.timeline.json'
        old = target.read_bytes()
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z')
        link = os.link
        manual = b'{"fixture":"concurrent new file"}'

        def creating(source, destination, **kwargs):
            if Path(source).name.startswith('.sync-stage-') and destination == target:
                target.write_bytes(manual)
            return link(source, destination, **kwargs)

        with patch('deploy.sync_final_timelines.os.link', side_effect=creating):
            with self.assertRaisesRegex(SyncError, '其他写入者创建'):
                self.sync(apply=True)
        self.assertEqual(target.read_bytes(), manual)
        self.assertTrue(any(path.read_bytes() == old for path in
                            (self.collection / 'timeline' / '.final-sync-backups').glob('*/posture.timeline.json')))

    def test_open_writer_after_capture_is_preserved_in_backup(self):
        self.sync(apply=True)
        target = self.target / 'posture.timeline.json'
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z')
        link = os.link
        manual = b'{"fixture":"write through open descriptor"}'
        with target.open('r+b') as writer:
            def writing(source, destination, **kwargs):
                if Path(source).name.startswith('.sync-stage-') and destination == target:
                    writer.seek(0)
                    writer.write(manual)
                    writer.truncate()
                    writer.flush()
                return link(source, destination, **kwargs)

            with patch('deploy.sync_final_timelines.os.link', side_effect=writing):
                result = self.sync(apply=True)
        self.assertEqual((Path(result['backup']) / target.name).read_bytes(), manual)
        self.assertEqual(target.read_bytes(), self.store.objects[DIRECTORY + target.name])

    def test_source_change_after_writes_recovers_to_newer_accepted_version(self):
        self.sync(apply=True)
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))
        get = self.store.get
        count = 0

        def changing(key):
            nonlocal count
            if key == DIRECTORY + MARKER:
                count += 1
                if count == 3:
                    self.store.publish(version='v3', accepted_at='2026-10-21T03:00:00Z', axes=('posture', 'scene'))
            return get(key)

        with patch.object(self.store, 'get', side_effect=changing):
            with self.assertRaisesRegex(SyncError, '写入期间变化'):
                self.sync(apply=True)
        self.assertTrue((self.target / PENDING).is_file())
        self.assertEqual(self.sync(apply=True)['copy'], 2)
        self.assertFalse((self.target / PENDING).exists())
        for name in ('posture.timeline.json', 'scene.timeline.json', MARKER):
            self.assertEqual((self.target / name).read_bytes(), self.store.objects[DIRECTORY + name])

    def test_interrupted_update_resumes_new_source_but_rejects_manual_content(self):
        from deploy.sync_final_timelines import guarded_write
        self.sync(apply=True)
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))

        def interrupt(path, data, expected, backup=None):
            if path.name == 'scene.timeline.json':
                raise OSError('fixture disconnected NAS')
            guarded_write(path, data, expected, backup)

        with patch('deploy.sync_final_timelines.guarded_write', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        self.store.publish(version='v3', accepted_at='2026-10-21T03:00:00Z', axes=('posture', 'scene'))
        self.assertEqual(self.sync(apply=True)['copy'], 2)
        self.assertFalse((self.target / PENDING).exists())
        (self.target / 'posture.timeline.json').write_bytes(b'{"fixture":"real human edit"}')
        with self.assertRaisesRegex(SyncError, '人工修改'):
            self.sync(apply=True)

    def test_hardlink_failure_leaves_original_files_in_place(self):
        self.sync(apply=True)
        old = (self.target / 'posture.timeline.json').read_bytes()
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z')
        with patch('deploy.sync_final_timelines.os.link', side_effect=OSError('fixture unsupported')):
            with self.assertRaisesRegex(SyncError, '不支持.*硬链接'):
                self.sync(apply=True)
        self.assertEqual((self.target / 'posture.timeline.json').read_bytes(), old)

    def test_mount_check_failure_is_logged_before_any_s3_request(self):
        args = ['--config', str(self.root / 'not-read.json'), '--nas-root', str(self.nas), '--apply',
                '--mount-root', str(self.root / 'nas')]
        with patch('deploy.sync_final_timelines.os.path.ismount', return_value=False), \
                patch('deploy.sync_final_timelines.R2Store') as store, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(args), 1)
        store.assert_not_called()
        self.assertIn('NAS 挂载检查失败', errors.getvalue())

    def test_restart_after_marker_capture_recovers_using_persisted_journal(self):
        from deploy.sync_final_timelines import guarded_write
        self.sync(apply=True)
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))

        def crash(path, data, expected, backup=None):
            if path.name == MARKER:
                os.rename(path, backup)
                raise OSError('fixture process died after marker capture')
            guarded_write(path, data, expected, backup)

        with patch('deploy.sync_final_timelines.guarded_write', side_effect=crash):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        self.assertFalse((self.target / MARKER).exists())
        self.assertTrue((self.target / PENDING).exists())
        self.store.publish(version='v3', accepted_at='2026-10-21T03:00:00Z', axes=('posture', 'scene'))
        self.assertEqual(self.sync(apply=True)['copy'], 2)
        self.assertEqual((self.target / MARKER).read_bytes(), self.store.objects[DIRECTORY + MARKER])
        self.assertFalse((self.target / PENDING).exists())

    def test_restart_after_completed_marker_only_cleans_pending_record(self):
        unlink = Path.unlink
        pending = self.target / PENDING

        def crash(path, *args, **kwargs):
            if path == pending:
                raise OSError('fixture process died before journal cleanup')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', crash):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        self.assertTrue(pending.is_file())
        self.assertTrue((self.target / MARKER).is_file())
        self.assertEqual(self.sync(apply=True)['copy'], 0)
        self.assertFalse(pending.exists())

    def test_unknown_edit_is_rejected_while_own_update_is_pending(self):
        from deploy.sync_final_timelines import guarded_write
        self.sync(apply=True)
        self.store.publish(version='v2', accepted_at='2026-10-21T02:00:00Z', axes=('posture', 'scene'))

        def interrupt(path, data, expected, backup=None):
            if path.name == 'scene.timeline.json':
                raise OSError('fixture interrupted update')
            guarded_write(path, data, expected, backup)

        with patch('deploy.sync_final_timelines.guarded_write', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        manual = b'{"fixture":"unknown edit while pending"}'
        (self.target / 'posture.timeline.json').write_bytes(manual)
        self.store.publish(version='v3', accepted_at='2026-10-21T03:00:00Z', axes=('posture', 'scene'))
        with self.assertRaisesRegex(SyncError, '人工修改'):
            self.sync(apply=True)
        self.assertEqual((self.target / 'posture.timeline.json').read_bytes(), manual)

    def test_older_source_is_refused_in_preview_and_apply(self):
        self.sync(apply=True)
        marker = (self.target / MARKER).read_bytes()
        self.store.publish(version='older', accepted_at='2026-10-20T01:00:00Z')
        for apply in (False, True):
            with self.assertRaisesRegex(SyncError, '早于 NAS'):
                self.sync(apply=apply)
        self.assertEqual((self.target / MARKER).read_bytes(), marker)

    def test_missing_collection_is_not_created(self):
        capture = 'daily/20261022/1022test'
        self.store.publish(capture)
        with self.assertRaisesRegex(SyncError, '找不到对应采集目录'):
            sync_capture(self.store, self.nas, capture, apply=True)
        self.assertFalse((self.nas / '20261022').exists())

    def test_existing_nas_hierarchy_is_used_without_imposing_s3_dates(self):
        direct = self.nas / '1021张三'
        self.collection.rename(direct)
        result = self.sync(apply=True)
        self.assertEqual(result['target'], str(direct / 'timeline' / 'finally'))
        self.assertTrue((direct / 'timeline' / 'finally' / 'posture.timeline.json').is_file())
        self.assertFalse(self.target.exists())

    def test_ambiguous_nas_names_are_never_written(self):
        other = self.nas / 'existing' / '1021张三'
        other.mkdir(parents=True)
        with self.assertRaisesRegex(SyncError, '多个同名采集目录'):
            self.sync(apply=True)
        self.assertFalse(self.target.exists())
        self.assertFalse((other / 'timeline').exists())

    def test_source_binding_prevents_another_year_from_reusing_target(self):
        self.sync(apply=True)
        another = 'daily/20271021/1021张三'
        self.store.publish(another, version='other-year', accepted_at='2027-10-21T01:00:00Z')
        with self.assertRaisesRegex(SyncError, '已绑定其他 S3'):
            sync_capture(self.store, self.nas, another, apply=True)

    def test_ambiguous_s3_names_fail_both_instead_of_overwriting_nas(self):
        self.store.publish('daily/20271021/1021张三')
        args = ['--config', str(self.root / 'not-read.json'), '--nas-root', str(self.nas), '--apply',
                '--state-dir', str(self.root / 'state')]
        with patch('deploy.sync_final_timelines.R2Store', return_value=self.store), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 1)
        summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary['failed'], 2)
        self.assertEqual(summary['copy'], 0)
        self.assertFalse(self.target.exists())

    def test_lock_excludes_duplicate_runs(self):
        state = self.root / 'state'
        with run_lock(state, self.nas):
            with self.assertRaisesRegex(SyncError, '已有同步进程'):
                with run_lock(state, self.nas):
                    self.fail('acquired duplicate lock')
        with run_lock(state, self.nas):
            pass

    def test_cli_reports_partial_failure_and_preview_never_creates_state(self):
        self.store.publish('daily/20261022/missing')
        state = self.root / 'state'
        args = ['--config', str(self.root / 'secret-not-read.json'), '--nas-root', str(self.nas),
                '--state-dir', str(state)]
        with patch('deploy.sync_final_timelines.R2Store', return_value=self.store), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(args), 1)
        summary = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(summary['collections'], 2)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['copy'], 2)
        self.assertFalse(state.exists())
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
