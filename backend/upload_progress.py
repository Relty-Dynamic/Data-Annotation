"""Per-stage wall time and factual clip progress; parallel times are not added."""
import threading
import time
from contextlib import contextmanager


class UploadProgress:
    def __init__(self, clips, prediction=False, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.finished = None
        self.lock = threading.RLock()
        self.stages = {key: dict(total=total, done=set(), active={}, elapsed=0., since=None)
                       for key, total in [('source', clips), ('compress', clips), ('cache', clips),
                                          ('upload', clips), ('verify', clips),
                                          *([('prediction', 1)] if prediction else []), ('publish', 1)]}
        self.outcome = None

    def begin(self, key, index):
        with self.lock:
            if self.finished is not None:
                return
            stage = self.stages[key]
            if not stage['active']:
                stage['since'] = self.clock()
            stage['active'][index] = 0.

    def advance(self, key, index, percent):
        with self.lock:
            if self.finished is not None:
                return
            stage = self.stages[key]
            if index in stage['active']:
                stage['active'][index] = max(stage['active'][index], min(100., max(0., percent)))

    def complete(self, key, index):
        with self.lock:
            if self.finished is not None:
                return
            stage = self.stages[key]
            stage['done'].add(index)
            stage['active'].pop(index, None)
            if not stage['active'] and stage['since'] is not None:
                stage['elapsed'] += (self.clock() if self.finished is None else self.finished) - stage['since']
                stage['since'] = None

    @contextmanager
    def measure(self, key, index):
        self.begin(key, index)
        yield
        self.complete(key, index)

    def finish(self, outcome):
        with self.lock:
            if self.finished is None:
                self.finished, self.outcome = self.clock(), outcome

    def snapshot(self):
        with self.lock:
            now = self.clock() if self.finished is None else self.finished
            rows = []
            for key, stage in self.stages.items():
                done, total = len(stage['done']), stage['total']
                state = 'done' if done == total else ('running' if stage['active'] else 'waiting')
                if self.outcome == 'error' and state != 'done':
                    state = 'stopped'
                elapsed = stage['elapsed'] + (now-stage['since'] if stage['since'] is not None else 0)
                rows.append(dict(stage=key, state=state, completed=done, total=total,
                    active=len(stage['active']), progress=round((done+sum(stage['active'].values())/100)/total*100, 1),
                    elapsed_ms=round(elapsed*1000)))
            return dict(elapsed_ms=round((now-self.started)*1000), stages=rows)
