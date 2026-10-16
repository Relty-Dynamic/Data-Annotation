"""Progress, cancellation and safe failure reporting on streamed IMU requests."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import httpx
from fastapi import HTTPException

from backend.prediction_stream import submit_input
from backend.upload_errors import failure_detail


class PredictionStreamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'imu.txt'
        self.data = b'fixture-imu-data' * 50000
        self.source.write_bytes(self.data)
        self.messages = []

    def submit(self, request, cancel=lambda: None, **kwargs):
        return submit_input(SimpleNamespace(request=request), '/fixture/inputs/0', self.source,
                            len(self.data), '正在提交 IMU 1/1', self.messages.append, cancel, **kwargs)

    def test_full_bytes_have_explicit_length_and_only_response_confirms_success(self):
        def request(method, path, **kwargs):
            self.assertEqual(method, 'PUT')
            self.assertEqual(kwargs['headers']['Content-Length'], str(len(self.data)))
            self.assertEqual(kwargs['timeout'].write, 15)
            self.assertEqual(kwargs['timeout'].read, 30)
            self.assertEqual(b''.join(kwargs['content']), self.data)
            self.assertIn('等待服务端确认', self.messages[-1])
            self.assertFalse(any('已确认接收' in m for m in self.messages))
            return {'ok': True}
        tick = iter(range(100))
        self.assertEqual(self.submit(request, clock=lambda: next(tick)), {'ok': True})
        self.assertTrue(any('尚未确认完成' in m for m in self.messages))
        self.assertIn('已确认接收', self.messages[-1])

    def test_parallel_failure_cancels_stream_before_all_bytes_are_sent(self):
        stopped = False
        def cancel():
            if stopped:
                raise HTTPException(503, 'fixture parallel upload failed')
        def request(method, path, **kwargs):
            nonlocal stopped
            chunks = iter(kwargs['content'])
            self.assertLess(len(next(chunks)), len(self.data))
            stopped = True
            next(chunks)
            self.fail('cancelled stream must not continue')
        with self.assertRaises(HTTPException) as error:
            self.submit(request, cancel)
        self.assertEqual(error.exception.detail, 'fixture parallel upload failed')
        self.assertFalse(any('已确认接收' in m for m in self.messages))

    def test_total_deadline_stops_slow_but_not_idle_stream(self):
        tick = iter([0, 0, 901, 902])
        def request(method, path, **kwargs):
            return b''.join(kwargs['content'])
        with self.assertRaises(HTTPException) as error:
            self.submit(request, clock=lambda: next(tick))
        self.assertEqual(error.exception.status_code, 504)
        self.assertIn('15 分钟', error.exception.detail)

    def test_transport_error_keeps_signed_urls_and_credentials_out_of_diagnosis(self):
        secret = 'https://storage.example.com/file?secret=fixture-never-display'
        def request(*args, **kwargs):
            raise httpx.WriteTimeout(secret)
        with self.assertRaises(HTTPException) as error:
            self.submit(request)
        self.assertIn('IMU 提交', error.exception.detail)
        self.assertNotIn('fixture-never-display', error.exception.detail)
        self.assertIn('网络连接或传输超时', failure_detail(httpx.ReadTimeout(secret), '视频上传'))
        self.assertNotIn(secret, failure_detail(httpx.ReadTimeout(secret)))

    def test_changed_file_length_is_not_reported_as_complete(self):
        self.source.write_bytes(b'shortened')
        with self.assertRaises(HTTPException) as error:
            self.submit(lambda method, path, **kwargs: b''.join(kwargs['content']))
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(any('已确认接收' in m for m in self.messages))
