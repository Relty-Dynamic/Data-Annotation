"""Track browser connections without relying on throttled background-tab timers."""
import asyncio
import time
from contextlib import suppress

class BrowserLifetime:
    def __init__(self, on_idle=None, grace=15.0, startup_wait=120.0, clock=time.monotonic):
        self.on_idle = on_idle
        self.grace = grace
        self.clock = clock
        self.clients = set()
        self.active_requests = 0
        self.deadline = clock() + startup_wait
        self.stopping = False
        self.task = None

    def connect(self, token):
        if self.stopping:
            return False
        self.clients.add(token)
        return True

    def disconnect(self, token):
        self.clients.discard(token)
        if not self.clients:
            self.deadline = self.clock() + self.grace

    def reserve(self):
        if not self.stopping:
            self.deadline = max(self.deadline, self.clock() + 120)
        return not self.stopping

    def check(self):
        if self.on_idle and not self.stopping and not self.clients and not self.active_requests and self.clock() >= self.deadline:
            self.stopping = True
            self.on_idle()
            return True
        return False

    async def run(self):
        while not self.stopping:
            await asyncio.sleep(1)
            self.check()

    def start(self):
        if self.on_idle:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
