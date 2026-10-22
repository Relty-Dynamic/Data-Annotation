"""Event-driven NAS delivery worker; no public API, account DB, or S3 credentials."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import time

from .nas_delivery_queue import DeliveryQueue
from .service import document_bytes
from deploy.sync_final_timelines import (MARKER, PENDING, SyncError, find_collections, manifest, pending_record,
                                        run_lock, safe_path, sha, sync_capture)


class MountUnavailable(OSError):
    pass


class Snapshot:
    """Read only this frozen, S3-verified delivery; reject all other keys."""
    def __init__(self, payload, root, collections):
        self.capture = payload['capture']
        self.prefix = self.capture+'/timeline/final/'
        self.marker = manifest(document_bytes(payload['marker']))
        self.files = {name:text.encode('utf-8') for name,text in payload['files'].items()}
        if set(self.files) != set(self.marker['files']) or sum(map(len,self.files.values()))>128*1024*1024:
            raise SyncError('交付 JSON 快照不完整或超过大小上限。')
        for name,data in self.files.items():
            if len(data)>32*1024*1024 or sha(data)!=self.marker['files'][name]:
                raise SyncError('交付 JSON 快照摘要不一致。')
            record = json.loads(data)['task_result']
            if not all(record.get(k) for k in ('final_version_id','accepted_at','accepted_by')):
                raise SyncError('交付 JSON 缺少验收版本。')
        candidates = collections.get(self.capture.rsplit('/',1)[-1],[])
        if len(candidates)==1:
            target = safe_path(root,[*candidates[0],'timeline','final'])
            marker = safe_path(target,[MARKER])
            pending_path = safe_path(target,[PENDING])
            pending = pending_record(pending_path.read_bytes() if pending_path.exists() else None,self.capture,'final')
            previous = manifest(marker.read_bytes()) if marker.exists() else {'files':{}}
            if previous.get('metadata',{}).get('accepted_at','')>self.marker['metadata']['accepted_at']:
                self.marker['metadata']=previous['metadata']
            # Pending proof also survives a crash after the previous marker was
            # moved to backup. Retain all verified axes, even in that narrow gap.
            for name in sorted(set(previous['files'])|set(pending['files'])):
                path = safe_path(target,[name])
                old = path.read_bytes() if path.exists() else None
                known = set(pending['files'].get(name,[]))
                if name in previous['files']:
                    known.add(previous['files'][name])
                if old is None:
                    if name in self.files and self.marker['files'][name] in pending['files'].get(name,[]):
                        continue  # Replacement interrupted after backing up old file.
                    raise SyncError('NAS 已登记的最终 JSON 缺失，请核对后重试。')
                digest = sha(old)
                if digest not in known:
                    raise SyncError('NAS 最终 JSON 已被人工修改，请核对后重试。')
                old_result = json.loads(old)['task_result']
                if name in self.files:
                    if old_result['accepted_at']>json.loads(self.files[name])['task_result']['accepted_at']:
                        raise SyncError('NAS 已有更新的交付版本，旧事件未覆盖。')
                else:
                    self.files[name]=old
                    self.marker['files'][name]=digest
                    if old_result['accepted_at']>self.marker['metadata']['accepted_at']:
                        self.marker['metadata']={key:old_result[key] for key in
                            ('task_id','final_version_id','accepted_at','accepted_by')}
        self.raw_marker = document_bytes(self.marker)

    def get(self, key):
        if not key.startswith(self.prefix):
            raise SyncError('交付快照不允许读取其他采集。')
        name = key.removeprefix(self.prefix)
        if name==MARKER:
            return self.raw_marker
        return self.files[name]


class NasWorker:
    def __init__(self, queue, root, mount, clock=time.time):
        self.queue, self.root, self.mount = queue, Path(root), Path(mount)
        self.clock = clock

    def check_mount(self):
        if (not self.mount.is_absolute() or self.mount.is_symlink() or not os.path.ismount(self.mount)
                or not self.root.is_absolute() or self.root.is_symlink() or not self.root.is_dir()
                or not self.root.resolve().is_relative_to(self.mount.resolve())):
            raise MountUnavailable('NAS 挂载不可用，后台将重试。')
        # A Docker bind mount alone is not proof the host NAS is mounted.
        entries=Path('/proc/self/mountinfo').read_text().splitlines()
        filesystem=next((line.split(' - ',1)[1].split()[0] for line in entries
                         if line.split()[4].replace('\\040',' ')==str(self.mount)),None)
        if filesystem not in ('cifs','nfs','nfs4'):
            raise MountUnavailable('目标不是已挂载的 NAS 文件系统，未写入本机目录。')

    def recover(self):
        # Only call while holding the single-worker process lock.
        with self.queue.db() as db:
            db.execute("UPDATE items SET state='pending',retry_at=0 WHERE state='running'")

    def check_job(self, ident):
        self.check_mount()
        with self.queue.db() as db:
            row=db.execute('SELECT state FROM items WHERE id=?',(ident,)).fetchone()
        if not row or row['state']=='cancelled':
            raise SyncError('关联任务已删除，停止 NAS 同步。')

    def process_once(self):
        with self.queue.db() as db:
            rows=db.execute("""SELECT i.* FROM items i JOIN batches b ON b.id=i.batch_id
                WHERE b.state='queued' AND i.state IN ('pending','error') AND i.retry_at<=? ORDER BY i.rowid""",(self.clock(),)).fetchall()
        for row in rows:
            ident=row['id']
            with self.queue.db() as db:
                changed=db.execute("UPDATE items SET state='running',error=NULL WHERE id=? AND state IN ('pending','error')",(ident,)).rowcount
            if not changed:
                continue
            try:
                self.check_job(ident)
                payload=json.loads(row['payload'])
                collections=find_collections(self.root,[payload['capture']])
                snapshot=Snapshot(payload,self.root,collections)
                result=sync_capture(snapshot,self.root,payload['capture'],apply=True,collections=collections,
                                    directory='final',check_cancel=lambda:self.check_job(ident))
                with self.queue.db() as db:
                    completed=db.execute("UPDATE items SET state='ready',error=NULL WHERE id=? AND state='running'",(ident,)).rowcount
                print(json.dumps(dict(job=ident,state='ready' if completed else 'cancelled',
                                      copy=result['copy'],unchanged=result['unchanged'])),flush=True)
            except Exception as error:
                retry=isinstance(error,(OSError,sqlite3.Error))
                detail = 'NAS 读取或写入暂时失败，后台将重试。' if retry else str(error) if isinstance(error,SyncError) else '交付快照无效，请核对后重试。'
                state='error' if retry else 'blocked'
                with self.queue.db() as db:
                    updated=db.execute("UPDATE items SET state=?,error=?,attempts=attempts+1,retry_at=? WHERE id=? AND state='running'",
                               (state,detail,self.clock()+min(300,5*2**min(row['attempts'],6)),ident)).rowcount
                print(json.dumps(dict(job=ident,state=state if updated else 'cancelled',detail=detail),ensure_ascii=False),flush=True)

    def delay(self):
        with self.queue.db() as db:
            value=db.execute("SELECT MIN(i.retry_at) FROM items i JOIN batches b ON b.id=i.batch_id WHERE b.state='queued' AND i.state IN ('pending','error')").fetchone()[0]
        return max(.05,value-self.clock()) if value is not None else None


class Changes:
    """Linux directory notifications wake the worker on committed queue changes."""
    def __init__(self, root):
        libc=ctypes.CDLL(None,use_errno=True)
        self.fd=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
        if self.fd<0:raise OSError(ctypes.get_errno(),'inotify_init1')
        # Journal create/delete/close events notify committed SQLite changes.
        if libc.inotify_add_watch(self.fd,os.fsencode(root),0x8|0x80|0x100|0x200)<0:
            self.close();raise OSError(ctypes.get_errno(),'inotify_add_watch')

    def drain(self):
        while True:
            try:os.read(self.fd,65536)
            except BlockingIOError:return

    def close(self):
        os.close(self.fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue',type=Path,required=True)
    parser.add_argument('--nas-root',type=Path,required=True)
    parser.add_argument('--mount-root',type=Path,required=True)
    parser.add_argument('--state-dir',type=Path,required=True,help='Same local lock directory used by the former cron runner')
    args=parser.parse_args()
    queue=DeliveryQueue(args.queue)
    worker=NasWorker(queue,args.nas_root,args.mount_root)
    stopped=False
    read_fd,write_fd=os.pipe2(os.O_NONBLOCK|os.O_CLOEXEC)
    def stop(*_):
        nonlocal stopped
        stopped=True
        os.write(write_fd,b'x')
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    changes=Changes(queue.root)
    try:
        with run_lock(args.state_dir,args.nas_root):
            worker.recover()
            while not stopped:
                changes.drain()
                worker.process_once()
                if stopped:break
                select.select([changes.fd,read_fd],[],[],worker.delay())
    finally:
        changes.close();os.close(read_fd);os.close(write_fd)


if __name__=='__main__':
    main()
