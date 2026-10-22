"""Durable delivery-only outbox shared with an isolated NAS worker, not its mount."""
from contextlib import contextmanager
from contextlib import closing
import os
from pathlib import Path
import sqlite3


SCHEMA = '''
CREATE TABLE IF NOT EXISTS batches(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS items(
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id), output_job TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, retry_generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS links(item_id TEXT NOT NULL REFERENCES items(id), task_id TEXT NOT NULL,
 PRIMARY KEY(item_id,task_id));
CREATE TRIGGER IF NOT EXISTS frozen_payload BEFORE UPDATE OF payload,output_job,batch_id ON items
 BEGIN SELECT RAISE(ABORT,'immutable delivery snapshot'); END;
'''


class DeliveryQueue:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError('NAS delivery queue must not contain symlinks')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root/'events.sqlite3'
        if self.path.is_symlink():
            raise ValueError('NAS delivery queue must not be a symlink')
        with self.db() as db:
            db.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            # Queue commits remain local to this database.
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise ValueError('Delivery queue requires DELETE journal mode')
            with db:
                yield db


def status_rows(rows):
    if not rows:
        return None
    records = [dict(zip(('state','error','batch_state','batch_error'), row)) for row in rows]
    ready = sum(r['state']=='ready' for r in records)
    failures = [r for r in records if r['state'] in ('blocked','error','cancelled') or r['batch_state'] in ('blocked','error')]
    if failures:
        value = failures[0]
        state, detail = 'error', value['error'] or value['batch_error'] or 'NAS 同步未完成。'
    elif ready == len(records):
        state, detail = 'ready', '本次交付的最终 JSON 已同步到 NAS 并读回核验。'
    elif any(r['state']=='running' for r in records):
        state, detail = 'running', '正在同步本次交付的最终 JSON 到 NAS。'
    elif any(r['batch_state']!='queued' for r in records):
        state, detail = 'pending', '交付批次已保存，等待整批 S3 结果核验后同步 NAS。'
    else:
        state, detail = 'pending', '交付批次已核验，等待 NAS 后台同步。'
    return dict(state=state,detail=detail,ready=ready,total=len(records),
                retryable=not any(r['state']=='cancelled' for r in records),
                retry_scheduled=any(r['state']=='error' or r['batch_state']=='error' for r in records))
