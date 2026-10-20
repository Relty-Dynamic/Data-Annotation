"""Algorithm PR #5 contract regression tests with isolated files and task storage."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.prediction_import import convert, warning
from backend.prediction_metadata import metadata
from backend.prediction_prepare import PredictionFailure
from backend.r2_folder import scan_collection, FolderUploadManager
from backend import test_prediction as fixtures
from backend.test_prediction import document, project, v13_document


def add_mounting(doc, *, review=True):
    ids = []
    for record in doc['recordings']:
        record.update(needs_mounting_review=review,
                      leg_mounting_method='short_duration_consistency' if review else 'legacy_sample_count',
                      leg_mounting_version='1.0.0')
        if review:
            ids.append(record['id'])
    doc['provenance']['upload_batch']['needs_mounting_review_record_ids'] = ids
    return doc


def mounting_diagnostic(**changes):
    return dict(code='LEG_MOUNTING_EVIDENCE_INSUFFICIENT', stage='leg.mounting',
                near_vertical_samples=80, required_near_vertical_samples=500,
                qualified_near_vertical_samples=75, qualified_covered_seconds=.08,
                support_bins=0, sign_agreement=1., rejection_reason='insufficient_time_coverage',
                **changes)


def add_failure(doc):
    row = doc['provenance']['upload_batch']['excluded_records'][0]
    row.update(reason='inference_failed', diagnostics=[mounting_diagnostic(private_path='/private/worker')])
    return doc


class MountingContractTests(unittest.TestCase):
    def test_mounting_review_is_independent_of_confirmed_alignment_and_preserves_coordinates(self):
        doc = add_mounting(v13_document(project()))
        rows, info = convert(json.dumps(doc).encode(), project())
        self.assertFalse(info['needs_alignment_review'])
        self.assertTrue(info['needs_mounting_review'])
        self.assertTrue(info['needs_review'])
        self.assertEqual(info['mounting_records'][0], dict(
            record_id=doc['recordings'][0]['id'], path='IMU/session.txt',
            needs_mounting_review=True, method='short_duration_consistency', version='1.0.0'))
        self.assertIn('腿部安装方向需要人工复核', warning(info))
        self.assertIn('IMU/session.txt', warning(info))
        self.assertNotIn('校时', warning(info))
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in rows], [(0, 3000), (5000, 8000)])

    def test_old_producers_and_legacy_mounting_path_do_not_gain_false_warnings(self):
        for doc in (json.loads(document(project())), v13_document(project()),
                    add_mounting(v13_document(project()), review=False)):
            info = metadata(doc)
            self.assertFalse(info['needs_mounting_review'])
            self.assertFalse(info['needs_review'])
            self.assertIsNone(warning(info))

    def test_malformed_or_suppressed_mounting_flags_reject_import(self):
        cases = []
        for key, value in [('needs_mounting_review', False), ('needs_mounting_review', 'true'),
                           ('leg_mounting_version', '9.0.0'), ('leg_mounting_method', 'inherited')]:
            doc = add_mounting(v13_document(project()))
            doc['recordings'][0][key] = value
            cases.append(doc)
        for values in ([], ['other'], [1], None):
            doc = add_mounting(v13_document(project()))
            doc['provenance']['upload_batch']['needs_mounting_review_record_ids'] = values
            cases.append(doc)
        doc = add_mounting(v13_document(project()))
        del doc['recordings'][0]['needs_mounting_review']
        cases.append(doc)
        doc = add_mounting(v13_document(project()))
        del doc['provenance']['upload_batch']['needs_mounting_review_record_ids']
        cases.append(doc)
        doc = add_mounting(v13_document(project()))
        doc['export_profile'] = 'imu_fusion_prediction_timeline/1.2.0'
        cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                convert(json.dumps(doc).encode(), project())

    def test_partial_diagnostics_and_both_review_reasons_are_visible(self):
        doc = add_failure(add_mounting(v13_document(project(), partial=True, inherited=True)))
        info = metadata(doc)
        text = warning(info)
        for expected in ('IMU/short.txt', '80/500', '0.08 秒', '支持窗口 0', '方向一致性',
                         '有效近竖直覆盖时长不足', '同日其他记录', '腿部安装方向需要人工复核'):
            self.assertIn(expected, text)
        self.assertNotIn('/private/worker', json.dumps(info))
        self.assertEqual(info['excluded_records'][0]['diagnostics'][0]['near_vertical_samples'], 80)
        doc['provenance']['upload_batch']['excluded_records'][0]['diagnostics'][0]['rejection_reason'] = 'conflicting_direction_evidence'
        self.assertIn('安装方向证据冲突', warning(metadata(doc)))

    def test_diagnostics_reject_invalid_numbers_and_fields_instead_of_hiding_them(self):
        for key, value in [('near_vertical_samples', True), ('support_bins', -1),
                           ('qualified_covered_seconds', float('nan')), ('sign_agreement', 1.1),
                           ('rejection_reason', '/private/error'), ('stage', 'unknown'),
                           ('code', '<script>')]:
            doc = add_failure(v13_document(project(), partial=True))
            doc['provenance']['upload_batch']['excluded_records'][0]['diagnostics'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                metadata(doc)


class MountingUploadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DesktopPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def set_document(self, doc):
        f = self.fixture
        f.raw = json.dumps(doc).encode()
        f.result.update(size_bytes=len(f.raw), sha256=hashlib.sha256(f.raw).hexdigest(),
                        needs_mounting_review=True, needs_review=True)

    def test_downloaded_result_retains_review_and_safe_partial_diagnostics(self):
        f = self.fixture
        self.set_document(add_failure(add_mounting(f.use_v13(partial=True))))
        _, report = f.prepare(scan_collection(f.capture))
        self.assertTrue(report['needs_mounting_review'])
        self.assertEqual(report['mounting_records'][0]['path'], 'IMU/session.txt')
        self.assertIn('80/500', report['excluded_records'][0]['reason_detail'])
        self.assertNotIn('/private/worker', json.dumps(report))
        self.assertEqual((f.capture/'prediction/posture.json').read_bytes(), f.raw)
        manager = FolderUploadManager(SimpleNamespace(capture_exists=lambda _: False), f.root/'staging')
        selection = manager.select_folder('fixture-user', f.capture)
        job = manager.start(dict(id='fixture-user', username='fixture', display_name='fixture'),
                            'fixture', None, None, selection['selection_id'])
        manager.jobs[job['id']]['prediction_result'] = report
        visible = manager.status(job['id'], 'fixture-user')['prediction']
        self.assertEqual(visible['mounting_records'], report['mounting_records'])
        self.assertTrue(visible['needs_mounting_review'])
        self.assertIn('80/500', visible['excluded_records'][0]['reason_detail'])
        with self.assertRaises(HTTPException):
            manager.status(job['id'], 'other-user')

    def test_job_artifact_review_disagreement_never_publishes(self):
        f = self.fixture
        self.set_document(add_mounting(f.use_v13()))
        for value in (False, 'true'):
            f.result['needs_mounting_review'] = value
            with self.subTest(value=value), self.assertRaises(HTTPException):
                f.prepare(scan_collection(f.capture))
            self.assertFalse((f.capture/'prediction/posture.json').exists())

    def failed_job(self, diagnostic=None, path='IMU/session.txt'):
        f = self.fixture
        original = f.request
        def request(method, url, **kwargs):
            job = original(method, url, **kwargs)
            if not url.endswith('capabilities'):
                job.update(state='failed', error='generic error',
                    error_details=[dict(record_id=hashlib.sha256(path.encode()).hexdigest()[:16],
                        path=path, reason='inference_failed', diagnostics=[diagnostic or mounting_diagnostic()])])
            return job
        f.target.request = request
        with self.assertRaises(HTTPException) as error:
            f.prepare()
        self.assertFalse((f.capture/'prediction/posture.json').exists())
        self.assertIn('b'*64, error.exception.detail)
        return error.exception.detail

    def test_all_failed_diagnostics_show_reason_counts_and_job_id(self):
        text = self.failed_job(mounting_diagnostic(private_path='/private/worker'))
        for expected in ('IMU/session.txt', '80/500', '0.08 秒', '有效近竖直覆盖时长不足'):
            self.assertIn(expected, text)
        self.assertNotIn('/private/worker', text)

    def test_all_failed_rejects_unrelated_input_and_invalid_diagnostics(self):
        self.assertIn('诊断无效', self.failed_job(path='IMU/other.txt'))
        diagnostic = mounting_diagnostic()
        diagnostic['qualified_covered_seconds'] = float('inf')
        self.assertIn('诊断无效', self.failed_job(diagnostic))

    def test_many_failures_complete_agent_command_and_relay_every_diagnostic(self):
        from backend.upload_agent import AgentEngine
        from backend.upload_bridge import UploadBridge
        from backend.test_r2_integration import USER
        f = self.fixture
        paths = ['IMU/session.txt'] + [f'IMU/record-{index:04d}.txt' for index in range(63)]
        for path in paths[1:]:
            (f.capture/path).write_bytes(b'explicit-IMU-fixture')
        rows = [dict(record_id=hashlib.sha256(path.encode()).hexdigest()[:16], path=path,
                     reason='inference_failed', diagnostics=[mounting_diagnostic()]) for path in paths]
        failure = PredictionFailure(dict(id='b'*64, error_details=rows), [dict(path=path) for path in paths])
        self.assertLessEqual(len(failure.detail), 2000)
        self.assertIn('共 64 条', failure.detail)
        self.assertIn('b'*64, failure.detail)
        for pipeline in (False, True):
            with self.subTest(pipeline=pipeline):
                local = f.root/f'agent-{pipeline}'
                local.mkdir()
                engine = AgentEngine(f.root, SimpleNamespace(), service=SimpleNamespace(local=local))
                engine.target.request = lambda *args, **kwargs: USER
                engine.target.capture_exists = lambda _: False
                engine.target.pipeline_enabled = pipeline
                engine.selection = engine.manager.select_folder(USER['id'], f.capture)
                engine.target.attachments = engine.selection['attachments']
                bridge = UploadBridge(SimpleNamespace(request=lambda *args, **kwargs: {'user': USER}))
                registered = bridge.register(dict(platform='darwin', protocol=1))
                bridge.pair(USER, registered, 'explicit-fixture-session')
                value = bridge.agents[registered['id']]
                bridge.command(USER, value['id'], dict(action='start', fields=dict(
                    collector='fixture', prediction='generate', chest_device='chest', thigh_device='thigh')))
                command = bridge.poll(value, {})['command']
                with patch('backend.prediction_prepare.prepare_prediction', side_effect=failure), \
                        patch.object(engine.manager, '_compress_video', side_effect=failure):
                    engine.execute(command)
                report = engine.snapshot()
                bridge.poll(value, dict(report=report, completed=engine.completed))
                visible = bridge.public(value)
                self.assertTrue(visible['online'])
                self.assertIsNone(visible['command'])
                self.assertFalse(visible['report']['busy'])
                self.assertLessEqual(len(visible['report']['error']), 2000)
                self.assertEqual(visible['report']['job']['state'], 'error')
                details = visible['report']['job']['prediction_failure']
                self.assertEqual(details['job_id'], 'b'*64)
                self.assertEqual([row['path'] for row in details['records']], paths)
                self.assertIn('80/500', details['records'][-1]['reason_detail'])
                bridge.command(USER, value['id'], dict(action='start'))

    def test_long_unicode_paths_bound_diagnostics_and_acknowledge_relay_command(self):
        from backend.upload_bridge import UploadBridge
        from backend.test_r2_integration import USER
        paths = ['IMU/' + '/'.join(['采' * 50] * 6) + f'/{index}.txt' for index in range(64)]
        rows = [dict(record_id=hashlib.sha256(path.encode()).hexdigest()[:16], path=path,
                     reason='inference_failed', diagnostics=[mounting_diagnostic()]) for path in paths]
        failure = PredictionFailure(dict(id='b'*64, error_details=rows), [dict(path=p) for p in paths])
        preview = failure.prediction_failure
        self.assertLessEqual(len(json.dumps(preview)), 64 * 1024)
        self.assertEqual(preview['total_records'], 64)
        self.assertGreater(preview['omitted_records'], 0)
        self.assertEqual(len(preview['records']) + preview['omitted_records'], 64)
        self.assertEqual(preview['records'][0]['path'], paths[0])
        bridge = UploadBridge(SimpleNamespace(request=lambda *args, **kwargs: {'user': USER}))
        registered = bridge.register(dict(platform='darwin', protocol=1))
        bridge.pair(USER, registered, 'explicit-fixture-session')
        value = bridge.agents[registered['id']]
        bridge.command(USER, value['id'], dict(action='start'))
        command = bridge.poll(value, {})['command']
        report = dict(busy=False, error=failure.detail,
                      selection=dict(name='fixture', capture_date='20261019',
                                     videos=[], attachments=paths, ignored=[]),
                      job=dict(state='error', detail=failure.detail, prediction_failure=preview))
        bridge.poll(value, dict(report=report, completed=command['id']))
        self.assertIsNone(bridge.public(value)['command'])
        self.assertTrue(bridge.public(value)['online'])


class MountingTaskTests(unittest.TestCase):
    def test_mounting_and_diagnostics_persist_after_human_clear_save_and_reopen(self):
        fixture = fixtures.TaskPredictionTests()
        self.addCleanup(fixture.doCleanups)
        f = fixture.fixture(review=False)
        doc = add_failure(add_mounting(v13_document(f.media_service.load(f.project['id']), partial=True)))
        doc['segments'] = [dict(start_ms=0, end_ms=500, label='站')]
        (Path(f.project['source_dir'])/'prediction/posture.json').write_text(json.dumps(doc))
        task = f.create()
        opened = f.workspace.public(f.a, task['id'])
        info = opened['prediction_import']
        self.assertTrue(info['needs_mounting_review'])
        self.assertTrue(any('腿部安装方向需要人工复核' in text and '80/500' in text for text in opened['warnings']))
        saved = f.store.get_task(f.a, task['id'])
        f.store.save_draft(f.a, task['id'], dict(expected_revision=saved['revision'], annotations={'posture': []}))
        reopened = f.workspace.public(f.a, task['id'])
        self.assertEqual(reopened['annotations']['posture'], [])
        self.assertEqual(reopened['prediction_import'], info)
        self.assertEqual(reopened['warnings'], opened['warnings'])
