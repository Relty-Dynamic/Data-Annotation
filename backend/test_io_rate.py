import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.io_rate import ReadRates, read_bytes


class ReadRateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.counts = {1: 100, 2: 200, 3: 300}
        self.rates = ReadRates(lambda pid: self.counts[pid], lambda: self.now)
        self.root = Path.cwd() / '.tmp' / 'rate-project'
        self.processes = {}
        for pid in self.counts:
            proc = SimpleNamespace(pid=pid, returncode=None)
            proc.poll = lambda proc=proc: proc.returncode
            self.processes[pid] = proc

    def test_sums_parallel_reads_and_reports_zero_when_stalled(self):
        for pid in (1, 2):
            self.rates.track(self.processes[pid], self.root / f'{pid}.log')
        self.assertIsNone(self.rates.snapshot(self.root))
        self.now = 2
        self.counts.update({1: 2000100, 2: 4000200})
        self.assertEqual(self.rates.snapshot(self.root), 3000000)
        self.now = 4
        self.assertEqual(self.rates.snapshot(self.root), 0)

    def test_other_projects_and_finished_processes_are_excluded(self):
        self.rates.track(self.processes[1], self.root / 'clip.log')
        self.rates.track(self.processes[2], self.root.parent / 'another' / 'clip.log')
        self.now = 1
        self.counts.update({1: 1100, 2: 9000200})
        self.assertEqual(self.rates.snapshot(self.root), 1000)
        self.processes[1].returncode = 0
        self.assertEqual(self.rates.snapshot(self.root), 0)
        self.assertNotIn(1, self.rates.entries)

    def test_missing_counters_are_unknown_and_recover_after_two_samples(self):
        self.counts[1] = None
        self.rates.track(self.processes[1], self.root / 'clip.log')
        self.now = 1
        self.assertIsNone(self.rates.snapshot(self.root))
        self.counts[1] = 100
        self.now = 2
        self.assertIsNone(self.rates.snapshot(self.root))
        self.counts[1] = 2100
        self.now = 3
        self.assertEqual(self.rates.snapshot(self.root), 2000)

    @unittest.skipUnless(os.name == 'nt', 'Windows I/O counters')
    def test_native_counter_measures_actual_child_file_reads(self):
        root = Path(__file__).resolve().parents[1] / '.tmp'
        with tempfile.TemporaryDirectory(dir=root, prefix='io-rate-') as folder:
            source = Path(folder) / 'sample.bin'
            source.write_bytes(b'x' * (2 * 1024 * 1024))
            code = 'import sys; print("ready",flush=True); input(); open(sys.argv[1],"rb").read(); print("done",flush=True); input()'
            process = subprocess.Popen([sys._base_executable, '-B', '-c', code, str(source)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                before = read_bytes(process.pid)
                process.stdin.write('\n'); process.stdin.flush()
                self.assertEqual(process.stdout.readline().strip(), 'done')
                after = read_bytes(process.pid)
                self.assertIsNotNone(before)
                self.assertGreaterEqual(after - before, source.stat().st_size)
            finally:
                process.kill(); process.wait(timeout=5)
                process.stdin.close(); process.stdout.close()
