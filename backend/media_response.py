from __future__ import annotations

import anyio
from starlette._utils import create_collapsing_task_group
from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send


class _MediaDisconnected(Exception):
    pass


class CancellableFileResponse(FileResponse):
    """Stop reading a media file when a seek abandons its HTTP range request."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await super().__call__(scope, receive, send)

        disconnected = False

        async def watch_disconnect() -> None:
            nonlocal disconnected
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    return

        async def send_connected(message: Message) -> None:
            if disconnected:
                raise _MediaDisconnected()
            try:
                await send(message)
            except OSError as error:
                # ASGI 2.4+ may report disconnects from send instead of receive.
                raise _MediaDisconnected() from error
            if disconnected:
                raise _MediaDisconnected()

        async with create_collapsing_task_group() as group:
            group.start_soon(watch_disconnect)
            try:
                await super().__call__(scope, receive, send_connected)
            except _MediaDisconnected:
                # Unwind normally, rather than cancelling inside file.read(), so
                # AnyIO can close the SMB handle before the response returns.
                pass
            finally:
                group.cancel_scope.cancel()
