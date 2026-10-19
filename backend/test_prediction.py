"""Prediction integration with isolated transport, files and task databases."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from backend.prediction_gateway import Gateway
from backend.prediction_import import convert, epoch_ms, initialize, warning
from backend.prediction_metadata import metadata as prediction_metadata
from backend.prediction_prepare import _publish
from backend.prediction_prepare import prepare_prediction
from backend.r2_folder import scan_collection
from backend import test_collection_tasks as collection_fixtures


def document(project, *, label='站', review=False):
    return json.dumps(dict(axis='posture', export_profile='imu_fusion_prediction_timeline/1.2.0',
        timebase=dict(unit='ms', clock='fpv_camera', recording_start=project['recording_start'], duration_ms=project['duration_ms']),
        segments=[dict(start_ms=0, end_ms=project['duration_ms'], label=label, kind='interval')],
        recordings=[dict(needs_alignment_review=review, camera_minus_imu_offset_s=150)],
        provenance=dict(version='0.6.0', inputs=[dict(path='IMU/session.txt', sha256=hashlib.sha256(b'explicit-IMU-fixture').hexdigest())], videos=[dict(name=Path(v['name']).stem + '.avi',
            recording_start=v.get('filename_recording_start', v['recording_start']), duration_ms=v['duration_ms'])
            for v in project['videos']]))).encode()


def project():
    return dict(id='fixture', source_fingerprint='source-fixture', duration_ms=8000,
        recording_start='2026-10-15T12:00:00+08:00', fixed_tracks=['posture'], selected_tracks=['posture'],
        track_labels={'posture': ['坐', '站', '躺', '移动']}, annotations={'posture': []},
        videos=[dict(id='v0001', name='A00001_20261015120000_0001.mp4',
                     recording_start='2026-10-15T12:00:00+08:00', start_ms=0, end_ms=3000, duration_ms=3000),
                dict(id='v0002', name='A00001_20261015120005_0002.mp4',
                     recording_start='2026-10-15T12:00:05+08:00', start_ms=5000, end_ms=8000, duration_ms=3000)])


def v13_document(p, *, partial=False, inherited=False):
    doc = json.loads(document(p))
    doc['export_profile'] = 'imu_fusion_prediction_timeline/1.3.0'
    first = hashlib.sha256(b'IMU/session.txt').hexdigest()[:16]
    doc['provenance']['inputs'][0]['record_id'] = first
    doc['recordings'][0].update(id=first, alignment_status='signal_supported')
    excluded = []
    if partial:
        path = 'IMU/short.txt'
        excluded = [dict(record_id=hashlib.sha256(path.encode()).hexdigest()[:16], path=path, reason='alignment_unresolved')]
        doc['segments'] = [dict(start_ms=1000, end_ms=2000, label='坐')]
    if inherited:
        path = 'IMU/later.txt'; donor = hashlib.sha256(path.encode()).hexdigest()[:16]
        doc['provenance']['inputs'].append(dict(path=path, sha256='a'*64, record_id=donor))
        doc['recordings'].append(dict(id=donor, needs_alignment_review=False, alignment_status='signal_supported'))
        doc['recordings'][0].update(alignment_status='same_day_inherited', alignment_source_record_id=donor,
                                   needs_alignment_review=True)
    doc['provenance']['upload_batch'] = dict(batch_status='partial' if partial else 'complete',
        validated_records=len(doc['recordings']), total_records=len(doc['recordings'])+len(excluded),
        excluded_records=excluded, excluded_record_ids=[r['record_id'] for r in excluded],
        clock_group='confirmed-current-collection' if inherited else None)
    return doc


class ImportTests(unittest.TestCase):
    def test_v13_partial_import_preserves_blank_ranges_and_exclusion_warning(self):
        p = project(); doc = v13_document(p, partial=True)
        rows, info = convert(json.dumps(doc).encode(), p)
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in rows], [(1000, 2000)])
        self.assertEqual(info['batch_status'], 'partial')
        self.assertTrue(info['needs_alignment_review'])
        self.assertIn('IMU/short.txt', warning(info))
        self.assertIn('1/2', warning(info))

    def test_v13_inherited_alignment_remains_visible_without_reapplying_offset(self):
        p = project(); doc = v13_document(p, inherited=True)
        rows, info = convert(json.dumps(doc).encode(), p)
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in rows], [(0, 3000), (5000, 8000)])
        self.assertEqual(len(info['inherited_record_ids']), 1)
        self.assertIn('同日其他记录', warning(info))

    def test_v13_complete_without_review_and_legacy_are_both_supported(self):
        for raw in (document(project()), json.dumps(v13_document(project())).encode()):
            rows, info = convert(raw, project())
            self.assertEqual(len(rows), 2)
            self.assertIsNone(warning(info))

    def test_new_batch_metadata_cannot_be_disguised_as_legacy_format(self):
        doc = v13_document(project(), partial=True)
        doc['export_profile'] = 'imu_fusion_prediction_timeline/1.2.0'
        with self.assertRaises(ValueError):
            convert(json.dumps(doc).encode(), project())

    def test_v13_inconsistent_partial_metadata_and_unreviewed_inheritance_are_rejected(self):
        cases = []
        for key, value in [('batch_status', 'complete'), ('total_records', 3), ('validated_records', True),
                           ('excluded_record_ids', []), ('excluded_records', [])]:
            doc = v13_document(project(), partial=True); doc['provenance']['upload_batch'][key] = value; cases.append(doc)
        doc = v13_document(project(), partial=True); doc['provenance']['upload_batch']['excluded_records'][0]['path'] = '/private/short.txt'; cases.append(doc)
        doc = v13_document(project(), inherited=True); doc['recordings'][0]['needs_alignment_review'] = False; cases.append(doc)
        doc = v13_document(project(), inherited=True); doc['recordings'][0]['alignment_source_record_id'] = 'unknown'; cases.append(doc)
        doc = v13_document(project()); doc['export_profile'] = 'imu_fusion_prediction_timeline/9.0.0'; cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                convert(json.dumps(doc).encode(), project())

    def test_intersects_video_coverage_without_filling_real_gaps_or_applying_imu_offset_again(self):
        p = project()
        rows, metadata = convert(document(p, review=True), p)
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in rows], [(0, 3000), (5000, 8000)])
        self.assertTrue(metadata['needs_alignment_review'])
        self.assertEqual(rows, convert(document(p, review=True), p)[0])

    def test_partial_json_leaves_unpredicted_video_blank(self):
        p = project(); doc = json.loads(document(p))
        doc['segments'] = [dict(start_ms=1000, end_ms=2000, label='坐')]
        rows, _ = convert(json.dumps(doc).encode(), p)
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in rows], [(1000, 2000)])

    def test_video_seam_shift_is_applied_once_from_each_clips_nominal_start(self):
        p = project()
        p['videos'][0].update(end_ms=3250, duration_ms=3250)
        p['videos'][1].update(filename_recording_start='2026-10-15T12:00:03+08:00',
            recording_start='2026-10-15T12:00:03.250+08:00', start_ms=3250, end_ms=6250,
            name='A00001_20261015120003_0002.mp4', alignment_offset_ms=250)
        raw = json.loads(document(p)); raw['segments'] = [dict(start_ms=3000, end_ms=4000, label='站')]
        rows, _ = convert(json.dumps(raw).encode(), p)
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in rows], [(3000, 3250), (3250, 4250)])

    def test_invalid_labels_overlaps_clock_and_material_mismatch_reject_entire_import(self):
        p = project(); cases = []
        d = json.loads(document(p)); d['segments'][0]['label'] = 'unexpected'; cases.append(d)
        d = json.loads(document(p)); d['segments'].append(dict(d['segments'][0])); cases.append(d)
        d = json.loads(document(p)); d['timebase']['clock'] = 'imu'; cases.append(d)
        d = json.loads(document(p)); d['provenance']['videos'][0]['duration_ms'] += 251; cases.append(d)
        d = json.loads(document(p)); d['provenance']['videos'][0]['recording_start'] = '2026-10-15T12:00:01+08:00'; cases.append(d)
        d = json.loads(document(p)); d['segments'][0]['start_ms'] = True; cases.append(d)
        for d in cases:
            with self.subTest(d=d), self.assertRaises(ValueError):
                convert(json.dumps(d).encode(), p)
        self.assertEqual(p['annotations']['posture'], [])

    def test_existing_human_data_empty_saved_draft_and_formal_output_take_precedence(self):
        for changes in ({'draft_dirty': True}, {'last_writeback': {'save_id': 'human'}},
                        {'annotations': {'posture': [], 'scene': [dict(label='室内')]}},
                        {'_external_hashes': {'posture': 'human-hash'}}):
            p = {**project(), **changes, '_prediction_eligible': True}
            source = SimpleNamespace(read_prediction=lambda _: self.fail('Human draft must not read prediction'))
            initialize(source, p)
            self.assertNotIn('prediction_import', p)

    def test_initialization_is_once_even_after_user_clears_prediction(self):
        p = {**project(), '_prediction_eligible': True}
        source = SimpleNamespace(read_prediction=lambda _: document(p))
        initialize(source, p)
        self.assertEqual(p['prediction_import']['state'], 'applied')
        p['annotations']['posture'] = []; p['draft_dirty'] = False
        initialize(source, p)
        self.assertEqual(p['annotations']['posture'], [])

    def test_download_publication_never_overwrites_and_failed_read_has_visible_status(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'prediction/posture.json'
            _publish(path, b'{"fixture":1}')
            _publish(path, b'{"fixture":1}')
            with self.assertRaises(HTTPException):
                _publish(path, b'{"fixture":2}')
            self.assertEqual(path.read_bytes(), b'{"fixture":1}')
        p = {**project(), '_prediction_eligible': True}
        initialize(SimpleNamespace(read_prediction=lambda _: b'not JSON'), p)
        self.assertEqual(p['prediction_import']['state'], 'rejected')
        self.assertEqual(p['annotations']['posture'], [])


class GatewayTests(unittest.TestCase):
    def test_disabled_service_and_upload_permission_guard(self):
        gateway = Gateway()
        response = asyncio.run(gateway.forward({'id': 'a', 'can_upload': True}, 'GET', 'capabilities'))
        self.assertEqual(json.loads(response.body), {'enabled': False})
        with self.assertRaises(HTTPException) as error:
            asyncio.run(gateway.forward({'id': 'a', 'can_upload': False}, 'POST', 'jobs'))
        self.assertEqual(error.exception.status_code, 403)

    def test_worker_credential_is_server_only_and_owner_is_derived_from_session(self):
        with tempfile.TemporaryDirectory() as raw:
            secret = Path(raw) / 'credential'; secret.write_text('fixture-worker-credential-not-a-real-secret')
            seen = []
            def worker(request):
                seen.append(request)
                return httpx.Response(200, json={'protocol': 1})
            gateway = Gateway('http://worker:8778', secret, transport=httpx.MockTransport(worker))
            response = asyncio.run(gateway.forward({'id': 'owner-a', 'can_upload': True}, 'GET', 'capabilities'))
            self.assertEqual(seen[0].headers['X-Prediction-Owner'], 'owner-a')
            self.assertNotIn(secret.read_bytes(), response.body)
            self.assertEqual(json.loads(response.body)['enabled'], True)

    def test_upstream_auth_and_transport_failures_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as raw:
            secret = Path(raw) / 'credential'; secret.write_text('fixture-worker-credential-not-a-real-secret')
            gateway = Gateway('http://worker:8778', secret, transport=httpx.MockTransport(
                lambda r: httpx.Response(401, json={'detail': str(secret)})))
            with self.assertRaises(HTTPException) as error:
                asyncio.run(gateway.forward({'id': 'a', 'can_upload': True}, 'GET', 'capabilities'))
            self.assertEqual(error.exception.status_code, 503)
            self.assertNotIn(str(secret), error.exception.detail)


class DesktopPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.capture = self.root / '1015fixture'
        (self.capture / 'FPV').mkdir(parents=True); (self.capture / 'IMU').mkdir()
        self.video = self.capture / 'FPV/A00001_20261015120000_0001.avi'
        self.video.write_bytes(b'explicit-video-fixture-sampler-mocked')
        (self.capture / 'IMU/session.txt').write_bytes(b'explicit-IMU-fixture-worker-mocked')
        self.selection = scan_collection(self.capture)
        self.raw = document(project())
        self.result = dict(size_bytes=len(self.raw), sha256=hashlib.sha256(self.raw).hexdigest(), segments=1, needs_alignment_review=True)
        self.calls = []
        self.manifests = []
        self.target = SimpleNamespace(request=self.request, response=self.response, attachments=[])
        self.module = SimpleNamespace(probe=lambda path: dict(name=path.name, start=epoch_ms('2026-10-15T12:00:00+08:00') / 1000, duration=8))
        self.patcher = patch('backend.prediction_prepare.sampler', return_value=self.module)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if path.endswith('capabilities'):
            return dict(enabled=True, implementation_sha256='a' * 64)
        if method == 'POST' and path == '/api/prediction/jobs':
            self.manifests.append(kwargs['json'])
        return dict(id='b' * 64, implementation_sha256='a' * 64, state='completed', missing_inputs=[], result=self.result)

    def response(self, method, path, **kwargs):
        return httpx.Response(200, content=self.raw)

    def prepare(self, selection=None, roles=None):
        return prepare_prediction(self.target, SimpleNamespace(local=self.root / 'cache'), self.capture,
            selection or self.selection, roles if roles is not None else dict(chest='chest-device', thigh='thigh-device'))

    def test_device_address_separators_produce_the_same_worker_manifest(self):
        expected = dict(chest='WT901BLE68(F0:46:23:95:C9:F0)', thigh='WT901BLE67(F9:07:E6:4C:66:D3)')
        for separator in (':', '_'):
            with self.subTest(separator=separator):
                roles = {role: ' ' + name.replace(':', separator) + ' ' for role, name in expected.items()}
                original = dict(roles)
                self.prepare(scan_collection(self.capture), roles)
                self.assertEqual(self.manifests[-1]['roles'], expected)
                self.assertEqual(roles, original)
        self.assertEqual(self.manifests[0], self.manifests[1])

    def test_same_device_with_different_separators_is_rejected_before_remote_calls(self):
        with self.assertRaises(HTTPException) as error:
            self.prepare(roles=dict(chest='WT901BLE67(F9_07_E6_4C_66_D3)',
                                    thigh='WT901BLE67(F9:07:E6:4C:66:D3)'))
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.capture / 'prediction/posture.json').exists())

    def test_device_names_keep_underscores_outside_the_address(self):
        roles = dict(chest='chest_sensor(F0_46_23_95_C9_F0)', thigh='thigh_sensor')
        self.prepare(roles=roles)
        self.assertEqual(self.manifests[-1]['roles'],
                         dict(chest='chest_sensor(F0:46:23:95:C9:F0)', thigh='thigh_sensor'))

    def test_new_prediction_is_rescanned_and_retries_reuse_identical_artifact(self):
        selected, _ = self.prepare()
        self.assertEqual({r['output'] for r in self.target.attachments}, {'IMU/session.txt', 'prediction/posture.json'})
        self.assertEqual((self.capture / 'prediction/posture.json').read_bytes(), self.raw)
        self.prepare(selected)
        self.assertFalse(any(path.endswith('/start') for _, path in self.calls))

    def test_changed_source_during_result_download_never_publishes_prediction(self):
        def changed(*args, **kwargs):
            self.video.write_bytes(b'changed during remote processing')
            return httpx.Response(200, content=self.raw)
        self.target.response = changed
        with self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse((self.capture / 'prediction/posture.json').exists())

    def test_existing_different_prediction_survives_remote_result(self):
        destination = self.capture / 'prediction/posture.json'
        destination.parent.mkdir(); destination.write_bytes(b'{"human_supplied_prediction":true}')
        with self.assertRaises(HTTPException):
            self.prepare(scan_collection(self.capture))
        self.assertEqual(destination.read_bytes(), b'{"human_supplied_prediction":true}')

    def use_v13(self, *, partial=False):
        doc = v13_document(project(), partial=partial)
        doc['provenance']['inputs'][0]['sha256'] = hashlib.sha256((self.capture / 'IMU/session.txt').read_bytes()).hexdigest()
        if partial:
            (self.capture / 'IMU/short.txt').write_bytes(b'explicit-short-IMU-fixture')
        self.raw = json.dumps(doc).encode()
        self.result = dict(size_bytes=len(self.raw), sha256=hashlib.sha256(self.raw).hexdigest(), segments=len(doc['segments']),
                           needs_alignment_review=partial, batch_status='partial' if partial else 'complete',
                           excluded_record_ids=prediction_metadata(doc)['excluded_record_ids'])
        return doc

    def test_v13_partial_keeps_all_original_inputs_and_reports_exclusions(self):
        self.use_v13(partial=True)
        original = (self.capture / 'IMU/short.txt').read_bytes()
        selected, result = self.prepare(scan_collection(self.capture))
        self.assertEqual(result['batch_status'], 'partial')
        self.assertEqual(result['excluded_records'][0]['path'], 'IMU/short.txt')
        self.assertEqual(result['job_id'], 'b'*64)
        self.assertEqual((self.capture / 'IMU/short.txt').read_bytes(), original)
        self.assertIn('IMU/short.txt', {r['output'] for r in selected['attachments']})

    def test_v13_job_file_disagreement_never_publishes(self):
        self.use_v13(partial=True)
        self.result['batch_status'] = 'complete'
        with self.assertRaises(HTTPException):
            self.prepare(scan_collection(self.capture))
        self.assertFalse((self.capture / 'prediction/posture.json').exists())

    def test_v13_changed_source_digest_is_rejected(self):
        doc = self.use_v13()
        doc['provenance']['inputs'][0]['sha256'] = 'f'*64
        self.raw = json.dumps(doc).encode(); self.result.update(size_bytes=len(self.raw), sha256=hashlib.sha256(self.raw).hexdigest())
        with self.assertRaises(HTTPException):
            self.prepare()
        self.assertFalse((self.capture / 'prediction/posture.json').exists())

    def test_clock_group_is_sent_only_after_explicit_confirmation_and_capability(self):
        original = self.target.request
        def capable(method, path, **kwargs):
            result = original(method, path, **kwargs)
            return {**result, 'same_day_clock_group_supported': True} if path.endswith('capabilities') else result
        self.target.request = capable
        prepare_prediction(self.target, SimpleNamespace(local=self.root/'cache'), self.capture, self.selection,
                           dict(chest='chest', thigh='thigh'), clock_group='confirmed-current-collection')
        self.assertEqual(self.manifests[-1]['clock_group'], 'confirmed-current-collection')
        self.target.request = original
        with self.assertRaises(HTTPException):
            prepare_prediction(self.target, SimpleNamespace(local=self.root/'cache'), self.capture, scan_collection(self.capture),
                               dict(chest='chest', thigh='thigh'), clock_group='confirmed-current-collection')

    def test_legacy_client_does_not_infer_clock_continuity(self):
        self.prepare()
        self.assertNotIn('clock_group', self.manifests[-1])

    def test_algorithm_failure_exposes_diagnostic_job_id_without_publishing(self):
        original = self.target.request
        def failed(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if path == '/api/prediction/jobs' or path.endswith('/start'):
                value.update(state='failed', error='fixture inference failed')
            return value
        self.target.request = failed
        with self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertIn('b'*64, error.exception.detail)
        self.assertFalse((self.capture / 'prediction/posture.json').exists())


class TaskPredictionTests(unittest.TestCase):
    def fixture(self, *, label='站', review=True):
        case = collection_fixtures.CollectionTaskTests('test_one_collection_one_task_complete_timeline_and_material_snapshot')
        case.setUp(); self.addCleanup(case.doCleanups)
        path = Path(case.project['source_dir']) / 'prediction/posture.json'
        imu = path.parent.parent / 'IMU/session.txt'; imu.parent.mkdir(); imu.write_bytes(b'explicit-IMU-fixture')
        path.parent.mkdir(); path.write_bytes(document(case.media_service.load(case.project['id']), label=label, review=review))
        return case

    def test_task_seeds_private_draft_once_and_preserves_user_cleared_empty_draft(self):
        f = self.fixture(); task = f.create()
        p = f.workspace.project(f.a, task['id'])
        self.assertEqual([(r['start_ms'], r['end_ms']) for r in p['annotations']['posture']], [(0, 1000), (2000, 3000)])
        stored = f.store.get_task(f.a, task['id'])
        revision = stored['revision']
        self.assertTrue(stored['draft']['prediction_import']['needs_alignment_review'])
        f.workspace.project(f.a, task['id'])
        self.assertEqual(f.store.get_task(f.a, task['id'])['revision'], revision)
        f.store.save_draft(f.a, task['id'], {'expected_revision': revision, 'annotations': {'posture': []}})
        self.assertEqual(f.workspace.project(f.a, task['id'])['annotations']['posture'], [])
        self.assertEqual(f.store.get_task(f.a, task['id'])['draft']['prediction_import']['state'], 'applied')

    def test_existing_draft_is_never_replaced_and_reviewer_does_not_get_new_seed(self):
        f = self.fixture(); task = f.create()
        human = {'posture': [dict(id='human', start_ms=0, end_ms=500, label='站', lane=1)]}
        f.store.save_draft(f.a, task['id'], {'expected_revision': task['revision'], 'annotations': human})
        self.assertEqual(f.workspace.project(f.a, task['id'])['annotations']['posture'], human['posture'])
        f.workspace.project(f.r, task['id'])
        self.assertIsNone(f.store.get_task(f.r, task['id'])['draft'])

    def test_rule_label_mismatch_is_visible_and_does_not_create_invalid_draft(self):
        f = self.fixture(label='坐'); task = f.create()
        p = f.workspace.public(f.a, task['id'])
        self.assertEqual(p['annotations']['posture'], [])
        self.assertEqual(p['prediction_import']['state'], 'rejected')
        self.assertTrue(p['warnings'])
        self.assertIsNone(f.store.get_task(f.a, task['id'])['draft'])

    def test_prediction_from_changed_imu_is_rejected_before_draft_initialization(self):
        f = self.fixture(); task = f.create()
        (Path(f.project['source_dir']) / 'IMU/session.txt').write_bytes(b'different-IMU')
        p = f.workspace.public(f.a, task['id'])
        self.assertEqual(p['annotations']['posture'], [])
        self.assertEqual(p['prediction_import']['state'], 'rejected')

    def test_partial_batch_warning_survives_task_draft_save_and_reopen(self):
        f = self.fixture(); p = f.media_service.load(f.project['id'])
        doc = v13_document(p, partial=True)
        doc['segments'] = [dict(start_ms=0, end_ms=500, label='站')]
        (Path(f.project['source_dir']) / 'prediction/posture.json').write_text(json.dumps(doc))
        task = f.create(); opened = f.workspace.public(f.a, task['id'])
        self.assertEqual(opened['prediction_import']['batch_status'], 'partial')
        self.assertTrue(any('IMU/short.txt' in text for text in opened['warnings']))
        saved = f.store.get_task(f.a, task['id'])
        self.assertEqual(saved['draft']['prediction_import']['excluded_records'][0]['path'], 'IMU/short.txt')
        f.store.save_draft(f.a, task['id'], dict(expected_revision=saved['revision'], annotations={'posture': []}))
        again = f.workspace.public(f.a, task['id'])
        self.assertEqual(again['annotations']['posture'], [])
        self.assertEqual(again['prediction_import']['batch_status'], 'partial')


if __name__ == '__main__':
    unittest.main()
