from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException

from backend.project_storage import ProjectStorage
from backend.remote import RemoteClient
from backend.session_cache import PROFILE, SessionCache
from backend.test_playback_session import FAST, MEDIA, ROOT, SHEET, THUMB


class RemoteClientTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='remote-client-test-', dir=ROOT / '.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        token = self.root / 'token'
        token.write_text('fixture-only-' + 'a' * 32, encoding='utf-8')
        self.client = RemoteClient(self.root, {
            'token_file': str(token),
            'mappings': [{'local': r'\\Relty\homes', 'remote': '/mnt/nas/homes'},
                         {'local': r'\\Relty\homes\special', 'remote': '/mnt/nas/datasets/special'}],
        })
        self.addCleanup(self.client.close)
        self.source = {
            'path': r'\\Relty\homes\data\A09999_20260917120000_0000.avi',
            'stamp': {'size': 12345, 'mtime_ns': 1234567890000000000,
                      'sample_sha256': hashlib.sha256(b'original-video').hexdigest()},
            'duration_ms': 1000, 'media_start_seconds': 0,
        }
        self.key = SessionCache._source_key(self.source)
        self.ident = 'a' * 32
        self.project = {
            'id': self.ident, 'source_fingerprint': 'fixture-fingerprint',
            '_sources': {'v0001': self.source},
            'videos': [{'id': 'v0001', 'name': 'A09999_20260917120000_0000.avi', 'duration_ms': 1000}],
        }
        self.storage = ProjectStorage(self.root / '.local')
        self.directory = self.storage.directory(self.ident) / 'playback'
        self.directory.mkdir(parents=True)
        self.service = SimpleNamespace(
            storage=self.storage, remote=self.client,
            load=Mock(side_effect=lambda ident: copy.deepcopy(self.project)),
            source_available=Mock(side_effect=AssertionError('Unexpected NAS source check')),
            preview_spec=Mock(side_effect=AssertionError('Unexpected original preview lookup')),
            probe=Mock(side_effect=AssertionError('Unexpected local probe')),
            tool=Mock(side_effect=AssertionError('Unexpected local FFmpeg lookup')),
        )
        self.sessions = SessionCache(self.service)
        self.addCleanup(self.sessions.close)
        self.updates = []
        self.stopping = threading.Event()

    def archive(self, mutate=None):
        contents = {'normal.mp4': MEDIA, 'fast.mp4': FAST, 'thumbnail.jpg': THUMB, 'sheet-0.jpg': SHEET}
        def record(name):
            data = contents[name]
            return {'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        manifest = {
            'protocol': 1, 'profile': PROFILE, 'source_stamp': copy.deepcopy(self.source['stamp']),
            'duration_ms': self.source['duration_ms'], 'media_start_seconds': 0,
            'storyboard': SessionCache._storyboard_metadata(self.source['duration_ms']),
            'files': {'normal': record('normal.mp4'), 'fast': record('fast.mp4'),
                      'thumbnail': record('thumbnail.jpg')},
            'sheets': [record('sheet-0.jpg')],
        }
        if mutate:
            mutate(manifest, contents)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            for name, data in contents.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    @contextmanager
    def mock_transport(self, responder, *, async_responder=None):
        with patch.object(self.client, '_ensure_tunnel'), patch.object(
                self.client, '_http', side_effect=lambda **kwargs: httpx.Client(
                    base_url=self.client.base_url, transport=httpx.MockTransport(responder))) as sync_http, patch.object(
                self.client, '_async_http', side_effect=lambda **kwargs: httpx.AsyncClient(
                    base_url=self.client.base_url, transport=httpx.MockTransport(async_responder or responder))) as async_http:
            yield sync_http, async_http

    @contextmanager
    def worker(self, data, *, digest=None, initial_state='ready', override=None):
        requests = []
        status = {'id': 'b' * 64, 'state': 'ready',
                  'archive': {'size': len(data), 'sha256': digest or hashlib.sha256(data).hexdigest()}}
        def respond(request):
            requests.append(request)
            if override is not None:
                response = override(request)
                if response is not None:
                    return response
            if request.method == 'POST' and request.url.path == '/v1/jobs':
                body = json.loads(request.content)
                self.assertEqual(body['path'], '/mnt/nas/homes/data/A09999_20260917120000_0000.avi')
                self.assertEqual(body['profile'], PROFILE)
                return httpx.Response(200, json={**status, 'state': initial_state, 'progress': 10})
            if request.method == 'GET' and request.url.path == '/v1/jobs/' + status['id']:
                return httpx.Response(200, json=status)
            if request.method == 'GET' and request.url.path.endswith('/archive'):
                return httpx.Response(200, content=data)
            raise AssertionError('Unexpected worker request: ' + str(request.url))
        with self.mock_transport(respond):
            yield requests

    def prepare(self):
        return self.client.prepare(self.source, self.directory, self.key,
                                   lambda *args: self.updates.append(args), self.stopping)

    def seed_cache(self):
        with self.worker(self.archive()):
            return self.prepare()

    def snapshot_cache(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.directory.iterdir() if path.name.startswith(self.key)}

    def assert_local_assets(self, entry):
        self.assertEqual(self.sessions._load_entry(self.directory, self.key, self.source), entry)
        for record, expected in ((entry['normal'], MEDIA), (entry['fast'], FAST),
                                 (entry['thumbnail'], THUMB), (entry['sheets'][0], SHEET)):
            path = self.directory / record['path']
            self.assertTrue(path.resolve().is_relative_to(self.directory.resolve()))
            self.assertEqual(path.read_bytes(), expected)

    def run_session(self):
        self.sessions.start(self.ident)
        self.assertTrue(self.sessions._entries[self.ident]['done'].wait(5), 'Preparation did not finish')
        return self.sessions.status(self.ident)

    def configure_tunnel(self):
        directory = self.root / '.local' / 'remote'
        directory.mkdir(parents=True)
        for name in ('identity', 'known_hosts'):
            (directory / name).write_text('test-fixture-only', encoding='ascii')
        self.client.config.update(host='192.168.2.25', user='relty', port=18120,
                                  identity_file=str(directory / 'identity'),
                                  known_hosts_file=str(directory / 'known_hosts'))
        return directory / 'tunnel.log'

    @contextmanager
    def no_local_processing(self):
        original_stat = Path.stat
        original_open = Path.open
        def guard(original):
            def invoke(path, *args, **kwargs):
                if str(path).lower().startswith('\\\\relty\\'):
                    raise AssertionError('Local process accessed NAS: ' + str(path))
                return original(path, *args, **kwargs)
            return invoke
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'stat', guard(original_stat)))
            stack.enter_context(patch.object(Path, 'open', guard(original_open)))
            for function in ('run_ffmpeg', 'render_local_fast', 'render_local_thumbnail'):
                stack.enter_context(patch('backend.session_cache.' + function,
                                         side_effect=AssertionError('Unexpected local media processing')))
            stack.enter_context(patch.object(self.sessions._local_storyboards, 'render',
                                             side_effect=AssertionError('Unexpected local storyboard processing')))
            yield

    def test_unc_mapping_is_case_insensitive_and_uses_longest_matching_directory(self):
        self.assertEqual(self.client.map_path(r'\\rELTY\HOMES\Data\clip.avi'), '/mnt/nas/homes/Data/clip.avi')
        self.assertEqual(self.client.map_path('//relty/homes/special/clip.avi'), '/mnt/nas/datasets/special/clip.avi')
        self.assertEqual(self.client.map_path(r'\\Relty\homes'), '/mnt/nas/homes')
        for path in (r'\\Relty\homes-other\clip.avi', r'\\Other\homes\clip.avi',
                     r'C:\video\clip.avi', r'homes\clip.avi'):
            with self.subTest(path=path):
                self.assertIsNone(self.client.map_path(path))

    def test_unc_mapping_rejects_parent_traversal_and_alternate_streams(self):
        for path in (r'\\Relty\homes\..\secret.avi', r'\\Relty\homes\data\..\clip.avi',
                     r'\\Relty\homes\clip.avi:alternate'):
            with self.subTest(path=path), self.assertRaises(HTTPException) as error:
                self.client.map_path(path)
            self.assertEqual(error.exception.status_code, 422)

    def test_occupied_tunnel_port_never_sends_token_even_with_old_success_log(self):
        log = self.configure_tunnel()
        log.write_bytes(b'Local forwarding listening on 127.0.0.1 port 18121.\n')
        process = Mock()
        process.poll.side_effect = [None, 255, 255]
        def spawn(args, **kwargs):
            kwargs['stderr'].write(b'bind [127.0.0.1]:18121: Address already in use\n')
            kwargs['stderr'].flush()
            return process
        with patch('backend.remote.subprocess.Popen', side_effect=spawn), \
                patch('backend.remote.time.sleep'), patch.object(self.client, '_healthy') as healthy, \
                patch.object(self.client, '_http') as http, self.assertRaises(HTTPException) as error:
            self.client._ensure_tunnel()
        self.assertEqual(error.exception.status_code, 503)
        self.assertFalse(self.client._tunnel_ready)
        healthy.assert_not_called()
        http.assert_not_called()
        self.client._tunnel = None

    def test_ssh_exit_after_listener_marker_never_sends_token(self):
        self.configure_tunnel()
        process = Mock()
        process.poll.side_effect = [None, 255, 255]
        def spawn(args, **kwargs):
            kwargs['stderr'].write(b'Local forwarding listening on 127.0.0.1 port 18121.\n')
            kwargs['stderr'].flush()
            return process
        with patch('backend.remote.subprocess.Popen', side_effect=spawn), \
                patch('backend.remote.time.sleep'), patch.object(self.client, '_healthy') as healthy, \
                patch.object(self.client, '_http') as http, self.assertRaises(HTTPException) as error:
            self.client._ensure_tunnel()
        self.assertEqual(error.exception.status_code, 503)
        healthy.assert_not_called()
        http.assert_not_called()
        self.client._tunnel = None

    def test_tunnel_authenticates_only_after_its_own_listener_is_ready_and_ignores_global_config(self):
        log = self.configure_tunnel()
        process = Mock()
        process.poll.return_value = None
        events = []
        def spawn(args, **kwargs):
            events.append('spawn')
            kwargs['stderr'].write(b'Authenticated to server.\n')
            kwargs['stderr'].flush()
            return process
        def publish_listener(seconds):
            events.append('listener')
            with log.open('ab') as handle:
                handle.write(b'Local forwarding listening on 127.0.0.1 port 18121.\n')
        def healthy():
            self.assertTrue(self.client._tunnel_ready)
            self.assertIs(self.client._tunnel, process)
            events.append('health')
            return True
        with patch('backend.remote.subprocess.Popen', side_effect=spawn) as popen, \
                patch('backend.remote.time.sleep', side_effect=publish_listener), \
                patch.object(self.client, '_healthy', side_effect=healthy) as health:
            self.client._ensure_tunnel()
            self.assertEqual(events, ['spawn', 'listener', 'health'])
            health.assert_called_once()
            args = popen.call_args.args[0]
            self.assertEqual(args[args.index('-F') + 1], os.devnull)
            self.assertIn('-v', args)
            self.assertIn('ExitOnForwardFailure=yes', args)
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertEqual(args[args.index('-L') + 1], '127.0.0.1:18121:127.0.0.1:18120')
            self.assertNotIn(self.client.token, ' '.join(args))
            self.client._ensure_tunnel()
            popen.assert_called_once()
            self.assertEqual(health.call_count, 1)
        self.client._tunnel = None

    def test_remote_prepare_downloads_compatible_assets_and_polls_until_ready(self):
        with self.worker(self.archive(), initial_state='running') as requests:
            entry = self.prepare()
        self.assert_local_assets(entry)
        self.assertEqual([request.method for request in requests], ['POST', 'GET', 'GET'])
        self.assertEqual(self.updates[0][0], 'nas')
        self.assertEqual(self.updates[-1][0], 'download')
        self.assertFalse(list(self.directory.glob('*.partial')))
        self.assertFalse(list(self.directory.glob('.remote-install-*')))

    def test_nas_hit_installs_without_waiting_for_a_transcode(self):
        data = self.archive()
        def cached(request):
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'b' * 64, 'state': 'ready', 'cache_origin': 'nas',
                    'archive': {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                    'timings': {'prepare_seconds': 0}})
        with self.worker(data, override=cached) as requests:
            entry = self.prepare()
        self.assert_local_assets(entry)
        self.assertEqual([(request.method, request.url.path) for request in requests],
                         [('POST', '/v1/jobs'), ('GET', '/v1/jobs/' + 'b' * 64 + '/archive')])
        self.assertTrue(any('NAS 完整缓存包' in detail for _, _, detail in self.updates))
        self.assertTrue(any('无需重新生成' in detail for _, _, detail in self.updates))
        self.service.tool.assert_not_called()
        self.service.source_available.assert_not_called()

    def test_download_notifications_are_throttled_without_buffering_network_chunks(self):
        chunk = b'payload-' * 512
        data = chunk * 100
        clock = [0.0]
        rates = []
        client = self.client
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(100):
                    clock[0] += .01
                    yield chunk
        def respond(request):
            return httpx.Response(200, stream=Stream())
        def update(*values):
            self.updates.append(values)
            rates.append(client.download_rate())
        target = self.directory / 'throttled.zip.partial'
        with self.mock_transport(respond), patch('backend.remote.time.monotonic', side_effect=lambda: clock[0]):
            client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                             target, update, self.stopping)
            self.assertEqual(client.download_rate(), 0)
        self.assertEqual(target.read_bytes(), data)
        self.assertGreater(len(self.updates), 1)
        self.assertLess(len(self.updates), 10)
        self.assertTrue(all(rate > 0 for rate in rates))
        self.assertEqual(self.updates[-1][1], 97)

    def test_download_rate_combines_active_transfers_and_ignores_stale_values(self):
        self.client._transfer_rates = {'one': (1000, 10), 'two': (2000, 10.5), 'old': (9999, 1)}
        with patch('backend.remote.time.monotonic', return_value=11):
            self.assertEqual(self.client.download_rate(), 3000)

    def test_live_ready_tunnel_is_shared_without_rechecking_brief_health_failures(self):
        process = Mock()
        process.poll.return_value = None
        self.client._tunnel = process
        self.client._tunnel_ready = True
        with patch.object(self.client, '_healthy', return_value=False) as health, \
                patch('backend.remote.subprocess.Popen') as spawn:
            self.client._ensure_tunnel(self.stopping)
            self.client._ensure_tunnel(self.stopping)
        health.assert_not_called()
        spawn.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertIs(self.client._tunnel, process)
        self.client._tunnel = None

    def test_lost_submission_response_retries_same_body_and_reuses_one_job(self):
        submissions = []
        committed_jobs = set()
        def interrupted(request):
            if request.method == 'POST':
                body = json.loads(request.content)
                submissions.append(body)
                committed_jobs.add(json.dumps(body, sort_keys=True))
                if len(submissions) == 1:
                    raise httpx.ReadTimeout('Response lost after server accepted the job', request=request)
            return None
        with self.worker(self.archive(), initial_state='running', override=interrupted) as requests, \
                patch.object(self.stopping, 'wait', return_value=False):
            entry = self.prepare()
        self.assert_local_assets(entry)
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0], submissions[1])
        self.assertEqual(len(committed_jobs), 1)
        self.assertEqual(sum(request.url.path.endswith('/archive') for request in requests), 1)

    def test_poll_timeout_and_transient_server_error_recover_without_resubmission(self):
        attempts = []
        def interrupted(request):
            if request.method == 'GET' and not request.url.path.endswith('/archive'):
                attempts.append(request.url.path)
                if len(attempts) == 1:
                    raise httpx.ReadTimeout('Brief status timeout', request=request)
                if len(attempts) == 2:
                    return httpx.Response(503, json={'detail': 'temporarily busy'})
            return None
        with self.worker(self.archive(), initial_state='running', override=interrupted) as requests, \
                patch.object(self.stopping, 'wait', return_value=False):
            entry = self.prepare()
        self.assert_local_assets(entry)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(sum(request.method == 'POST' for request in requests), 1)

    def test_busy_and_temporary_gateway_responses_are_retried(self):
        for status in (429, 502, 503, 504):
            attempts = []
            def temporarily_busy(request):
                if request.method == 'POST':
                    attempts.append(request)
                    if len(attempts) == 1:
                        return httpx.Response(status, json={'detail': 'temporary failure'})
                return None
            with self.subTest(status=status), self.worker(self.archive(), override=temporarily_busy), \
                    patch.object(self.stopping, 'wait', return_value=False):
                entry = self.prepare()
            self.assert_local_assets(entry)
            self.assertEqual(len(attempts), 2)

    def test_permanent_server_errors_do_not_retry_or_replace_ready_cache(self):
        original = self.seed_cache()
        before = self.snapshot_cache()
        for status in (401, 403, 409, 422, 507):
            attempts = []
            def permanently_invalid(request):
                attempts.append(request)
                self.assertEqual(len(attempts), 1, 'Permanent failure must not be retried')
                return httpx.Response(status, json={'detail': 'permanent failure'})
            with self.subTest(status=status), self.worker(self.archive(), override=permanently_invalid), \
                    patch.object(self.stopping, 'wait', return_value=False), self.assertRaises(HTTPException) as error:
                self.prepare()
            self.assertEqual(error.exception.status_code, status)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(self.snapshot_cache(), before)
        self.assert_local_assets(original)

    def test_retry_deadline_is_bounded_and_passes_remaining_budget(self):
        clock = [0.0]
        remaining = []
        waits = []
        retries = []
        def unavailable(seconds):
            remaining.append(seconds)
            self.assertLess(len(remaining), 30, 'Retry loop ignored its deadline')
            self.client._check_response(httpx.Response(503, json={'detail': 'offline'}))
        def elapse(seconds):
            waits.append(seconds)
            clock[0] += seconds
            return False
        fake_time = SimpleNamespace(monotonic=lambda: clock[0])
        with patch('backend.remote.time', fake_time), patch.object(self.stopping, 'wait', side_effect=elapse), \
                self.assertRaises(HTTPException) as error:
            self.client._retry(unavailable, self.stopping, lambda *args: retries.append(args))
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(clock[0], 300)
        self.assertEqual(remaining[0], 300)
        self.assertTrue(all(0 < value <= 300 for value in remaining))
        self.assertTrue(all(left > right for left, right in zip(remaining, remaining[1:])))
        self.assertEqual(waits[:6], [1, 2, 4, 8, 15, 30])
        self.assertTrue(retries)

    def test_cancelling_retry_backoff_prevents_another_request(self):
        attempts = []
        def unavailable(request):
            attempts.append(request)
            raise httpx.ConnectError('Disconnected', request=request)
        def cancel_wait(seconds):
            self.stopping.set()
            return True
        with self.mock_transport(unavailable), patch.object(self.stopping, 'wait', side_effect=cancel_wait), \
                self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(list(self.directory.glob('*.partial')))

    def test_cancelling_long_submission_aborts_async_request_promptly(self):
        entered, cancelled = threading.Event(), threading.Event()
        async def never_respond(request):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
            raise AssertionError('Cancelled request continued')
        def sync_request(request):
            raise AssertionError('Preparation must use cancellable async HTTP')
        def cancel_after_entering():
            if entered.wait(3):
                self.stopping.set()
        thread = threading.Thread(target=cancel_after_entering, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.mock_transport(sync_request, async_responder=never_respond), self.assertRaises(HTTPException) as error:
                self.prepare()
            self.assertEqual(error.exception.status_code, 503)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(entered.is_set())
            self.assertTrue(cancelled.is_set())
        finally:
            self.stopping.set()
            thread.join(timeout=3)

    def test_worker_restart_missing_job_resubmits_same_source_and_continues(self):
        polls = []
        def restarted(request):
            if request.method == 'GET' and not request.url.path.endswith('/archive'):
                polls.append(request)
                if len(polls) == 1:
                    return httpx.Response(404, json={'detail': 'unknown job after restart'})
            return None
        with self.worker(self.archive(), initial_state='running', override=restarted) as requests, \
                patch.object(self.stopping, 'wait', return_value=False):
            entry = self.prepare()
        self.assert_local_assets(entry)
        submissions = [json.loads(request.content) for request in requests if request.method == 'POST']
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[0], submissions[1])
        self.assertEqual(len(polls), 2)

    def test_missing_job_resubmission_is_limited_to_three_restarts(self):
        def missing(request):
            if request.method == 'GET' and not request.url.path.endswith('/archive'):
                return httpx.Response(404, json={'detail': 'unknown job'})
            return None
        with self.worker(self.archive(), initial_state='running', override=missing) as requests, \
                patch.object(self.stopping, 'wait', return_value=False), self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertIn(error.exception.status_code, (404, 503))
        self.assertEqual(sum(request.method == 'POST' for request in requests), 4)
        self.assertFalse(any(request.url.path.endswith('/archive') for request in requests))

    def test_submission_poll_and_download_use_bounded_read_timeouts(self):
        with self.worker(self.archive(), initial_state='running'), patch.object(
                self.client, '_async_http', wraps=self.client._async_http) as asynchronous, patch.object(
                self.client, '_http', wraps=self.client._http) as synchronous:
            self.prepare()
        timeouts = [call.kwargs['read_timeout'] for call in asynchronous.call_args_list + synchronous.call_args_list]
        self.assertCountEqual(timeouts, [180, 15, 20])

    def test_already_cancelled_preparation_makes_no_server_request(self):
        self.stopping.set()
        with patch.object(self.client, 'request') as request, patch.object(self.client, '_download') as download, \
                patch.object(self.client, '_ensure_tunnel') as tunnel, self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 503)
        request.assert_not_called()
        download.assert_not_called()
        tunnel.assert_not_called()
        self.assertEqual(self.updates, [])

    def test_cancelling_during_poll_wait_prevents_poll_and_download_requests(self):
        def cancel_during_wait(seconds):
            self.assertLessEqual(seconds, .5)
            self.stopping.set()
            return True
        with patch.object(self.client, 'request', return_value={'id': 'b' * 64, 'state': 'running'}) as request, \
                patch.object(self.stopping, 'wait', side_effect=cancel_during_wait), \
                patch.object(self.client, '_download') as download, self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 503)
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ('POST', '/v1/jobs'))
        download.assert_not_called()

    def test_cancelling_as_job_becomes_ready_prevents_download(self):
        def ready_when_cancelled(*args, **kwargs):
            self.stopping.set()
            return {'id': 'b' * 64, 'state': 'ready'}
        with patch.object(self.client, 'request', side_effect=ready_when_cancelled), \
                patch.object(self.client, '_download') as download, self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 503)
        download.assert_not_called()

    def test_corrupt_archive_or_hash_mismatch_does_not_replace_existing_cache(self):
        original = self.seed_cache()
        before = self.snapshot_cache()
        for data, digest in ((b'not-a-zip-file', None), (self.archive(), '0' * 64)):
            with self.subTest(digest=digest), self.worker(data, digest=digest), self.assertRaises(HTTPException) as error:
                self.prepare()
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(self.snapshot_cache(), before)
            self.assert_local_assets(original)

    def test_invalid_asset_hash_or_image_does_not_replace_existing_cache(self):
        original = self.seed_cache()
        before = self.snapshot_cache()
        def wrong_hash(manifest, contents):
            manifest['files']['normal']['sha256'] = '0' * 64
        def invalid_image(manifest, contents):
            data = b'not-a-jpeg-image'
            contents['thumbnail.jpg'] = data
            manifest['files']['thumbnail'].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        for mutate in (wrong_hash, invalid_image):
            with self.subTest(mutation=mutate.__name__), self.worker(self.archive(mutate)), self.assertRaises(HTTPException) as error:
                self.prepare()
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(self.snapshot_cache(), before)
            self.assert_local_assets(original)

    def test_zip_path_escape_is_rejected_before_any_cached_file_is_replaced(self):
        original = self.seed_cache()
        before = self.snapshot_cache()
        def escape(manifest, contents):
            contents['../escaped.mp4'] = contents.pop('normal.mp4')
            manifest['files']['normal']['name'] = '../escaped.mp4'
        with self.worker(self.archive(escape)), self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 422)
        self.assertFalse((self.directory.parent / 'escaped.mp4').exists())
        self.assertEqual(self.snapshot_cache(), before)
        self.assert_local_assets(original)

    def test_changed_source_manifest_is_rejected_without_publishing_download(self):
        original = self.seed_cache()
        before = self.snapshot_cache()
        def changed_source(manifest, contents):
            manifest['source_stamp']['sample_sha256'] = '0' * 64
        with self.worker(self.archive(changed_source)), self.assertRaises(HTTPException) as error:
            self.prepare()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.snapshot_cache(), before)
        self.assert_local_assets(original)

    def test_download_resumes_partial_file_with_range_and_validates_complete_hash(self):
        data = self.archive()
        offset = len(data) // 3
        target = self.directory / 'resume.zip.partial'
        target.write_bytes(data[:offset])
        requests = []
        def respond(request):
            requests.append(request)
            self.assertEqual(request.headers['Range'], f'bytes={offset}-')
            return httpx.Response(206, content=data[offset:],
                                  headers={'Content-Range': f'bytes {offset}-{len(data) - 1}/{len(data)}'})
        with self.mock_transport(respond):
            self.client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                  target, lambda *args: self.updates.append(args), self.stopping)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.updates[-1][1], 97)

    def test_download_after_a_real_stream_disconnect_resumes_persisted_bytes(self):
        data = os.urandom(1024 * 1024 + 65536)
        target = self.directory / 'interrupted.zip.partial'
        requests = []
        resume_offsets = []
        class InterruptedStream(httpx.SyncByteStream):
            def __iter__(stream):
                yield data[:1024 * 1024]
                yield data[1024 * 1024:1024 * 1024 + 777]
                raise httpx.ReadError('Connection lost during response body')
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                self.assertNotIn('Range', request.headers)
                return httpx.Response(200, stream=InterruptedStream(), headers={'Content-Length': str(len(data))})
            offset = target.stat().st_size
            resume_offsets.append(offset)
            self.assertEqual(offset, 1024 * 1024 + 777)
            self.assertEqual(request.headers['Range'], f'bytes={offset}-')
            return httpx.Response(206, content=data[offset:],
                headers={'Content-Range': f'bytes {offset}-{len(data) - 1}/{len(data)}'})
        with self.mock_transport(respond), patch.object(self.stopping, 'wait', return_value=False):
            self.client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                  target, lambda *args: self.updates.append(args), self.stopping)
        self.assertEqual(len(requests), 2)
        self.assertEqual(resume_offsets, [1024 * 1024 + 777])
        self.assertEqual(target.read_bytes(), data)
        self.assertTrue(any(operation == 'reconnect' for operation, _, _ in self.updates))

    def test_cleanly_truncated_body_is_preserved_and_resumed(self):
        data = self.archive()
        offset = len(data) // 2
        target = self.directory / 'short-response.zip.partial'
        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=data[:offset], headers={'Content-Length': str(len(data))})
            self.assertEqual(target.read_bytes(), data[:offset])
            self.assertEqual(request.headers['Range'], f'bytes={offset}-')
            return httpx.Response(206, content=data[offset:],
                headers={'Content-Range': f'bytes {offset}-{len(data) - 1}/{len(data)}'})
        with self.mock_transport(respond), patch.object(self.stopping, 'wait', return_value=False):
            self.client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                  target, lambda *args: None, self.stopping)
        self.assertEqual(len(requests), 2)
        self.assertEqual(target.read_bytes(), data)

    def test_completed_download_hash_mismatch_is_not_retried(self):
        data = self.archive()
        target = self.directory / 'bad-hash.zip.partial'
        requests = []
        def respond(request):
            requests.append(request)
            self.assertEqual(len(requests), 1, 'Checksum failures must not retry a bad artifact')
            return httpx.Response(200, content=data)
        with self.mock_transport(respond), patch.object(self.stopping, 'wait') as wait, \
                self.assertRaises(HTTPException) as error:
            self.client._download('b' * 64, {'size': len(data), 'sha256': '0' * 64},
                                  target, lambda *args: None, self.stopping)
        self.assertEqual(error.exception.status_code, 422)
        self.assertFalse(target.exists())
        wait.assert_not_called()

    def test_download_cancellation_keeps_partial_file_without_retrying(self):
        data = os.urandom(1024 * 1024 + 4096)
        target = self.directory / 'cancelled-download.zip.partial'
        requests = []
        stopping = self.stopping
        class CancelledStream(httpx.SyncByteStream):
            def __iter__(stream):
                yield data[:1024 * 1024]
                stopping.set()
                raise httpx.ReadError('Connection lost while user cancelled')
        def respond(request):
            requests.append(request)
            return httpx.Response(200, stream=CancelledStream())
        with self.mock_transport(respond), patch.object(self.stopping, 'wait') as wait, \
                self.assertRaises(HTTPException) as error:
            self.client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                  target, lambda *args: None, self.stopping)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(len(requests), 1)
        self.assertEqual(target.read_bytes(), data[:1024 * 1024])
        wait.assert_not_called()

    def test_slow_download_cancels_between_small_chunks_without_buffering(self):
        data = os.urandom(4096)
        target = self.directory / 'cancelled-slow-download.zip.partial'
        requests = []
        closed = []
        stopping = self.stopping
        test = self
        class SlowStream(httpx.SyncByteStream):
            def __iter__(stream):
                yield data[:1024]
                stopping.set()
                yield data[1024:2048]
                test.fail('Cancellation must stop consuming the next small network chunk')
            def close(stream):
                closed.append(True)
        def respond(request):
            requests.append(request)
            return httpx.Response(200, stream=SlowStream(), headers={'Content-Length': str(len(data))})
        with self.mock_transport(respond), patch.object(self.stopping, 'wait') as wait, \
                self.assertRaises(HTTPException) as error:
            self.client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                  target, lambda *args: self.updates.append(args), self.stopping)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(len(requests), 1)
        self.assertEqual(target.read_bytes(), data[:1024])
        self.assertEqual([operation for operation, _, _ in self.updates], ['download'])
        self.assertEqual(closed, [True])
        wait.assert_not_called()

    def test_inconsistent_resume_response_preserves_partial_data(self):
        data = self.archive()
        partial = data[:100]
        target = self.directory / 'resume.zip.partial'
        target.write_bytes(partial)
        with self.mock_transport(lambda request: httpx.Response(206, content=data[100:],
                headers={'Content-Range': f'bytes 0-{len(data) - 1}/{len(data)}'})):
            with self.assertRaises(HTTPException) as error:
                self.client._download('b' * 64, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                                      target, lambda *args: None, self.stopping)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(target.read_bytes(), partial)

    def test_ready_local_cache_is_reused_offline_without_server_nas_or_ffmpeg(self):
        entry = self.seed_cache()
        with self.no_local_processing(), patch.object(self.client, 'prepare', side_effect=AssertionError('Unexpected remote preparation')), \
                patch.object(self.client, 'request', side_effect=AssertionError('Unexpected server request')), \
                patch.object(self.client, '_ensure_tunnel', side_effect=AssertionError('Unexpected server connection')):
            status = self.run_session()
            self.assertEqual(status['state'], 'ready')
            self.assertEqual(status['items'][0]['operation'], 'reuse')
            manifest = self.sessions.manifest(self.ident)
            for kind, expected in (('normal', MEDIA), ('fast', FAST), ('thumbnail', THUMB), ('sheet', SHEET)):
                path = self.sessions.asset(self.ident, kind, 'v0001', 0 if kind == 'sheet' else None,
                                           version=manifest['version'])
                self.assertTrue(path.is_relative_to(self.directory))
                self.assertEqual(path.read_bytes(), expected)
        self.assert_local_assets(entry)
        self.service.source_available.assert_not_called()

    def test_missing_cache_is_installed_via_remote_without_local_processing(self):
        with self.worker(self.archive()), self.no_local_processing(), patch.object(
                self.client, 'prepare', wraps=self.client.prepare) as prepare:
            status = self.run_session()
        self.assertEqual(status['state'], 'ready')
        prepare.assert_called_once()
        self.assertEqual(status['items'][0]['operation'], 'download')
        entry = self.sessions._load_entry(self.directory, self.key, self.source)
        self.assertIsNotNone(entry)
        self.assert_local_assets(entry)
        self.service.source_available.assert_not_called()

    def test_worker_failure_is_retryable_and_never_falls_back_to_local_processing(self):
        with self.no_local_processing(), patch.object(self.client, 'prepare',
                side_effect=HTTPException(503, 'Worker is offline')) as prepare:
            status = self.run_session()
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['failed'], 1)
        self.assertEqual(status['detail'], 'Worker is offline')
        prepare.assert_called_once()
        self.service.source_available.assert_not_called()
        self.assertIsNone(self.sessions._load_entry(self.directory, self.key, self.source))
        with self.worker(self.archive()), self.no_local_processing():
            self.assertEqual(self.run_session()['state'], 'ready')


if __name__ == '__main__':
    unittest.main()
