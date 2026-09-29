"""Read throughput of this application's tracked video processes (not NIC bandwidth)."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path


if os.name == 'nt':
    from ctypes import wintypes

    class _IOCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    _kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel.OpenProcess.restype = wintypes.HANDLE
    _kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IOCounters)]
    _kernel.GetProcessIoCounters.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL


def read_bytes(pid: int) -> int | None:
    if os.name != 'nt':
        return None
    handle = _kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        counters = _IOCounters()
        return int(counters.ReadTransferCount) if _kernel.GetProcessIoCounters(handle, ctypes.byref(counters)) else None
    finally:
        _kernel.CloseHandle(handle)


class ReadRates:
    def __init__(self, counter=read_bytes, clock=time.monotonic):
        self.counter = counter
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}

    def _read(self, process):
        try:
            return self.counter(process.pid)
        except (OSError, ValueError, TypeError):
            return None

    def track(self, process, log_path: Path):
        with self.lock:
            self._prune()
            if process.pid not in self.entries:
                self.entries[process.pid] = {
                    'process': process, 'path': Path(log_path).absolute(),
                    'time': self.clock(), 'bytes': self._read(process), 'rate': None,
                }

    def _prune(self):
        for pid, entry in list(self.entries.items()):
            if entry['process'].poll() is not None:
                del self.entries[pid]

    def snapshot(self, directory: Path) -> float | None:
        # Only process counters and lexical local paths; never touch the NAS.
        with self.lock:
            self._prune()
            now = self.clock()
            rates = []
            for entry in self.entries.values():
                if not entry['path'].is_relative_to(directory.absolute()):
                    continue
                elapsed = now - entry['time']
                if elapsed >= .5:
                    value = self._read(entry['process'])
                    previous = entry['bytes']
                    entry['rate'] = (max(0, value - previous) / elapsed
                                     if value is not None and previous is not None else None)
                    entry.update(time=now, bytes=value)
                rates.append(entry['rate'])
            return None if any(rate is None for rate in rates) else sum(rates)


read_rates = ReadRates()
