from __future__ import annotations

import os
import stat
import unittest
from unittest.mock import patch

import anyio
from starlette.middleware.base import BaseHTTPMiddleware

from backend.media_response import CancellableFileResponse


class FakeMediaFile:
    size = 8 * 1024 * 1024

    def __init__(self):
        self.position = 0
        self.read_bytes = 0
        self.reads = 0
        self.closed = False
        self.seeks = []
        self.fail_read = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        # A cancelled AnyIO scope would interrupt this close; the response must
        # unwind normally after disconnect and allow the handle to be released.
        await anyio.sleep(0)
        self.closed = True

    async def seek(self, value):
        self.position = value
        self.seeks.append(value)

    async def read(self, count):
        await anyio.sleep(0)
        if self.fail_read:
            raise OSError("Shared media read failed")
        count = min(count, self.size - self.position)
        self.position += count
        self.read_bytes += count
        self.reads += 1
        return b"v" * count


async def exercise(*, range_value=None, method="GET", disconnect=False,
                   send_error=False, middleware=False, if_range=None,
                   fail_read=False, spec_version="2.3"):
    media = FakeMediaFile()
    media.fail_read = fail_read
    ended = anyio.Event()
    messages = []
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await ended.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            if send_error:
                raise OSError("Client disconnected")
            if disconnect:
                ended.set()

    async def open_file(*args, **kwargs):
        return media

    file_stat = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, media.size, 0, 0, 0))
    response = CancellableFileResponse("fake.mp4", media_type="video/mp4", stat_result=file_stat)
    headers = []
    if range_value is not None:
        headers.append((b"range", range_value.encode("ascii")))
    if if_range is not None:
        headers.append((b"if-range", if_range.encode("ascii")))
    scope = {"type": "http", "method": method, "path": "/media", "query_string": b"",
             "headers": headers, "asgi": {"version": "3.0", "spec_version": spec_version}}

    async def app(scope, receive, send):
        await response(scope, receive, send)

    async def dispatch(request, call_next):
        return await call_next(request)

    target = BaseHTTPMiddleware(app, dispatch=dispatch) if middleware else app
    with patch("starlette.responses.anyio.open_file", open_file):
        with anyio.fail_after(2):
            await target(scope, receive, send)
    return media, messages


def run(**kwargs):
    async def task():
        return await exercise(**kwargs)
    return anyio.run(task)


class MediaResponseTests(unittest.TestCase):
    def test_abandoned_open_range_stops_reading_and_closes_handle(self):
        for middleware in (False, True):
            with self.subTest(middleware=middleware):
                media, messages = run(range_value="bytes=0-", disconnect=True, middleware=middleware)
                self.assertLessEqual(media.read_bytes, CancellableFileResponse.chunk_size * 4)
                self.assertGreater(media.read_bytes, 0)
                self.assertTrue(media.closed)
                self.assertEqual(messages[0]["status"], 206)

    def test_full_response_disconnect_also_stops_reading(self):
        media, _ = run(disconnect=True, middleware=True)
        self.assertLessEqual(media.read_bytes, CancellableFileResponse.chunk_size * 4)
        self.assertTrue(media.closed)

    def test_new_asgi_send_disconnect_closes_handle(self):
        media, _ = run(range_value="bytes=0-", send_error=True, spec_version="2.4")
        self.assertLessEqual(media.read_bytes, CancellableFileResponse.chunk_size)
        self.assertTrue(media.closed)

    def test_connected_bounded_range_is_unchanged(self):
        media, messages = run(range_value="bytes=123-77999", middleware=True)
        body = b"".join(message.get("body", b"") for message in messages)
        headers = dict(messages[0]["headers"])
        self.assertEqual(messages[0]["status"], 206)
        self.assertEqual(headers[b"content-range"], b"bytes 123-77999/8388608")
        self.assertEqual(len(body), 77999 - 123 + 1)
        self.assertEqual(media.seeks, [123])
        self.assertTrue(media.closed)

    def test_head_range_reads_no_media_and_returns(self):
        media, messages = run(range_value="bytes=10-19", method="HEAD", middleware=True)
        self.assertEqual(messages[0]["status"], 206)
        self.assertEqual(dict(messages[0]["headers"])[b"content-length"], b"10")
        self.assertEqual(media.read_bytes, 0)

    def test_suffix_and_multiple_ranges_keep_standard_semantics(self):
        media, messages = run(range_value="bytes=-20")
        self.assertEqual(media.read_bytes, 20)
        self.assertEqual(media.seeks, [media.size - 20])
        self.assertEqual(messages[0]["status"], 206)
        media, messages = run(range_value="bytes=10-19,100-109")
        headers = dict(messages[0]["headers"])
        self.assertTrue(headers[b"content-type"].startswith(b"multipart/byteranges;"))
        body = b"".join(message.get("body", b"") for message in messages)
        self.assertIn(b"Content-Range: bytes 10-19/8388608", body)
        self.assertIn(b"Content-Range: bytes 100-109/8388608", body)
        self.assertEqual(len(body), int(headers[b"content-length"]))
        self.assertEqual(media.read_bytes, 20)
        self.assertTrue(media.closed)

    def test_if_range_mismatch_uses_full_response(self):
        media, messages = run(range_value="bytes=10-19", if_range="wrong-etag", disconnect=True)
        self.assertEqual(messages[0]["status"], 200)
        self.assertLessEqual(media.read_bytes, CancellableFileResponse.chunk_size * 4)
        self.assertTrue(media.closed)

    def test_invalid_ranges_return_without_opening_media(self):
        for range_value, expected in [("bytes=9000000-", 416), ("bad", 400)]:
            with self.subTest(range_value=range_value):
                media, messages = run(range_value=range_value)
                self.assertEqual(messages[0]["status"], expected)
                self.assertEqual(media.read_bytes, 0)

    def test_media_io_errors_are_not_hidden_as_disconnects(self):
        with self.assertRaisesRegex(OSError, "Shared media read failed"):
            run(range_value="bytes=0-", fail_read=True)


if __name__ == "__main__":
    unittest.main()
