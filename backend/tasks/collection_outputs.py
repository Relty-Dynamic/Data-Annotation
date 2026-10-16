"""Recoverable S3 publication of per-person revisions and approved timelines."""
import copy
import csv
import hashlib
import io
import json
import threading

from fastapi import HTTPException

from ..desktop_cache import safe_name
from ..service import AXES, DEFAULT_TRACK_LABELS, document_bytes, empty_annotations
from ..r2_service import capture_prefix

APPLICATION = 'DataMark-task-timeline-v1'


class CollectionOutputs:
    def __init__(self, store, media, editor):
        self.store, self.media, self.editor = store, media, editor
        self.provider = media.service.provider
        self.lock = threading.RLock()
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capture_output_people(
                    prefix TEXT,user_id TEXT,folder TEXT,name TEXT,version INTEGER NOT NULL,
                    PRIMARY KEY(prefix,user_id),UNIQUE(prefix,folder));
                CREATE TABLE IF NOT EXISTS capture_output_jobs(
                    id TEXT PRIMARY KEY,kind TEXT,body TEXT,state TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS capture_output_links(
                    job_id TEXT,task_id TEXT,PRIMARY KEY(job_id,task_id));
            ''')

    def _plan(self, db, task):
        capture = task.get('collection')
        if not capture or capture.get('backend') != 's3':
            return None
        source = self.media.service.load(capture['id'])
        if any(source.get(key) != capture.get(key) for key in ('recording_start','duration_ms','source_fingerprint')):
            raise HTTPException(409,'采集素材身份或录制起点已变化，不能写入旧任务结果。')
        prefix = capture_prefix(source.get('_r2_prefix')) + '/timeline'
        members = {item['source']['key']: item for item in capture['videos']}
        if set(members) != {source['id']+'/'+video['id'] for video in source['videos']}:
            raise HTTPException(409, '采集视频清单已变化，不能写入错误的结果目录。')
        for video in source['videos']:
            frozen = members[source['id']+'/'+video['id']]
            if any(video[key] != frozen[key] for key in ('duration_ms','start_ms','end_ms')):
                raise HTTPException(409, '采集视频时间已变化，不能移动已提交标注的坐标。')
        project = copy.deepcopy(source)
        axes = self.store._read(db, 'rules', task['rule_id'])['axes']
        project.update(name=capture['name'], fixed_tracks=[axis for axis in AXES if any(a['id']==axis for a in axes)],
                       selected_tracks=[a['id'] for a in axes],
                       custom_tracks=[{k:a[k] for k in ('id','name','mode','labels')} for a in axes if a['id'] not in AXES],
                       track_labels={**copy.deepcopy(DEFAULT_TRACK_LABELS), **{a['id']:a['labels'] for a in axes if a['id'] in AXES}},
                       track_names={a['id']:a['name'] for a in axes})
        return prefix, project

    @staticmethod
    def _display(db,user_id,names):
        if names.get(user_id):
            return names[user_id]
        row=db.execute("SELECT body FROM audit WHERE json_extract(body,'$.actor_id')=? AND json_extract(body,'$.actor_name')!='' ORDER BY rowid DESC LIMIT 1",(user_id,)).fetchone()
        return json.loads(row[0])['actor_name'] if row else user_id

    def _person(self, db, prefix, user_id, names):
        row = db.execute('SELECT folder,name FROM capture_output_people WHERE prefix=? AND user_id=?',(prefix,user_id)).fetchone()
        if row:
            return {'id':user_id,'folder':row[0],'name':row[1]}
        name = self._display(db,user_id,names)
        folder = safe_name(name)
        if folder.casefold() == 'finally' or db.execute(
                'SELECT 1 FROM capture_output_people WHERE prefix=? AND lower(folder)=lower(?)',(prefix,folder)).fetchone():
            folder += '-' + hashlib.sha256(user_id.encode()).hexdigest()[:12]
        db.execute('INSERT INTO capture_output_people VALUES (?,?,?,?,0)',(prefix,user_id,folder,name))
        return {'id':user_id,'folder':folder,'name':name}

    def _documents(self, project, annotations, metadata):
        project = copy.deepcopy(project)
        project['annotations'] = {**empty_annotations(), **annotations}
        for axis in project['custom_tracks']:
            project['annotations'].setdefault(axis['id'],[])
        _, documents = self.editor.export_documents(project,for_writeback=True,include_retired=False)
        return {axis+'.timeline.json':json.dumps({**json.loads(payload),'task_result':metadata},ensure_ascii=False,indent=2)
                for axis,payload in documents.items()}

    def _queue(self, db, ident, kind, task_ids, body):
        db.execute('INSERT OR IGNORE INTO capture_output_jobs VALUES (?,?,?,\'pending\',NULL)',
                   (ident,kind,json.dumps(body,ensure_ascii=False)))
        for task_id in task_ids:
            db.execute('INSERT OR IGNORE INTO capture_output_links VALUES (?,?)',(ident,task_id))
        return ident

    def submissions(self, db, task, names):
        plan = self._plan(db,task)
        if plan is None:
            return []
        prefix,project = plan
        jobs = []
        for sid,raw in db.execute('SELECT id,body FROM submissions WHERE task_id=? ORDER BY rowid',(task['id'],)).fetchall():
            record = json.loads(raw)
            if record.get('type') == 'final':
                continue
            ident = 'submission:'+sid
            jobs.append(ident)
            if db.execute('SELECT 1 FROM capture_output_jobs WHERE id=?',(ident,)).fetchone():
                continue
            record = self.store._read(db,'submissions',sid)
            person = self._person(db,prefix,record['user_id'],names)
            db.execute('UPDATE capture_output_people SET version=version+1 WHERE prefix=? AND user_id=?',(prefix,person['id']))
            version = db.execute('SELECT version FROM capture_output_people WHERE prefix=? AND user_id=?',(prefix,person['id'])).fetchone()[0]
            metadata = {'task_id':task['id'],'rule_id':task['rule_id'],'submission_id':sid,
                        'annotator_id':person['id'],'annotator_name':person['name'],'version':version,'submitted_at':record['at']}
            directory = prefix+'/'+person['folder']+'/v'+str(version)
            files = self._documents(project,record['annotations'],metadata)
            self._queue(db,ident,'submission',[task['id']],{'prefix':prefix,'directory':directory,
                        'files':files,'metadata':metadata,'person':person,'dependencies':[]})
        return jobs

    def final(self, db, task, names):
        dependencies = self.submissions(db,task,names)
        plan = self._plan(db,task)
        if plan is None or task.get('status') not in ('accepted','delivered'):
            return None
        ident = 'final:'+task['final_version_id']
        if db.execute('SELECT 1 FROM capture_output_jobs WHERE id=?',(ident,)).fetchone():
            return ident
        prefix,project = plan
        self.store._verify_final(db,task)
        metadata = {'task_id':task['id'],'rule_id':task['rule_id'],'final_version_id':task['final_version_id'],
                    'accepted_by':task['accepted_by'],'accepted_by_name':self._display(db,task['accepted_by'],names),
                    'accepted_at':task['accepted_at']}
        files = self._documents(project,task['final_annotations'],metadata)
        return self._queue(db,ident,'final',[task['id']],{'prefix':prefix,'directory':prefix+'/finally',
                           'files':files,'metadata':metadata,'dependencies':dependencies})

    def delivery(self, db, tasks, delivery, content, names):
        groups = {}
        for task in tasks:
            final = self.final(db,task,names)
            plan = self._plan(db,task)
            if plan is not None:
                groups.setdefault(plan[0],[]).append((task,final))
        source = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
        for prefix,items in groups.items():
            ident = 'delivery:'+delivery['id']+':'+hashlib.sha256(prefix.encode()).hexdigest()[:24]
            if db.execute('SELECT 1 FROM capture_output_jobs WHERE id=?',(ident,)).fetchone():
                continue
            fields = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))).fieldnames or [])
            fields += ['task_id','assigned_annotator_names','annotator_names','reviewer_name','adjudicator_name',
                       'accepted_by','accepted_by_name','submission_versions','delivery_id']
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream,fieldnames=fields)
            writer.writeheader()
            for task,_ in items:
                versions = []
                for row in db.execute("SELECT body FROM capture_output_jobs WHERE kind='submission' AND id IN (SELECT job_id FROM capture_output_links WHERE task_id=?) ORDER BY rowid",(task['id'],)):
                    job = json.loads(row[0])
                    versions.append({**job['person'],'version':job['metadata']['version'],'submission_id':job['metadata']['submission_id']})
                rows = [row for row in source if row['final_version_id']==task['final_version_id']]
                # Even an entirely empty annotation has an attributable delivery.
                if not rows:
                    video=self.store._task_video(db,task)
                    rows = [{'final_version_id':task['final_version_id'],'collection_id':task['collection']['id'],
                             'collection_name':task['collection']['name'],'rule_id':task['rule_id'],'task_type':task['task_type'],
                             'recording_start':task['collection']['recording_start'],'video_id':task['video_id'],
                             'video_ids':'|'.join(task.get('video_ids',[task['video_id']])),'collector_id':video['collector_id'],
                             'uploader_id':video['uploader_id']}]
                for row in rows:
                    reviewer = task.get('reviewed_by',task['reviewer_id'])
                    writer.writerow({**row,'task_id':task['id'],'assigned_annotator_names':'|'.join(self._display(db,u,names) for u in task['annotator_ids']),
                        'annotator_names':'|'.join(self._display(db,u,names) for u in row.get('annotators','').split('|') if u),
                        'reviewer_name':self._display(db,reviewer,names),
                        'adjudicator_name':self._display(db,task.get('adjudicated_by',''),names),
                        'accepted_by':task['accepted_by'],'accepted_by_name':self._display(db,task['accepted_by'],names),
                        'submission_versions':json.dumps(versions,ensure_ascii=False),'delivery_id':delivery['id']})
            self._queue(db,ident,'delivery',[task['id'] for task,_ in items],{'prefix':prefix,
                'directory':prefix,'files':{'delivery-'+delivery['id']+'.csv':'\ufeff'+stream.getvalue()},
                'metadata':{'delivery_id':delivery['id']},'dependencies':[final for _,final in items if final]})

    def status(self, db, task_id):
        rows = db.execute('SELECT state,error FROM capture_output_jobs WHERE id IN (SELECT job_id FROM capture_output_links WHERE task_id=?)',(task_id,)).fetchall()
        if not rows:
            task = self.store._read(db,'tasks',task_id)
            if not task.get('collection') or task['collection'].get('backend') != 's3' or not db.execute('SELECT 1 FROM submissions WHERE task_id=? LIMIT 1',(task_id,)).fetchone():
                return None
            return {'state':'pending','detail':'此任务已有提交，可同步到采集目录的 timeline/。'}
        failed = next((error for state,error in rows if state=='error'),None)
        if failed:
            return {'state':'error','detail':failed,'ready':sum(state=='ready' for state,_ in rows),'total':len(rows)}
        ready = all(state=='ready' for state,_ in rows)
        return {'state':'ready' if ready else 'pending','detail':'采集目录 timeline/ 已同步并读回核验。' if ready else '任务结果已保存，采集目录结果尚待同步。',
                'ready':sum(state=='ready' for state,_ in rows),'total':len(rows)}

    def _immutable(self, key, text):
        data = text.encode('utf-8')
        if key.endswith('.csv'):
            try:
                self.provider.put(key,data,create=True,content_type='text/csv; charset=utf-8')
            except HTTPException as error:
                if error.status_code != 409 or self.provider.get(key) != data:
                    raise
        else:
            self.provider.put_if_absent(key,data)
        if self.provider.get(key) != data:
            raise HTTPException(503,'采集结果文件读回校验失败。')

    def _publish(self, kind, body):
        directory,files = body['directory'],body['files']
        manifest = {'application':APPLICATION,'metadata':body['metadata'],
                    'files':{name:hashlib.sha256(text.encode()).hexdigest() for name,text in files.items()}}
        if kind != 'final':
            for name,text in files.items():
                self._immutable(directory+'/'+name,text)
            if kind == 'submission':
                self._immutable(directory+'/.datamark-version.json',json.dumps(manifest,ensure_ascii=False))
            return
        marker = directory+'/.datamark-final.json'
        previous,etag = self.provider.optional_json(marker)
        if previous and previous.get('application') != APPLICATION:
            raise HTTPException(409,'最终目录含无法确认归属的记录，原文件已保留。')
        if previous and previous['metadata']['accepted_at'] > body['metadata']['accepted_at']:
            return  # Backfilling history must not replace a newer approved result.
        known = previous.get('files',{}) if previous else {}
        for name,text in files.items():
            key,data = directory+'/'+name,text.encode()
            existing,version = self.provider.optional_json(key)
            if version is None:
                self.provider.put(key,data,create=True)
            else:
                raw = self.provider.get(key)
                if raw != data:
                    if hashlib.sha256(raw).hexdigest() != known.get(name):
                        raise HTTPException(409,'最终目录文件已被外部修改，原文件已保留，请核对后重试。')
                    self.provider.put(key,data,etag=version)
            if self.provider.get(key) != data:
                raise HTTPException(503,'最终 JSON 读回校验失败。')
        manifest['files'] = {**known,**manifest['files']}
        payload = document_bytes(manifest)
        self.provider.put(marker,payload,etag=etag,create=etag is None)
        if self.provider.get(marker) != payload:
            raise HTTPException(503,'最终结果清单读回校验失败。')

    def flush(self, task_id):
        with self.lock:
            with self.store.db() as db:
                ids = [row[0] for row in db.execute('SELECT id FROM capture_output_jobs WHERE state!=\'ready\' AND id IN (SELECT job_id FROM capture_output_links WHERE task_id=?) ORDER BY rowid',(task_id,))]
            for ident in ids:
                if not self._run(ident,set()):
                    break
            with self.store.db() as db:
                return self.status(db,task_id)

    def _run(self, ident, visiting):
        if ident in visiting:
            raise HTTPException(503,'结果同步依赖异常。')
        with self.store.db() as db:
            kind,raw,state = db.execute('SELECT kind,body,state FROM capture_output_jobs WHERE id=?',(ident,)).fetchone()
        if state=='ready':
            return True
        body = json.loads(raw)
        visiting.add(ident)
        for dependency in body.get('dependencies',[]):
            if not self._run(dependency,visiting):
                return False
        try:
            with self.store.db() as db:
                for task_id, in db.execute('SELECT task_id FROM capture_output_links WHERE job_id=?',(ident,)):
                    if self.store._read(db,'tasks',task_id)['status']=='deleted':
                        raise HTTPException(410,'关联任务已删除，停止写入采集结果目录，历史任务记录保留。')
            self._publish(kind,body)
        except Exception as error:
            detail = error.detail if isinstance(error,HTTPException) and isinstance(error.detail,str) else '采集目录结果同步失败，请重试；任务记录已保留。'
            with self.store.db() as db:
                db.execute('UPDATE capture_output_jobs SET state=\'error\',error=? WHERE id=?',(detail,ident))
            return False
        finally:
            visiting.remove(ident)
        with self.store.db() as db:
            db.execute('UPDATE capture_output_jobs SET state=\'ready\',error=NULL WHERE id=?',(ident,))
        return True

    def backfill(self, actor, task_id, names):
        task = self.store.get_task(actor,task_id)
        if task['status']=='deleted':
            raise HTTPException(410,'已删除任务不能继续写入结果目录。')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            raw = self.store._read(db,'tasks',task_id)
            self.submissions(db,raw,names)
            self.final(db,raw,names)
            if raw.get('delivery_id'):
                delivery = self.store._read(db,'deliveries',raw['delivery_id'])
                tasks = [self.store._read(db,'tasks',ident) for ident in delivery['task_ids']]
                self.delivery(db,tasks,delivery,self.store.artifacts.get(delivery['artifact_key']),names)
        return self.flush(task_id)
