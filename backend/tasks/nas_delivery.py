"""Release a frozen NAS delivery only after the entire S3 batch is verified."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from fastapi import HTTPException
from ..nas_delivery_queue import DeliveryQueue, SCHEMA, status_rows
from ..service import document_bytes


class NasDelivery:
    def __init__(self, outputs):
        self.outputs, self.store = outputs, outputs.store
        self.queue = DeliveryQueue(self.store.path.parent/'nas-sync')
        schema = re.sub(r'\b(batches|items|links|frozen_payload)\b',lambda m:'nas_'+m[0],SCHEMA)
        with self.store.db() as db:
            db.executescript(schema)
        self.event, self.stopping = threading.Event(), threading.Event()
        self.thread = None

    def register(self, db, delivery, groups):
        """Called inside the same transaction as the immutable delivery record."""
        db.execute('INSERT OR IGNORE INTO nas_batches(id,state) VALUES (?,?)', (delivery['id'],'waiting_s3'))
        for prefix, tasks in groups.items():
            output_job = 'delivery:'+delivery['id']+':'+hashlib.sha256(prefix.encode()).hexdigest()[:24]
            files, metadata = {}, None
            # Only delivered axes, never another task's later accepted result.
            finals = [json.loads(db.execute('SELECT body FROM capture_output_jobs WHERE id=?', (final,)).fetchone()[0])
                      for _,final in tasks if final]
            for final in sorted(finals,key=lambda body:body['metadata']['accepted_at']):
                files.update(final['files']);metadata=final['metadata']
            if not metadata:
                continue
            payload = dict(capture=prefix.removesuffix('/timeline'),files=files,
                marker=dict(application='DataMark-task-timeline-v1',metadata=metadata,
                            files={name:hashlib.sha256(text.encode()).hexdigest() for name,text in files.items()}))
            ident = hashlib.sha256(output_job.encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO nas_items(id,batch_id,output_job,payload) VALUES (?,?,?,?)',
                       (ident,delivery['id'],output_job,json.dumps(payload,ensure_ascii=False)))
            db.executemany('INSERT OR IGNORE INTO nas_links VALUES (?,?)', [(ident,task['id']) for task,_ in tasks])

        # Explicit historical backfill can include a task deleted before this
        # feature was enabled. Such a batch must never be newly released.
        if any(task['status']=='deleted' for tasks in groups.values() for task,_ in tasks):
            db.execute("UPDATE nas_batches SET state='cancelled' WHERE id=? AND state!='queued'",(delivery['id'],))
            db.execute("""UPDATE nas_items SET state='cancelled',error='交付批次包含已删除任务，未启动 NAS 同步。'
                WHERE batch_id=? AND batch_id IN (SELECT id FROM nas_batches WHERE state='cancelled')""",(delivery['id'],))

    def status(self, db, task_id):
        rows = db.execute("""SELECT i.id,i.state,i.error,b.state,b.error FROM nas_items i
            JOIN nas_batches b ON b.id=i.batch_id JOIN nas_links l ON l.item_id=i.id WHERE l.task_id=?""",(task_id,)).fetchall()
        values=[]
        if not rows:
            return None
        try:
            with self.queue.db() as queue:
                for ident,state,error,batch_state,batch_error in rows:
                    remote=queue.execute('SELECT state,error FROM items WHERE id=?',(ident,)).fetchone()
                    if remote is not None and (state!='cancelled' or remote['state']=='ready'):
                        state,error=remote['state'],remote['error']
                    values.append((state,error,batch_state,batch_error))
        except (OSError,sqlite3.Error):
            # A separate queue outage must not roll back an otherwise valid delivery.
            return dict(state='error',detail='NAS 队列状态暂不可用，交付记录已保留。',ready=0,
                        total=len(rows),retryable=True,retry_scheduled=True)
        return status_rows(values)

    def wake(self):
        self.event.set()

    def retry(self, task_id):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Explicit retry resets only failed work; an active worker is untouched.
            db.execute("UPDATE nas_items SET state='pending',error=NULL,retry_at=0 WHERE state IN ('error','blocked') AND id IN (SELECT item_id FROM nas_links WHERE task_id=?)",(task_id,))
            db.execute("UPDATE nas_batches SET state='waiting_s3',error=NULL,retry_at=0 WHERE state IN ('error','blocked') AND id IN (SELECT batch_id FROM nas_items WHERE id IN (SELECT item_id FROM nas_links WHERE task_id=?))",(task_id,))
            db.execute("UPDATE nas_items SET retry_generation=retry_generation+1 WHERE state!='cancelled' AND id IN (SELECT item_id FROM nas_links WHERE task_id=?)",(task_id,))
        self.wake()

    def cancel(self, db, task_id):
        db.execute("UPDATE nas_items SET state='cancelled',error='关联任务已删除，停止 NAS 同步。' WHERE id IN (SELECT item_id FROM nas_links WHERE task_id=?)",(task_id,))
        # Before release, the whole batch must remain verifiable. Do not leave
        # other captures waiting forever for a now-deleted dependency.
        db.execute("""UPDATE nas_items SET state='cancelled',error='交付批次包含已删除任务，未启动 NAS 同步。'
            WHERE batch_id IN (SELECT batch_id FROM nas_items WHERE state='cancelled')
            AND batch_id IN (SELECT id FROM nas_batches WHERE state!='queued')""")
        db.execute("UPDATE nas_batches SET state='cancelled' WHERE state!='queued' AND id IN (SELECT batch_id FROM nas_items WHERE state='cancelled')")

    def dispatch_once(self, now=None):
        now = time.time() if now is None else now
        with self.store.db() as db:
            batches = db.execute("SELECT id,attempts FROM nas_batches WHERE state IN ('waiting_s3','error') AND retry_at<=? ORDER BY rowid",(now,)).fetchall()
        # Reconcile committed deletions before releasing or retrying any work.
        with self.store.db() as db:
            cancelled=db.execute("SELECT id FROM nas_items WHERE state='cancelled'").fetchall()
            retries=db.execute('SELECT retry_generation,id FROM nas_items WHERE retry_generation>0').fetchall()
        with self.queue.db() as db:
            db.executemany("UPDATE items SET state='cancelled',error='关联任务已删除，停止 NAS 同步。' WHERE id=? AND state NOT IN ('ready','cancelled')",cancelled)
            for generation,item_id in retries:
                db.execute("""UPDATE items SET state=CASE WHEN state IN ('error','blocked') THEN 'pending' ELSE state END,
                    error=CASE WHEN state IN ('error','blocked') THEN NULL ELSE error END,retry_at=0,retry_generation=?
                    WHERE id=? AND retry_generation<? AND state!='cancelled'""",(generation,item_id,generation))
        for ident,attempts in batches:
            with self.store.db() as db:
                rows = db.execute('SELECT id,output_job,payload,state FROM nas_items WHERE batch_id=? ORDER BY rowid',(ident,)).fetchall()
                if not rows or any(row[3]=='cancelled' for row in rows):
                    continue
                if any(db.execute('SELECT state FROM capture_output_jobs WHERE id=?',(row[1],)).fetchone()[0]!='ready' for row in rows):
                    continue
            try:
                # Freeze and read back the exact delivered JSON in S3. The worker
                # consumes these same bytes, never the mutable final/ directory.
                for item_id,_,raw,_ in rows:
                    payload = json.loads(raw)
                    prefix = 'tasks/nas-deliveries/'+ident+'/'+item_id
                    for name,text in payload['files'].items():
                        self.outputs._immutable(prefix+'/'+name,text)
                    self.outputs._immutable(prefix+'/.datamark-final.json',document_bytes(payload['marker']).decode())
                with self.store.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute("SELECT 1 FROM nas_items WHERE batch_id=? AND state='cancelled'",(ident,)).fetchone():
                        continue
                    # A separate durable queue never exposes the task database to
                    # the NAS worker. Queue commit can be replayed if API dies before
                    # the platform acknowledgement; immutable IDs prevent duplicates.
                    with self.queue.db() as queue:
                        queue.execute("INSERT OR IGNORE INTO batches(id,state) VALUES (?,'queued')",(ident,))
                        for item_id,output_job,raw,_ in rows:
                            old=queue.execute('SELECT payload FROM items WHERE id=?',(item_id,)).fetchone()
                            if old is not None and old['payload']!=raw:
                                raise HTTPException(409,'交付队列快照不一致。')
                            queue.execute('INSERT OR IGNORE INTO items(id,batch_id,output_job,payload) VALUES (?,?,?,?)',(item_id,ident,output_job,raw))
                            links=db.execute('SELECT item_id,task_id FROM nas_links WHERE item_id=?',(item_id,)).fetchall()
                            queue.executemany('INSERT OR IGNORE INTO links VALUES (?,?)',links)
                    db.execute("UPDATE nas_batches SET state='queued',error=NULL WHERE id=?",(ident,))
            except Exception as error:
                retry = isinstance(error,(OSError,sqlite3.Error)) or isinstance(error,HTTPException) and error.status_code>=500
                detail = 'S3 交付快照核验暂时失败，后台将重试。' if retry else 'S3 交付快照存在冲突或校验失败，请核对后重试。'
                with self.store.db() as db:
                    db.execute('UPDATE nas_batches SET state=?,error=?,attempts=attempts+1,retry_at=? WHERE id=?',
                        ('error' if retry else 'blocked',detail,now+min(300,5*2**min(attempts,6)),ident))

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.stopping.is_set():
                self.event.clear()
                try:
                    self.dispatch_once()
                    with self.store.db() as db:
                        next_retry = db.execute("SELECT MIN(retry_at) FROM nas_batches WHERE state='error'").fetchone()[0]
                    delay = max(.1,next_retry-time.time()) if next_retry is not None else None
                except Exception:
                    # Queue I/O failure must not terminate recovery or expose SQL/payloads.
                    delay = 30
                self.event.wait(delay)
        self.thread = threading.Thread(target=run,name='nas-delivery-dispatch',daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set();self.event.set()
        if self.thread is not None:
            self.thread.join()
